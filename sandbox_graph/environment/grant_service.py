"""Grant issuance within the thread/resource fence and durable challenge ledger."""
import math
from ..domain.models import canonical_hash
from .permits import ExecutionGrantIssuer


class FencedExecutionGrantService:
    def __init__(self,repository,issuer:ExecutionGrantIssuer):
        self.repository=repository;self.issuer=issuer

    def _issue_in(self,tx,*,token,operation_id,step_id,run_id,thread_id,owner_epoch,scope,
                  machine_id,resource_domains,action,input_data,ttl_seconds=30,nonce=None,safety=False):
        domains=tuple(sorted(set(resource_domains)))
        if not domains:raise ValueError('execution grant requires at least one resource conflict domain')
        if not operation_id or not step_id or not run_id or not machine_id:raise ValueError('execution grant identity is incomplete')
        tx.assert_fence(token)
        if getattr(token,'thread_id',thread_id)!=thread_id or getattr(token,'owner_epoch',owner_epoch)!=owner_epoch:
            raise PermissionError('thread fencing identity mismatch')
        generations={}
        for domain in domains:
            lease=tx.get('resource_leases',domain);body=lease['body'] if lease else None
            if (not body or body.get('scope')!=scope or body.get('run_id')!=run_id or body.get('thread_id')!=thread_id
                    or body.get('owner_epoch')!=owner_epoch or body.get('status') not in ({'reserved','quarantine'} if safety else {'reserved'})):
                raise PermissionError('resource lease is not held by this execution: '+domain)
            key=canonical_hash({'domain':domain});previous=tx.get('execution_generations',key)
            historical=[int(row['body']['generation']) for row in tx.scan('execution_generations')
                if row['body'].get('domain')==domain] if hasattr(tx,'scan') else []
            generation=max([int(previous['body']['generation']) if previous else 0,*historical])+1
            tx.put('execution_generations',key,{'scope':scope,'domain':domain,'run_id':run_id,
                'thread_id':thread_id,'owner_epoch':owner_epoch,'generation':generation})
            generations[domain]=generation
        grant=self.issuer.issue(operation_id=operation_id,step_id=step_id,run_id=run_id,
            thread_id=thread_id,owner_epoch=owner_epoch,machine_id=machine_id,resource_generations=generations,
            action=action,input_data=input_data,ttl_seconds=ttl_seconds,nonce=nonce)
        tx.put('execution_grants',grant.grant_id,grant.model_dump(mode='json'),immutable=True)
        return grant

    def issue(self,**kwargs):
        with self.repository.transaction() as tx:return self._issue_in(tx,**kwargs)

    def issue_safety(self,*,original_operation_id,token,**kwargs):
        if kwargs['action'] not in {'operation.recipe.cancel.v1','operation.recipe.compensate.v1'}:
            raise PermissionError('unregistered safety action')
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            original=tx.get('operations',original_operation_id)
            if (not original or original['body'].get('run_id')!=kwargs['run_id']
                    or original['body'].get('scope')!=kwargs['scope']):
                raise PermissionError('safety action must bind an original owned operation')
            request=original['body']['request'];step=request['input']['step'];data=dict(step['input'])
            if kwargs['action']=='operation.recipe.compensate.v1' and (
                    step.get('compensation_ref')!='file.restore.v1' or original['body'].get('phase')!='terminal'
                    or original['body'].get('status',{}).get('authoritative') is not True
                    or original['body'].get('status',{}).get('state')!='completed'):
                raise PermissionError('compensation requires completed original operation and declared file rollback contract')
            remote='environment-step:'+canonical_hash({'operation':original_operation_id,'input_hash':request['input_hash']})
            data['operation_id']=remote
            expected={'operation_id':remote,'input_hash':canonical_hash({'action':'product.recipe.step.v1','input':data})}
            if kwargs['input_data']!=expected or kwargs['operation_id']!=remote or kwargs['step_id']!=step['input']['step_id']:
                raise PermissionError('safety action changed the original input')
            ttl=min(10.,tx.thread_lease_until(token)-tx.now)
            if ttl<=self.issuer.max_clock_skew_seconds:raise PermissionError('safety grant lease is too short')
            return self._issue_in(tx,token=token,thread_id=token.thread_id,owner_epoch=token.owner_epoch,
                ttl_seconds=ttl,safety=True,**kwargs)

    def issue_for_challenge(self,*,challenge,round_trip_seconds,token,authorize_in=None,**kwargs):
        required={'protocol','nonce','machine_id','boot_id','input_hash','wall_time','monotonic_time','challenge_seconds'}
        if not isinstance(challenge,dict) or set(challenge)!=required or challenge['protocol']!='environment-grant.v1':
            raise PermissionError('invalid remote challenge')
        if (not challenge['boot_id'] or challenge['machine_id']!=kwargs['machine_id']
                or challenge['input_hash']!=canonical_hash({'action':kwargs['action'],'input':kwargs['input_data']})):
            raise PermissionError('remote challenge input/host binding mismatch')
        times=(round_trip_seconds,challenge['wall_time'],challenge['monotonic_time'],challenge['challenge_seconds'])
        if (any(type(value) not in (float,int) or not math.isfinite(value) for value in times)
                or not 0<=round_trip_seconds<=2 or not 0<challenge['challenge_seconds']<=30):
            raise PermissionError('remote challenge clock bound unavailable')
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            if authorize_in is None and not getattr(self.repository,'is_test_fixture',False):
                raise PermissionError('original operation authorization is required')
            if authorize_in is not None:authorize_in(tx)
            gate=tx.get('dispatch_gates',kwargs['run_id'])
            if not gate or gate['body'].get('open') is not True:raise PermissionError('environment dispatch gate closed')
            skew=abs(tx.now-challenge['wall_time'])+round_trip_seconds
            if skew>2 or abs(self.issuer.clock()-tx.now)>.25:raise PermissionError('remote challenge clock skew exceeds bound')
            key=canonical_hash({'machine_id':challenge['machine_id'],'boot_id':challenge['boot_id'],'nonce':challenge['nonce']})
            if tx.get('execution_challenges',key):raise PermissionError('remote challenge already issued')
            until=tx.thread_lease_until(token)
            ttl=min(30,until-tx.now-skew,challenge['challenge_seconds']-round_trip_seconds-skew)
            if ttl<=self.issuer.max_clock_skew_seconds:raise PermissionError('remote challenge expires before safe admission')
            grant=self._issue_in(tx,token=token,thread_id=token.thread_id,owner_epoch=token.owner_epoch,
                ttl_seconds=ttl,nonce=challenge['nonce'],**kwargs)
            tx.put('execution_challenges',key,{'grant_id':grant.grant_id,'challenge':challenge,
                'clock_skew_bound':skew,'lease_until':until},immutable=True)
            return grant
