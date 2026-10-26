"""Independent configuration and original-canary evidence verification.

A missing proof requests one bounded repair. An unresolved original operation is
never converted into permission to create another sandbox.
"""
from datetime import datetime,timezone,timedelta
import json
from .contracts import CheckObservation
from .declarations import _unique_json_object
from ..domain.models import EvidenceRef


class ProductVerification:
    def __init__(self,*,probe,repository=None,scope,store,max_age_seconds=300,runtime=None):
        if not 0<max_age_seconds<=3600:raise ValueError('finite verification freshness required')
        self.probe=probe;self.repository=repository;self.scope=scope;self.store=store
        self.max_age_seconds=max_age_seconds;self.runtime=runtime

    def _proofs(self,declaration,node,boot_id,now):
        if self.repository is None:return 'missing',(),{'reason':'canary_not_executed'}
        with self.repository.transaction() as tx:
            rows=[]
            for row in tx.scan('environment_canaries'):
                if row['body'].get('scope')!=self.scope:continue
                operation=tx.get('operations',row['key'])
                if not operation:continue
                data=operation['body'].get('request',{}).get('input',{})
                request=data.get('canary',data.get('step',{}).get('input',{}).get('canary',{}))
                if request.get('declaration_hash')==declaration.declaration_hash and node.node_id in [*request.get('node_ids',[]),*request.get('control_routes',{}),*request.get('management_dependencies',{})]:
                    rows.append(row['body'])
            if not rows:return 'missing',(),{'reason':'canary_not_executed'}
            if any(row.get('phase')!='closed' for row in rows):return 'unknown',(),{'reason':'original_canary_unresolved'}
            candidates=[]
            for row in rows:
                terminal=row.get('terminal',{})
                if terminal.get('state')!='completed' or terminal.get('authoritative') is not True:continue
                proofs={};refs=terminal.get('artifact_refs',())
                for name in refs:
                    record=tx.get('environment_evidence',name)
                    if not record:continue
                    ref=EvidenceRef.model_validate(record['body'])
                    if ref.scope!=self.scope or ref.producer!='environment-canary.v1' or name!='artifact://'+ref.uri:continue
                    self.store.verify(ref)
                    if ref.size>1024*1024:raise ValueError('canary proof exceeds byte budget')
                    proof=json.loads(self.store.path_for(ref).read_bytes(),object_pairs_hook=_unique_json_object)
                    if (proof.get('declaration_hash')!=declaration.declaration_hash or node.node_id not in proof.get('scope',[])
                            or proof.get('boot_ids',{}).get(node.node_id,proof.get('boot_id'))!=boot_id or proof.get('operation_id')!=row['operation_id']):continue
                    observed=datetime.fromisoformat(proof['observed_at'].replace('Z','+00:00'))
                    if observed.tzinfo is None or not now-timedelta(seconds=self.max_age_seconds)<=observed<=now:continue
                    if not set(node.roles)&{'compute','runtime','server','conductor'}:
                        from .canary import MANAGEMENT_ROLES
                        if set(node.roles)<=MANAGEMENT_ROLES.get(declaration.product,set()):
                            dependency=proof.get('management_dependencies',{}).get(node.node_id,{})
                            if (dependency.get('status')!='verified' or dependency.get('boot_id')!=boot_id
                                    or dependency.get('node_id')!=node.node_id or dependency.get('roles')!=list(node.roles)
                                    or dependency.get('evidence_level')!='group_dependency'
                                    or not proof.get('control_authentication')
                                    or any(value.get('status')!='verified' for value in proof['control_authentication'].values())):continue
                        elif proof.get('control_authentication',{}).get(node.node_id,{}).get('status')!='verified':continue
                    proofs[proof['kind']]=proof
                functional=proofs.get('functional',{});cleanup=proofs.get('cleanup',{})
                if (functional.get('status')=='passed' and functional.get('authentication',{}).get('status')=='verified'
                        and cleanup.get('status')=='complete' and cleanup.get('cleanup_observation')=='detail_404'
                        and functional.get('resource_ids') and functional.get('resource_ids')==cleanup.get('resource_ids')):
                    candidates.append((tuple(refs),{'functional':functional,'cleanup':cleanup}))
            if candidates:return 'satisfied',*candidates[-1]
        return 'missing',(),{'reason':'no_current_successful_canary_proof'}

    async def check(self,declaration,node,*,fresh,boot_id):
        now=datetime.now(timezone.utc);observed_at=now.isoformat();rows=[]
        def observation(check,status,observed,*,component='product',refs=(),repair=None):
            if not refs:refs=(self.probe.commit({'kind':check,'node_id':node.node_id,'boot_id':boot_id,
                'observed_at':observed_at,'result':observed}),)
            rows.append(CheckObservation(check_id=check,rule_ref='environment-verification.v1:'+check,node_id=node.node_id,
                component=component,status=status,observed=observed,observed_at=observed_at,evidence_refs=refs,
                boot_id=boot_id,repair_code=repair))
        try:
            configs=await self.probe.request(node,'probe.configuration.v1',
                {'product':declaration.product,'roles':list(node.roles),'paths':node.paths})
            if not isinstance(configs.get('components'),dict):raise ValueError('configuration response shape differs')
            for component,value in configs['components'].items():
                status=value.get('status','unknown')
                observation('configuration:'+component,status,value,component=component,
                    repair='CONFIGURATION_MISSING' if status=='missing' else None)
            if not configs['components'] and set(node.roles)!={'runtime'}:
                observation('configuration','unknown',{'reason':'component_configuration_observations_required'})
        except (OSError,ValueError,KeyError,TypeError) as exc:
            observation('configuration','unknown',{'reason':type(exc).__name__})
        ublk_target=declaration.targets.host_attributes.get('ublks_max') if declaration.product=='agentenv' else None
        if ublk_target is not None:
            if 'server' not in node.roles:
                observation('effective:host_attributes:ublks_max','not_applicable',{'reason':'server_host_only'})
            else:
                try:
                    value=await self.probe.request(node,'probe.ublk.v1',{})
                    actual=value.get('ublks_max');status='unknown'
                    if value.get('loaded') is True and type(actual) is int and actual>0 and value.get('evidence_level')=='runtime_effective':
                        if ublk_target.mode=='preserve':status='satisfied'
                        elif ublk_target.mode=='exact':
                            status='satisfied' if ublk_target.unit in (None,'count') and actual==ublk_target.value else 'mismatch'
                        else:
                            low=declaration.budgets.get(ublk_target.budget_ref+'.minimum')
                            high=declaration.budgets.get(ublk_target.budget_ref+'.maximum')
                            if type(low) is int and type(high) is int and 0<low<=high:
                                status='satisfied' if low<=actual<=high else 'mismatch'
                    observation('effective:host_attributes:ublks_max',status,value|{'target':ublk_target.model_dump(mode='json')},
                        component='ublk_drv',repair='CONFIGURATION_MISMATCH' if status=='mismatch' else None)
                except (OSError,ValueError,KeyError,TypeError):
                    observation('effective:host_attributes:ublks_max','unknown',{'reason':'kernel_parameter_observation_unavailable'})
        # File presence/startup binding cannot prove that a requested platform
        # limit is effective. Registered runtime probes provide the latter.
        needs_runtime=bool(declaration.targets.platform_limits or declaration.targets.performance_policy or set(declaration.targets.host_attributes)-({'ublks_max'} if declaration.product=='agentenv' else set()))
        if self.runtime is not None:
            for item in await self.runtime.check(declaration,node,boot_id=boot_id):
                rows.append(CheckObservation.model_validate(item))
        elif needs_runtime or len(declaration.nodes)>1:
            observation('effective_configuration_registration','unknown',{'reason':'runtime_observation_binding_required'})
        if set(node.roles)&{'compute','runtime','server','conductor'}:
            status,refs,proof=self._proofs(declaration,node,boot_id,now)
            observation('functional_readiness',status,proof,refs=refs,repair='CANARY_MISSING' if status=='missing' else None)
        elif set(node.roles)&{'control','router','gateway'}:
            status,refs,proof=self._proofs(declaration,node,boot_id,now)
            observation('functional_readiness',status,proof,refs=refs,repair='CANARY_MISSING' if status=='missing' else None)
        else:
            status,refs,proof=self._proofs(declaration,node,boot_id,now)
            observation('functional_readiness',status,proof,refs=refs,repair='CANARY_MISSING' if status=='missing' else None)
        return tuple(rows)
