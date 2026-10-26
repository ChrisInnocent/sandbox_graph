"""条件参数文件（profile）的独立测试：字面量 oracle，不调用被测函数生成预期。"""
import json
from pathlib import Path

import pytest

from sandbox_bench.conditions import (ConditionProfile, SpecProfile, load_profile,
                                      parse_spec_label)

GIB = 1024 ** 3
ROOT = Path(__file__).resolve().parents[3]


def _profile(**changes):
    base = {'schema_version': 'conditions.v1', 'profile': 'unit', 'spec': '2v2g',
            'groups': 1, 'works_per_group': 2, 'concurrency': 1, 'requests': 2}
    base.update(changes)
    return base


# --- 规格标签 ---

def test_parse_spec_label_literal_values():
    assert parse_spec_label('2v2g') == (2, 2 * GIB)
    assert parse_spec_label('2v4') == (2, 4 * GIB)
    assert parse_spec_label('8v16G') == (8, 16 * GIB)


@pytest.mark.parametrize('bad', ['2x2g', '2v', 'v2g', '0v2g', '2v0g', '2v2v2g', '', 'abc'])
def test_parse_spec_label_rejects_malformed(bad):
    with pytest.raises(ValueError):
        parse_spec_label(bad)


def test_spec_profile_from_label_and_back():
    spec = SpecProfile.model_validate('2v4g')
    assert (spec.vcpus, spec.memory_gib, spec.memory_bytes, spec.label) == (2, 4, 4 * GIB, '2v4g')


# --- 模型校验 ---

def test_unknown_field_is_rejected_not_ignored():
    with pytest.raises(ValueError):
        ConditionProfile.model_validate(_profile(sandbox_count=3))


def test_negative_and_zero_conditions_rejected():
    for field in ('groups', 'works_per_group', 'concurrency', 'requests'):
        with pytest.raises(ValueError):
            ConditionProfile.model_validate(_profile(**{field: 0}))
    with pytest.raises(ValueError):
        ConditionProfile.model_validate(_profile(methods=[]))


# --- 派生与展开 ---

def test_mock_kwargs_maps_conditions_without_inventing_values():
    profile = ConditionProfile.model_validate(_profile(spec='2v4g', groups=2, works_per_group=3,
                                                      concurrency=4, requests=5, max_work_concurrency=4))
    assert profile.mock_kwargs() == {'groups': 2, 'works': 6, 'requests': 5, 'concurrency': 4,
                                     'max_work_concurrency': 4, 'vcpus': 2, 'memory_bytes': 4 * GIB,
                                     'fail_last_group': False}
    assert profile.total_works == 6


def test_cells_default_to_single_cell_when_no_axes():
    profile = ConditionProfile.model_validate(_profile(spec='2v2g', concurrency=1, requests=2))
    assert profile.cells() == [{'spec': '2v2g', 'vcpus': 2, 'memory_bytes': 2 * GIB,
                                'concurrency': 1, 'requests': 2}]


def test_cells_expand_axes_in_declared_order():
    profile = ConditionProfile.model_validate(_profile(axes={
        'specs': ['2v2g', '2v4g'], 'concurrency': [1, 2], 'requests': [3]}))
    cells = profile.cells()
    assert [(c['spec'], c['concurrency'], c['requests']) for c in cells] == [
        ('2v2g', 1, 3), ('2v2g', 2, 3), ('2v4g', 1, 3), ('2v4g', 2, 3)]
    assert all(c['memory_bytes'] in (2 * GIB, 4 * GIB) for c in cells)


# --- 文件读取 ---

def test_load_yaml_profile_file_roundtrip(tmp_path):
    path = tmp_path / 'c.yaml'
    path.write_text('schema_version: conditions.v1\nprofile: f\nspec: 2v2g\nconcurrency: 1\nrequests: 1\n')
    profile = load_profile(path)
    assert profile.profile == 'f' and profile.spec.label == '2v2g'


def test_load_json_profile_file_roundtrip(tmp_path):
    path = tmp_path / 'c.json'
    path.write_text(json.dumps(_profile()))
    assert load_profile(path).spec.label == '2v2g'


def test_missing_file_is_explicit(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_profile(tmp_path / 'nope.yaml')


# --- 仓库自带档 ---

def test_repo_verify_low_profile_is_valid_and_low():
    profile = load_profile(ROOT / 'config' / 'conditions.yaml')
    assert profile.profile == 'verify-low'
    assert (profile.groups, profile.works_per_group, profile.concurrency, profile.requests) == (1, 2, 1, 2)
    assert (profile.spec.vcpus, profile.spec.memory_gib) == (2, 2)
    assert profile.target_density_per_node == 2


def test_repo_matrix_small_profile_expands_to_four_cells():
    profile = load_profile(ROOT / 'config' / 'matrix-small.yaml')
    assert len(profile.cells()) == 4
