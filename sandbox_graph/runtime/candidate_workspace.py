"""Git-base candidate trees and immutable revisions; never mutate the source tree."""
from __future__ import annotations
import base64
import fcntl
from functools import wraps
import hashlib
import io
import json
import os
from pathlib import Path,PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import selectors
import signal
import time
from contextlib import contextmanager

from ..domain.models import canonical_hash

MAX_ARCHIVE_BYTES=272*1024*1024


@contextmanager
def _git_archive(repository,commit):
    """Bound time, memory and temporary bytes before extracting an exact base."""
    with tempfile.TemporaryFile() as archive:
        process=subprocess.Popen(['git','-C',str(repository),'archive','--format=tar',commit],
            stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True)
        try:
            total=0;deadline=time.monotonic()+60
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout,selectors.EVENT_READ)
                while True:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise TimeoutError('base archive time budget exceeded')
                    if not selector.select(remaining):raise TimeoutError('base archive time budget exceeded')
                    chunk=os.read(process.stdout.fileno(),65536)
                    if not chunk:break
                    total+=len(chunk)
                    if total>MAX_ARCHIVE_BYTES:raise ValueError('base archive byte budget exceeded')
                    archive.write(chunk)
            if process.wait(timeout=max(.001,deadline-time.monotonic())):
                raise ValueError('base archive export failed')
            archive.seek(0)
            yield archive
        finally:
            if process.poll() is None:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait(timeout=5)
            process.stdout.close()


def classify_diff(paths):
    """Only positively recognized non-executable documentation is low risk.

    Unknown influence and all runtime/test/policy/lock changes require R3.
    A model's claimed risk never feeds this classifier.
    """
    if paths and all((path.startswith('docs/') or path=='README.md') and path.endswith('.md') for path in paths):
        return 'R0'
    return 'R3'


def _relative(path):
    if (not isinstance(path,str) or not path or '\\' in path or '\x00' in path
        or path.startswith('/') or any(x in ('','.','..','.git') for x in path.split('/'))):
        raise ValueError('canonical candidate-relative path required')
    return path


def _tree(path):
    output={};size=0
    for item in sorted(path.rglob('*')):
        if item.is_symlink():raise ValueError('candidate symlinks are not permitted')
        if item.is_dir():continue
        if not item.is_file():raise ValueError('candidate contains a nonregular file')
        if size+item.stat().st_size>256*1024*1024:raise ValueError('candidate tree exceeds byte budget')
        with item.open('rb') as stream:raw=stream.read(256*1024*1024-size+1)
        size+=len(raw)
        if size>256*1024*1024:raise ValueError('candidate tree exceeds byte budget')
        output[item.relative_to(path).as_posix()]={'sha256':hashlib.sha256(raw).hexdigest(),
            'executable':bool(item.stat().st_mode & 0o111),'size':len(raw)}
    return output


def _write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as stream:
        stream.write(json.dumps(value,sort_keys=True,separators=(',',':')).encode());stream.flush();os.fsync(stream.fileno())
        temporary=Path(stream.name)
    os.replace(temporary,path)


def _locked(method):
    @wraps(method)
    def invoke(self,proposal_id,*args,**kwargs):
        path=self.root/(hashlib.sha256(proposal_id.encode()).hexdigest()+'.lock')
        fd=os.open(path,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
        try:
            fcntl.flock(fd,fcntl.LOCK_EX)
            return method(self,proposal_id,*args,**kwargs)
        finally:os.close(fd)
    return invoke


class CandidateWorkspace:
    def __init__(self,repository,root):
        self.repository=Path(repository).resolve(strict=True);self.root=Path(root).absolute()
        if (self.root.is_symlink() or self.root.resolve()==self.repository
                or self.repository in self.root.resolve().parents):
            raise ValueError('candidate root must be separate from user repository')
        self.root.mkdir(parents=True,mode=0o700,exist_ok=True)
        if self.root.resolve()!=self.root:raise ValueError('candidate root contains a symlink')
        if self.root.stat().st_uid!=os.geteuid():raise PermissionError('candidate root owner differs')
        self.root.chmod(0o700)

    def _folder(self,proposal_id):return self.root/hashlib.sha256(proposal_id.encode()).hexdigest()

    @_locked
    def create(self,proposal_id,base_commit,*,allowed_paths):
        if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})',base_commit):raise ValueError('exact base commit required')
        if not allowed_paths or len(set(allowed_paths))!=len(allowed_paths):raise ValueError('unique nonempty allowed paths required')
        for path in allowed_paths:_relative(path)
        folder=self._folder(proposal_id);meta=folder/'metadata.json'
        binding={'proposal_id':proposal_id,'base_commit':base_commit,'allowed_paths':list(allowed_paths)}
        if meta.exists():
            if json.loads(meta.read_text())['binding']!=binding:raise ValueError('proposal workspace identity conflict')
            return folder/'work'
        if folder.exists():raise ValueError('unidentified incomplete workspace requires original recovery evidence')
        actual=subprocess.check_output(['git','-C',str(self.repository),'rev-parse',base_commit+'^{commit}'],stderr=subprocess.DEVNULL,timeout=5).decode().strip()
        if actual!=base_commit:raise ValueError('base commit mismatch')
        staging=folder.with_name('.'+folder.name+'.initializing')
        if staging.exists():
            binding_file=staging/'binding.json'
            if (staging.is_symlink() or not staging.is_dir() or binding_file.is_symlink()
                    or any(staging.iterdir()) and (not binding_file.is_file() or json.loads(binding_file.read_text())!=binding)):
                raise ValueError('interrupted workspace staging identity differs')
            shutil.rmtree(staging)
        staging.mkdir(mode=0o700);_write(staging/'binding.json',binding)
        base=staging/'base';base.mkdir()
        try:
            with _git_archive(self.repository,base_commit) as archive, tarfile.open(fileobj=archive,mode='r|') as tar:
                count=0;total=0
                for member in tar:
                    count+=1;total+=member.size
                    if count>10000 or total>256*1024*1024:raise ValueError('base archive extraction budget exceeded')
                    name=member.name.rstrip('/');_relative(name)
                    if not member.isfile() and not member.isdir():raise ValueError('base archive contains unsafe entries')
                    dest=base/name
                    if member.isdir():dest.mkdir(parents=True,exist_ok=True);continue
                    dest.parent.mkdir(parents=True,exist_ok=True)
                    with tar.extractfile(member) as source,dest.open('xb') as sink:shutil.copyfileobj(source,sink)
                    dest.chmod(0o755 if member.mode & 0o111 else 0o644)
            tree=_tree(base)
            shutil.copytree(base,staging/'work')
            _write(staging/'metadata.json',{'binding':binding,'base_tree':tree,'base_tree_hash':canonical_hash(tree),'revisions':[]})
            os.replace(staging,folder)
            fd=os.open(folder.parent,os.O_RDONLY)
            try:os.fsync(fd)
            finally:os.close(fd)
            return folder/'work'
        except BaseException:
            if staging.exists():shutil.rmtree(staging)
            raise

    @staticmethod
    def _allowed(path,allowed):
        if not any(path==prefix or path.startswith(prefix+'/') for prefix in allowed):
            raise PermissionError('candidate path is outside proposal scope')

    @_locked
    def edit(self,proposal_id,edits,*,validate_only=False,resume=False):
        folder=self._folder(proposal_id);meta=json.loads((folder/'metadata.json').read_text());root=folder/'work'
        if not isinstance(edits,list) or not 1<=len(edits)<=256:raise ValueError('bounded explicit edits required')
        seen=set();prepared=[]
        for edit in edits:
            if not isinstance(edit,dict) or set(edit)!={'path','before_sha256','content'}:raise ValueError('unknown coding edit field')
            path=_relative(edit['path']);self._allowed(path,meta['binding']['allowed_paths'])
            if path in seen:raise ValueError('duplicate coding edit path')
            seen.add(path);target=root/path
            if target.is_symlink() or root not in target.resolve().parents:raise ValueError('candidate edit escapes workspace')
            actual=hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
            content=edit['content']
            if content is not None and (not isinstance(content,str) or len(content.encode())>1024*1024):
                raise ValueError('coding edit exceeds UTF-8 byte budget')
            desired=hashlib.sha256(content.encode()).hexdigest() if content is not None else None
            if resume and actual==desired:continue
            if actual!=edit['before_sha256']:raise ValueError('coding edit base digest changed')
            prepared.append((target,content))
        if resume and not prepared:return
        original=_tree(root);projected=dict(original)
        for target,content in prepared:
            name=target.relative_to(root).as_posix()
            if content is None:projected.pop(name,None)
            else:projected[name]={'sha256':hashlib.sha256(content.encode()).hexdigest(),
                'size':len(content.encode()),'executable':original.get(name,{}).get('executable',False)}
        if projected==original or projected==meta['base_tree']:raise ValueError('candidate made no progress')
        if validate_only:return
        for target,content in prepared:
            if content is None:target.unlink();continue
            target.parent.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as stream:
                temporary=Path(stream.name)
                stream.write(content.encode());stream.flush();os.fsync(stream.fileno())
            try:
                temporary.chmod(target.stat().st_mode&0o777 if target.exists() else 0o644)
                os.replace(temporary,target)
            finally:temporary.unlink(missing_ok=True)

    def resume_edits(self,proposal_id,edits):
        return self.edit(proposal_id,edits,resume=True)

    def validate_edits(self,proposal_id,edits):
        return self.edit(proposal_id,edits,validate_only=True)

    @_locked
    def observe_edits(self,proposal_id,edits):
        """Observe original local mutations under the same exclusive writer lock."""
        folder=self._folder(proposal_id);meta=json.loads((folder/'metadata.json').read_text());root=folder/'work'
        paths=[]
        for edit in edits:
            name=_relative(edit['path']);self._allowed(name,meta['binding']['allowed_paths']);path=root/name
            if path.is_symlink() or root not in path.resolve().parents:raise PermissionError('candidate path escaped')
            if path.exists() and path.stat().st_size>1024*1024:raise ValueError('original edited file exceeds limit')
            actual=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            desired=hashlib.sha256(edit['content'].encode()).hexdigest() if edit['content'] is not None else None
            state='after' if actual==desired else 'before' if actual==edit['before_sha256'] else 'unknown'
            paths.append({'path':name,'sha256':actual,'state':state})
        return {'status':'closed' if paths and all(row['state']!='unknown' for row in paths) else 'unknown','paths':paths}

    @_locked
    def freeze(self,proposal_id,*,revision,graph_ref):
        folder=self._folder(proposal_id);meta=json.loads((folder/'metadata.json').read_text());root=folder/'work'
        if type(revision) is not int or revision<1:raise ValueError('candidate revision must be positive')
        if revision<=len(meta['revisions']):
            old=meta['revisions'][revision-1]['record']
            if old['graph_ref']!=graph_ref:raise ValueError('candidate revision identity changed')
            return self.verify(old)
        if revision!=len(meta['revisions'])+1:raise ValueError('candidate revision must be sequential')
        tree=_tree(root)
        if _tree(folder/'base')!=meta['base_tree']:raise ValueError('candidate base was modified')
        paths=sorted(path for path in set(tree)|set(meta['base_tree']) if tree.get(path)!=meta['base_tree'].get(path))
        if not paths:raise ValueError('candidate made no progress')
        for path in paths:self._allowed(path,meta['binding']['allowed_paths'])
        if meta['revisions'] and tree==meta['revisions'][-1]['tree']:raise ValueError('candidate revision made no progress')
        patch=[{'path':path,'before':meta['base_tree'].get(path),'after':tree.get(path),
            'content_base64':base64.b64encode((root/path).read_bytes()).decode() if path in tree else None} for path in paths]
        if 'requirements.lock' not in tree:raise ValueError('frozen dependency lock required')
        destination=folder/'frozen'/str(revision);destination.parent.mkdir(exist_ok=True)
        result={'base_commit':meta['binding']['base_commit'],'revision':revision,
            'parent_revision':revision-1 if revision>1 else None,'tree_hash':canonical_hash(tree),
            'patch_hash':canonical_hash(patch),'changed_paths':paths,'workspace_ref':str(destination),
            'risk':classify_diff(paths),'graph_ref':graph_ref,
            'graph_hash':canonical_hash({'graph_ref':graph_ref,'definitions':{k:v for k,v in tree.items() if k.startswith('sandbox_graph/graphs/')}}),
            'lock_hash':tree['requirements.lock']['sha256']}
        manifest=destination.parent/(str(revision)+'.json')
        value={'record':result,'tree':tree,'patch':patch}
        if manifest.exists() and json.loads(manifest.read_text())!=value:
            raise ValueError('interrupted candidate publication identity changed')
        if destination.exists():
            if not manifest.exists() or _tree(destination)!=tree:
                raise ValueError('existing frozen candidate publication differs')
        else:
            staging=destination.parent/('.'+str(revision)+'.staging')
            if staging.is_symlink():raise ValueError('candidate staging path is unsafe')
            # Only this private, revision-specific staging directory is disposable.
            if staging.exists():shutil.rmtree(staging)
            try:
                shutil.copytree(root,staging)
                if _tree(staging)!=tree:raise ValueError('candidate changed during freeze')
                for path in staging.rglob('*'):
                    if path.is_file():path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
                _write(manifest,value)
                os.replace(staging,destination)
                fd=os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(fd)
                finally:os.close(fd)
            except BaseException:
                if staging.exists():shutil.rmtree(staging)
                raise
        meta['revisions'].append({'record':result,'tree':tree});_write(folder/'metadata.json',meta)
        self.verify(result)
        return result

    def verify(self,record):
        path=Path(record['workspace_ref'])
        if self.root not in path.resolve().parents or path.is_symlink():raise ValueError('frozen candidate path invalid')
        stored=json.loads((path.parent/(path.name+'.json')).read_text())
        if stored['record']!=record or canonical_hash(stored['patch'])!=record['patch_hash']:
            raise ValueError('frozen candidate manifest differs')
        if canonical_hash(_tree(path))!=record['tree_hash']:raise ValueError('frozen candidate tree differs')
        return dict(record)

    def verify_revision(self,candidate):
        path=self._folder(candidate.proposal_id)/'frozen'/str(candidate.revision)
        stored=json.loads((path.parent/(path.name+'.json')).read_text())['record']
        value=candidate.model_dump(mode='json')
        if str(path)!=candidate.workspace_ref or any(value.get(key)!=item for key,item in stored.items()):
            raise ValueError('frozen revision differs from registered candidate')
        return self.verify(stored)
