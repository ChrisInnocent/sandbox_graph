"""Bounded private log export, addressed by original journal identity, never a path.

The authenticated SSH account already owns the journal. This read-only action
exports only a complete sealed stream; callers store bytes privately, not in reports.
"""
import base64
import hashlib
import os
from pathlib import Path
import stat
import tempfile

MAX_CHUNK=64*1024
MAX_LOG=8*1024*1024


def read_chunk(journal,data):
    if set(data)!={'operation_id','input_hash','stream','sha256','offset','limit'}:
        raise ValueError('exact original log identity required')
    if (type(data['offset']) is not int or data['offset']<0 or type(data['limit']) is not int
            or not 0<data['limit']<=MAX_CHUNK or type(data['stream']) is not int or data['stream']<0):
        raise ValueError('bounded log range required')
    row=journal.get(data['operation_id'])
    if (not row or row['input_hash']!=data['input_hash'] or row['machine_id']!=journal.machine_id
            or row['state'] not in {'completed','failed','cancelled'}):
        raise PermissionError('sealed original operation log required')
    logs=(row.get('result') or {}).get('logs',[])
    if data['stream']>=len(logs):raise ValueError('sealed log stream unavailable')
    item=logs[data['stream']];size=item['bytes']
    if (item['sha256']!=data['sha256'] or type(size) is not int or not 0<=size<=MAX_LOG
            or data['offset']>size):raise ValueError('sealed log range or digest differs')
    path=Path(item['private_path']);root=journal.path.parent/'private-logs'
    if path.parent!=root or path.resolve()!=path or root.resolve()!=root:
        raise PermissionError('private journal log path required')
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.geteuid() or before.st_mode&0o077:
            raise PermissionError('private log ownership required')
        if before.st_size!=size:raise ValueError('log is truncated or has changed')
        digest=hashlib.sha256()
        for block in iter(lambda:stream.read(MAX_CHUNK),b''):digest.update(block)
        if digest.hexdigest()!=data['sha256']:raise ValueError('log digest differs')
        stream.seek(data['offset']);raw=stream.read(min(data['limit'],size-data['offset']))
        after=os.fstat(stream.fileno())
        if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
            raise ValueError('log changed during export')
    end=data['offset']+len(raw)
    return {key:data[key] for key in ('operation_id','input_hash','stream','sha256','offset')}|{
        'machine_id':journal.machine_id,'boot_id':row['boot_id'],'size_bytes':size,
        'next_offset':end,'eof':end==size,'data':base64.b64encode(raw).decode()}


async def download(fetch,request,*,output,machine_id):
    output=Path(output).absolute()
    if output.exists() or output.is_symlink():raise FileExistsError('private output already exists')
    if output.parent.resolve()!=output.parent:raise ValueError('canonical output directory required')
    if request['offset']!=0:raise ValueError('complete sealed log export starts at zero')
    fd,name=tempfile.mkstemp(prefix='.log-',dir=output.parent)
    offset=0;size=None;checksum=hashlib.sha256();boot=None
    try:
        with os.fdopen(fd,'wb') as sink:
            while True:
                value=await fetch(request|{'offset':offset})
                if any(value.get(k)!=request[k] for k in ('operation_id','input_hash','stream','sha256')):
                    raise ValueError('log receipt identity differs')
                if value.get('machine_id')!=machine_id or value.get('offset')!=offset:
                    raise ValueError('log host or offset differs')
                if size is None:size=value['size_bytes'];boot=value['boot_id']
                if (type(size) is not int or not 0<=size<=MAX_LOG or value['size_bytes']!=size
                        or value['boot_id']!=boot):raise ValueError('log incarnation or size changed')
                raw=base64.b64decode(value['data'],validate=True)
                if len(raw)>min(request['limit'],MAX_CHUNK) or offset+len(raw)>size:
                    raise ValueError('log response exceeds byte budget')
                end=offset+len(raw)
                if value['next_offset']!=end or value['eof'] is not (end==size) or not raw and end!=size:
                    raise ValueError('log response progress invalid')
                checksum.update(raw);sink.write(raw);offset=end
                if end==size:break
            if checksum.hexdigest()!=request['sha256']:raise ValueError('downloaded log digest differs')
            sink.flush();os.fsync(sink.fileno())
        # link gives no-clobber publication even if another writer won the path.
        os.link(name,output)
        directory=os.open(output.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
        return {'operation_id':request['operation_id'],'sha256':checksum.hexdigest(),'bytes':size,'private_path':str(output)}
    finally:Path(name).unlink(missing_ok=True)
