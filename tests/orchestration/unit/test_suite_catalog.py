import hashlib
import json
from pathlib import Path
import pytest


def contract(tmp_path):
    from sandbox_graph.services.gate_policy import GATES
    from sandbox_graph.testing.controller_pytest import suite_digest
    source=tmp_path/'controller';source.mkdir(mode=0o700)
    # Explicit controlled oracle; never used as a real product/PG qualification.
    test=source/'test_oracle.py';test.write_text('import json,os\ndef test_observation():\n    assert json.load(open(os.environ["SANDBOX_OBSERVATIONS"]))["stdout"] == "observed"\n')
    test.chmod(0o600)
    repo=tmp_path/'repository';repo.mkdir();candidate=tmp_path/'candidates';candidate.mkdir()
    value={'schema_version':1,'profile_ref':'test:1','policy_hash':'a'*64,
        'repository':str(repo),'candidate_root':str(candidate),'gates':{gate:{
            'suite_root':str(source),'suite_hash':suite_digest(source),'required_ids':['test_oracle.py::test_observation'],
            'command_ref':'observe/'+gate,'stdin':{'case':gate},'timeout_s':10} for gate in GATES}}
    return value,source


def test_compile_collects_fixed_oracles_and_binds_all_required_gates(tmp_path):
    from sandbox_graph.testing.suite_catalog import compile_catalog
    value,source=contract(tmp_path)
    result=compile_catalog(value,tmp_path/'registered')
    assert result['status']=='prepared' and result['qualification']=='unverified'
    profile=json.loads((tmp_path/'registered/profile.json').read_text())
    cases=json.loads((tmp_path/'registered/cases.json').read_text())
    assert len(profile['cases'])==len(cases)==6
    assert {r['observer'] for r in cases.values()}=={'controller-pytest.v1'}
    assert all(Path(r['configuration']['suite_root']).is_relative_to(tmp_path/'registered/suites') for r in cases.values())
    assert profile['suite_hash']==result['suite_hash']
    assert not (tmp_path/'registered/GateManifest.json').exists()


@pytest.mark.parametrize('fault',['gate','nodeid','digest','source_overlap','output_overlap'])
def test_incomplete_or_untrusted_catalog_never_publishes_profile(tmp_path,fault):
    from sandbox_graph.testing.suite_catalog import compile_catalog
    value,source=contract(tmp_path);output=tmp_path/'registered'
    if fault=='gate':del value['gates']['G3']
    elif fault=='nodeid':value['gates']['G1']['required_ids']=['test_oracle.py::missing']
    elif fault=='digest':value['gates']['G1']['suite_hash']='b'*64
    elif fault=='source_overlap':value['repository']=str(source)
    else:output=Path(value['candidate_root'])/'registered'
    with pytest.raises((ValueError,PermissionError)):compile_catalog(value,output)
    assert not (output/'profile.json').exists()
