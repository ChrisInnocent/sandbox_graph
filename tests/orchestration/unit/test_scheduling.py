from sandbox_graph.services.scheduling import SchedulingService
from sandbox_graph.interfaces.service import RunService
from sandbox_graph.planning.compiler import PlanCompiler,Case,Selection
from sandbox_graph.runtime.leases import LeaseService


def test_group_dispatch_is_durable_independent_and_work_domain_conflicts_queue(repository):
    cases=[Case(capability='supported',adapter_verified=True,case_id=g,group_id=g,sandbox_type='synthetic',scenario_id='B01',method_id='B01.TEMPLATE',priority='base',conditions=({'cpu':1},),conflict_domains=domains,contract_ref='fixture') for g,domains in [('A',('host_A',)),('B',('host_B',)),('C',('host_A',))]]
    service=RunService(repository,PlanCompiler(cases)); plan=service.preview(Selection(),authorized_groups={'A','B','C'})
    run=service.start('start',plan.hash,authorized_groups={'A','B','C'})
    scheduler=SchedulingService(repository); root=LeaseService(repository).acquire_thread(run.root_thread_id,'root',100)
    assert len(scheduler.dispatch_groups(run.run_id,token=root,deadline=1090))==3
    assert len(scheduler.dispatch_groups(run.run_id,token=root,deadline=1090))==3
    for group in ('A','B','C'):
        scheduler.expand_work(run.run_id,group,plan_ref=plan.hash)
    a=LeaseService(repository).acquire_thread('group:A','A',100)
    b=LeaseService(repository).acquire_thread('group:B','B',100)
    c=LeaseService(repository).acquire_thread('group:C','C',100)
    assert len(scheduler.dispatch_work(run.run_id,'A',token=a,max_concurrency=3,deadline=1090))==1
    assert len(scheduler.dispatch_work(run.run_id,'B',token=b,max_concurrency=3,deadline=1090))==1
    assert scheduler.dispatch_work(run.run_id,'C',token=c,max_concurrency=3,deadline=1090)==[]
    with repository.transaction() as tx:
        work=[x['body'] for x in tx.scan('work_instances')]
        assert len(work)==3
        assert [x['queue_reason'] for x in work if x['group_id']=='C']==['resource_conflict']
        assert len([j for j in tx.scan('jobs') if j['body']['job_kind']=='group'])==3


def test_pending_or_accepted_reuse_candidate_prevents_duplicate_measurement(repository):
    case=Case(capability='supported',adapter_verified=True,case_id='case',group_id='G_A',sandbox_type='synthetic',scenario_id='B01',method_id='B01.TEMPLATE',priority='base',conditions=({'cpu':1},),conflict_domains=('host_A',))
    service=RunService(repository,PlanCompiler([case]));plan=service.preview(Selection(),authorized_groups={'G_A'});run=service.start('s',plan.hash,authorized_groups={'G_A'})
    scheduler=SchedulingService(repository);work=scheduler.expand_work(run.run_id,'G_A',plan_ref=plan.hash)[0]
    from sandbox_graph.services.reuse import ReuseService,ReuseContract
    contract=ReuseContract(environment_ref='env',spec_hash='spec',method_ref='B01.TEMPLATE:1',completion_event='ready',time_domain='host:mono',script_ref='script:1',valid_until=1100)
    ReuseService(repository).consider(work,'late:1',old_contract=contract,new_contract=contract,deadline=1090,allowed_source_scopes={'old'})
    token=LeaseService(repository).acquire_thread('group','worker',100)
    assert scheduler.dispatch_work(run.run_id,'G_A',token=token,max_concurrency=1,deadline=1090)==[]
    with repository.transaction() as tx:
        assert tx.get('work_instances',work)['body']['queue_reason']=='reuse_candidate_waiting'
        assert tx.get('resource_leases','host_A') is None


def test_expansion_preserves_method_version_and_experiment_repeat_identity(repository):
    import pytest
    from pydantic import ValidationError
    from sandbox_graph.planning.compiler import Case,PlanCompiler,Selection
    from sandbox_graph.interfaces.service import RunService
    base=dict(group_id='G_A',sandbox_type='x',scenario_id='B01',method_id='B01.TEMPLATE',priority='base',conditions=({'size':1},),conflict_domains=('host',))
    cases=[Case(capability='supported',adapter_verified=True,case_id='round1',repeat_id=1,method_version='v1',**base),Case(capability='supported',adapter_verified=True,case_id='round2',repeat_id=2,method_version='v1',**base),Case(capability='supported',adapter_verified=True,case_id='version2',repeat_id=1,method_version='v2',**base)]
    with pytest.raises(ValidationError): Case(capability='supported',adapter_verified=True,case_id='bad',repeat_id=True,**base)
    service=RunService(repository,PlanCompiler(cases))
    plan=service.preview(Selection(),authorized_groups={'G_A'})
    run=service.start('r',plan.hash,authorized_groups={'G_A'})
    ids=SchedulingService(repository).expand_work(run.run_id,'G_A',plan_ref=plan.hash)
    assert len(set(ids))==3
    with repository.transaction() as tx:
        keys=[tx.get('work_instances',identity)['body']['key'] for identity in ids]
    assert {(key['repeat_id'],key['method_version']) for key in keys}=={(1,'v1'),(2,'v1'),(1,'v2')}


def test_explicit_environment_ref_flows_to_group_and_work_without_guessing(repository):
    import pytest
    from sandbox_graph.planning.compiler import Case,PlanCompiler,Selection
    from sandbox_graph.interfaces.service import RunService
    from sandbox_graph.runtime.leases import LeaseService
    base=dict(group_id='G_A',sandbox_type='x',scenario_id='B01',method_id='B01.TEMPLATE',priority='base',conditions=({'size':1},),conflict_domains=('host',))
    case=Case(capability='supported',adapter_verified=True,case_id='case',environment_ref='environment:verified',**base)
    service=RunService(repository,PlanCompiler([case]))
    plan=service.preview(Selection(),authorized_groups={'G_A'})
    run=service.start('run',plan.hash,authorized_groups={'G_A'})
    token=LeaseService(repository).acquire_thread(run.root_thread_id,'owner',30)
    scheduler=SchedulingService(repository)
    jobs=scheduler.dispatch_groups(run.run_id,token=token,deadline=1100)
    work=scheduler.expand_work(run.run_id,'G_A',plan_ref=plan.hash)
    with repository.transaction() as tx:
        assert tx.get('jobs',jobs[0])['body']['payload']['environment_ref']=='environment:verified'
        assert tx.get('work_instances',work[0])['body']['environment_ref']=='environment:verified'
    conflicting=case.model_copy(update={'case_id':'other','environment_ref':'environment:other'})
    with pytest.raises(ValueError,match='environment'):
        PlanCompiler([case,conflicting]).compile(Selection(),authorized_groups={'G_A'})
