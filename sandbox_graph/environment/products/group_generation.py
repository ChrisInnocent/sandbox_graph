"""Publish a bounded private Kuasar JSON generation without partial visibility."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from ..declarations import _unique_json_object


def activate(step,asset,expected_before,*,backup_dir,authorize):
    """Recheck original sources immediately before the placer config CAS.

    All writers to these source directories must honor the environment host
    conflict domain. Uncoordinated external edits are detected by this snapshot
    check, but no cross-host filesystem transaction is claimed.
    """
    from dataclasses import asdict
    import yaml
    from ...domain.models import canonical_hash
    from ..file_transaction import replace_file
    from ..config import load_config
    if asset is None or asset.stat().st_mode&0o077:raise PermissionError('private activation asset required')
    with asset.open('rb') as stream:raw=stream.read(1024*1024+1)
    if len(raw)>1024*1024 or hashlib.sha256(raw).hexdigest()!=step.asset['sha256']:
        raise ValueError('activation asset changed or exceeds budget')
    data=json.loads(raw,object_pairs_hook=_unique_json_object)
    if set(data)!={'configuration','sources','generation'} or not isinstance(data['sources'],list) or len(data['sources'])>256:
        raise ValueError('bounded source snapshot required')
    total=0
    for source in data['sources']:
        root=Path(source['path'])
        if not root.is_absolute() or root.resolve()!=root or not root.is_dir():raise ValueError('original source unavailable')
        found=[]
        for index,path in enumerate(root.iterdir()):
            if index>=10000:raise ValueError('source entry budget exceeded')
            if path.suffix!='.json':continue
            if path.is_symlink() or not path.is_file():raise ValueError('original source contains unsafe entry')
            total+=path.stat().st_size
            if total>1024*1024:raise ValueError('source snapshot byte budget exceeded')
            found.append(load_config(path))
        key=lambda row:row.get('group','')
        if canonical_hash(sorted(found,key=key))!=canonical_hash(sorted(source['records'],key=key)):
            raise ValueError('original group source changed before activation')
    from .common import RecipeStep
    generation=data['generation'];target=Path(generation['target']);blob=Path(generation['asset']['path'])
    if not target.is_dir() or blob.resolve()!=blob or not blob.is_file():raise ValueError('published group generation missing')
    published=RecipeStep('verify:kuasar-groups','kuasar-groups',(),asset=generation['asset'],target=str(target))
    publish(published,blob,{str(target):None})  # existing immutable content must still match exactly
    if step.target not in expected_before:raise ValueError('placer config requires frozen before hash')
    if authorize is None or not authorize():raise PermissionError('activation authority expired')
    path=Path(step.target)
    if not path.is_absolute() or path.resolve()!=path:raise ValueError('canonical placer config required')
    path.parent.mkdir(parents=True,exist_ok=True)
    receipt=replace_file(path,yaml.safe_dump(data['configuration'],sort_keys=False).encode(),
        expected_sha256=expected_before[step.target],backup_dir=backup_dir,mode=0o600)
    return {'status':'completed','file':asdict(receipt),'source_snapshots_verified':len(data['sources'])}


def publish(step,asset,expected_before):
    if asset is None or asset.stat().st_mode&0o077:raise PermissionError('private group asset required')
    with asset.open('rb') as stream:raw=stream.read(1024*1024+1)
    if len(raw)>1024*1024 or hashlib.sha256(raw).hexdigest()!=step.asset['sha256']:
        raise ValueError('group asset changed or exceeds budget')
    records=json.loads(raw,object_pairs_hook=_unique_json_object)
    if not isinstance(records,dict) or not 1<=len(records)<=4096:raise ValueError('bounded nonempty group generation required')
    files={};seen=set()
    for name,record in records.items():
        group=record.get('group') if isinstance(record,dict) else None
        if (not re.fullmatch('[a-f0-9]{64}\\.json',name) or not isinstance(group,str) or not group or group in seen
                or name!=hashlib.sha256(group.encode()).hexdigest()+'.json'):
            raise ValueError('group generation identity differs')
        seen.add(group);files[name]=json.dumps(record,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    target=Path(step.target)
    if (not target.is_absolute() or target.resolve()!=target or target.name!=step.asset['sha256']
            or step.target not in expected_before or expected_before[step.target] is not None):
        raise ValueError('immutable digest-named absent group destination required')
    if target.exists():
        if (not target.is_dir() or target.stat().st_uid!=os.geteuid() or target.stat().st_mode&0o077
                or {p.name for p in target.iterdir()}!=set(files)):
            raise ValueError('existing group generation differs')
        for name,data in files.items():
            path=target/name
            if (path.is_symlink() or not path.is_file() or path.stat().st_uid!=os.geteuid()
                    or path.stat().st_mode&0o077 or path.stat().st_size!=len(data) or path.read_bytes()!=data):
                raise ValueError('existing group generation differs')
        return {'status':'completed','generation':target.name,'groups':len(files),'reused':True}
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if target.parent.resolve()!=target.parent:raise ValueError('group directory parent unsafe')
    stage=Path(tempfile.mkdtemp(prefix='.groups-',dir=target.parent))
    try:
        for name,data in files.items():
            fd=os.open(stage/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        fd=os.open(stage,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
        # Host execution lock serializes publication; never mutate an old generation.
        if target.exists():raise ValueError('group generation appeared concurrently')
        os.rename(stage,target)
        fd=os.open(target.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if stage.exists():shutil.rmtree(stage)
    return {'status':'completed','generation':target.name,'groups':len(files),'reused':False}
