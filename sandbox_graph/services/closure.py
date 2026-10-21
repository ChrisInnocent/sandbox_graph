"""Final publication is a revision CAS; incomplete resources remain visible."""
from ..domain.models import canonical_hash
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
            owned_scopes.update(r['body']['work_id'] for r in tx.scan('work_instances') if r['body']['run_id']==run_id)
            operations=[r['body'] for r in tx.scan('operations') if r['body']['run_id']==run_id]
            owned_scopes.update(o['scope'] for o in operations)
            leases=[r['body'] for r in tx.scan('resource_leases') if r['body']['scope'] in owned_scopes]
            for job in tx.scan('jobs'):
                body=job['body']
                if body.get('run_id')==run_id and body['job_kind'] in {'work','config'} and body['status'] in {'pending','running','draining'}:
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
