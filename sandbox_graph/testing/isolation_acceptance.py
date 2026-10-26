"""Host-observed isolation canaries, run only against an explicitly supplied rootfs."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile
from ..runtime.candidate_isolation import BwrapBackend

# Trusted probe is sent as argv; no candidate modules, conftest or startup hooks.
PROBE=r'''
import json,os,pathlib,socket,sys
x=json.load(sys.stdin)
r={'canary_absent':not pathlib.Path(x['canary']).exists(),'environment_absent':'SANDBOX_GRAPH_ISOLATION_SECRET' not in os.environ}
for name,path in [('candidate_readonly','/workspace/sentinel'),('root_readonly','/isolation-write-probe')]:
    try:pathlib.Path(path).write_text('changed');r[name]=False
    except OSError:r[name]=True
s=socket.socket();s.settimeout(.5)
try:s.connect(('127.0.0.1',x['port']));r['host_network_absent']=False
except OSError:r['host_network_absent']=True
finally:s.close()
print(json.dumps(r,sort_keys=True))
'''


async def qualify(rootfs,*,rootfs_hash,output,python='/usr/bin/python3',executable='bwrap'):
    if not python.startswith('/'):raise ValueError('absolute rootfs Python executable required')
    output=Path(output).absolute()
    output.mkdir(parents=True,exist_ok=False)
    with tempfile.TemporaryDirectory(prefix='isolation-canary-',dir=output) as temporary:
        base=Path(temporary);work=base/'work';work.mkdir();(work/'sentinel').write_text('original')
        host=base/'host-private';host.mkdir(mode=0o700);canary=host/'credential';canary.write_text(secrets.token_hex(32));canary.chmod(0o600)
        original=hashlib.sha256(canary.read_bytes()).hexdigest()
        server=socket.socket();server.bind(('127.0.0.1',0));server.listen(1)
        backend=BwrapBackend(rootfs=rootfs,rootfs_hash=rootfs_hash,state_dir=output/'journal',executable=executable,
            commands={'probe':(python,'-I','-S','-c',PROBE),'child':(python,'-I','-S','-c','import time; time.sleep(120)')})
        env_name='SANDBOX_GRAPH_ISOLATION_SECRET';previous=os.environ.get(env_name)
        os.environ[env_name]=secrets.token_hex(32)
        result={'status':'blocked','backend_verified':False,'rootfs_hash':rootfs_hash}
        try:
            availability=await backend.capabilities();result['availability']=availability
            if availability['status']!='available':return result
            payload=json.dumps({'canary':str(canary),'port':server.getsockname()[1]}).encode()
            observed=await backend.execute('isolation-canary',work,'probe',payload,timeout_s=10,readonly=True)
            result['probe']=observed
            facts=json.loads(observed.get('stdout','{}'))
            expected={'canary_absent','environment_absent','candidate_readonly','root_readonly','host_network_absent'}
            if (observed['status']!='completed' or set(facts)!=expected or any(value is not True for value in facts.values())
                    or (work/'sentinel').read_text()!='original' or hashlib.sha256(canary.read_bytes()).hexdigest()!=original):
                result.update(status='fail',reason='host_canary_contract_failed');return result
            running=asyncio.create_task(backend.execute('isolation-cancel',work,'child',timeout_s=20,readonly=True))
            try:
                for _ in range(200):
                    state=await backend.reconcile('isolation-cancel')
                    if state.get('status')=='running':break
                    if running.done():break
                    await asyncio.sleep(.01)
                stopped=await backend.cancel('isolation-cancel',timeout_s=5)
                await asyncio.wait_for(running,5)
                final=await backend.reconcile('isolation-cancel')
                result['cancellation']={'cancel':stopped,'final':final}
                if stopped.get('cleanup')!='complete' or final.get('cleanup')!='complete':
                    result.update(status='fail',reason='original_children_not_proven_stopped');return result
            finally:
                if not running.done():
                    running.cancel()
                    from contextlib import suppress
                    with suppress(asyncio.CancelledError):await running
            result.update(status='pass',backend_verified=True,probe_source_sha256=hashlib.sha256(PROBE.encode()).hexdigest())
            return result
        finally:
            if previous is None:os.environ.pop(env_name,None)
            else:os.environ[env_name]=previous
            server.close()
            (output/'summary.json').write_text(json.dumps(result,sort_keys=True,indent=2,default=str)+'\n')
