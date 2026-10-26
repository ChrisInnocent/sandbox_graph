"""Operator entrypoint for a registered bundle in a fresh Python interpreter.

This is deployment, not candidate isolation or authorization to promote. The
ordinary worker still attests its imported modules/dependencies and only claims
jobs matching its registered bundle and frozen deployment.
"""
import hashlib
import json
import os
from pathlib import Path
import sys

# Keep the child bootstrap stdlib-only: it validates before importing candidate code.
BOOTSTRAP=r'''
import hashlib,json,os,sys
from pathlib import Path
try:
    manifest_path=Path(sys.argv[1]);expected=sys.argv[2];configuration=json.loads(sys.argv[3])
    if manifest_path.resolve()!=manifest_path or manifest_path.stat().st_size>8*1024*1024:raise ValueError()
    raw=manifest_path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError()
    manifest=json.loads(raw);root=Path(manifest['path'])/'tree'
    if manifest_path.parent!=root.parent or root.resolve()!=root:raise ValueError()
    found={};total=0
    for path in sorted(root.rglob('*')):
        if path.is_symlink():raise ValueError()
        if path.is_dir():continue
        if not path.is_file():raise ValueError()
        total+=path.stat().st_size
        if total>256*1024*1024:raise ValueError()
        value=path.read_bytes()
        found[path.relative_to(root).as_posix()]={'sha256':hashlib.sha256(value).hexdigest(),
            'size':len(value),'executable':bool(path.stat().st_mode&0o111)}
    if found!=manifest['files']:raise ValueError()
    for path,digest in configuration['inputs'].items():
        target=Path(path)
        if target.resolve()!=target or target.stat().st_size>4*1024*1024 or hashlib.sha256(target.read_bytes()).hexdigest()!=digest:raise ValueError()
except Exception:
    print('bundle_launch_input_changed',file=sys.stderr);raise SystemExit(2)
sys.path.insert(0,str(root))
from sandbox_graph.runtime.evaluation_cli import main
raise SystemExit(main(configuration['argv']))
'''


def command(manifest,*,bundle_ref,runtime,lifecycle_runtime=None,once=False):
    root=Path(manifest['path']);path=root/'manifest.json'
    if root.resolve()!=root or not path.is_file() or path.resolve()!=path:raise ValueError('canonical bundle manifest required')
    raw=path.read_bytes()
    if len(raw)>8*1024*1024 or json.loads(raw)!=manifest:raise ValueError('registered bundle manifest differs')
    inputs={};args=['worker']
    for flag,value in (('--runtime',runtime),('--lifecycle-runtime',lifecycle_runtime)):
        if value is None:continue
        value=Path(value).absolute()
        if value.resolve()!=value or not value.is_file() or value.stat().st_size>4*1024*1024:raise ValueError('bounded canonical runtime required')
        inputs[str(value)]=hashlib.sha256(value.read_bytes()).hexdigest();args+=[flag,str(value)]
    args+=['--bundle-ref',bundle_ref]
    if once:args+=['--once']
    env={key:os.environ[key] for key in ('PATH','LANG','LC_ALL','HOME','TMPDIR','SANDBOX_GRAPH_POSTGRES_DSN',
        'PGSSLROOTCERT','PGSSLCERT','PGSSLKEY','PGSSLMODE') if key in os.environ}
    return [sys.executable,'-I','-B','-c',BOOTSTRAP,str(path),hashlib.sha256(raw).hexdigest(),
        json.dumps({'inputs':inputs,'argv':args})],env


def launch(repository,*,bundle_ref,runtime,lifecycle_runtime=None,once=False,execve=os.execve):
    from ..services.worker_bundles import WorkerBundleService
    with repository.transaction() as tx:row=tx.get('worker_bundles',bundle_ref)
    if not row:raise ValueError('registered worker bundle required')
    service=WorkerBundleService(repository,root=Path(row['body']['path']).parent)
    body=service.verify(bundle_ref)
    argv,env=command(body,bundle_ref=bundle_ref,runtime=runtime,lifecycle_runtime=lifecycle_runtime,once=once)
    # No shell, background child or discarded process handle. Existing supervisor
    # retains this PID and exit status after replacement.
    execve(argv[0],argv,env)
