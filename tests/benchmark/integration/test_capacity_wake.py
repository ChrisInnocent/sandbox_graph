"""回归：工作槽位释放必须唤醒卡在并发预算上的组，否则组永久死锁。

复现 issue：`SchedulingService.dispatch_work` 在 slots=0 时把 work 标为
`queue_reason='concurrency_budget'` 并让组 park；但容量释放时没有任何事件唤醒
这些组（`_publish_completion` 只发本组 aggregate 的 `work_completed`），因此
抢不到全局槽位的组的剩余 work 永远不被派发。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))

from mock_pipeline import run_mock


@pytest.mark.asyncio
async def test_capacity_release_wakes_groups_waiting_on_concurrency_budget():
    # 2 组 × 3 个 work，全局并发 2：先跑的组会占满槽位，另一组必须能被后续释放唤醒。
    summary = await run_mock(groups=2, works=6, requests=1, max_work_concurrency=2)
    states = list(summary['work_states'].values())
    delivered = [state for state in states if state[1] == 'delivered']
    stalled = {k: v for k, v in summary['work_states'].items() if v[1] != 'delivered'}
    assert len(delivered) == 6, f'仍有 work 卡在并发预算上：{stalled}'


@pytest.mark.asyncio
async def test_capacity_wake_does_not_require_extra_slots_than_authorized():
    # 并发=1：每组串行也必须最终全部交付。
    summary = await run_mock(groups=2, works=4, requests=1, max_work_concurrency=1)
    assert all(v[1] == 'delivered' for v in summary['work_states'].values()), summary['work_states']
