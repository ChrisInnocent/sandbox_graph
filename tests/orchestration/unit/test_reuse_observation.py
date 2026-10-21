import pytest
from sandbox_graph.services.reuse import ReuseService,ReuseContract
from sandbox_graph.runtime.observation import ObservationService
from sandbox_graph.persistence.repository import Conflict


def contract(**updates):
    return ReuseContract(environment_ref='env1',spec_hash='spec1',method_ref='B01:1',completion_event='ready',time_domain='host:boot:mono',script_ref='script:1',valid_until=1100,**updates)


def test_reuse_requires_accepted_compatible_evidence_and_preserves_old_work(repository,clock):
    reuse=ReuseService(repository)
    with repository.transaction() as tx:
        tx.put('deliveries','old:1',{'work_refs':['old-work'],'result':{'execution_status':'succeeded'}},immutable=True)
        tx.put('delivery_reviews','old:1',{'decision':'accepted','review_id':'review'})
    candidate=reuse.consider('new-work','old:1',old_contract=contract(),new_contract=contract(),deadline=1050)
    assert candidate['status']=='accepted'
    with repository.transaction() as tx:
        assert tx.get('reuse_refs','new-work')['body']['reuse_from']=='old:1'
        assert tx.get('deliveries','old:1')['body']['work_refs']==['old-work']
    with pytest.raises(Conflict): reuse.consider('different','old:1',old_contract=contract(),new_contract=contract().model_copy(update={'spec_hash':'changed'}),deadline=1050)


def test_pending_reuse_expires_without_silently_starting_duplicate_measurement(repository,clock):
    reuse=ReuseService(repository)
    candidate=reuse.consider('new-work','late:1',old_contract=contract(),new_contract=contract(),deadline=1005)
    assert candidate['status']=='waiting'
    assert not reuse.can_measure('new-work')
    clock.advance(6)
    assert reuse.expire('new-work',policy='blocked')['status']=='blocked'
    assert not reuse.can_measure('new-work')


def test_observation_restart_marks_discontinuity_and_never_reuses_monotonic_epoch(repository):
    service=ObservationService(repository)
    service.start('sample','work',operation_id='observe-op',clock_epoch='boot-A',deadline=1090)
    service.append('sample',seq=1,clock_epoch='boot-A',value={'pss_bytes':42})
    service.restart('sample',clock_epoch='boot-B')
    with pytest.raises(Conflict): service.append('sample',seq=2,clock_epoch='boot-A',value={'pss_bytes':0})
    service.append('sample',seq=2,clock_epoch='boot-B',value={'pss_bytes':43})
    result=service.stop('sample',last_seq=3)
    assert result['interrupted'] and result['missing_sequences']==[3]
    assert result['data_status']=='insufficient'
