import os
from types import SimpleNamespace
from sandbox_graph.environment.probes.configuration import inspect_configuration


def fixture(tmp_path):
    config=tmp_path/'conductor.yaml';config.write_text('sandbox:\n  capacity: 20\napi_secret: keep-private\n')
    proc=tmp_path/'proc';task=proc/'42';task.mkdir(parents=True)
    (proc/'stat').write_text('btime 2000000000\n')
    # Linux stat field 22 is starttime. Deliberately allow spaces in comm.
    fields=['S']+['0']*18+['100']+['0']*8
    (task/'stat').write_text('42 (node ctl) '+' '.join(fields))
    (task/'cmdline').write_bytes(b'/usr/bin/node-ctl\x00conductor\x00--config\x00'+os.fsencode(config)+b'\x00')
    (task/'environ').write_bytes(b'LANG=C\x00')
    run=lambda *a,**k:SimpleNamespace(returncode=0,stdout='MainPID=42\nActiveState=active\n')
    os.utime(config,(1900000000,1900000000))
    return config,proc,run


def test_bound_configuration_preserves_safe_values_and_hides_secrets(tmp_path):
    path,proc,run=fixture(tmp_path)
    result=inspect_configuration('kuasar',roles=('conductor',),paths={'config:conductor':str(path)},proc_root=proc,runner=run)
    row=result['components']['conductor']
    assert row['status']=='satisfied' and row['evidence_level']=='startup_bound'
    assert row['values']['sandbox']['capacity']==20
    assert 'keep-private' not in str(result)


def test_modified_after_start_or_unbound_file_is_not_effective(tmp_path):
    path,proc,run=fixture(tmp_path);os.utime(path,(2100000000,2100000000))
    row=inspect_configuration('kuasar',roles=('conductor',),paths={'config:conductor':str(path)},proc_root=proc,runner=run)['components']['conductor']
    assert row['status']=='unknown'
    os.utime(path,(1900000000,1900000000));(proc/'42/cmdline').write_bytes(b'/usr/bin/node-ctl\x00')
    row=inspect_configuration('kuasar',roles=('conductor',),paths={'config:conductor':str(path)},proc_root=proc,runner=run)['components']['conductor']
    assert row['status']=='unknown'


def test_missing_configuration_is_repairable_not_ready(tmp_path):
    path,proc,run=fixture(tmp_path);path.unlink()
    row=inspect_configuration('kuasar',roles=('conductor',),paths={'config:conductor':str(path)},proc_root=proc,runner=run)['components']['conductor']
    assert row['status']=='missing'


def test_disappeared_process_is_unknown_not_missing_configuration(tmp_path):
    path,proc,run=fixture(tmp_path);(proc/'42/stat').unlink()
    row=inspect_configuration('kuasar',roles=('conductor',),paths={'config:conductor':str(path)},proc_root=proc,runner=run)['components']['conductor']
    assert row['status']=='unknown' and path.exists()


def test_control_container_config_requires_immutable_mount_and_running_process(tmp_path):
    import json
    path,proc,_=fixture(tmp_path)
    json_path=path.with_suffix('.json');json_path.write_text('{"port":8080}');os.utime(json_path,(1900000000,1900000000))
    (proc/'42/cmdline').write_bytes(b'/app/gateway\x00-config\x00/config/default.json\x00')
    body=[{'Id':'container/a','Image':'sha256:locked','State':{'Running':True,'Pid':42},
        'Mounts':[{'Source':str(json_path),'Destination':'/config/default.json','RW':False}]}]
    def run(argv,**kwargs):
        assert argv==['docker','inspect','aenv-gateway']
        return SimpleNamespace(returncode=0,stdout=json.dumps(body))
    row=inspect_configuration('agentenv',roles=('gateway',),paths={'config:gateway':str(json_path)},proc_root=proc,runner=run)['components']['gateway']
    assert row['status']=='satisfied' and row['container_id']=='container/a' and row['pid']==42
    body[0]['Mounts'][0]['RW']=True
    assert inspect_configuration('agentenv',roles=('gateway',),paths={'config:gateway':str(json_path)},proc_root=proc,runner=run)['components']['gateway']['status']=='unknown'


def test_cube_api_environment_file_proves_real_process_values_and_cli_precedence(tmp_path):
    path,proc,run=fixture(tmp_path);env=tmp_path/'api.env'
    env.write_text("RATE_LIMIT_PER_SEC='1000'\nCUBE_API_KEY='private-sentinel'\n")
    os.utime(env,(1900000000,1900000000))
    (proc/'42/cmdline').write_bytes(b'/usr/bin/CubeAPI\0--rate-limit-per-sec\0005\0')
    (proc/'42/environ').write_bytes(b'RATE_LIMIT_PER_SEC=1000\0CUBE_API_KEY=private-sentinel\0')
    row=inspect_configuration('cubesandbox',roles=('control',),paths={'config:CubeAPI':str(env)},proc_root=proc,runner=run)['components']['CubeAPI']
    assert row['status']=='satisfied' and row['configuration_bound']
    assert 'private-sentinel' not in str(row)
    assert row['resolved_values']['RATE_LIMIT_PER_SEC']=='5'
    assert row['sources']['RATE_LIMIT_PER_SEC']=='cli'
