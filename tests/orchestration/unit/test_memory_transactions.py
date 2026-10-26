import pytest


def test_fixture_transactions_do_not_copy_unmodified_history(repository,monkeypatch):
    import tests.orchestration.conftest as fixtures
    with repository.transaction() as tx:tx.put('large_history','original',{'items':list(range(1000))})
    original=fixtures.deepcopy;calls=[]
    monkeypatch.setattr(fixtures,'deepcopy',lambda value:(calls.append(type(value)),original(value))[1])
    with repository.transaction():pass
    assert calls==[]


def test_nested_fixture_rollback_and_caller_mutation_remain_isolated(repository):
    value={'nested':{'value':1}}
    with repository.transaction() as tx:tx.put('test','value',value)
    value['nested']['value']=99
    with pytest.raises(RuntimeError):
        with repository.transaction() as tx:
            current=tx.get('test','value');current['body']['nested']['value']=2
            assert tx.get('test','value')['body']['nested']['value']==1
            tx.put('test','value',current['body'])
            with pytest.raises(ValueError):
                with repository.transaction() as nested:
                    nested.delete('test','value');nested.acquire_thread('new','owner',10)
                    raise ValueError('inner rollback')
            assert tx.get('test','value')['body']['nested']['value']==2
            assert 'new' not in repository.leases
            raise RuntimeError('outer rollback')
    with repository.transaction() as tx:assert tx.get('test','value')['body']['nested']['value']==1
