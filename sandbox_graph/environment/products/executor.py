"""Registered recipes under a host lock, durable intent and per-step authorization.

The surrounding remote grant handler supplies authorize. This executor never
accepts a command/recipe from the wire, invents success, or replays started work.
"""
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import signal
import subprocess
import tarfile
import tempfile
import time

from ...domain.models import canonical_hash
from ..file_transaction import replace_file
from ..probes.inventory import parse_proc_stat


class RecipeExecutor:
    def __init__(self,recipes,*,journal,lock_dir,backup_dir,authorize):
        self.recipes=dict(recipes);self.journal=journal;self.authorize=authorize
        for steps in self.recipes.values():
            if len({step.step_id for step in steps})!=len(steps):raise ValueError('recipe step identities must be unique')
        self.lock_dir=Path(lock_dir);self.backup_dir=Path(backup_dir)
        self.lock_dir.mkdir(parents=True,mode=0o700,exist_ok=True)
        if self.lock_dir.resolve()!=self.lock_dir.absolute():raise ValueError('unsafe recipe lock directory')

    def recipe_hash(self,name):return canonical_hash([asdict(step) for step in self.recipes[name]])

    def execute(self,operation_id,recipe,*,expected_before):
        if recipe not in self.recipes:raise ValueError('unregistered product recipe')
        payload={'recipe':recipe,'recipe_hash':self.recipe_hash(recipe),'expected_before':expected_before}
        lock=os.open(self.lock_dir/'host.lock',os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return {'status':'blocked','reason':'host_execution_still_holds_lock'}
            # grant verification is fresh after acquiring the actual OS lock
            if not self.authorize(operation_id,payload):raise PermissionError('recipe execution grant required')
            row=self.journal.accept(operation_id=operation_id,action='product.recipe.v1',input_data=payload)
            if row['state'] in self.journal.TERMINAL:return row['result']
            if row['state']=='started':return {'status':'unknown','reason':'original_execution_requires_reconciliation'}
            identity=parse_proc_stat(Path('/proc/self/stat').read_text())
            self.journal.transition(operation_id,state='started',process_identity={'pid':os.getpid(),'start_ticks':identity['start_ticks']})
            completed=[]
            try:
                for step in self.recipes[recipe]:
                    if not self.authorize(operation_id,payload):raise PermissionError('recipe authority expired')
                    step_operation='recipe-step:'+canonical_hash({'operation_id':operation_id,'step_id':step.step_id})
                    step_payload={'recipe_hash':payload['recipe_hash'],'step':asdict(step),'expected_before':expected_before}
                    self.journal.accept(operation_id=step_operation,action='product.recipe.step.v1',input_data=step_payload)
                    self.journal.transition(step_operation,state='started',process_identity={'pid':os.getpid(),'start_ticks':identity['start_ticks']})
                    try:result=self._step(step,expected_before,lock,operation_id=step_operation,
                        still_authorized=lambda:self.authorize(operation_id,payload))
                    except Exception as exc:
                        result={'status':'failed' if isinstance(exc,(ValueError,PermissionError)) else 'unknown','reason':type(exc).__name__}
                    self.journal.transition(step_operation,state=result['status'],result=result)
                    completed.append({'step_id':step.step_id,'operation_id':step_operation,**result})
                    if result['status']!='completed':
                        output={'status':result['status'],'reason':result.get('reason'),'steps':completed,'recipe_hash':payload['recipe_hash']}
                        break
                else:output={'status':'completed','steps':completed,'recipe_hash':payload['recipe_hash']}
            except Exception as exc:
                output={'status':'failed','reason':type(exc).__name__,'steps':completed,'recipe_hash':payload['recipe_hash']}
            state=output['status'] if output['status'] in {'completed','unknown','cancelled'} else 'failed'
            self.journal.transition(operation_id,state=state,result=output)
            return output
        finally:
            # An in-flight child retains this open-file-description lock after
            # a parent crash; do not explicitly unlock it here.
            os.close(lock)

    def _asset(self,step):
        if step.asset is None:return None
        path=Path(step.asset['path'])
        if path.resolve()!=path or path.is_symlink() or not path.is_file():raise ValueError('asset path unsafe')
        checksum=hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):checksum.update(block)
        if checksum.hexdigest()!=step.asset['sha256']:raise ValueError('asset digest differs')
        return path

    def _step(self,step,expected_before,lock,*,operation_id=None,still_authorized=None):
        asset=self._asset(step)
        if step.kind=='ublk-limit':
            from dataclasses import replace
            import sys
            target=int(step.target)
            if str(target)!=step.target or not 1<=target<2**32:raise ValueError('finite ublk target required')
            child=replace(step,kind='command',argv=(sys.executable,'-B','-m',
                'sandbox_graph.environment.products.host_tuning',json.dumps(target)),environment={})
            result=self._command(child,lock,operation_id=operation_id)
            if result.get('returncode')==75:result['status']='unknown'
            return result
        if step.kind=='kuasar-groups':
            from .group_generation import publish
            return publish(step,asset,expected_before)
        if step.kind=='kuasar-group-config':
            from .group_generation import activate
            return activate(step,asset,expected_before,backup_dir=self.backup_dir,authorize=still_authorized)
        if step.kind=='cube-compose':
            from ..config import load_config
            from .common import pinned_image
            document=load_config(step.target);services=document.get('services',{})
            if set(services)!={'cube-proxy'}:raise ValueError('control proxy Compose must contain exactly cube-proxy')
            value=services['cube-proxy'];pinned_image(value.get('image'))
            if step.environment.get('expected_image') and value.get('image')!=step.environment['expected_image']:
                raise ValueError('proxy image differs from loaded artifact')
            if value.get('pull_policy')!='never':raise ValueError('offline proxy requires pull_policy never')
            if value.get('build') or value.get('privileged') or value.get('network_mode')!='host':
                raise ValueError('unexpected proxy Compose build, privilege or network mode')
            return {'status':'completed','image':value['image'],'config_sha256':hashlib.sha256(Path(step.target).read_bytes()).hexdigest()}
        if step.kind=='kuasar-tenant':
            from .server_auth import tenant_over_unix_socket
            if asset is None or asset.stat().st_size>4096 or asset.stat().st_mode&0o077:
                raise PermissionError('private bounded tenant key pair asset required')
            if still_authorized is None:raise PermissionError('active tenant grant required')
            raw=asset.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=step.asset['sha256']:raise ValueError('tenant asset changed')
            return tenant_over_unix_socket(json.loads(raw),path=step.target,
                authorize=still_authorized,timeout_s=step.timeout_s)
        if step.kind in {'owned-container','docker-image'}:
            import sys
            from dataclasses import replace
            if step.kind=='owned-container':
                key='container:'+step.target
                if key not in expected_before:raise ValueError('container requires frozen previous identity')
                data={'kind':step.kind,'spec':step.environment,'expected_id':expected_before[key]}
            else:
                data={'kind':step.kind,'spec':{'archive':str(asset),'sha256':step.asset['sha256'],'image':step.target}}
            child=replace(step,kind='command',argv=(sys.executable,'-B','-m',
                'sandbox_graph.environment.products.container_action',json.dumps(data)),environment={})
            result=self._command(child,lock,operation_id=operation_id)
            if result.get('returncode')==75:result['status']='unknown'
            return result
        if step.kind=='verify':return {'status':'completed','asset_sha256':step.asset['sha256']}
        if step.kind=='dependency-lock':
            from .common import RecipeStep,require_asset
            data=json.loads(asset.read_bytes())
            if (not isinstance(data,dict) or set(data)!={'schema_version','assets'}
                    or data['schema_version']!=1 or not isinstance(data['assets'],dict) or not data['assets']):
                raise ValueError('explicit nonempty dependency asset lock required')
            for name in sorted(data['assets']):
                self._asset(RecipeStep('verify:'+name,'verify',(),asset=require_asset(data['assets'],name)))
            return {'status':'completed','asset_sha256':step.asset['sha256'],'verified_dependencies':sorted(data['assets'])}
        if step.kind=='file':
            if step.target not in expected_before:raise ValueError('file requires frozen before hash')
            target=Path(step.target);target.parent.mkdir(parents=True,exist_ok=True)
            data=asset.read_bytes()
            if hashlib.sha256(data).hexdigest()!=step.asset['sha256']:raise ValueError('asset changed during read')
            receipt=replace_file(target,data,expected_sha256=expected_before[step.target],backup_dir=self.backup_dir,mode=step.mode)
            return {'status':'completed','file':asdict(receipt)}
        if step.kind=='archive':return self._archive(step,asset,expected_before)
        if step.kind=='command':return self._command(step,lock,operation_id=operation_id)
        raise ValueError('unregistered installation primitive')

    def _archive(self,step,asset,expected_before):
        target=Path(step.target)
        if step.target not in expected_before or expected_before[step.target] is not None or target.exists():
            raise ValueError('archive extraction requires absent frozen destination')
        with tarfile.open(asset,'r:*') as archive:
            members=archive.getmembers();seen=set();total=0
            if len(members)>100000:raise ValueError('archive member budget exceeded')
            for member in members:
                path=PurePosixPath(member.name)
                if (path.is_absolute() or '..' in path.parts or '\\' in member.name or member.name in seen
                    or not (member.isfile() or member.isdir()) or member.mode & 0o6000):
                    raise ValueError('unsafe archive member')
                seen.add(member.name);total+=member.size
                if total>16*1024**3:raise ValueError('archive size budget exceeded')
            if target.parent.resolve()!=target.parent:raise ValueError('archive parent path unsafe')
            target.parent.mkdir(parents=True,exist_ok=True);target.mkdir(mode=0o755)
            for member in members:
                destination=target/member.name
                if member.isdir():destination.mkdir(parents=True,exist_ok=True);continue
                destination.parent.mkdir(parents=True,exist_ok=True)
                with archive.extractfile(member) as source,destination.open('xb') as sink:
                    while block:=source.read(1024*1024):sink.write(block)
                    sink.flush();os.fsync(sink.fileno())
                destination.chmod(member.mode & 0o777)
        return {'status':'completed','asset_sha256':step.asset['sha256'],'target':step.target,'members':len(members)}

    def _command(self,step,lock,*,operation_id=None):
        if 'ONE_CLICK_ENV_FILE' in step.environment:
            from .literal_env import parse_literal_env
            path=Path(step.environment['ONE_CLICK_ENV_FILE'])
            if not path.is_file() or path.resolve()!=path:raise ValueError('installer environment path is unsafe')
            parse_literal_env(path.read_bytes())
        if step.step_id=='account:aenv':
            import pwd
            try:account=pwd.getpwnam('aenv')
            except KeyError:account=None
            if account:
                if account.pw_dir!='/var/lib/aenv' or account.pw_shell!='/usr/sbin/nologin' or account.pw_uid==0:
                    raise ValueError('existing aenv account does not match native profile')
                return {'status':'completed','account':'aenv','reused':True,'uid':account.pw_uid,'gid':account.pw_gid}
        argv=list(step.argv)
        if step.network=='none':argv=['unshare','--net','--',*argv]
        env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C',**step.environment}
        logs=self.journal.path.parent/'private-logs';logs.mkdir(mode=0o700,exist_ok=True)
        if logs.resolve()!=logs or logs.stat().st_uid!=os.geteuid():raise ValueError('private log directory is unsafe')
        logs.chmod(0o700)
        stem=canonical_hash({'operation_id':operation_id,'step':step.step_id})
        paths=[logs/(stem+'.'+name) for name in ('stdout','stderr')]
        from contextlib import ExitStack
        with ExitStack() as opened:
            streams=[]
            for path in paths:
                fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
                streams.append(opened.enter_context(os.fdopen(fd,'w+b')))
            stdout,stderr=streams
            process=subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,
                env=env,start_new_session=True,pass_fds=(lock,))
            ticks=parse_proc_stat(Path('/proc',str(process.pid),'stat').read_text())['start_ticks']
            if operation_id:self.journal.bind_execution(operation_id,{'pid':process.pid,'start_ticks':ticks,
                'boot_id':self.journal.boot_id,'machine_id':self.journal.machine_id,'pgid':process.pid,
                'logs':[str(path) for path in paths]})
            deadline=time.monotonic()+step.timeout_s
            exceeded=False
            while process.poll() is None:
                if time.monotonic()>=deadline or os.fstat(stdout.fileno()).st_size+os.fstat(stderr.fileno()).st_size>8*1024*1024:
                    exceeded=True;os.killpg(process.pid,signal.SIGTERM)
                    try:process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid,signal.SIGKILL)
                        try:process.wait(timeout=2)
                        except subprocess.TimeoutExpired:pass
                    break
                time.sleep(.02)
            evidence=[]
            for path,stream in zip(paths,(stdout,stderr)):
                stream.flush();os.fsync(stream.fileno())
                stream.seek(0);checksum=hashlib.sha256();size=0
                while size<8*1024*1024:
                    block=stream.read(min(65536,8*1024*1024-size))
                    if not block:break
                    checksum.update(block);size+=len(block)
                evidence.append({'sha256':checksum.hexdigest(),'bytes':size,'private_path':str(path),
                    'truncated':os.fstat(stream.fileno()).st_size>size})
            cancelled=operation_id and self.journal.get(operation_id).get('cancel_requested') and process.returncode is not None
            return {'status':'cancelled' if cancelled else 'unknown' if exceeded else 'completed' if process.returncode==0 else 'failed',
                'returncode':process.returncode,'logs':evidence,'reason':'command_budget_exceeded' if exceeded else None}
