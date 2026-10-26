"""Bubblewrap backend with a locked rootfs and durable original-request identity.

Only registered argv execute. No host root, home, credentials or network are
mounted. A process result never claims the separate isolation acceptance passed.
"""
from __future__ import annotations
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat

from ..domain.models import canonical_hash
from .candidate_workspace import _write


class BwrapBackend:
    def __init__(self,*,rootfs,rootfs_hash,commands,state_dir,executable='bwrap',max_output_bytes=1024*1024):
        self.rootfs=Path(rootfs).absolute();self.rootfs_hash=rootfs_hash
        self.commands=dict(commands);self.executable=executable;self.max_output_bytes=max_output_bytes
        self.state_dir=Path(state_dir).absolute();self.state_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.state_dir.resolve()!=self.state_dir or self.state_dir.stat().st_uid!=os.geteuid():
            raise ValueError('private isolation journal required')
        self.state_dir.chmod(0o700)
        if (self.rootfs.resolve()!=self.rootfs or self.rootfs==self.state_dir
                or self.state_dir in self.rootfs.parents or self.rootfs in self.state_dir.parents):
            raise ValueError('rootfs and isolation journal must be separate real directories')
        if type(max_output_bytes) is not int or not 1<=max_output_bytes<=16*1024*1024:raise ValueError('output budget invalid')
        for argv in self.commands.values():
            if not argv or not argv[0].startswith('/') or any(not isinstance(arg,str) or '\x00' in arg for arg in argv):
                raise ValueError('registered absolute executable argv required')

    @staticmethod
    def hash_rootfs(root):
        root=Path(root)
        if root.is_symlink() or not root.is_dir():raise ValueError('rootfs must be a real directory')
        manifest={}
        for path in sorted(root.rglob('*')):
            name=path.relative_to(root).as_posix();info=path.lstat()
            if stat.S_ISLNK(info.st_mode):manifest[name]={'link':os.readlink(path)};continue
            if stat.S_ISDIR(info.st_mode):continue
            if not stat.S_ISREG(info.st_mode):raise ValueError('rootfs contains device/socket/nonregular file')
            digest=hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
            manifest[name]={'sha256':digest.hexdigest(),'mode':stat.S_IMODE(info.st_mode)}
        return canonical_hash(manifest)

    def argv(self,workspace,command_ref,*,readonly=False):
        workspace=Path(workspace).absolute()
        if (workspace.is_symlink() or not workspace.is_dir() or workspace.resolve()!=workspace
                or self.state_dir==workspace or workspace in self.state_dir.parents
                or self.state_dir in workspace.parents or self.rootfs==workspace
                or self.rootfs in workspace.parents or workspace in self.rootfs.parents):
            raise ValueError('isolation workspace must be separate from control state and rootfs')
        if self.hash_rootfs(self.rootfs)!=self.rootfs_hash:raise ValueError('isolation rootfs changed')
        if command_ref not in self.commands:raise ValueError('unregistered isolation command')
        return [self.executable,'--unshare-all','--new-session','--die-with-parent','--cap-drop','ALL',
            '--ro-bind',str(self.rootfs),'/', '--ro-bind' if readonly else '--bind',str(workspace),'/workspace',
            '--tmpfs','/tmp','--proc','/proc','--dev','/dev','--chdir','/workspace','--clearenv',
            '--setenv','PATH','/usr/local/bin:/usr/bin:/bin','--setenv','LANG','C',
            '--setenv','PYTHONDONTWRITEBYTECODE','1','--',*self.commands[command_ref]]

    def record_path(self,request_id):
        return self.state_dir/(hashlib.sha256(request_id.encode()).hexdigest()+'.json')

    def _store(self,path,value):
        """Serialize observations; a late runner cannot resurrect a closed group."""
        lock=os.open(str(path)+'.lock',os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            fcntl.flock(lock,fcntl.LOCK_EX)
            previous=json.loads(path.read_text()) if path.exists() else None
            if previous:
                if previous['identity']!=value['identity']:raise PermissionError('original isolation identity changed')
                if previous.get('cleanup')=='complete':return previous
                if previous.get('status') in {'cancel_requested','cancelled'} and value.get('status')=='completed':
                    value=value|{'status':'cancelled','reason':'original_cancel_observed'}
            _write(path,value)
            return value
        finally:os.close(lock)

    async def capabilities(self):
        """Probe namespace/mount startup with no candidate or production mounts.

        This is availability evidence only. Host-canary escape/cleanup acceptance
        must independently qualify a deployment before backend_verified is set.
        """
        executable=shutil.which(self.executable)
        if not executable:return {'status':'blocked','reason':'bubblewrap_missing','backend_verified':False}
        if self.hash_rootfs(self.rootfs)!=self.rootfs_hash:raise ValueError('isolation rootfs changed')
        argv=[executable,'--unshare-all','--new-session','--die-with-parent','--cap-drop','ALL',
            '--ro-bind',str(self.rootfs),'/', '--tmpfs','/tmp','--proc','/proc','--dev','/dev',
            '--clearenv','--','/bin/true']
        try:
            process=await asyncio.create_subprocess_exec(*argv,stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,env={'PATH':os.defpath},start_new_session=True)
        except OSError as exc:
            return {'status':'blocked','reason':'bubblewrap_'+type(exc).__name__,'backend_verified':False}
        try:stdout,stderr=await asyncio.wait_for(process.communicate(),5)
        except (TimeoutError,asyncio.CancelledError) as exc:
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            try:await asyncio.wait_for(process.wait(),3)
            except TimeoutError:pass
            if isinstance(exc,asyncio.CancelledError):raise
            return {'status':'blocked','reason':'bubblewrap_probe_timeout','backend_verified':False}
        return {'status':'available' if process.returncode==0 else 'blocked',
            'reason':None if process.returncode==0 else 'bubblewrap_setup_failed',
            'returncode':process.returncode,'rootfs_hash':self.rootfs_hash,
            'stdout_sha256':hashlib.sha256(stdout).hexdigest(),'stderr_sha256':hashlib.sha256(stderr).hexdigest(),
            'stderr':stderr[:4096].decode(errors='replace'),'backend_verified':False}

    def identity(self,request_id,workspace,command_ref,stdin,*,timeout_s,readonly=False):
        if not 0<timeout_s<=3600 or not isinstance(stdin,bytes) or len(stdin)>1024*1024:
            raise ValueError('finite isolation time/input bounds required')
        if command_ref not in self.commands:raise ValueError('unregistered isolation command')
        return {'request_id':request_id,'workspace':str(Path(workspace).absolute()),'command_ref':command_ref,
            'argv_hash':canonical_hash(self.commands[command_ref]),'rootfs_hash':self.rootfs_hash,
            'stdin_sha256':hashlib.sha256(stdin).hexdigest(),'timeout_s':timeout_s,'readonly':readonly}

    @staticmethod
    def _pid_identity(pid):
        from ..environment.probes.inventory import parse_proc_stat
        return parse_proc_stat(Path('/proc',str(pid),'stat').read_text())['start_ticks']

    @staticmethod
    def _boot_id():return Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    @staticmethod
    def _group_members(pgid):
        members=[]
        for path in Path('/proc').iterdir():
            if not path.name.isdecimal():continue
            try:
                raw=(path/'stat').read_text();fields=raw[raw.rfind(')')+2:].split()
                if int(fields[2])==pgid and fields[0]!='Z':members.append((int(path.name),int(fields[19])))
            except FileNotFoundError:continue
        return members

    async def reconcile(self,request_id):
        path=self.record_path(request_id)
        if not path.exists():return {'status':'unknown','reason':'original_isolation_journal_missing','backend_verified':False}
        row=json.loads(path.read_text())
        if row['identity']['request_id']!=request_id:raise PermissionError('isolation journal identity changed')
        if row.get('cleanup')=='complete':return row
        if row['status'] not in {'starting','running','unknown','cancel_requested','completed','failed','cancelled'}:return row
        if not row.get('boot_id') or not row.get('start_ticks') or not row.get('pid'):
            return row|{'status':'unknown','reason':'original_process_identity_missing'}
        if row['boot_id']!=self._boot_id():
            result=row|{'status':'cancelled','cleanup':'complete','reason':'original_boot_ended','backend_verified':False}
            return self._store(path,result)
        try:
            identity=self._pid_identity(row['pid'])
            if identity!=row['start_ticks']:return row|{'status':'unknown','reason':'original_pid_reused'}
        except FileNotFoundError:pass
        try:members=self._group_members(row['pid'])
        except (OSError,ValueError):return row|{'status':'unknown','reason':'original_process_group_unobservable'}
        if members:return row|{'status':'running','processes':[{'pid':pid,'start_ticks':ticks} for pid,ticks in members]}
        result=row|{'status':row['status'] if row['status'] in {'completed','failed','cancelled'} else 'cancelled',
            'cleanup':'complete','reason':'original_processes_exited','backend_verified':False}
        return self._store(path,result)

    async def cancel(self,request_id,*,timeout_s=5):
        if not 0<timeout_s<=30:raise ValueError('bounded cancellation deadline required')
        row=await self.reconcile(request_id)
        if row['status']!='running':return row
        if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):
            return row|{'status':'unknown','reason':'pidfd_cancellation_unavailable'}
        path=self.record_path(request_id);row=self._store(path,row|{'status':'cancel_requested'})
        if row.get('cleanup')=='complete':return row
        for identity in row['processes']:
            fd=None
            try:
                fd=os.pidfd_open(identity['pid'])
                if self._pid_identity(identity['pid'])!=identity['start_ticks']:
                    return row|{'status':'unknown','reason':'original_process_identity_changed'}
                signal.pidfd_send_signal(fd,signal.SIGKILL)
            except ProcessLookupError:pass
            except OSError:return row|{'status':'unknown','reason':'original_process_stop_unavailable'}
            finally:
                if fd is not None:os.close(fd)
        deadline=asyncio.get_running_loop().time()+timeout_s
        while True:
            result=await self.reconcile(request_id)
            if result.get('cleanup')=='complete' or result['status']=='unknown':return result
            if asyncio.get_running_loop().time()>=deadline:return result|{'status':'unknown','reason':'original_process_stop_timeout'}
            await asyncio.sleep(.02)

    async def execute(self,request_id,workspace,command_ref,stdin=b'',*,timeout_s,readonly=False,before_start=None):
        identity=self.identity(request_id,workspace,command_ref,stdin,timeout_s=timeout_s,readonly=readonly)
        path=self.record_path(request_id)
        if path.exists():
            row=json.loads(path.read_text())
            if row['identity']!=identity:raise ValueError('isolation request identity conflict')
            if row['status'] in {'starting','running'}:
                return row|{'status':'unknown','reason':'original_isolation_requires_reconciliation','backend_verified':False}
            return row
        if not shutil.which(self.executable):
            return self._store(path,{'identity':identity,'status':'blocked','reason':'bubblewrap_missing',
                'cleanup':'complete','submission':'proven_not_started','backend_verified':False})
        argv=self.argv(workspace,command_ref,readonly=readonly)
        argv[0]=shutil.which(self.executable)
        # O_EXCL prevents another worker starting the same request before PID storage.
        try:
            with path.open('x') as stream:
                json.dump({'identity':identity,'status':'starting'},stream);stream.flush();os.fsync(stream.fileno())
        except FileExistsError:return await self.execute(request_id,workspace,command_ref,stdin,timeout_s=timeout_s,readonly=readonly,before_start=before_start)
        try:
            if before_start is not None:before_start()
        except Exception as exc:
            return self._store(path,{'identity':identity,'status':'blocked','reason':'isolation_authority_'+type(exc).__name__,
                'cleanup':'complete','submission':'proven_not_started','backend_verified':False})
        try:
            process=await asyncio.create_subprocess_exec(*argv,stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env={'PATH':os.defpath},start_new_session=True)
        except OSError as exc:
            result={'identity':identity,'status':'blocked','reason':'isolation_spawn_'+type(exc).__name__,
                'cleanup':'complete','submission':'proven_not_started','backend_verified':False}
            return self._store(path,result)
        try:start_ticks=self._pid_identity(process.pid)
        except OSError:start_ticks=None
        row={'identity':identity,'status':'running','pid':process.pid,'start_ticks':start_ticks,'boot_id':self._boot_id()}
        self._store(path,row)
        async def read(stream):
            chunks=[];size=0
            while chunk:=await stream.read(65536):
                size+=len(chunk)
                if size>self.max_output_bytes:raise ValueError('isolation output budget exceeded')
                chunks.append(chunk)
            return b''.join(chunks)
        async def communicate():
            process.stdin.write(stdin);await process.stdin.drain();process.stdin.close()
            tasks=[asyncio.create_task(read(process.stdout)),asyncio.create_task(read(process.stderr)),asyncio.create_task(process.wait())]
            try:
                stdout,stderr,_=await asyncio.gather(*tasks)
                return stdout,stderr
            finally:
                for task in tasks:
                    if not task.done():task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
        try:
            stdout,stderr=await asyncio.wait_for(communicate(),timeout_s)
            result=row|{'status':'completed' if process.returncode==0 else 'failed','returncode':process.returncode,
                'stdout':stdout.decode('utf-8',errors='replace'),'stderr':stderr.decode('utf-8',errors='replace'),
                'stdout_sha256':hashlib.sha256(stdout).hexdigest(),'stderr_sha256':hashlib.sha256(stderr).hexdigest(),
                'backend_verified':False}
        except (TimeoutError,ValueError,OSError,asyncio.CancelledError) as exc:
            if process.returncode is None:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                try:await asyncio.wait_for(process.wait(),3)
                except TimeoutError:pass
            result=row|{'status':'cancelled' if process.returncode is not None else 'unknown',
                'reason':'isolation_'+type(exc).__name__,'backend_verified':False}
            result=self._store(path,result)
            if isinstance(exc,asyncio.CancelledError):raise
            return result
        self._store(path,result)
        return await self.reconcile(request_id)
