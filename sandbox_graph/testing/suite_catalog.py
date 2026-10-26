"""Compile the complete G0/G1/G2/G3/G4/G6.pre controller-oracle catalog.

Only an operator-maintained external suite is accepted. Collection happens on
the controller, without importing candidate code. Preparation is not a test
verdict, producer authorization, backend qualification or publication permit.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from ..domain.models import canonical_hash
from ..services.gate_policy import GATES
from ..runtime.gate_runner import GateCase
from .controller_pytest import observe,suite_digest
from .local_gate import validate_collection


def _path(value):
    path=Path(value)
    if not path.is_absolute() or path.resolve()!=path:raise ValueError('canonical absolute controller path required')
    return path


def _overlap(a,b):return a==b or a in b.parents or b in a.parents


def compile_catalog(contract,output):
    if (set(contract)!={'schema_version','profile_ref','policy_hash','repository','candidate_root','gates'}
            or contract['schema_version']!=1 or set(contract['gates'])!=set(GATES)
            or not isinstance(contract['profile_ref'],str) or not contract['profile_ref']
            or not re.fullmatch('[a-f0-9]{64}',contract['policy_hash'])):
        raise ValueError('complete standard gate catalog and policy binding required')
    excluded=[_path(contract[k]) for k in ('repository','candidate_root')];output=_path(output)
    if any(_overlap(output,p) for p in excluded):raise PermissionError('controller output overlaps executable trees')
    roots={};validated={}
    fields={'suite_root','suite_hash','required_ids','command_ref','stdin','timeout_s'}
    for gate,row in contract['gates'].items():
        if set(row)!=fields:raise ValueError('exact controller oracle configuration required')
        root=_path(row['suite_root'])
        if any(_overlap(root,p) for p in [*excluded,output]):raise PermissionError('trusted suite overlaps candidate/source/output')
        digest=suite_digest(root)
        if digest!=row['suite_hash']:raise ValueError('trusted suite digest changed')
        ids=row['required_ids'];timeout=row['timeout_s']
        if (not isinstance(ids,list) or not ids or len(ids)!=len(set(ids)) or len(ids)>10000
                or any(not isinstance(n,str) or not re.fullmatch(r'[A-Za-z0-9_./-]+\.py::[^\r\n]+',n)
                    or Path(n.split('::')[0]).is_absolute() or '..' in Path(n.split('::')[0]).parts for n in ids)
                or not isinstance(row['command_ref'],str) or not row['command_ref']
                or type(timeout) not in (float,int) or not math.isfinite(timeout) or not 0<timeout<=300
                or len(json.dumps(row['stdin'],allow_nan=False).encode())>1024*1024):
            raise ValueError('bounded fixed oracle and command contract required')
        roots[str(root)]=digest;validated[gate]=dict(row)
    output.mkdir(mode=0o700,parents=True,exist_ok=False)
    suites=output/'suites';suites.mkdir(mode=0o700);receipts=output/'collection';receipts.mkdir(mode=0o700)
    copied={};collected={};cases={};assignments={}
    for gate,row in validated.items():
        source=Path(row['suite_root']);digest=roots[str(source)]
        if str(source) not in copied:
            destination=suites/canonical_hash({'source':str(source),'hash':digest})
            shutil.copytree(source,destination)
            if suite_digest(source)!=digest or suite_digest(destination)!=digest:raise ValueError('trusted suite changed during installation')
            copied[str(source)]=destination
        destination=copied[str(source)]
        # Collect once per identical suite/file selection, retain exact receipts.
        files=sorted({n.split('::')[0] for n in row['required_ids']});key=canonical_hash([str(destination),files])
        report=receipts/(key+'.json')
        if key not in collected:
            env={k:os.environ[k] for k in ('PATH','HOME','TMPDIR','LANG') if k in os.environ}
            env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',PYTHONDONTWRITEBYTECODE='1',
                PYTHONPATH=str(Path(__file__).resolve().parents[2]),SANDBOX_GATE_REPORT=str(report))
            ini=receipts/'pytest.ini';ini.write_text('[pytest]\nasyncio_mode = auto\n')
            argv=[sys.executable,'-B','-m','pytest','--collect-only','-q','-c',str(ini),
                '--rootdir',str(destination),'--confcutdir',str(destination),'--import-mode=importlib',
                '-p','no:cacheprovider','-p','pytest_asyncio.plugin','-p','sandbox_graph.testing.pytest_receipts',*files]
            with (receipts/(key+'.log')).open('xb') as stream:
                run=subprocess.run(argv,cwd=destination,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=60,check=False)
            if run.returncode!=0 or not report.is_file() or report.stat().st_size>4*1024*1024:
                raise ValueError('trusted oracle collection failed')
            collected[key]=json.loads(report.read_text())
        observed=collected[key]
        if observed.get('collection_errors') or not validate_collection(row['required_ids'],observed.get('collected',[])):
            raise ValueError('required controller nodeids differ from actual collection')
        if suite_digest(destination)!=digest:raise ValueError('controller collection modified its suite')
        case_id=gate+'/controller-oracles.v1';assignments[gate]=[case_id]
        cases[case_id]={'observer':'controller-pytest.v1','version':'1','configuration':{
            **row,'suite_root':str(destination),'output_root':str(output/'executions'/gate)}}
    identities={key:GateCase(row['version'],observe,row['configuration']).identity() for key,row in cases.items()}
    suite_hash=canonical_hash(identities)
    profile={'profile_ref':contract['profile_ref'],'suite_hash':suite_hash,'policy_hash':contract['policy_hash'],'cases':assignments}
    for name,value in (('cases.json',cases),('profile.json',profile)):
        with (output/name).open('x') as stream:json.dump(value,stream,indent=2,sort_keys=True);stream.write('\n')
    result={'schema_version':1,'status':'prepared','qualification':'unverified','suite_hash':suite_hash,
        'input_hash':canonical_hash(contract),'collection_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in receipts.iterdir() if p.is_file()}}
    (output/'preparation.json').write_text(json.dumps(result,indent=2)+'\n');return result
