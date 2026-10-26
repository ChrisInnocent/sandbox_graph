"""Source-pinned server credential assets and Kuasar's local admin protocol.

Rendering is not authentication evidence. Files enter the normal recipe/CAS
path; service reload and protected owned-resource canaries remain mandatory.
No secret enters argv, the returned manifest or the public operation journal.
"""
import copy
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import time

from .common import absolute
from .literal_env import render_literal_env


def deployment_recipe(manifest,*,scope,secret_root,target,unit,expected_before,dropin_before,output):
    """Render server-side CAS steps plus the matching executor client binding.

    Paths/unit are operator-owned deployment inputs. The installed process and
    protected endpoint must subsequently pass independent verification.
    """
    from dataclasses import asdict
    from .common import file_step,command
    ref=manifest['credential_ref'];parts=ref.split('/')
    if len(parts)!=5 or parts[:2]!=['secret:',''] or parts[2]!=scope:
        raise PermissionError('server/client credential scope differs')
    if manifest['credential_version']!=parts[4]:raise ValueError('server/client credential version differs')
    if manifest['product'] not in {'agentenv','cubesandbox'}:raise ValueError('Kuasar requires tenant/group registration')
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service',unit):raise ValueError('explicit service unit required')
    absolute(target)
    if not re.fullmatch(r'/[A-Za-z0-9_./@-]+',target):raise ValueError('service credential path requires literal safe characters')
    root=Path(secret_root)
    if not root.is_absolute() or root.resolve()!=root:raise ValueError('canonical private secret store required')
    client={'auth_ref':ref,'auth_scope':scope,'auth_version':parts[4],
        'server_credential_version':parts[4],'secret_store_path':str(root)}
    if manifest['carrier']=='callback':return {'steps':[],'expected_before':{},'client':client,'evidence_level':'rendered_only'}
    if manifest['carrier'] not in {'environment','external_file','managed_file'} or not manifest.get('asset'):
        raise ValueError('registered service credential carrier required')
    steps=[file_step({'server-auth':manifest['asset']},'server-auth',target,mode=0o600)]
    before={target:expected_before}
    if manifest['carrier']=='environment':
        asset=private_asset(output,('[Service]\nEnvironmentFile='+target+'\n').encode())
        path='/etc/systemd/system/'+unit+'.d/50-sandbox-auth.conf'
        before[path]=dropin_before;steps.append(file_step({'auth-dropin':asset},'auth-dropin',path,mode=0o600))
        steps.append(command('auth:daemon-reload','systemctl','daemon-reload'))
    steps.append(command('auth:restart','systemctl','restart',unit))
    return {'steps':[asdict(s) for s in steps],'expected_before':before,'client':client,
        'evidence_level':'rendered_only','requires':['effective_source_probe','protected_canary']}


def private_asset(path,data):
    path=Path(path).absolute()
    if path.resolve()!=path or len(data)>1024*1024:raise ValueError('bounded canonical private asset required')
    path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    try:fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    except FileExistsError:
        info=path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode&0o077
                or info.st_size!=len(data) or path.read_bytes()!=data):
            raise ValueError('existing private asset differs; explicit new version required')
    else:
        with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    return {'path':str(path),'sha256':hashlib.sha256(data).hexdigest()}


def _override(argv,name):
    values=[]
    for index,value in enumerate(argv):
        if value==name:
            if index+1>=len(argv):raise ValueError('missing startup override value')
            values.append(argv[index+1])
        elif value.startswith(name+'='):values.append(value[len(name)+1:])
    if len(values)>1:raise ValueError('ambiguous startup override')
    return values[0] if values else None


def prepare_auth(product,*,store,scope,credential_ref,observed,output):
    value=store.resolve(credential_ref,scope=scope)
    environment=dict(observed.get('environment',{}))
    manifest={'product':product,'credential_ref':credential_ref,'credential_version':credential_ref.rsplit('/',1)[1],
        'evidence_level':'rendered_only','requires':['service_reload','effective_source_probe','protected_canary']}
    if product=='agentenv':
        from .agentenv import credential_source
        credential_source(env={'AENV_API_KEY':value},external=None,managed=None)
        # An explicitly empty env is invalid upstream, not a fallback to a file.
        credential_source(env=environment,external=observed.get('external'),managed=None)
        carrier='environment' if 'AENV_API_KEY' in environment else 'external_file' if observed.get('external') is not None else 'managed_file'
        if carrier=='environment':environment['AENV_API_KEY']=value;data=render_literal_env(environment)
        else:data=value.encode()
    elif product=='cubesandbox':
        argv=observed.get('argv',[])
        callback=_override(argv,'--auth-callback-url')
        if callback is None:callback=environment.get('AUTH_CALLBACK_URL')
        if callback:return manifest|{'carrier':'callback','asset':None,'requires':['existing_callback_credential','protected_canary']}
        if any(_override(argv,name) is not None for name in ('--cube-api-key','--api-key')):
            raise ValueError('unsupported startup credential override; Cube static key uses CUBE_API_KEY')
        if not value or len(value)>4096 or any(c in value for c in '\r\n\x00'):raise ValueError('invalid Cube credential')
        environment['CUBE_API_KEY']=value;data=render_literal_env(environment);carrier='environment'
    elif product=='kuasar':
        pair=kuasar_pair(value)
        data=json.dumps(pair,sort_keys=True,separators=(',',':')).encode();carrier='manifest_key_pair'
    else:raise ValueError('unsupported server authentication product')
    return manifest|{'carrier':carrier,'asset':private_asset(output,data)}


def kuasar_pair(manifest_key,api_secret=None):
    if not isinstance(manifest_key,str) or not re.fullmatch('[a-f0-9]{64}',manifest_key):raise ValueError('canonical manifest key required')
    derived=hmac.new(bytes.fromhex(manifest_key),b'kuasar-api-secret-v1',hashlib.sha256).hexdigest()
    api_secret=derived if api_secret is None else api_secret
    if not isinstance(api_secret,str) or not re.fullmatch('[a-f0-9]{64}',api_secret):raise ValueError('canonical API secret required')
    return {'manifest_key':manifest_key,'api_secret':api_secret}


def ensure_kuasar_tenant(pair,*,call,authorize,ttl_seconds=0):
    if set(pair)!={'manifest_key','api_secret'} or type(ttl_seconds) is not int or not 0<=ttl_seconds<=31536000:
        raise ValueError('explicit bounded tenant key pair required')
    pair=kuasar_pair(**pair)
    fingerprints={name+'_fingerprint':hashlib.sha256(bytes.fromhex(value)).hexdigest() for name,value in pair.items()}
    observations=[]
    def invoke(op):
        if not authorize():raise PermissionError('tenant execution authority expired')
        response=call({'op':op,**pair,**({'ttl_seconds':ttl_seconds} if op=='add' else {})})
        if (not isinstance(response,dict) or response.get('op')!=op or response.get('error')
                or response.get('status') not in ({'present','absent'} if op=='check' else {'added','refreshed'})
                or any(response.get(key)!=value for key,value in fingerprints.items())):
            raise ValueError('tenant response is not an authoritative matching pair')
        observations.append({'op':op,'status':response['status'],**fingerprints})
        return response['status']
    created=invoke('check')=='absent'
    if created:
        # Only this absence permits add. A disconnect is left in the original
        # remote journal; no second add/TTL refresh is made on uncertain replay.
        if invoke('add')!='added':raise ValueError('tenant changed concurrently; TTL may have changed')
        if invoke('check')!='present':raise ValueError('tenant registration did not become effective')
    return {'status':'completed','created':created,'observations':observations,**fingerprints}


def kuasar_group(record,*,pair,existing_groups):
    record=copy.deepcopy(record);group=record.get('group')
    if not isinstance(group,str) or not group or group in existing_groups:raise ValueError('unique explicit group required')
    pair=kuasar_pair(**pair)
    for key,value in pair.items():
        desired={'type':'inline','value':value}
        if record.get(key) and record[key]!=desired:raise ValueError('group credential differs; explicit rotation required')
        record[key]=desired
    return record


def tenant_over_unix_socket(pair,*,path,authorize,timeout_s):
    absolute(path);sock_path=Path(path);info=sock_path.stat()
    if (sock_path.resolve()!=sock_path or not stat.S_ISSOCK(info.st_mode) or info.st_uid not in {0,os.geteuid()}
            or info.st_mode&0o007 or not 0<timeout_s<=300):raise PermissionError('private local Kuasar admin socket required')
    deadline=time.monotonic()+timeout_s
    class UnixConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout);self.sock.connect(path)
    def call(body):
        remaining=deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('tenant operation deadline')
        connection=UnixConnection('localhost',timeout=remaining)
        try:
            connection.request('POST','/internal/admin/manifest-keys',json.dumps(body),{'Content-Type':'application/json'})
            response=connection.getresponse();raw=response.read(65537)
            if response.status!=200 or len(raw)>65536:raise ValueError('tenant admin response invalid')
            from ..declarations import _unique_json_object
            return json.loads(raw,object_pairs_hook=_unique_json_object)
        finally:connection.close()
    return ensure_kuasar_tenant(pair,call=call,authorize=authorize)
