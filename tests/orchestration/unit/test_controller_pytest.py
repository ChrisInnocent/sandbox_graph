import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import pytest


def fixture(tmp_path,body='assert observed["stdout"] == "42"'):
    suite=tmp_path/'trusted';suite.mkdir(mode=0o700)
    (suite/'test_contract.py').write_text('import json, os\nfrom pathlib import Path\ndef test_output():\n    observed=json.loads(Path(os.environ["SANDBOX_OBSERVATIONS"]).read_text())\n    '+body+'\n')
    (suite/'test_contract.py').chmod(0o600)
    candidate=tmp_path/'candidate';candidate.mkdir()
    from sandbox_graph.testing.controller_pytest import suite_digest
    config={'suite_root':str(suite),'suite_hash':suite_digest(suite),'required_ids':['test_contract.py::test_output'],
        'output_root':str(tmp_path/'evidence'),'command_ref':'contract-observation','stdin':{},'timeout_s':10}
    return SimpleNamespace(request_id='gate-case:fixed',configuration=config,
        candidate=SimpleNamespace(workspace_ref=str(candidate)),isolation=None)


class Isolation:
    def __init__(self,output='42'):self.output=output;self.calls=[]
    async def execute(self,ref,stdin,*,timeout_s):
        self.calls.append((ref,stdin))
        return {'status':'completed','returncode':0,'stdout':self.output,'stderr':'',
            'cleanup':'complete','backend_verified':False}


@pytest.mark.asyncio
async def test_controller_collect_run_junit_phase_reconciliation_and_separate_outputs(tmp_path):
    from sandbox_graph.testing.controller_pytest import observe
    context=fixture(tmp_path);context.isolation=Isolation()
    result=await observe(context)
    assert result['status']=='pass'
    output=Path(result['observations']['output'])
    assert all((output/name).is_file() for name in ('collect.json','run.json','junit.xml','collect.log','run.log'))
    assert not output.is_relative_to(context.candidate.workspace_ref)
    assert len(context.isolation.calls)==1
    assert (await observe(context))['status']=='blocked'  # no silent rerun after original intent


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['self_report','skip','missing_case','changed_suite','candidate_suite','no_backend'])
async def test_candidate_claim_or_incomplete_trusted_suite_never_passes(tmp_path,fault):
    from sandbox_graph.testing.controller_pytest import observe
    context=fixture(tmp_path,'__import__("pytest").skip("not evidence")' if fault=='skip' else 'assert observed["stdout"] == "42"')
    context.isolation=Isolation('{"status":"pass"}' if fault=='self_report' else '42')
    if fault=='missing_case':context.configuration['required_ids']+=['test_contract.py::test_missing']
    if fault=='changed_suite':(Path(context.configuration['suite_root'])/'test_contract.py').write_text('def test_output(): pass\n')
    if fault=='candidate_suite':context.configuration['suite_root']=context.candidate.workspace_ref
    if fault=='no_backend':context.isolation=None
    result=await observe(context)
    assert result['status']!='pass'
    if fault in {'missing_case','changed_suite','candidate_suite'}:assert not context.isolation.calls


@pytest.mark.asyncio
async def test_candidate_added_tests_run_separately_and_cannot_override_trusted_failure(tmp_path):
    from sandbox_graph.testing.controller_pytest import observe
    context=fixture(tmp_path);context.isolation=Isolation('wrong')
    context.configuration['candidate_tests_command_ref']='candidate-tests'
    result=await observe(context)
    assert [call[0] for call in context.isolation.calls]==['contract-observation','candidate-tests']
    assert result['status']=='fail'
    assert result['observations']['candidate_tests_status']=='completed'


@pytest.mark.asyncio
async def test_world_writable_suite_root_cannot_be_a_trusted_gate(tmp_path):
    from sandbox_graph.testing.controller_pytest import observe
    context=fixture(tmp_path);context.isolation=Isolation()
    Path(context.configuration['suite_root']).chmod(0o777)
    assert (await observe(context))['status']=='fail'
    assert not context.isolation.calls


@pytest.mark.asyncio
async def test_invalid_unhashable_nodeid_is_a_rejected_contract(tmp_path):
    from sandbox_graph.testing.controller_pytest import observe
    context=fixture(tmp_path);context.isolation=Isolation()
    context.configuration['required_ids']=[['untrusted']]
    assert (await observe(context))['status']=='fail'
    assert not context.isolation.calls
