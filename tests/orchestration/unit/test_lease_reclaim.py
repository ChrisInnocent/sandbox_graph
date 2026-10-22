"""issue #3：隔离租约的有界、凭证据回收（绝不静默释放）。"""
import pytest

from tests.orchestration.conftest import MemoryRepository, FakeClock
from sandbox_graph.persistence.repository import Conflict
from sandbox_graph.runtime.leases import LeaseService


def _quarantine(repo, domain='host_A', scope='G_A', resources=()):
    with repo.transaction() as tx:
        tx.put('resource_leases', domain, {'scope': scope, 'status': 'quarantine',
                                           'reason': 'delivery cleanup remains unverified'})
        for key, status in resources:
            tx.put('resources', key, {'scope': scope, 'status': status})


def test_reclaim_releases_quarantine_when_evidence_and_all_resources_closed():
    repo = MemoryRepository(FakeClock())
    _quarantine(repo, resources=[('r1', 'closed'), ('r2', 'closed')])
    service = LeaseService(repo)
    token = service.acquire_thread('operator', 'op', 100)
    reclaimed = service.reclaim(['host_A'], scope='G_A', token=token,
                                evidence_ref='artifact://closure/proof', reason='control plane confirms gone')
    assert reclaimed == ['host_A']
    with repo.transaction() as tx:
        assert tx.get('resource_leases', 'host_A') is None
        row = tx.get('lease_reclaims', 'host_A')['body']
        assert row['evidence_ref'] == 'artifact://closure/proof'
        assert any(r['body']['kind'] == 'resource_domains_available' for r in tx.scan('outbox'))


def test_reclaim_requires_evidence_reference():
    repo = MemoryRepository(FakeClock())
    _quarantine(repo)
    service = LeaseService(repo)
    token = service.acquire_thread('operator', 'op', 100)
    with pytest.raises(ValueError, match='evidence'):
        service.reclaim(['host_A'], scope='G_A', token=token, evidence_ref='', reason='x')


def test_reclaim_is_blocked_by_unclosed_resources():
    repo = MemoryRepository(FakeClock())
    _quarantine(repo, resources=[('r1', 'closed'), ('r2', 'active')])
    service = LeaseService(repo)
    token = service.acquire_thread('operator', 'op', 100)
    with pytest.raises(Conflict, match='unclosed'):
        service.reclaim(['host_A'], scope='G_A', token=token, evidence_ref='artifact://x', reason='y')
    with repo.transaction() as tx:
        assert tx.get('resource_leases', 'host_A')['body']['status'] == 'quarantine'


def test_reclaim_refuses_non_quarantine_lease():
    repo = MemoryRepository(FakeClock())
    with repo.transaction() as tx:
        tx.put('resource_leases', 'host_A', {'scope': 'G_A', 'status': 'reserved'})
    service = LeaseService(repo)
    token = service.acquire_thread('operator', 'op', 100)
    with pytest.raises(Conflict, match='quarantined'):
        service.reclaim(['host_A'], scope='G_A', token=token, evidence_ref='artifact://x', reason='y')


def test_reclaim_candidates_lists_quarantine_with_reclaimability():
    repo = MemoryRepository(FakeClock())
    _quarantine(repo, 'host_A', 'G_A', resources=[('r1', 'closed')])
    _quarantine(repo, 'host_B', 'G_B', resources=[('r2', 'active')])
    candidates = {c['domain']: c for c in LeaseService(repo).reclaim_candidates()}
    assert candidates['host_A']['reclaimable'] is True
    assert candidates['host_B']['reclaimable'] is False
    assert candidates['host_B']['scope'] == 'G_B'
