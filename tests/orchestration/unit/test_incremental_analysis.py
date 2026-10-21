"""P10.4 / ORC-047-049,052,053,068: a new delivery version freezes a new input, keeping the old."""
from sandbox_graph.services.reviews import ReviewService


def seed(repository):
    with repository.transaction() as tx:
        for version in (1, 2):
            key = f'd1:{version}'
            tx.put('deliveries', key, {'scope': 'g', 'plan_ref': 'p', 'delivery_id': 'd1',
                                       'version': version})
            tx.put('delivery_reviews', key, {'review_id': f'rv{version}', 'decision': 'accepted'})


def test_new_delivery_version_creates_a_new_frozen_input_and_keeps_the_old(repository):
    seed(repository)
    service = ReviewService(repository, None)
    first = service.freeze_analysis('p', {'g': ('d1', 1)}, rules_ref='rules:1')
    second = service.freeze_analysis('p', {'g': ('d1', 2)}, rules_ref='rules:1')
    assert first != second
    assert service.get_analysis(first)['deliveries'] == {'g': ['d1', 1]}   # old input retained
    assert service.get_analysis(second)['deliveries'] == {'g': ['d1', 2]}


def test_freezing_the_same_version_is_idempotent(repository):
    seed(repository)
    service = ReviewService(repository, None)
    assert service.freeze_analysis('p', {'g': ('d1', 1)}, rules_ref='rules:1') == \
           service.freeze_analysis('p', {'g': ('d1', 1)}, rules_ref='rules:1')
