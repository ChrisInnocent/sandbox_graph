"""Run planning checks against literal temporary task/link inputs."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def planning_check(tmp_path, monkeypatch, capsys):
    script = Path(__file__).resolve().parents[3] / 'scripts' / 'verify_planning.py'
    spec = importlib.util.spec_from_file_location('planning_check_under_test', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    change = tmp_path / 'openspec/changes/sandbox-evaluation-workflow'
    change.mkdir(parents=True)
    (tmp_path / 'docs').mkdir()
    (tmp_path / 'README.md').write_text('[tasks](openspec/changes/sandbox-evaluation-workflow/tasks.md)\n')
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    calls = []

    def openspec(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='validated', stderr='')

    monkeypatch.setattr(module.subprocess, 'run', openspec)

    def run(text, *, readme=None, strict_exit=0, node_count=None):
        (change / 'tasks.md').write_text(text)
        if readme is not None:
            (tmp_path / 'README.md').write_text(readme)
        if strict_exit:
            monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw:
                SimpleNamespace(returncode=strict_exit, stdout='', stderr='strict failed'))
        if node_count is not None:
            monkeypatch.setattr(module, 'standard_registry', lambda:
                SimpleNamespace(nodes=[None] * node_count))
        output = tmp_path / 'checks.json'
        monkeypatch.setattr(module.sys, 'argv', ['verify_planning.py',
            '--openspec-bin', '/explicit/openspec', '--output', str(output)])
        code = module.main()
        result = json.loads(capsys.readouterr().out)
        assert json.loads(output.read_text()) == result
        return code, result, calls

    return run


@pytest.mark.parametrize('count', [1, 96, 209], ids=['one', 'historical-96', 'current-209'])
def test_accepts_any_nonempty_unique_well_formed_task_set(planning_check, count):
    text = ''.join(f'- [ ] 42.{i} Explicit task\n' for i in range(1, count + 1))
    code, result, calls = planning_check(text)
    assert code == 0
    assert result['valid'] is True
    assert result['task_count'] == result['unique_task_ids'] == count
    assert result['registered_nodes'] == 45
    assert calls[0][0] == ['/explicit/openspec', 'validate', 'sandbox-evaluation-workflow', '--strict']


@pytest.mark.parametrize('text', ['', '# No tasks\n', '- [ ] 42.1 First\n- [x] 42.1 Duplicate\n'],
                         ids=['empty', 'heading-only', 'duplicate-id'])
def test_rejects_empty_or_duplicate_tasks(planning_check, text):
    code, result, _ = planning_check(text)
    assert code == 1
    assert result['valid'] is False


@pytest.mark.parametrize('invalid', ['- [X] 43.1 Uppercase', '- [?] 43.1 Unknown',
    '- [] 43.1 Missing state', '- [ ] 43 Missing subtask', '- [ ] 43.1',
    '- [ ] 43.1 ', '- [ ]43.1 Missing separator', '  - [ ] 43.1 Indented'],
    ids=['uppercase', 'unknown-state', 'missing-state', 'missing-subtask',
         'missing-description', 'blank-description', 'missing-separator', 'indented'])
def test_rejects_malformed_checkbox_in_otherwise_valid_tasks(planning_check, invalid):
    valid = ''.join(f'- [ ] 42.{i} Valid\n' for i in range(1, 97))
    code, result, _ = planning_check(valid + invalid + '\n')
    assert code == 1
    assert result['valid'] is False
    assert result['invalid_task_lines'] == [{'line': 97, 'text': invalid}]


@pytest.mark.parametrize('constraint', ['broken-link', 'strict-failure', 'node-count'])
def test_retains_link_openspec_and_registry_constraints(planning_check, constraint):
    options = {'broken-link': {'readme': '[missing](docs/missing.md)'},
        'strict-failure': {'strict_exit': 1}, 'node-count': {'node_count': 44}}
    code, result, _ = planning_check('- [x] 42.1 Valid\n', **options[constraint])
    assert code == 1
    assert result['valid'] is False
    assert result['completed_task_ids'] == ['42.1']
    if constraint == 'broken-link':
        assert result['broken_links'] == [{'document': 'README.md', 'target': 'docs/missing.md'}]
    elif constraint == 'strict-failure':
        assert result['openspec']['exit_code'] == 1
    else:
        assert result['registered_nodes'] == 44


def test_discovers_other_changes_and_namespaces_repeated_ids(planning_check,tmp_path):
    other=tmp_path/'openspec/changes/another-change';other.mkdir(parents=True)
    (other/'tasks.md').write_text('- [ ] 42.1 Separate change task\n')
    code,result,calls=planning_check('- [x] 42.1 Parent task\n')
    assert code==0 and result['task_count']==2 and result['unique_task_ids']==2
    assert {call[0][2] for call in calls}=={'sandbox-evaluation-workflow','another-change'}
    (other/'design.md').write_text('[missing](absent.md)')
    code,result,_=planning_check('- [x] 42.1 Parent task\n')
    assert code==1 and result['broken_links'][0]['document'].endswith('another-change/design.md')
