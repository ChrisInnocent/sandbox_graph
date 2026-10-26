"""Immutable executable bundles and attestations of actually imported worker code."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import re
from ..domain.models import canonical_hash
from ..domain.evolution import TARGET_FIELDS
from ..runtime.candidate_workspace import _tree,_write


def dependency_versions(root):
    from importlib.metadata import version,PackageNotFoundError
    path=Path(root)/'requirements.lock'
    with path.open('rb') as stream:raw=stream.read(1024*1024+1)
    if len(raw)>1024*1024:raise ValueError('dependency lock exceeds byte budget')
    found={}
    for line in raw.decode().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):continue
        matched=re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9][A-Za-z0-9.+!_-]*)',line.strip())
        if not matched:raise ValueError('exact dependency version lock required')
        name,expected=matched.groups();name=re.sub(r'[-_.]+','-',name).lower()
        if name in found:raise ValueError('duplicate locked dependency')
        try:actual=version(name)
        except PackageNotFoundError:raise ValueError('locked worker dependency is missing: '+name) from None
        if actual!=expected:raise ValueError('installed worker dependency differs: '+name)
        found[name]=actual
    if not found:raise ValueError('nonempty dependency lock required')
    return {'lock_hash':hashlib.sha256(raw).hexdigest(),'versions':found,'level':'installed_version_match'}


class WorkerBundleService:
    def __init__(self,repository,*,root):
        self.repository=repository;self.root=Path(root).absolute()
        if self.root.resolve()!=self.root:raise ValueError('canonical worker bundle root required')
        self.root.mkdir(parents=True,exist_ok=True)

    def build(self,candidate,*,token,workspace):
        frozen={key:value for key,value in candidate.model_dump(mode='json').items() if key not in {
            'schema_version','candidate_ref','proposal_id','budget_ref',*TARGET_FIELDS}}
        workspace.verify(frozen)
        manifest=_tree(Path(candidate.workspace_ref))
        if canonical_hash(manifest)!=candidate.tree_hash:raise ValueError('candidate tree differs')
        identity={'schema_version':2,'candidate_ref':candidate.candidate_ref,'candidate_hash':candidate.candidate_hash,
            'graph_ref':candidate.graph_ref,'graph_hash':candidate.graph_hash,'tree_hash':candidate.tree_hash,
            'lock_hash':candidate.lock_hash,'files':manifest,'producer':'worker-bundle.v1'}
        ref='worker-bundle:'+canonical_hash(identity);folder=self.root/canonical_hash(identity)
        body=identity|{'path':str(folder)}
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            row=tx.get('candidate_revisions',candidate.candidate_ref)
            if not row or row['body']!=candidate.model_dump(mode='json'):raise ValueError('candidate revision is not registered')
        if not folder.exists():
            temporary=Path(tempfile.mkdtemp(prefix='.bundle-',dir=self.root))
            try:
                shutil.copytree(candidate.workspace_ref,temporary/'tree')
                if canonical_hash(_tree(temporary/'tree'))!=candidate.tree_hash:raise ValueError('bundle tree changed during copy')
                _write(temporary/'manifest.json',body)
                for path in temporary.rglob('*'):
                    if path.is_file():path.chmod(0o555 if path.stat().st_mode&0o111 else 0o444)
                try:os.rename(temporary,folder)
                except FileExistsError:pass
            finally:
                if temporary.exists():shutil.rmtree(temporary)
        if json.loads((folder/'manifest.json').read_text())!=body:raise ValueError('bundle manifest differs')
        with self.repository.transaction() as tx:
            tx.assert_fence(token);tx.put('worker_bundles',ref,body,immutable=True)
        self.verify(ref);return ref

    def verify(self,reference):
        with self.repository.transaction() as tx:row=tx.get('worker_bundles',reference)
        if not row:raise ValueError('worker bundle is unregistered')
        return self._verify_body(reference,row['body'])

    def _verify_body(self,reference,body):
        identity={key:value for key,value in body.items() if key!='path'}
        if body.get('schema_version')!=2 or reference!='worker-bundle:'+canonical_hash(identity):raise ValueError('worker bundle identity differs')
        folder=Path(body['path'])
        if folder.parent!=self.root or folder.resolve()!=folder:raise ValueError('worker bundle path differs')
        if json.loads((folder/'manifest.json').read_text())!=body:raise ValueError('worker bundle manifest differs')
        tree=_tree(folder/'tree')
        if tree!=body['files'] or canonical_hash(tree)!=body['tree_hash']:raise ValueError('worker bundle tree differs')
        if tree.get('requirements.lock',{}).get('sha256')!=body['lock_hash']:raise ValueError('worker dependency lock differs')
        return body

    def register_ready(self,worker_id,reference,*,token,modules=('sandbox_graph','sandbox_bench'),ttl=30):
        if not modules or not 0<ttl<=60:raise ValueError('finite worker attestation required')
        bundle=self.verify(reference);root=Path(bundle['path'])/'tree';loaded={}
        selected=set(modules)|{name for name in sys.modules if any(name.startswith(prefix+'.') for prefix in modules)}
        for name in sorted(selected):
            module=sys.modules.get(name);path=Path(getattr(module,'__file__','')).absolute()
            if root not in path.resolve().parents or path.resolve()!=path:
                raise PermissionError('worker imported code outside the frozen bundle')
            relative=path.relative_to(root).as_posix();actual=hashlib.sha256(path.read_bytes()).hexdigest()
            if bundle['files'].get(relative,{}).get('sha256')!=actual:raise ValueError('loaded worker code differs from bundle')
            loaded[name]={'path':relative,'sha256':actual}
        dependencies=dependency_versions(root)
        with self.repository.transaction() as tx:
            tx.assert_fence(token)
            proof={'worker_id':worker_id,'bundle_ref':reference,'bundle_hash':canonical_hash(bundle),
                'graph_ref':bundle['graph_ref'],'loaded_modules':loaded,'dependencies':dependencies,'python':sys.version,'pid':os.getpid(),
                'thread_id':token.thread_id,'owner_epoch':token.owner_epoch,'lease_id':token.lease_id,
                'observed_at':tx.now,'expires_at':tx.now+ttl}
            proof_ref='worker-attestation:'+canonical_hash(proof)
            tx.put('worker_bundle_attestations',proof_ref,proof,immutable=True)
            registration={key:proof[key] for key in ('bundle_ref','bundle_hash','graph_ref','expires_at')}
            registration.update(status='ready',attestation_ref=proof_ref)
            tx.put('worker_registrations',worker_id,registration)
            return registration

    @staticmethod
    def ready_in(tx,registration,*,allow_draining=False):
        bundle=tx.get('worker_bundles',registration.get('bundle_ref',''))
        if not bundle or canonical_hash(bundle['body'])!=registration.get('bundle_hash'):return False
        if bundle['body'].get('schema_version')!=2:
            # Historical registrations remain readable; they do not establish
            # that an executable candidate bundle was actually loaded.
            return False
        row=tx.get('worker_bundle_attestations',registration.get('attestation_ref',''))
        if not row:return False
        proof=row['body']
        from ..persistence.repository import LeaseToken,StaleOwner
        try:tx.assert_fence(LeaseToken(proof['thread_id'],proof['owner_epoch'],proof['lease_id'],0))
        except (KeyError,StaleOwner):return False
        return (registration.get('status') in ({'ready','draining'} if allow_draining else {'ready'}) and registration.get('expires_at',0)>tx.now
            and registration['attestation_ref']=='worker-attestation:'+canonical_hash(proof)
            and proof['expires_at']>tx.now and bool(proof['loaded_modules'])
            and proof.get('dependencies',{}).get('lock_hash')==bundle['body']['lock_hash']
            and bool(proof.get('dependencies',{}).get('versions'))
            and all(proof.get(key)==registration.get(key) for key in ('bundle_ref','bundle_hash','graph_ref','expires_at')))

    @staticmethod
    def register_deployment_in(tx,candidate_hash,body):
        identity={key:value for key,value in body.items() if key!='worker_id'}
        old=tx.get('candidate_deployment_bindings',candidate_hash)
        if old and {key:value for key,value in old['body'].items() if key!='worker_id'}!=identity:
            raise ValueError('frozen candidate deployment differs')
        if not old:tx.put('candidate_deployment_bindings',candidate_hash,body,immutable=True)
        proof={'candidate_hash':candidate_hash,'worker_id':body['worker_id'],'deployment_hash':canonical_hash(identity)}
        tx.put('candidate_deployment_workers',canonical_hash(proof),proof,immutable=True)

    @classmethod
    def deployment_ready_in(cls,tx,body):
        identity={key:value for key,value in body.items() if key!='worker_id'}
        worker_ids={body.get('worker_id')}
        worker_ids.update(row['body']['worker_id'] for row in tx.scan('candidate_deployment_workers')
            if row['body'].get('candidate_hash')==body.get('candidate_hash')
            and row['body'].get('deployment_hash')==canonical_hash(identity))
        return any(row['key'] in worker_ids and row['body'].get('bundle_ref')==body.get('bundle_ref')
            and row['body'].get('bundle_hash')==body.get('bundle_hash') and cls.ready_in(tx,row['body'])
            for row in tx.scan('worker_registrations'))
