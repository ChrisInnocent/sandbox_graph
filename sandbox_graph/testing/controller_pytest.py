"""Pinned controller tests assert isolated observations, never import candidate code.

The operator suite receives a JSON observation through SANDBOX_OBSERVATIONS.
Candidate-provided JUnit/verdicts are data, not test receipts. Tests, plugins and
output directories belong to the controller; the candidate mount is read-only.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import xml.etree.ElementTree as ET
from ..domain.models import canonical_hash
from .local_gate import evaluate_cases,validate_collection


def suite_digest(root):
    root=Path(root)
    if not root.is_absolute() or root.resolve()!=root or not root.is_dir():raise ValueError('canonical trusted suite required')
    if root.stat().st_uid not in {0,os.geteuid()} or root.stat().st_mode&0o022:
        raise PermissionError('trusted suite root writable by another identity')
    files={};total=0
    for path in sorted(root.rglob('*')):
        info=path.lstat()
        if path.is_symlink() or info.st_uid not in {0,os.geteuid()} or info.st_mode&0o022:
            raise PermissionError('trusted suite writable by another identity or contains symlink')
        if path.is_dir():continue
        if not path.is_file():raise ValueError('trusted suite contains special file')
        total+=info.st_size
        if total>16*1024*1024 or len(files)>=10000:raise ValueError('trusted suite exceeds budget')
        files[path.relative_to(root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    if not files:raise ValueError('empty trusted suite')
    return canonical_hash(files)


async def _execute(argv,*,cwd,env,log,timeout):
    process=await asyncio.create_subprocess_exec(*argv,cwd=cwd,env=env,stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,start_new_session=True)
    async def drain():
        total=0
        with log.open('xb') as stream:
            while data:=await process.stdout.read(65536):
                total+=len(data)
                if total>4*1024*1024:raise ValueError('controller log exceeds byte budget')
                stream.write(data)
        return await process.wait()
    try:return {'returncode':await asyncio.wait_for(drain(),timeout),'timed_out':False}
    except (TimeoutError,ValueError,asyncio.CancelledError) as exc:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        await process.wait()
        if isinstance(exc,asyncio.CancelledError):raise
        return {'returncode':process.returncode,'timed_out':True,'reason':type(exc).__name__}


def _json(path):
    if path.stat().st_size>4*1024*1024:raise ValueError('controller receipt exceeds byte budget')
    return json.loads(path.read_bytes())


async def observe(context):
    config=context.configuration
    required={'suite_root','suite_hash','required_ids','output_root','command_ref','stdin','timeout_s'}
    if not required<=set(config) or set(config)-required-{'candidate_tests_command_ref'}:
        raise ValueError('explicit fixed controller pytest configuration required')
    def result(status,reason,**extra):return {'status':status,'observations':{'reason':reason,**extra}}
    if context.isolation is None:return result('blocked','isolation_backend_required')
    suite=Path(config['suite_root']);root=Path(config['output_root']);candidate=Path(context.candidate.workspace_ref).resolve()
    for path in (suite,root):
        if (not path.is_absolute() or path.resolve()!=path or path==candidate
                or path.is_relative_to(candidate) or candidate.is_relative_to(path)):
            return result('fail','controller_paths_overlap_candidate_or_are_noncanonical')
    if root==suite or root.is_relative_to(suite) or suite.is_relative_to(root):return result('fail','suite_and_outputs_overlap')
    ids=config['required_ids'];timeout=config['timeout_s']
    if (not isinstance(ids,list) or not ids or len(ids)>10000
            or any(not isinstance(n,str) or n.startswith('-') or '::' not in n
                or Path(n.split('::')[0]).is_absolute() or '..' in Path(n.split('::')[0]).parts for n in ids)
            or len(ids)!=len(set(ids)) or type(timeout) not in (int,float) or not 0<timeout<=300):
        return result('fail','invalid_bounded_controller_suite')
    try:
        initial=suite_digest(suite)
        if initial!=config['suite_hash']:return result('fail','trusted_suite_digest_changed')
    except (OSError,ValueError,PermissionError):return result('fail','trusted_suite_invalid')
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    if root.stat().st_uid!=os.geteuid() or root.stat().st_mode&0o077:return result('fail','private_controller_output_required')
    output=root/canonical_hash(context.request_id)
    try:output.mkdir(mode=0o700)
    except FileExistsError:return result('blocked','original_controller_execution_requires_reconciliation',output=str(output))
    (output/'intent.json').write_text(json.dumps({'request_id':context.request_id,'configuration_hash':canonical_hash(config)}))
    # No caller PYTHONPATH, plugin autoload, candidate conftest, or inherited credentials.
    env={'PATH':os.defpath,'LANG':'C','HOME':str(output),'PYTHONDONTWRITEBYTECODE':'1',
        'PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1','SANDBOX_OBSERVATIONS':str(output/'observations.json')}
    # Pin the receipts plugin through its installed controller package path.
    env['PYTHONPATH']=str(Path(__file__).resolve().parents[2])
    ini=output/'pytest.ini';ini.write_text('[pytest]\nasyncio_mode = auto\n')
    common=[sys.executable,'-m','pytest','-c',str(ini),'--rootdir',str(suite),'--confcutdir',str(suite),
        '-p','no:cacheprovider','-p','pytest_asyncio.plugin','-p','sandbox_graph.testing.pytest_receipts',
        '--import-mode=importlib','-q','--tb=short',*sorted({node.split('::')[0] for node in ids})]
    async def run(phase,args):
        value=await _execute(common+args,cwd=suite,env=env|{'SANDBOX_GATE_REPORT':str(output/(phase+'.json'))},
            log=output/(phase+'.log'),timeout=timeout)
        (output/(phase+'-process.json')).write_text(json.dumps(value|{'argv':common+args}))
        return value
    collection=await run('collect',['--collect-only'])
    try:collected=_json(output/'collect.json')
    except (OSError,ValueError):return result('fail','collection_receipt_missing',output=str(output))
    if (collection['returncode']!=0 or collection['timed_out'] or collected.get('collection_errors')
            or not validate_collection(ids,collected.get('collected',[]))):
        return result('fail','required_collection_differs',output=str(output))
    observation=await context.isolation.execute(config['command_ref'],json.dumps(config['stdin'],sort_keys=True).encode(),timeout_s=timeout)
    if observation.get('status')!='completed' or observation.get('cleanup')!='complete' or observation.get('returncode')!=0:
        return result('blocked' if observation.get('status') in {'blocked','unknown','running'} else 'fail',
            'isolated_observation_incomplete',output=str(output))
    raw=json.dumps(observation,allow_nan=False).encode()
    if len(raw)>4*1024*1024:return result('fail','observation_exceeds_budget',output=str(output))
    (output/'observations.json').write_bytes(raw)
    execution=await run('run',['--junitxml='+str(output/'junit.xml')])
    try:
        report=_json(output/'run.json')
        if (output/'junit.xml').stat().st_size>4*1024*1024:raise ValueError('JUnit exceeds budget')
        cases=list(ET.parse(output/'junit.xml').getroot().iter('testcase'))
        count=None if any(any(case.find(tag) is not None for tag in ('failure','error','skipped')) for case in cases) else len(cases)
        unchanged=suite_digest(suite)==initial
    except (OSError,ValueError,ET.ParseError):return result('fail','execution_receipts_invalid',output=str(output))
    verdict=evaluate_cases(ids,report,returncode=execution['returncode'],junit_count=count,
        timed_out=execution['timed_out'],inputs_unchanged=unchanged)
    additional=None
    if config.get('candidate_tests_command_ref'):
        additional=await context.isolation.execute(config['candidate_tests_command_ref'],b'',timeout_s=timeout)
        raw=json.dumps(additional,allow_nan=False).encode()
        if len(raw)>4*1024*1024:return result('fail','candidate_tests_output_exceeds_budget',output=str(output))
        (output/'candidate-tests.json').write_bytes(raw)
        if additional.get('status')!='completed' or additional.get('cleanup')!='complete' or additional.get('returncode')!=0:
            if verdict['status']=='pass':verdict['status']='blocked' if additional.get('status') in {'blocked','unknown','running'} else 'fail'
            verdict['errors'].append('candidate_additional_tests_incomplete')
    hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()}
    return result(verdict['status'],'controller_pytest_reconciled',output=str(output),files=hashes,verdict=verdict,
        candidate_tests_status=additional.get('status') if additional else 'not_selected')
