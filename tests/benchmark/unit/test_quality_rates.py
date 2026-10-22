"""创建成功率/接纳率：api_status 必须被写入，quality 给出显式比率（RT-36）。"""
from sandbox_bench.metrics.quality import summarize_quality


def test_quality_reports_accepted_and_completed_rates():
    rows = [
        {'operation_id': '0', 'status': 'success', 'api_status': 'accepted'},
        {'operation_id': '1', 'status': 'success', 'api_status': 'accepted'},
        {'operation_id': '2', 'status': 'failed', 'api_status': 'rejected', 'reason': 'api_rejected'},
        {'operation_id': '3', 'status': 'unknown', 'api_status': 'accepted'},
    ]
    q = summarize_quality(rows, final_cohort=['s0', 's1'])
    assert q['n_attempts'] == 4
    assert q['api_success_count'] == 3 and q['accepted_rate'] == 0.75
    assert q['completed_success_count'] == 2 and q['completed_success_rate'] == 0.5
    assert q['final_cohort_count'] == 2
    assert q['failure_statuses'] == {'failed': 1, 'unknown': 1}


def test_quality_rates_are_null_when_no_attempts():
    q = summarize_quality([])
    assert q['n_attempts'] == 0 and q['accepted_rate'] is None and q['completed_success_rate'] is None
