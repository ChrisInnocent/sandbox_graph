"""Fixed offline bootstrap receiver. Keep this file standard-library-only.

Sent as trusted controller code over an initial authenticated SSH connection.
It provisions the helper, never executes a sandbox product recipe.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import zipfile

MAX_HEADER=1024*1024
MAX_TOTAL=2*1024**3
MAX_FILES=512
MAX_LOG=1024*1024
MAX_EXPANDED=2*1024**3


class CommandFailed(RuntimeError):
    def __init__(self,returncode):
        super().__init__('offline helper command failed')
        self.returncode=returncode


def encode(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def checksum(value):return hashlib.sha256(value).hexdigest()


def unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate JSON key')
        result[key]=value
    return result


def digest_file(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file():raise ValueError('unsafe artifact file')
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        while block:=stream.read(1024*1024):digest.update(block)
    return digest.hexdigest()


def private_read(path,limit=MAX_HEADER):
    path=Path(path)
    if path.resolve()!=path or not path.is_file():raise ValueError('canonical private file required')
    info=path.stat()
    if info.st_uid!=os.geteuid() or info.st_mode&0o077:raise PermissionError('private file ownership or mode differs')
    if info.st_size>limit:raise ValueError('private file size limit')
    with path.open('rb') as stream:raw=stream.read(limit+1)
    if len(raw)>limit:raise ValueError('private file size limit')
    return raw


def private_dir(path):
    path=Path(path)
    if not path.is_absolute() or path.resolve()!=path or '..' in path.parts:
        raise ValueError('canonical absolute directory required')
    if not path.parent.exists():private_dir(path.parent)
    path.mkdir(mode=0o700,exist_ok=True)
    info=path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077:
        raise PermissionError('private directory ownership or mode differs')
    return path


def sync_dir(path):
    fd=os.open(path,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def atomic(path,raw,mode=0o600):
    if path.is_symlink():raise ValueError('refusing symlink publication')
    fd,name=tempfile.mkstemp(prefix='.stage-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            os.fchmod(stream.fileno(),mode)
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path);sync_dir(path.parent)
    finally:
        if os.path.exists(name):os.unlink(name)


def validate_manifest(manifest):
    if not isinstance(manifest,dict) or set(manifest)!={'schema_version','files'} or manifest['schema_version']!=1:
        raise ValueError('invalid bootstrap manifest')
    rows=manifest['files']
    if not isinstance(rows,list) or not 3<=len(rows)<=MAX_FILES:raise ValueError('bounded file inventory required')
    names=set();kinds=[];total=0
    for row in rows:
        if set(row)!={'name','kind','sha256','size_bytes'}:raise ValueError('invalid asset record')
        name=row['name'];kind=row['kind'];size=row['size_bytes']
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,190}',name) or name in names:
            raise ValueError('unique safe asset names required')
        if kind not in {'wheel','asset','grant_key','recipes'}:raise ValueError('unknown bootstrap asset kind')
        if kind=='wheel' and not name.endswith('.whl'):raise ValueError('wheel artifact required')
        if not isinstance(row['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',row['sha256']):raise ValueError('invalid artifact digest')
        if type(size) is not int or not 0<size<=MAX_TOTAL:raise ValueError('invalid asset size')
        if kind=='wheel' and size>128*1024*1024:raise ValueError('wheel byte limit')
        if kind in {'grant_key','recipes'} and size>4*1024*1024:raise ValueError('configuration too large')
        total+=size;names.add(name);kinds.append(kind)
    if total>MAX_TOTAL or kinds.count('grant_key')!=1 or kinds.count('recipes')!=1:
        raise ValueError('bounded assets and one key/catalog required')
    if not any(r['kind']=='wheel' and r['name'].startswith('sandbox_graph-') for r in rows):
        raise ValueError('sandbox-graph wheel required')
    return rows


def validate_wheels(paths):
    count=0;size=0
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                count+=1;size+=member.file_size
                parts=member.filename.rstrip('/').split('/')
                if (not parts or any(p in {'','.','..'} for p in parts) or '\\' in member.filename
                        or stat.S_ISLNK(member.external_attr>>16) or member.flag_bits&1):
                    raise ValueError('unsafe wheel member')
                if count>50000 or size>MAX_EXPANDED:raise ValueError('expanded wheel budget exceeded')


def command(argv,log,locks,timeout=300):
    """Bounded private output; descendants retain host locks on controller loss."""
    env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PIP_CONFIG_FILE':os.devnull,'PIP_NO_INDEX':'1'}
    process=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,env=env,start_new_session=True,pass_fds=tuple(locks))
    output=bytearray();deadline=time.monotonic()+timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout,selectors.EVENT_READ)
            with log.open('xb') as stream:
                os.chmod(log,0o600)
                while selector.get_map():
                    if time.monotonic()>=deadline:raise TimeoutError('bootstrap command deadline')
                    for key,_ in selector.select(min(.1,max(0,deadline-time.monotonic()))):
                        block=os.read(key.fileobj.fileno(),65536)
                        if not block:selector.unregister(key.fileobj);continue
                        if len(output)+len(block)>MAX_LOG:raise ValueError('bootstrap log byte limit')
                        output.extend(block);stream.write(block)
                stream.flush();os.fsync(stream.fileno())
        code=process.wait(timeout=max(.01,deadline-time.monotonic()))
        if code:raise CommandFailed(code)
        return bytes(output)
    finally:
        # Kill surviving descendants too; an installer must not outlive its step.
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.wait();process.stdout.close()


def tree_hash(root):
    digest=hashlib.sha256();count=0;total=0
    for directory,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=sorted(dirs)
        for name in sorted(dirs+files):
            path=Path(directory)/name;info=path.lstat();count+=1
            if count>50000:raise ValueError('runtime file count limit')
            relative=path.relative_to(root).as_posix()
            if path.is_symlink():
                if not path.resolve().is_relative_to(root):raise ValueError('runtime symlink escapes release')
                value=['link',os.readlink(path)]
            elif path.is_file():
                total+=info.st_size
                if total>MAX_TOTAL:raise ValueError('runtime byte limit')
                value=['file',digest_file(path),stat.S_IMODE(info.st_mode)]
            elif path.is_dir():value=['dir',stat.S_IMODE(info.st_mode)]
            else:raise ValueError('unsupported runtime member')
            digest.update(encode([relative,value]))
    return digest.hexdigest()


def _receive(stream,header,*,machine_id_file,home):
    required={'schema_version','operation_id','machine_id','root','expected_launcher_sha256','bundle_sha256','manifest'}
    if set(header)!=required or header['schema_version']!=1:raise ValueError('invalid bootstrap request')
    if not isinstance(header['operation_id'],str) or not 1<=len(header['operation_id'])<=256:raise ValueError('invalid operation identity')
    if not header['machine_id'] or Path(machine_id_file).read_text().strip()!=header['machine_id']:raise ValueError('machine identity differs')
    if not (3,11)<=sys.version_info[:2]<(3,15):raise ValueError('Python 3.11 through 3.14 required')
    rows=validate_manifest(header['manifest'])
    if checksum(encode(header['manifest']))!=header['bundle_sha256']:raise ValueError('bundle digest differs')
    expected=header['expected_launcher_sha256']
    if expected is not None and (not isinstance(expected,str) or not re.fullmatch('[a-f0-9]{64}',expected)):
        raise ValueError('invalid launcher CAS digest')
    if (not isinstance(header['root'],str) or not re.fullmatch(r'/[A-Za-z0-9_./-]+',header['root'])
            or str(Path(header['root']))!=header['root']):
        raise ValueError('safe canonical bootstrap root required')
    root=private_dir(Path(header['root']))
    host_locks=private_dir(Path(home)/'.local/state/sandbox-graph/locks')
    locks=[]
    try:
        for path in (host_locks/'host.lock',root/'.bootstrap.lock'):
            fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600);locks.append(fd)
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        requests=private_dir(root/'requests');objects=private_dir(root/'objects');private_dir(root/'bin')
        record_path=requests/(checksum(header['operation_id'].encode())+'.json')
        identity=checksum(encode(header));entry=root/'bin/sandbox-graph-remote-agent'
        state=json.loads(private_read(record_path),object_pairs_hook=unique) if record_path.exists() or record_path.is_symlink() else {'identity':identity,'attempts':0}
        if state['identity']!=identity:raise ValueError('operation input conflict')
        actual=digest_file(entry) if entry.exists() or entry.is_symlink() else None
        if state.get('result') and actual!=state.get('launcher_sha256'):raise ValueError('completed launcher was changed or removed')
        if actual!=expected and actual!=state.get('launcher_sha256'):raise ValueError('launcher CAS differs')
        # Existing trust cannot be replaced merely by providing a new bundle.
        trust=root/'grant-key.sha256'
        key=next(row for row in rows if row['kind']=='grant_key')
        if (trust.exists() or trust.is_symlink()) and private_read(trust).decode()!=key['sha256']:
            raise ValueError('grant key rotation requires separate authorization')
        atomic(record_path,encode(state))
        for row in rows:
            fd,name=tempfile.mkstemp(prefix='.incoming-',dir=objects)
            try:
                digest=hashlib.sha256();remaining=row['size_bytes']
                with os.fdopen(fd,'wb') as output:
                    while remaining:
                        block=stream.read(min(1024*1024,remaining))
                        if not block:raise EOFError('truncated bootstrap asset')
                        remaining-=len(block);digest.update(block);output.write(block)
                    output.flush();os.fsync(output.fileno())
                if digest.hexdigest()!=row['sha256']:raise ValueError('asset digest differs')
                target=objects/row['sha256']
                if target.exists() or target.is_symlink():
                    if target.stat().st_mode&0o077 or digest_file(target)!=row['sha256']:raise ValueError('stored asset changed')
                else:os.replace(name,target);sync_dir(objects)
            finally:
                if os.path.exists(name):os.unlink(name)
        if stream.read(1):raise ValueError('unexpected trailing bootstrap payload')
        validate_wheels([objects/r['sha256'] for r in rows if r['kind']=='wheel'])
        if state.get('ready'):
            release=Path(state['release'])
            allowed=root/'releases'/checksum(header['operation_id'].encode())/str(state['attempts'])
            if release!=allowed or release.resolve()!=release:raise ValueError('stored release identity differs')
            if tree_hash(release)!=state['tree_hash']:raise ValueError('installed runtime changed')
        else:
            if state['attempts']>=3:raise ValueError('bootstrap attempt budget exhausted')
            state['attempts']+=1
            parent=private_dir(private_dir(root/'releases')/checksum(header['operation_id'].encode()))
            release=private_dir(parent/str(state['attempts']))
            state['release']=str(release);atomic(record_path,encode(state))
            wheels=private_dir(release/'wheels');config=private_dir(release/'config')
            for row in rows:
                if row['kind']=='wheel':os.link(objects/row['sha256'],wheels/row['name'])
                elif row['kind'] in {'grant_key','recipes'}:
                    name='execution-grant.key' if row['kind']=='grant_key' else 'recipes.json'
                    atomic(config/name,(objects/row['sha256']).read_bytes())
            if not 32<=(config/'execution-grant.key').stat().st_size<=4096:raise ValueError('invalid execution grant key')
            def run_step(name,argv):
                step={'step_id':name,'attempt':state['attempts'],'status':'started',
                      'private_log_path':str(release/(name+'.log'))}
                state.setdefault('steps',[]).append(step);atomic(record_path,encode(state))
                try:
                    output=command(argv,Path(step['private_log_path']),locks)
                    step.update(status='completed',returncode=0,log_sha256=digest_file(step['private_log_path']))
                    atomic(record_path,encode(state));return output
                except Exception as error:
                    step.update(status='unknown',reason=type(error).__name__)
                    if isinstance(error,CommandFailed):step.update(status='failed',returncode=error.returncode)
                    if Path(step['private_log_path']).is_file():step['log_sha256']=digest_file(step['private_log_path'])
                    atomic(record_path,encode(state));raise
            run_step('venv',[sys.executable,'-I','-B','-m','venv','--copies',str(release/'venv')])
            python=str(release/'venv/bin/python')
            run_step('install',[python,'-I','-B','-m','pip','--isolated','install','--no-index','--no-deps',
                     '--only-binary=:all:',*map(str,sorted(wheels.iterdir()))])
            run_step('dependencies',[python,'-I','-B','-m','pip','--isolated','check'])
            check='from sandbox_graph.environment.remote_agent import bootstrap_check; import json; print(json.dumps(bootstrap_check('+repr(str(config))+')))'
            observed=json.loads(run_step('check',[python,'-I','-B','-c',check]))
            if observed!={'protocols':['stdio-json','stdio-grant-v1']}:raise ValueError('helper self check differs')
            invocation='from sandbox_graph.environment.remote_agent import main; raise SystemExit(main(configuration_root='+repr(str(config))+'))'
            launcher=('#!/bin/sh\nexec '+shlex.quote(python)+' -I -B -c '+shlex.quote(invocation)+' "$@"\n').encode()
            atomic(release/'launcher',launcher,0o700)
            state.update(ready=True,tree_hash=tree_hash(release),launcher_sha256=checksum(launcher))
            atomic(record_path,encode(state))
        launcher=(release/'launcher').read_bytes()
        actual=digest_file(entry) if entry.exists() or entry.is_symlink() else None
        if actual not in (expected,state['launcher_sha256']):raise ValueError('launcher CAS changed before activation')
        atomic(trust,key['sha256'].encode())
        if actual!=state['launcher_sha256']:atomic(entry,launcher,0o700)
        result={'status':'completed','operation_id':header['operation_id'],'machine_id':header['machine_id'],'helper_path':str(entry),
                'bundle_sha256':header['bundle_sha256'],'launcher_sha256':state['launcher_sha256'],
                'steps':[{k:v for k,v in step.items() if k!='private_log_path'} for step in state.get('steps',[])],
                'assets':{r['name']:str(objects/r['sha256']) for r in rows if r['kind']=='asset'}}
        state['result']=result;atomic(record_path,encode(state))
        return result
    finally:
        for fd in reversed(locks):os.close(fd)


def receive(stream,*,machine_id_file='/etc/machine-id',home=None):
    raw=stream.readline(MAX_HEADER+1)
    if not raw.endswith(b'\n') or len(raw)>MAX_HEADER:raise ValueError('bootstrap header limit')
    header=json.loads(raw,object_pairs_hook=unique)
    return _receive(stream,header,machine_id_file=machine_id_file,home=home or Path.home())


def main():
    # Includes stalled stdin and lock/verification time, not only installer time.
    signal.signal(signal.SIGALRM,lambda *_:(_ for _ in ()).throw(TimeoutError('bootstrap deadline')))
    signal.alarm(900)
    try:
        result=receive(sys.stdin.buffer)
        print(json.dumps(result));return 0
    except Exception as error:
        print(json.dumps({'status':'blocked','reason':type(error).__name__}));return 2
    finally:signal.alarm(0)
