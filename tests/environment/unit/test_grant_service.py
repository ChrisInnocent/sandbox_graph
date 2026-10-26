import pytest
from sandbox_graph.environment.permits import ExecutionGrantIssuer
from sandbox_graph.environment.grant_service import FencedExecutionGrantService


class Tx:
    now=1000.0
    def __init__(self,records,scope='env',run='run'): self.records=records; self.scope=scope; self.run=run
    def assert_fence(self,token):
        if token!='live': raise RuntimeError('stale fence')
    def get(self,kind,key):
        value=self.records.get((kind,key)); return {'body':value} if value is not None else None
    def put(self,kind,key,body,**kwargs): self.records[(kind,key)]=body


class Repo:
    def __init__(self): self.records={}
    def transaction(self):
        repo=self
        class Context:
            def __enter__(self): self.tx=Tx(repo.records); return self.tx
            def __exit__(self,*args): return False
        return Context()


def test_grant_issuer_requires_fenced_run_and_all_leases_then_advances_domain_generations():
    repo=Repo(); domains=['host:h1','service:control']
    for domain in domains:
        repo.records[('resource_leases',domain)]={'scope':'env','run_id':'run','thread_id':'t','owner_epoch':3,'status':'reserved'}
    issuer=ExecutionGrantIssuer(b'x'*32,clock=lambda:1000)
    service=FencedExecutionGrantService(repo,issuer)
    grant=service.issue(token='live',operation_id='op',step_id='step',run_id='run',thread_id='t',owner_epoch=3,
        scope='env',machine_id='m',resource_domains=domains,action='service.start.v1',input_data={'unit':'x'})
    assert grant.resource_generations=={'host:h1':1,'service:control':1}
    again=service.issue(token='live',operation_id='op',step_id='step',run_id='run',thread_id='t',owner_epoch=3,
        scope='env',machine_id='m',resource_domains=domains,action='service.start.v1',input_data={'unit':'x'})
    assert all(again.resource_generations[d]==2 for d in domains)
    with pytest.raises(PermissionError,match='lease'):
        service.issue(token='live',operation_id='op',step_id='step',run_id='other',thread_id='t',owner_epoch=3,
            scope='env',machine_id='m',resource_domains=domains,action='service.start.v1',input_data={})


def test_resource_generation_survives_change_of_scope_and_owner():
    repo=Repo();issuer=ExecutionGrantIssuer(b'x'*32,clock=lambda:1000)
    service=FencedExecutionGrantService(repo,issuer)
    values=[]
    for scope in ('environment-a','environment-b'):
        repo.records[('resource_leases','host:shared')]={'scope':scope,'run_id':'run','thread_id':'t','owner_epoch':3,'status':'reserved'}
        grant=service.issue(token='live',operation_id='op:'+scope,step_id='step',run_id='run',thread_id='t',owner_epoch=3,
            scope=scope,machine_id='m',resource_domains=['host:shared'],action='service.start.v1',input_data={'unit':'x'})
        values.append(grant.resource_generations['host:shared'])
    assert values==[1,2]


def test_challenge_issuance_is_persisted_and_bounded_by_live_lease():
    from sandbox_graph.domain.models import canonical_hash
    from sandbox_graph.persistence.repository import LeaseToken
    from tests.orchestration.conftest import MemoryRepository,FakeClock
    from sandbox_graph.runtime.leases import LeaseService
    repo=MemoryRepository(FakeClock());token=LeaseService(repo).acquire_thread('thread','owner',5)
    LeaseService(repo).acquire_all(['host:host'],scope='env',token=token,owner_run_id='run')
    with repo.transaction() as tx:tx.put('dispatch_gates','run',{'open':True,'generation':1})
    issuer=ExecutionGrantIssuer(b'x'*32,clock=lambda:1000.)
    service=FencedExecutionGrantService(repo,issuer)
    data={'test':True};challenge={'protocol':'environment-grant.v1','machine_id':'host','boot_id':'boot',
        'nonce':'a'*43,'wall_time':1000.,'monotonic_time':10.,'challenge_seconds':15,
        'input_hash':canonical_hash({'action':'product.recipe.step.v1','input':data})}
    args=dict(token=token,operation_id='op',step_id='step',run_id='run',scope='env',machine_id='host',
        resource_domains=['host:host'],action='product.recipe.step.v1',input_data=data,
        challenge=challenge,round_trip_seconds=.1)
    grant=service.issue_for_challenge(**args)
    assert grant.nonce==challenge['nonce'] and grant.not_after<=1005.
    with repo.transaction() as tx:assert tx.get('execution_grants',grant.grant_id)
    with pytest.raises(PermissionError,match='challenge'):service.issue_for_challenge(**args)
    with pytest.raises(PermissionError):service.issue_for_challenge(**(args|{'challenge':challenge|{'nonce':'b'*43},'round_trip_seconds':3.}))


def test_compensation_requires_original_declared_rollback_contract():
    from tests.orchestration.conftest import MemoryRepository,FakeClock
    from sandbox_graph.runtime.leases import LeaseService
    from sandbox_graph.domain.models import canonical_hash
    repo=MemoryRepository(FakeClock());leases=LeaseService(repo);token=leases.acquire_thread('t','owner',60)
    leases.acquire_all(['host:m'],scope='env',token=token,owner_run_id='run')
    service=FencedExecutionGrantService(repo,ExecutionGrantIssuer(b'x'*32,clock=lambda:1000))
    data={'step_id':'file:config'};remote='environment-step:'+canonical_hash({'operation':'op','input_hash':'a'*64})
    expected={'operation_id':remote,'input_hash':canonical_hash({'action':'product.recipe.step.v1','input':data|{'operation_id':remote}})}
    original={'scope':'env','run_id':'run','phase':'terminal','status':{'authoritative':True,'state':'completed'},
        'request':{'input_hash':'a'*64,'input':{'step':{'input':data,'compensation_ref':None}}}}
    args=dict(original_operation_id='op',token=token,operation_id=remote,step_id='file:config',run_id='run',scope='env',
        machine_id='m',resource_domains=['host:m'],action='operation.recipe.compensate.v1',input_data=expected)
    with repo.transaction() as tx:tx.put('operations','op',original)
    with pytest.raises(PermissionError,match='compensation|rollback'):service.issue_safety(**args)
    original['request']['input']['step']['compensation_ref']='file.restore.v1'
    with repo.transaction() as tx:tx.put('operations','op',original)
    assert service.issue_safety(**args).input_hash==canonical_hash({'action':'operation.recipe.compensate.v1','input':expected})
