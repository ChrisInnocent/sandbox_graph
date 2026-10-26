"""Fixed-suite local verification with complete per-phase evidence.

This runs developer code, including conftest, in a development workspace. It
cannot produce a GateManifest or authorize promotion of untrusted candidates.
"""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def validate_collection(required, collected):
    return bool(required and len(required)==len(set(required)) and
        len(collected)==len(set(collected)) and set(required)==set(collected))


def evaluate_cases(required, execution, *, returncode, junit_count, timed_out, inputs_unchanged):
    errors=[]
    if not validate_collection(required,execution.get('collected',[])): errors.append('collection_differs')
    if execution.get('collection_errors'): errors.append('collection_errors')
    if returncode!=0: errors.append('process_nonzero')
    if timed_out: errors.append('timeout')
    if not inputs_unchanged: errors.append('input_changed')
    if junit_count!=len(required) or not junit_count: errors.append('junit_missing_or_count_differs')
    actual=Counter((p['nodeid'],p['when']) for p in execution.get('phases',[]))
    expected=Counter((node,phase) for node in required for phase in ('setup','call','teardown'))
    if actual!=expected: errors.append('phase_set_differs')
    if any(p.get('outcome')!='passed' or p.get('wasxfail') for p in execution.get('phases',[])):
        errors.append('unsuccessful_phase')
    return {'status':'fail' if errors else 'pass','errors':errors,'required_count':len(required)}


def freeze_inputs(root, paths=None, *, exclude=()):
    root=Path(root).resolve()
    head=None
    if paths is None:
        try:
            listed=subprocess.run(['git','ls-files','-z','--cached','--others','--exclude-standard'],
                cwd=root,capture_output=True,check=True,timeout=30).stdout
            paths=sorted(set(os.fsdecode(p) for p in listed.split(b'\0') if p))
            head=subprocess.run(['git','rev-parse','HEAD'],cwd=root,capture_output=True,
                check=True,text=True,timeout=30).stdout.strip()
        except subprocess.CalledProcessError:
            paths=[str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()]
    files={}
    for name in sorted(paths):
        path=root/name
        if any(path.is_relative_to(item) for item in exclude): continue
        # Evidence, caches and external product checkouts are not execution inputs.
        if any(part in {'artifacts','__pycache__','.pytest_cache','.git','.venv','.worktrees','tmp'} for part in Path(name).parts):
            continue
        if path.suffix not in {'.py','.json','.toml','.yaml','.yml','.ini','.lock','.txt','.md','.sql'}:
            continue
        if path.is_symlink():
            files[name]={'symlink':os.readlink(path)}
        elif path.is_file():
            files[name]=hashlib.sha256(path.read_bytes()).hexdigest()
        elif not path.exists():
            files[name]='deleted'
    value={'head':head,'files':files,'python':sys.version,'executable':sys.executable}
    return value|{'hash':digest(value)}


def _execute(argv,root,env,log,timeout):
    with log.open('w') as stream:
        child=subprocess.Popen(argv,cwd=root,env=env,stdout=stream,stderr=subprocess.STDOUT,
            start_new_session=True)
        timed_out=False
        try:
            code=child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out=True
            os.killpg(child.pid,signal.SIGKILL)
            code=child.wait(timeout=10)
    return {'argv':argv,'returncode':code,'timed_out':timed_out}


def run_gate(root,profile,output,*,config='pyproject.toml',timeout=900):
    root=Path(root).resolve();output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    def save(name,value):
        (output/name).write_text(json.dumps(value,sort_keys=True,indent=2)+'\n')
    def finish(value):
        value={'profile':profile['profile'],'verification_level':'local_only',**value}
        save('summary.json',value)
        return value
    missing=[name for name in profile.get('requires',[]) if not os.environ.get(name)]
    if missing:
        return finish({'status':'blocked','reason':'missing_environment','missing':missing})
    if (not profile.get('paths') or not profile.get('required_ids') or any(
            not (root/p).is_file() or (root/p).is_symlink() for p in profile['paths'])):
        return finish({'status':'fail','reason':'required_suite_missing'})
    initial=freeze_inputs(root,exclude=(output,));save('inputs.json',initial);save('profile.json',profile)
    env={k:v for k,v in os.environ.items() if not k.startswith(('PYTEST_','PYTHON'))}
    env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',PYTHONPATH=str(Path(__file__).resolve().parents[2]),
        PYTHONDONTWRITEBYTECODE='1')
    common=[sys.executable,'-m','pytest','-c',str(root/config),'-p','pytest_asyncio.plugin',
        '-p','sandbox_graph.testing.pytest_receipts','-o','addopts=--import-mode=importlib',
        '-o','cache_dir='+str(output/'cache'),*profile['paths'],'-q','--tb=short']
    env['SANDBOX_GATE_REPORT']=str(output/'collect.json')
    collected=_execute(common+['--collect-only'],root,env,output/'collect.log',timeout)
    save('collect-process.json',collected)
    try:
        collection=json.loads((output/'collect.json').read_text())
    except (OSError,ValueError):
        return finish({'status':'fail','reason':'collection_report_missing'})
    if (collected['returncode']!=0 or collected['timed_out'] or collection['collection_errors']
            or not validate_collection(profile['required_ids'],collection['collected'])):
        return finish({'status':'fail','reason':'required_collection_differs',
            'missing':sorted(set(profile['required_ids'])-set(collection['collected'])),
            'extra':sorted(set(collection['collected'])-set(profile['required_ids']))})
    env['SANDBOX_GATE_REPORT']=str(output/'run.json')
    execution=_execute(common+['--junitxml='+str(output/'junit.xml')],root,env,output/'run.log',timeout)
    save('run-process.json',execution)
    try:
        report=json.loads((output/'run.json').read_text())
    except (OSError,ValueError):
        report={}
    try:
        cases=list(ET.parse(output/'junit.xml').getroot().iter('testcase'))
        count=None if any(any(c.find(tag) is not None for tag in ('failure','error','skipped')) for c in cases) else len(cases)
    except (OSError,ET.ParseError):
        count=None
    final=freeze_inputs(root,exclude=(output,));save('inputs-after.json',final)
    return finish(evaluate_cases(profile['required_ids'],report,returncode=execution['returncode'],
        junit_count=count,timed_out=execution['timed_out'],inputs_unchanged=initial==final))
