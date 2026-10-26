"""Final publication is a revision CAS; incomplete resources remain visible."""
from ..domain.models import canonical_hash
from ..runtime.observation import ObservationService
from ..runtime.tasks import business_job_kind
from ..persistence.repository import Conflict

class ClosureService:
    def __init__(self,repository): self.repository=repository
    @staticmethod
    def classify_scope(result,processing_ref=None):
        if result.scope_disposition is None or not processing_ref: return 'open'
        if result.execution_status in {'running','pending'}: return 'open'
        if result.scope_disposition=='fulfilled' and result.data_status not in {'valid'}: return 'partial'
        if result.scope_disposition!='fulfilled' or result.cleanup_status in {'pending','incomplete','unknown'}: return 'partial'
        return 'complete'
    def seal_archive(self,run_id,*,expected_revision,analysis_ref,report_analysis_ref,excel_analysis_ref,scopes,inflight,artifact_refs,token=None,expected_plan_ref=None):
        if inflight: raise Conflict('in-flight measurement prevents closure')
        if analysis_ref!=report_analysis_ref or analysis_ref!=excel_analysis_ref: raise Conflict('report/Excel analysis versions disagree')
        statuses={key:self.classify_scope(result,processing) for key,(result,processing) in scopes.items()}
        if any(status=='open' for status in statuses.values()): raise Conflict('scope lacks final disposition')
        archive={'run_id':run_id,'closure_revision':expected_revision,'analysis_ref':analysis_ref,'scope_status':statuses,'cleanup':{key:result.cleanup_status for key,(result,_) in scopes.items()},'artifact_refs':list(artifact_refs),'status':'partial' if any(v=='partial' for v in statuses.values()) else 'complete'}
        identity=canonical_hash(archive)
        with self.repository.transaction() as tx:
            if token is not None: tx.assert_fence(token)
            run=tx.get('runs',run_id)
            if expected_plan_ref is not None and (not run or run['body']['active_plan_ref']!=expected_plan_ref):
                raise Conflict('archive active plan changed')
            owned_scopes=set(scopes)
            owned_scopes.add(run_id)
            works={r['body']['work_id']:r['body'] for r in tx.scan('work_instances')}
            owned_works={key for key,body in works.items() if body['run_id']==run_id}
            owned_groups={r['body']['group_id'] for r in tx.scan('groups') if r['body']['run_id']==run_id}
            owned_groups.update(body['group_id'] for body in works.values()
                                if body['run_id']==run_id and body.get('group_id'))
            operations=[r['body'] for r in tx.scan('operations') if r['body']['run_id']==run_id]
            operation_scopes={o['scope'] for o in operations}
            owned_scopes.update(owned_works | owned_groups | operation_scopes)
            from .lifecycle_inventory import LifecycleInventory
            inventory=LifecycleInventory.collect_in(tx,run_ref=run_id,scope_kind='run',
                scope_ref=run_id,extra_scopes=tuple(scopes))
            for item in inventory.blockers:
                if item.ownership=='ambiguous':
                    label={'work_execution_leases':'work execution', 'resource_leases':'resource reservation'}.get(item.kind,item.kind)
                    raise Conflict('ambiguous ' + label + ' ownership prevents closure')
                if item.kind=='resources':
                    resource=tx.get(item.kind,item.key)['body']
                    scope=resource.get('scope')
                    quarantined=any(r['body'].get('scope')==scope and r['body'].get('status')=='quarantine'
                        for r in tx.scan('resource_leases'))
                    if statuses.get(scope)!='partial' or not quarantined:
                        raise Conflict('unclosed resource prevents closure')
                if item.kind=='candidate_isolations':
                    raise Conflict('unclosed candidate isolation prevents closure')
            from .agent_lifecycle import decode
            from ..domain.agent_lifecycle import LifecycleRecord
            lifecycles=[]
            for row in tx.scan('agent_lifecycles'):
                if row['body'].get('key',{}).get('run_ref')!=run_id: continue
                record=decode(LifecycleRecord,row['body'])
                if not record.is_terminal:
                    raise Conflict('unfinished lifecycle prevents closure')
                lifecycles.append(record)
                owned_scopes.add(record.key.scope_ref)
            if lifecycles:
                archive['lifecycle_status']={r.key.lifecycle_id:r.phase for r in lifecycles}
                if any(r.phase!='fulfilled' for r in lifecycles): archive['status']='partial'
            from .lifecycle_disposition import DispositionVersions
            stop_proofs=[]
            for request in tx.scan('lifecycle_stop_requests'):
                if request['body']['run_ref']!=run_id:
                    continue
                status=tx.get('lifecycle_stop_status',request['key'])
                if not status or status['body'].get('status')!='closed':
                    raise Conflict('pending stop proof prevents closure')
                proof=tx.get('lifecycle_stop_proofs',status['body'].get('proof_ref',''))
                if not proof:
                    raise Conflict('missing stop proof prevents closure')
                DispositionVersions.verify_stop_in(tx,proof['body'])
                stop_proofs.append(proof['key'])
            archive['disposition_revision']=DispositionVersions.current_in(tx,run_id)
            if stop_proofs:
                archive['stop_proof_refs']=sorted(stop_proofs)
            unresolved_gaps=[]
            for row in tx.scan('delivery_rework_gaps'):
                gap=row['body']
                if gap['run_ref']!=run_id: continue
                status=tx.get('delivery_rework_status',row['key'])
                status=status['body'] if status else {'status':'pending'}
                life=tx.get('agent_lifecycles',status.get('lifecycle_id',''))
                if status['status'] in {'pending','accepted','linked'} and not life:
                    raise Conflict('pending delivery rework prevents closure')
                if status['status']=='resolved': continue
                unresolved_gaps.append(row['key'])
            if unresolved_gaps:
                if archive['status']=='complete':
                    raise Conflict('unresolved delivery rework prevents clean closure')
                archive['delivery_rework_gap_refs']=sorted(unresolved_gaps)
            isolated=[]
            from .lifecycle_safety import LifecycleSafetyService
            for row in tx.scan('repair_isolations'):
                scope=row['body'].get('work_ref')
                if scope not in owned_scopes: continue
                if LifecycleSafetyService.isolation_resolved_in(tx,row): continue
                # Explicit partial closure may preserve unknown effects; a
                # successful attempt alone never releases this quarantine.
                bound=[r for r in lifecycles if row['key'] in r.isolation_refs]
                if (statuses.get(scope)!='partial' or not bound
                        or any(r.phase!='blocked_closed' or r.key.scope_ref!=scope for r in bound)):
                    raise Conflict('unresolved repair isolation prevents clean closure')
                isolated.append(row['key'])
            if isolated:
                archive['repair_isolation_refs']=sorted(isolated)
                archive['status']='partial'
            from ..runtime.agent_bridge import RequestRecord
            for row in tx.scan('agent_provider_requests'):
                envelope=row['body'].get('envelope',{})
                provider_lifecycle=tx.get('agent_lifecycles',envelope.get('lifecycle_id'))
                if provider_lifecycle:
                    # An authoritative run identity wins over reusable group
                    # labels, even when both runs own a group of that name.
                    if provider_lifecycle['body']['key']['run_ref']!=run_id: continue
                else:
                    # Legacy/orphan records have no lifecycle to disambiguate
                    # them. Match typed ownership, retaining uncertainty for a
                    # relevant shared group; bare node names are never global.
                    scope=envelope.get('scope',{})
                    kind,ref=scope.get('kind'),scope.get('ref')
                    if kind=='run':
                        relevant=ref==run_id
                    elif kind=='work' and ref in works:
                        relevant=works[ref]['run_id']==run_id
                    elif kind=='group':
                        relevant=ref in owned_groups or ref in operation_scopes or ref in scopes or any(
                            r.key.scope_kind=='group' and r.key.scope_ref==ref for r in lifecycles)
                    elif kind=='node':
                        relevant=ref in owned_works or ref==run_id or ref in operation_scopes or any(
                            r.key.scope_kind=='node' and r.key.scope_ref==ref for r in lifecycles)
                    else:
                        relevant=kind in {'work','candidate'} and ref in owned_scopes
                    if not relevant: continue
                if not decode(RequestRecord,row['body']).terminal:
                    raise Conflict('in-flight or unknown provider task prevents closure')
            for row in tx.scan('observations'):
                body=row['body']
                if ObservationService.closed_in(tx,row): continue
                scope=body.get('scope')
                operation=tx.get('operations',body['operation_id']) if body.get('operation_id') else None
                operation=operation['body'] if operation else {}
                operation_run=operation.get('run_id')
                scope_run=run_id if scope==run_id else works.get(scope,{}).get('run_id')
                # Shared group labels only discover candidates. A consistent
                # operation binding establishes the observation's actual run.
                if (operation_run is not None and operation.get('scope')==scope
                        and scope_run in {None,operation_run}
                        and body.get('run_id',operation_run)==operation_run):
                    if operation_run!=run_id: continue
                if scope in owned_scopes or operation_run==run_id or body.get('run_id')==run_id:
                    raise Conflict('unclosed observation prevents closure')
            for row in tx.scan('work_execution_leases'):
                if row['key'] in owned_scopes:
                    raise Conflict('work execution ownership prevents closure')
            leases=[]
            for row in tx.scan('resource_leases'):
                body=row['body']
                scope,lease_run=body['scope'],body.get('run_id')
                scope_run=run_id if scope==run_id else works.get(scope,{}).get('run_id')
                relevant=scope in owned_scopes or lease_run==run_id
                if lease_run is not None:
                    if scope_run is not None and scope_run!=lease_run:
                        if relevant: raise Conflict('ambiguous resource reservation prevents closure')
                        continue
                    if lease_run!=run_id: continue
                if relevant: leases.append(body)
            for job in tx.scan('jobs'):
                body=job['body']
                # Analysis/report (including graph: aliases) are enclosing
                # publication jobs. They do not mutate resource disposition.
                if body.get('run_id')==run_id and business_job_kind(body['job_kind']) in {
                        'work','config','remedy','supervision','lifecycle','agent_team',
                        'candidate_build','candidate_verify','promotion',
                        'cleanup','reconcile'} and body['status'] in {'pending','running','draining'}:
                    raise Conflict('in-flight job prevents closure')
            for operation in operations:
                if operation['phase'] in {'intent','submission_started','running'}:
                    raise Conflict('in-flight operation prevents closure')
                if operation['phase']=='uncertain':
                    domains=[lease for lease in leases if lease['scope']==operation['scope']]
                    if not domains or any(lease['status']!='quarantine' for lease in domains) or statuses.get(operation['scope'])!='partial':
                        raise Conflict('unknown operation requires quarantine and partial scope disposition')
            for lease in leases:
                if lease['status']!='quarantine' or statuses.get(lease['scope'])!='partial':
                    raise Conflict('live resource reservation prevents closure')
            current=tx.get('closure_revisions',run_id)
            if (current['body']['revision'] if current else 0)!=expected_revision: raise Conflict('closure revision changed')
            run=tx.get('runs',run_id)
            if run:
                if current and current['body'].get('plan_ref',run['body']['active_plan_ref'])!=run['body']['active_plan_ref']:
                    raise Conflict('closure revision belongs to previous plan')
                archive['plan_ref']=run['body']['active_plan_ref']
            identity=canonical_hash(archive)
            tx.put('archives',identity,archive,immutable=True)
            tx.put('current_archives',run_id,{'archive_ref':identity,'closure_revision':expected_revision})
        return archive
