import hashlib
import json
from pathlib import Path
from dataclasses import replace

import pytest

from sandbox_graph.environment.products.server_auth import kuasar_pair
from sandbox_graph.environment.products.executor import RecipeExecutor
from sandbox_graph.environment.journal import OperationJournal


def prepare(tmp_path, **changes):
    from sandbox_graph.environment.products.kuasar import cluster_group_recipe
    config = {'placer': {'id': 'p1'}, 'import_groups': [
        {'source_id': 'owned', 'source_type': 'file', 'path': str(tmp_path/'old')},
        {'source_id': 'external', 'source_type': 'file', 'path': str(tmp_path/'other')}]}
    arguments = dict(config=config, groups_by_source={'owned': [{'group': 'existing', 'other': 42}],
        'external': [{'group': 'foreign'}]}, record={'group': 'bench', 'sandbox_config': {'cpu': '2'}},
        pair=kuasar_pair('01' * 32), source_id='owned', directory=str(tmp_path/'deployed'),
        config_target=str(tmp_path/'placer.yaml'), expected_before=None, output=str(tmp_path/'assets'))
    arguments.update(changes)
    for source in config['import_groups']:
        root=Path(source['path'])
        if not root.exists():
            root.mkdir(mode=0o700)
            for index,row in enumerate(arguments['groups_by_source'].get(source['source_id'],[])):
                path=root/(str(index)+'.json');path.write_text(json.dumps(row));path.chmod(0o600)
    return cluster_group_recipe(**arguments)


def executor(tmp_path, steps):
    return RecipeExecutor({'groups': steps}, journal=OperationJournal(tmp_path/'journal.json', machine_id='m', boot_id='b'),
        lock_dir=tmp_path/'locks', backup_dir=tmp_path/'backups', authorize=lambda *args: True)


def test_group_generation_is_private_atomic_preserves_other_sources_and_replays(tmp_path):
    plan = prepare(tmp_path)
    assert '01'*32 not in json.dumps(plan, default=lambda s: s.__dict__)
    steps = plan['steps']
    # Exercise actual filesystem primitives; service execution is a separate live gate.
    service = executor(tmp_path, steps[:2])
    result = service.execute('original', 'groups', expected_before=plan['expected_before'])
    assert result['status'] == 'completed'
    target = Path(steps[0].target)
    assert target.stat().st_mode & 0o777 == 0o700
    records = [json.loads(p.read_bytes()) for p in target.glob('*.json')]
    assert sorted(r['group'] for r in records) == ['bench', 'existing']
    assert next(r for r in records if r['group']=='existing')['other'] == 42
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in target.iterdir())
    import yaml
    config = yaml.safe_load((tmp_path/'placer.yaml').read_text())
    assert config['import_groups'][0]['path'] == str(target)
    assert config['import_groups'][1]['path'] == str(tmp_path/'other')
    assert [s.argv for s in steps[2:]] == [('/usr/bin/cluster-ctl', 'config', 'placer', '--config', str(tmp_path/'placer.yaml')),
        ('systemctl', 'restart', 'cluster-placer.service')]
    assert service.execute('original', 'groups', expected_before=plan['expected_before']) == result
    assert prepare(tmp_path) == plan


@pytest.mark.parametrize('fault', ['duplicate', 'missing_source', 'changed_key'])
def test_group_conflicts_are_rejected_before_rendering(tmp_path, fault):
    groups = {'owned': [{'group': 'existing'}], 'external': [{'group': 'foreign'}]}
    if fault=='duplicate': groups['external'].append({'group': 'existing'})
    if fault=='missing_source': del groups['external']
    if fault=='changed_key': groups['owned'].append({'group': 'bench', 'api_secret': {'type': 'inline', 'value': '02'*32}})
    with pytest.raises(ValueError): prepare(tmp_path, groups_by_source=groups)
    assert not (tmp_path/'assets').exists()


def test_config_drift_does_not_activate_new_generation(tmp_path):
    plan = prepare(tmp_path)
    (tmp_path/'placer.yaml').write_text('operator: changed\n')
    result = executor(tmp_path, plan['steps'][:2]).execute('original', 'groups', expected_before=plan['expected_before'])
    assert result['status'] != 'completed'
    assert (tmp_path/'placer.yaml').read_text() == 'operator: changed\n'
    assert len(result['steps']) == 2  # immutable, inactive generation remains recoverable


def test_generation_content_conflict_cannot_be_reused(tmp_path):
    plan = prepare(tmp_path)
    service = executor(tmp_path, plan['steps'][:1])
    assert service.execute('first', 'groups', expected_before=plan['expected_before'])['status']=='completed'
    path = next(Path(plan['steps'][0].target).glob('*.json'))
    path.write_text('{}')
    assert service.execute('second', 'groups', expected_before=plan['expected_before'])['status']!='completed'


def test_existing_source_drift_blocks_activation_without_losing_operator_groups(tmp_path):
    plan=prepare(tmp_path)
    (tmp_path/'old'/'new.json').write_text('{"group":"operator-added"}')
    result=executor(tmp_path,plan['steps'][:2]).execute('original','groups',expected_before=plan['expected_before'])
    assert result['status']!='completed'
    assert not (tmp_path/'placer.yaml').exists()
    assert (tmp_path/'old'/'new.json').read_text()=='{"group":"operator-added"}'


def test_generation_drift_between_publication_and_activation_is_rejected(tmp_path):
    plan=prepare(tmp_path);service=executor(tmp_path,plan['steps'])
    assert service._step(plan['steps'][0],plan['expected_before'],0)['status']=='completed'
    next(Path(plan['steps'][0].target).glob('*.json')).write_text('{}')
    with pytest.raises(ValueError):
        service._step(plan['steps'][1],plan['expected_before'],0,still_authorized=lambda:True)
    assert not (tmp_path/'placer.yaml').exists()


def test_group_preparation_script_emits_reusable_private_client_binding(tmp_path):
    from scripts.environment.prepare_kuasar_groups import render
    from sandbox_graph.environment.secrets import SecretStore
    from sandbox_bench.adapters import Adapter
    store=SecretStore(tmp_path/'secrets');ref=store.put('group','manifest','01'*32).ref
    config=tmp_path/'observed.yaml';config.write_text('placer:\n  id: p1\n')
    data={'secret_root':str(store.root),'scope':'group','manifest_key_ref':ref,'api_secret_name':'api',
        'config_snapshot':str(config),'groups_by_source':{},'record':{'group':'bench'},'source_id':'bench',
        'directory':str(tmp_path/'generations'),'config_target':str(tmp_path/'deployed.yaml'),
        'output':str(tmp_path/'assets'),'api_host':'api.example','domain':'example'}
    plan=render(data)
    assert plan==render(data) and '01'*32 not in json.dumps(plan)
    adapter=Adapter({'product':'kuasar','api_url':'http://localhost:8080','data_url':'http://localhost:8081',**plan['client']})
    assert adapter.headers['X-API-Key'].startswith('e2b_')


@pytest.mark.parametrize('version',['v1','v2'])
def test_group_preparation_accepts_independent_scoped_api_secret_only_at_same_version(tmp_path,version):
    from scripts.environment.prepare_kuasar_groups import render
    from sandbox_graph.environment.secrets import SecretStore
    store=SecretStore(tmp_path/'secrets')
    manifest=store.put('group','manifest','01'*32).ref
    api=store.put('group','external-api','02'*32,version=version).ref
    config=tmp_path/'observed.yaml';config.write_text('placer:\n  id: p1\n')
    data={'secret_root':str(store.root),'scope':'group','manifest_key_ref':manifest,
        'api_secret_ref':api,'api_secret_name':'client-api','config_snapshot':str(config),
        'groups_by_source':{},'record':{'group':'bench'},'source_id':'bench',
        'directory':str(tmp_path/'generations'),'config_target':str(tmp_path/'deployed.yaml'),
        'output':str(tmp_path/'assets'),'api_host':'api.example','domain':'example'}
    if version!='v1':
        with pytest.raises(ValueError,match='version'):render(data)
        assert not (tmp_path/'assets').exists()
        return
    plan=render(data)
    assert plan==render(data)
    raw=json.dumps(plan)
    assert '01'*32 not in raw and '02'*32 not in raw
    credential=plan['client']['kuasar_cluster']['credential']
    assert store.resolve(credential['auth_ref'],scope='group')=='02'*32
    rows=json.loads(Path(plan['steps'][0]['asset']['path']).read_text())
    assert next(iter(rows.values()))['api_secret']=={'type':'inline','value':'02'*32}
