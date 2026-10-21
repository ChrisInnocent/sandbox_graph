"""Registered guest fixture service: resident nonce, touched pages and file hashes.

Deploy explicitly into the guest. The nonce is generated inside this process and
never written to disk; restarting the service cannot prove memory continuity.
The Unix socket accepts a finite whitelist of JSON actions, never shell commands.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import time


class GuestProbe:
    def __init__(self,root,*,max_bytes):
        if type(max_bytes) is not int or max_bytes<=0:raise ValueError('positive memory limit required')
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.max_bytes=max_bytes;self.nonce=secrets.token_bytes(32)
        self.pages=bytearray();self.page_size=os.sysconf('SC_PAGE_SIZE');self.generation=secrets.token_hex(16)

    def inspect(self):
        files={}
        for p in sorted(self.root.iterdir()):
            if p.is_file() and not p.is_symlink():
                if len(files)>=1024 or p.stat().st_size>self.max_bytes:
                    raise ValueError('fixture inspection exceeds configured bounds')
                h=hashlib.sha256()
                with p.open('rb') as stream:
                    while block:=stream.read(65536):h.update(block)
                files[p.name]=h.hexdigest()
        return {'nonce_sha256':hashlib.sha256(self.nonce).hexdigest(),'process_generation':self.generation,
                'pid':os.getpid(),'allocated_bytes':len(self.pages),'page_size':self.page_size,
                'touched_pages':(len(self.pages)+self.page_size-1)//self.page_size,
                'memory_sha256':hashlib.sha256(self.pages).hexdigest(),'files':files}

    def handle(self,request):
        if not isinstance(request,dict):raise ValueError('JSON object required')
        action=request.get('action')
        if action=='touch':
            size=request.get('bytes')
            if type(size) is not int or not 0<size<=self.max_bytes:raise ValueError('touch size outside authorized limit')
            self.pages=bytearray(size)
            for i in range(0,size,self.page_size):self.pages[i]=1
            self.pages[-1]=1
        elif action=='mutate':
            index=request.get('index');value=request.get('value')
            if type(index) is not int or not 0<=index<len(self.pages) or type(value) is not int or not 0<=value<256:
                raise ValueError('invalid memory mutation')
            self.pages[index]=value
        elif action=='write_file':
            name=request.get('name');content=request.get('content')
            if not isinstance(name,str) or Path(name).name!=name or name in ('','.','..') or not isinstance(content,str):
                raise ValueError('simple fixture filename/content required')
            if len(content.encode())>self.max_bytes:raise ValueError('file outside authorized limit')
            target=self.root/name
            if target.is_symlink():raise ValueError('symlink forbidden')
            fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w') as stream:stream.write(content);stream.flush();os.fsync(stream.fileno())
        elif action!='inspect':raise ValueError('unregistered probe action')
        return self.inspect()


def serve(root,socket_path,*,max_bytes,max_requests,lifetime_s):
    if max_requests<=0 or lifetime_s<=0:raise ValueError('finite service bounds required')
    probe=GuestProbe(root,max_bytes=max_bytes);path=Path(socket_path)
    # Do not remove an existing socket whose owner/process is unknown.
    if path.exists():raise FileExistsError(path)
    deadline=time.monotonic()+lifetime_s
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as server:
        server.bind(str(path));os.chmod(path,0o600);server.listen(1)
        try:
            for _ in range(max_requests):
                remaining=deadline-time.monotonic()
                if remaining<=0:break
                server.settimeout(remaining)
                try:connection,_=server.accept()
                except TimeoutError:break
                with connection:
                    connection.settimeout(min(remaining,5));raw=bytearray()
                    try:
                        while b'\n' not in raw:
                            remaining=deadline-time.monotonic()
                            if remaining<=0:raise TimeoutError()
                            connection.settimeout(min(remaining,5))
                            block=connection.recv(4096)
                            if not block:break
                            raw.extend(block)
                            if len(raw)>65536:raise ValueError('request too large')
                        result={'status':'ok','result':probe.handle(json.loads(raw))}
                    except (ValueError,OSError):result={'status':'invalid_request'}
                    remaining=deadline-time.monotonic()
                    if remaining>0:
                        connection.settimeout(min(remaining,5))
                        try:connection.sendall(json.dumps(result).encode()+b'\n')
                        except OSError:pass
        finally:path.unlink(missing_ok=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True);parser.add_argument('--socket',required=True)
    parser.add_argument('--max-bytes',required=True,type=int);parser.add_argument('--max-requests',required=True,type=int)
    parser.add_argument('--lifetime',required=True,type=float)
    a=parser.parse_args(argv);serve(a.root,a.socket,max_bytes=a.max_bytes,max_requests=a.max_requests,lifetime_s=a.lifetime)

if __name__=='__main__':main()
