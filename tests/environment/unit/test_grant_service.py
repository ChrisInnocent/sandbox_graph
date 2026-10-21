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
