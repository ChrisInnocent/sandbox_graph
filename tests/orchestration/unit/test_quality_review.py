"""结果/报告合理性审视：只报告问题，不改事实。"""
from sandbox_graph.services.quality_review import (review_result_numbers, review_report_facts,
                                                   review_interpretation)


def test_result_numbers_flag_negatives_nonfinite_booleans_and_overcounts():
    assert review_result_numbers({'latency_s': 0.1, 'submitted': 3, 'successes': 3}) == []
    assert 'latency_s is not a plausible non-negative finite number' in review_result_numbers({'latency_s': -1})
    assert 'pss_bytes is not a plausible non-negative finite number' in review_result_numbers({'pss_bytes': float('inf')})
    assert 'throughput must be a number, not a boolean' in review_result_numbers({'throughput': True})
    assert 'successes exceed submitted requests' in review_result_numbers({'successes': 4, 'submitted': 3})


def test_report_facts_flag_coverage_unknown_manifests_and_null_valid_values():
    facts = {'cases': [{'case_id': 'a', 'manifest_sha256': 'h1'}],
             'facts': [{'fact_id': 'f1', 'value': None, 'qualification': 'valid', 'manifest_sha256': 'h1'},
                       {'fact_id': 'f2', 'value': 1, 'qualification': 'valid', 'manifest_sha256': 'zzz'},
                       {'fact_id': 'f3', 'value': float('nan'), 'qualification': 'valid', 'manifest_sha256': 'h1'}],
             'pairs': [{'left': 'a', 'right': 'ghost', 'status': 'comparable'}]}
    findings = review_report_facts(facts, expected_case_ids={'a', 'b'})
    assert 'coverage is missing selected cases: [\'b\']' in findings
    assert any('null value' in f for f in findings)
    assert any('unknown manifest' in f for f in findings)
    assert any('non-finite value' in f for f in findings)
    assert 'comparable pair references cases outside the frozen input' in findings


def test_report_facts_clean_input_has_no_findings():
    facts = {'cases': [{'case_id': 'a', 'manifest_sha256': 'h1'}, {'case_id': 'b', 'manifest_sha256': 'h2'}],
             'facts': [{'fact_id': 'f1', 'value': 1, 'qualification': 'valid', 'manifest_sha256': 'h1'}],
             'pairs': [{'left': 'a', 'right': 'b', 'status': 'incomparable', 'reasons': ['missing:condition']}]}
    assert review_report_facts(facts, expected_case_ids={'a', 'b'}) == []


def test_interpretation_layer_must_be_marked_unverified():
    assert review_interpretation(None) == []
    assert review_interpretation({'interpretation_status': 'unverified_model_interpretation'}) == []
    assert review_interpretation({'interpretation_status': 'facts'}) != []
