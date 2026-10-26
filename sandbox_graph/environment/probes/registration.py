"""Audited read-only management observations for the three product profiles."""
import asyncio
from datetime import datetime,timezone
import json
from urllib.parse import quote
from ..contracts import CheckObservation
from ..declarations import _unique_json_object


def target_roles(product,name):
    from ..tuning import RULES
    components={'cubelet':{'compute'},'CubeMaster':{'control'},'CubeAPI':{'control'},
        'conductor':{'conductor'},'server':{'server'},'scheduler':{'scheduler'}}
    result=set()
    for (candidate,component),rules in RULES.items():
        if candidate==product and name in rules:result.update(components.get(component,set()))
    if product=='cubesandbox' and name=='quota_memory':result.add('compute')
    return result or None


def interpret_registration(product,raw,*,node_id,now,max_age_seconds=120):
    if not isinstance(raw,dict) or not 0<max_age_seconds<=120:raise ValueError('bounded registration observation required')
    def fresh(value):
        try:
            at=datetime.fromisoformat(value.replace('Z','+00:00')).timestamp() if isinstance(value,str) else float(value)
            return 0<=now-at<=max_age_seconds
        except (ValueError,TypeError,OverflowError):return False
    if product=='cubesandbox':
        identity=raw.get('uuid') or raw.get('InstanceID')
        ready=identity==node_id and raw.get('Healthy') is True and raw.get('ReportedReady') is True and fresh(raw.get('MetaDataUpdateAt'))
        effective={key:raw[field] for key,field in {'max_sandboxes':'MaxMvmLimit','create_concurrency':'CreateConcurrentNum',
            'quota_cpu':'QuotaCpu','quota_memory':'QuotaMem'}.items() if field in raw}
        evidence={key:raw.get(key) for key in ('uuid','InstanceID','Healthy','ReportedReady','MetaDataUpdateAt')}
    elif product=='kuasar':
        node=raw.get('node') or {}
        ready=(raw.get('ok') is True and raw.get('found') is True and node.get('node_id')==node_id
            and bool(node.get('link_owner')) and fresh(node.get('last_heartbeat_unix')) and node.get('draining',False) is False)
        effective={'sandbox_capacity':node.get('capacity',0) or None}
        evidence={key:node.get(key) for key in ('node_id','link_owner','last_heartbeat_unix','api_endpoint','data_endpoint','runtime_digest','draining')}
    elif product=='agentenv':
        # ListObservedNodes derives status from the current heartbeat lease.
        ready=raw.get('id')==node_id and raw.get('status')=='ready' and bool(raw.get('serviceInstanceID'))
        effective={}
        evidence={key:raw.get(key) for key in ('id','serviceInstanceID','clusterID','status','version','commit')}
    else:raise ValueError('unregistered product')
    units={'max_sandboxes':'count','create_concurrency':'count','quota_cpu':'mCPU','quota_memory':'MiB'} if product=='cubesandbox' else {'sandbox_capacity':'count'}
    return {'status':'satisfied' if ready else 'mismatch','node_id':node_id,'effective':effective,'units':units,'observation':evidence}


class RuntimeRegistrationProbe:
    def __init__(self,*,probe,bindings,http=None):
        from sandbox_bench.transport import request
        self.probe=probe;self.bindings=bindings;self.http=http or request

    async def check(self,declaration,node,*,boot_id):
        from .parameter_capabilities import capability
        now=datetime.now(timezone.utc);observed_at=now.isoformat();rows=[]
        def add(check,status,value,repair=None):
            ref=self.probe.commit({'kind':check,'node_id':node.node_id,'boot_id':boot_id,'observed_at':observed_at,'result':value})
            rows.append(CheckObservation(check_id=check,rule_ref='runtime-registration.v1:'+declaration.product,
                node_id=node.node_id,component='management_registration',status=status,observed=value,
                observed_at=observed_at,evidence_refs=(ref,),boot_id=boot_id,repair_code=repair))
        if not set(node.roles)&{'compute','conductor','runtime','server'}:
            add('registration','not_applicable',{'reason':'management_role_is_not_a_compute_registration',
                'requires':'current_service_config_and_group_canary'})
            for category in ('platform_limits','performance_policy','host_attributes'):
                for name,target in getattr(declaration.targets,category).items():
                    if declaration.product=='agentenv' and category=='host_attributes' and name=='ublks_max':continue
                    roles=target_roles(declaration.product,name) if category!='host_attributes' else None
                    applicable=roles is None or bool(set(node.roles)&roles)
                    coverage=capability(declaration.product,name,node.roles) if category!='host_attributes' else None
                    if applicable and coverage and not coverage['supported']:
                        add('effective:'+category+':'+name,'unsupported',{'capability':coverage,'target':target.model_dump(mode='json')})
                        continue
                    add('effective:'+category+':'+name,'unknown' if applicable else 'not_applicable',
                        {'reason':'role_runtime_value_unavailable' if applicable else 'parameter_belongs_to_other_roles',
                         'target':target.model_dump(mode='json'),'applicable_roles':sorted(roles) if roles else None})
            return rows
        binding=self.bindings.get(node.node_id)
        if not binding:
            add('registration','unknown',{'reason':'registered_management_endpoint_and_product_node_id_required'})
            return rows
        if set(binding)-{'endpoint_ref','product_node_id','credential_config','max_age_seconds'}:
            raise ValueError('unregistered management probe binding')
        endpoint=declaration.service_endpoints.get(binding.get('endpoint_ref'))
        if not endpoint or not endpoint.startswith(('http://','https://')):raise ValueError('declared HTTP management endpoint required')
        identity=binding.get('product_node_id')
        if not isinstance(identity,str) or not identity:raise ValueError('explicit product node identity required')
        headers={'Content-Type':'application/json'}
        if binding.get('credential_config'):
            from ..bindings import resolve_benchmark_credential
            headers['X-API-Key']=resolve_benchmark_credential(binding['credential_config'])
        method='GET';payload=None
        if declaration.product=='cubesandbox':path='/internal/v1/nodes/'+quote(identity,safe='')
        elif declaration.product=='agentenv':path='/nodes/'+quote(identity,safe='')
        else:
            # This fixed RPC reads Runtime; none of the mutation operations are accepted.
            method='POST';path='/internal/node-owner';payload=json.dumps({'op':'runtime','node_id':identity}).encode()
        try:
            response=await asyncio.to_thread(self.http,method,endpoint.rstrip('/')+path,
                headers=headers,body=payload,timeout=20)
            if not 200<=response.status<300:raise ValueError('management endpoint rejected observation')
            raw=json.loads(response.body,object_pairs_hook=_unique_json_object)
            result=interpret_registration(declaration.product,raw,node_id=identity,now=now.timestamp(),
                max_age_seconds=binding.get('max_age_seconds',120))
        except Exception as exc:
            add('registration','unknown',{'reason':type(exc).__name__});return rows
        add('registration',result['status'],result,repair='REGISTRATION_MISMATCH' if result['status']=='mismatch' else None)
        for category in ('platform_limits','performance_policy','host_attributes'):
            for name,target in getattr(declaration.targets,category).items():
                if declaration.product=='agentenv' and category=='host_attributes' and name=='ublks_max':continue
                roles=target_roles(declaration.product,name) if category!='host_attributes' else None
                if roles is not None and not set(node.roles)&roles:
                    add('effective:'+category+':'+name,'not_applicable',{'reason':'parameter_belongs_to_other_roles','applicable_roles':sorted(roles)})
                    continue
                coverage=capability(declaration.product,name,node.roles) if category!='host_attributes' else None
                if coverage and not coverage['supported']:
                    add('effective:'+category+':'+name,'unsupported',{'capability':coverage,'target':target.model_dump(mode='json')})
                    continue
                actual=result['effective'].get(name)
                native_unit=result['units'].get(name)
                if target.unit and native_unit and actual is not None:
                    from ..parameters import _convert
                    actual=_convert(actual,native_unit,target.unit)
                if name not in result['effective']:status='unknown'
                elif target.mode=='preserve':status='satisfied'
                elif target.mode=='exact':status='satisfied' if actual==target.value else 'mismatch'
                else:
                    # Explicit finite budget resolution is supplied by the frozen declaration.
                    low=declaration.budgets.get(target.budget_ref+'.minimum');high=declaration.budgets.get(target.budget_ref+'.maximum')
                    status='satisfied' if type(actual) in (int,float) and type(low) in (int,float) and type(high) in (int,float) and low<=actual<=high else 'unknown'
                add('effective:'+category+':'+name,status,{'actual':actual,'target':target.model_dump(mode='json'),
                    'unit':target.unit or native_unit,'evidence_level':'runtime_effective' if name in result['effective'] else 'unavailable'},
                    repair='CONFIGURATION_MISMATCH' if status=='mismatch' else None)
        return rows
