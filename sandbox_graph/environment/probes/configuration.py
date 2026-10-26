"""Read configuration and prove its binding to an unchanged running process.

Startup binding is deliberately distinct from runtime-effective observations.
No executable, environment value or command is supplied by the caller.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import re
from ..config import load_config
from ..products.literal_env import parse_literal_env
from ..evidence import SecretRedactor
from ..products.probe import UNITS
from .inventory import parse_proc_stat

COMPONENTS={
    'cubesandbox':{'control':('CubeOps','CubeMaster','CubeAPI','cubeproxy','CubeTemplateCenter'),'compute':('cubelet',)},
    'kuasar':{role:(role,) for role in UNITS['kuasar'] if role!='runtime'},
    'agentenv':{'server':('server',),'scheduler':('scheduler',),'gateway':('gateway',)}}


def inspect_configuration(product,*,roles,paths,proc_root='/proc',runner=subprocess.run):
    if product not in COMPONENTS or not set(roles)<=set(UNITS[product]):raise ValueError('undeclared configuration role')
    proc=Path(proc_root);result={}
    for role in roles:
        for index,component in enumerate(COMPONENTS[product].get(role,())):
            path=paths.get('config:'+component)
            row={'status':'unknown','evidence_level':'no_evidence','reason':'declared_configuration_path_required'}
            result[component]=row
            if not path:continue
            target=Path(path)
            if not target.is_absolute() or target.resolve()!=target:raise ValueError('configuration path is not canonical')
            try:
                before=target.stat()
                if not stat.S_ISREG(before.st_mode) or before.st_size>1024*1024:raise ValueError('configuration size/type invalid')
                with target.open('rb') as stream:raw=stream.read(1024*1024+1)
                env_file=product=='cubesandbox' and component=='CubeAPI'
                proxy=product=='cubesandbox' and component=='cubeproxy'
                values={'format':'nginx','bytes':len(raw)} if proxy else parse_literal_env(raw) if env_file else load_config(target)
                if len(raw)>1024*1024 or target.stat().st_mtime_ns!=before.st_mtime_ns:raise ValueError('configuration changed')
                row.update(sha256=hashlib.sha256(raw).hexdigest(),mode=stat.S_IMODE(before.st_mode),
                    values=SecretRedactor().redact(values),evidence_level='file_only',reason='process_binding_unavailable')
                units=UNITS[product][role];unit=None;process_path=path
                if not units or proxy:
                    name=paths.get('container:'+component,'cube-proxy' if proxy else 'aenv-'+role)
                    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',name):raise ValueError('safe container name required')
                    response=runner(['docker','inspect',name],capture_output=True,text=True,timeout=10,check=False,shell=False)
                    containers=json.loads(response.stdout) if response.returncode==0 and len(response.stdout)<=1024*1024 else []
                    if len(containers)!=1 or containers[0].get('State',{}).get('Running') is not True:continue
                    container=containers[0]
                    mounts=[item for item in container.get('Mounts',()) if item.get('Source')==path and item.get('RW') is False]
                    if len(mounts)!=1:continue
                    process_path=mounts[0]['Destination'];pid=int(container['State']['Pid'])
                    row.update(container_id=container['Id'],image=container['Image'],config_mount=mounts[0])
                else:
                    unit=units[index];response=runner(['systemctl','show',unit,'--property=MainPID,ActiveState'],
                        capture_output=True,text=True,timeout=10,check=False,shell=False)
                    fields=dict(line.split('=',1) for line in response.stdout.splitlines() if '=' in line)
                    if response.returncode or fields.get('ActiveState')!='active':continue
                    pid=int(fields['MainPID'])
                if pid<=0:continue
                task=proc/str(pid);identity=parse_proc_stat((task/'stat').read_text())
                cmdline=(task/'cmdline').read_bytes()
                environ=(task/'environ').read_bytes()
                if len(cmdline)>65536 or len(environ)>1024*1024:raise ValueError('process binding exceeds byte budget')
                argv=[value.decode() for value in cmdline.split(b'\0') if value]
                env=dict(value.decode().split('=',1) for value in environ.split(b'\0') if b'=' in value)
                flags={'--config','-config','--config-file','-c'}
                bound=any(arg in flags and i+1<len(argv) and argv[i+1]==process_path for i,arg in enumerate(argv))
                bound=bound or any(flag+'='+process_path in argv for flag in flags)
                if proxy and process_path=='/usr/local/openresty/nginx/conf/nginx.conf':
                    bound=bound or bool(argv and Path(argv[0].split(':',1)[0]).name in {'nginx','openresty'})
                if product=='agentenv':bound=bound or env.get('AENV_CONFIG_PATH')==path
                if product=='cubesandbox':
                    source_env={'CubeOps':'CUBE_OPS_CONFIG','CubeMaster':'CUBE_MASTER_CONFIG_PATH',
                        'CubeTemplateCenter':'CUBE_TEMPLATE_CENTER_CONFIG_PATH'}
                    if component in source_env:
                        configured=env.get(source_env[component],'/etc/cube/ops.yaml' if component=='CubeOps' else None)
                        bound=bound or configured==path
                    if env_file:
                        bound=bool(values) and all(env.get(key)==value for key,value in values.items())
                        resolved=dict(values);sources={key:'process_environment' for key in values}
                        options={'bind':'CUBE_API_BIND','cubemaster-url':'CUBE_MASTER_ADDR',
                            'auth-callback-url':'AUTH_CALLBACK_URL','worker-threads':'WORKER_THREADS',
                            'rate-limit-per-sec':'RATE_LIMIT_PER_SEC','sandbox-domain':'CUBE_API_SANDBOX_DOMAIN',
                            'log-level':'LOG_LEVEL','log-dir':'LOG_DIR','log-prefix':'LOG_PREFIX','instance-type':'INSTANCE_TYPE'}
                        for i,arg in enumerate(argv):
                            name,sep,value=arg.partition('=')
                            key=options.get(name.removeprefix('--')) if name.startswith('--') else None
                            if key and (sep or i+1<len(argv)):
                                resolved[key]=value if sep else argv[i+1];sources[key]='cli'
                        row.update(resolved_values=SecretRedactor().redact(resolved),sources=sources,
                            authentication_mode='callback' if resolved.get('AUTH_CALLBACK_URL') else 'static' if resolved.get('CUBE_API_KEY') else 'unprotected')
                starts=next(int(line.split()[1]) for line in (proc/'stat').read_text().splitlines() if line.startswith('btime '))
                starts+=identity['start_ticks']/os.sysconf('SC_CLK_TCK')
                after=parse_proc_stat((task/'stat').read_text())
                after_file=target.stat()
                if (after['start_ticks']!=identity['start_ticks'] or
                        (after_file.st_dev,after_file.st_ino,after_file.st_mtime_ns,after_file.st_size)!=
                        (before.st_dev,before.st_ino,before.st_mtime_ns,before.st_size)):
                    row['reason']='configuration_or_process_changed';continue
                row.update(unit=unit,pid=pid,start_ticks=identity['start_ticks'],configuration_bound=bound,
                    file_precedes_process=before.st_mtime<=starts)
                if bound and before.st_mtime<=starts:
                    # File provenance, not a claim that product defaults/overrides equal it.
                    row.update(status='satisfied',evidence_level='startup_bound',reason=None)
            except FileNotFoundError:
                row.update(status='unknown' if row['evidence_level']!='no_evidence' else 'missing',
                    reason='process_disappeared' if row['evidence_level']!='no_evidence' else 'configuration_missing')
            except (OSError,ValueError,KeyError,StopIteration,UnicodeError,subprocess.TimeoutExpired):
                row.update(status='unknown',reason='configuration_observation_failed')
    return {'product':product,'components':result}
