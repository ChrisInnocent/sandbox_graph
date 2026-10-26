"""Build private digest-locked bundles and stream them through initial SSH."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import stat

from . import bootstrap_remote as receiver
from .transport import build_ssh_argv


def build_bundle(files,destination):
    destination=Path(destination).absolute()
    if destination.exists():raise ValueError('bundle destination already exists')
    rows=[];sources=[]
    for source,kind in files:
        source=Path(source).absolute()
        if source.resolve()!=source or not source.is_file():raise ValueError('canonical artifact file required')
        if kind=='grant_key' and source.stat().st_mode&0o077:raise PermissionError('grant key must be private')
        rows.append({'name':source.name,'kind':kind,'sha256':receiver.digest_file(source),'size_bytes':source.stat().st_size})
        sources.append(source)
    manifest={'schema_version':1,'files':rows};receiver.validate_manifest(manifest)
    receiver.private_dir(destination);blobs=receiver.private_dir(destination/'blobs')
    for source,row in zip(sources,rows):
        target=blobs/row['sha256']
        if target.exists():continue
        with source.open('rb') as src,target.open('xb') as dst:
            os.chmod(target,0o600)
            while block:=src.read(1024*1024):dst.write(block)
            dst.flush();os.fsync(dst.fileno())
        if target.stat().st_size!=row['size_bytes'] or receiver.digest_file(target)!=row['sha256']:
            raise ValueError('source changed during bundle preparation')
    receiver.sync_dir(blobs);receiver.atomic(destination/'manifest.json',receiver.encode(manifest))
    return {'manifest':manifest,'bundle_sha256':receiver.checksum(receiver.encode(manifest))}


def stream_bundle(bundle,request):
    bundle=Path(bundle).absolute()
    if bundle.resolve()!=bundle:raise ValueError('unsafe bundle directory')
    manifest=json.loads((bundle/'manifest.json').read_bytes(),object_pairs_hook=receiver.unique)
    receiver.validate_manifest(manifest)
    if manifest!=request['manifest'] or receiver.checksum(receiver.encode(manifest))!=request['bundle_sha256']:
        raise ValueError('bundle identity differs')
    header=receiver.encode(request)+b'\n'
    if len(header)>receiver.MAX_HEADER:raise ValueError('bootstrap header limit')
    yield header
    for row in manifest['files']:
        path=bundle/'blobs'/row['sha256']
        if path.stat().st_size!=row['size_bytes'] or receiver.digest_file(path)!=row['sha256']:
            raise ValueError('bundle asset changed')
        with path.open('rb') as stream:
            remaining=row['size_bytes']
            while remaining:
                block=stream.read(min(1024*1024,remaining))
                if not block:raise ValueError('bundle asset truncated')
                remaining-=len(block);yield block


async def deploy(*,bundle,request,address,port,user,known_hosts,identity_file,executable='ssh',timeout=900):
    # Only this repository's fixed program enters the remote shell string.
    source=Path(receiver.__file__).read_text()+'\nraise SystemExit(main())\n'
    argv=build_ssh_argv(address=address,port=port,user=user,known_hosts=known_hosts,
                        identity_file=identity_file,executable=executable)
    argv[-1]='python3 -I -B -c '+shlex.quote(source)
    process=await asyncio.create_subprocess_exec(*argv,stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    async def send():
        for block in stream_bundle(bundle,request):
            process.stdin.write(block);await process.stdin.drain()
        process.stdin.close();await process.stdin.wait_closed()
    async def read(stream):
        data=bytearray()
        while block:=await stream.read(65536):
            data.extend(block)
            if len(data)>receiver.MAX_HEADER:raise ValueError('bootstrap response limit')
        return bytes(data)
    tasks=[asyncio.create_task(send()),asyncio.create_task(read(process.stdout)),
           asyncio.create_task(read(process.stderr)),asyncio.create_task(process.wait())]
    try:
        _,stdout,_,code=await asyncio.wait_for(asyncio.gather(*tasks),timeout=timeout)
        result=json.loads(stdout,object_pairs_hook=receiver.unique)
        if code or result.get('status')!='completed':raise RuntimeError('remote bootstrap incomplete; reconcile original request')
        if (result.get('machine_id')!=request['machine_id'] or result.get('bundle_sha256')!=request['bundle_sha256']
                or result.get('operation_id')!=request['operation_id']
                or result.get('helper_path')!=str(Path(request['root'])/'bin/sandbox-graph-remote-agent')):
            raise ValueError('remote bootstrap receipt differs')
        return result
    finally:
        for task in tasks:task.cancel()
        if process.returncode is None:process.kill();await process.wait()
        await asyncio.gather(*tasks,return_exceptions=True)
