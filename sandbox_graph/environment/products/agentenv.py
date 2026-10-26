"""AgentENV native/runtime and digest-locked control-plane installation rules."""
import hashlib
import re
from urllib.parse import urlsplit
from .common import RecipeStep,archive_step,command,file_step,installation_state,pinned_image,roles_exact

AUTH_HEADER='X-API-Key'
ROLE_COMPONENTS={'server':('server','uvm-ublk-daemon'),'scheduler':('scheduler',),'gateway':('gateway',)}


def credential_source(*,env,external,managed):
    source,value=('environment',env['AENV_API_KEY']) if 'AENV_API_KEY' in env else (
        ('external',external) if external is not None else ('managed',managed))
    if value is None:return {'source':'absent','fingerprint':None}
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9._~-]{32,256}',value):
        raise ValueError('invalid AgentENV API credential')
    return {'source':source,'fingerprint':hashlib.sha256(value.encode()).hexdigest()}


def scheduler_eligibility(snapshot,limits):
    # Upstream keeps unreported nodes; our readiness requires a fresh report.
    if snapshot is None:return {'eligible':None,'ready':False,'reason':'node_report_missing'}
    if limits is None:return {'eligible':True,'ready':True,'exceeded':[]}
    values={
        'max_sandbox_count':snapshot.get('sandbox_count'),
        'max_sandbox_starting_count':snapshot.get('sandbox_starting_count'),
        'max_cpu_used_percent':snapshot.get('cpu_percent'),
        'max_sandbox_count_including_paused':snapshot.get('sandbox_count',0)+snapshot.get('paused_sandbox_count',0),
        'max_allocated_cpu_including_paused':snapshot.get('allocated_cpu',0)+snapshot.get('paused_allocated_cpu',0),
        'max_allocated_memory_bytes_including_paused':snapshot.get('allocated_memory_bytes',0)+snapshot.get('paused_allocated_memory_bytes',0)}
    for key,numerator,denominator in (('max_cpu_allocated_percent','allocated_cpu','cpu_count'),
        ('max_memory_used_percent','memory_used_bytes','memory_total_bytes'),
        ('max_memory_allocated_percent','allocated_memory_bytes','memory_total_bytes')):
        values[key]=snapshot.get(numerator,0)*100//snapshot[denominator] if snapshot.get(denominator,0)>0 else None
    if set(limits)-set(values):raise ValueError('unknown AgentENV scheduler threshold')
    for value in limits.values():
        if value is not None and (type(value) is not int or value<0):raise ValueError('invalid scheduler threshold')
    missing=[key for key,value in limits.items() if value is not None and values[key] is None]
    if missing:return {'eligible':None,'ready':False,'reason':'incomplete_node_report','missing':missing}
    exceeded=[key for key,value in limits.items() if value is not None and values[key]>value]
    return {'eligible':not exceeded,'ready':True,'exceeded':exceeded}


def server_unit():
    return '''[Unit]
Description=AgentENV Server
After=network.target
[Service]
User=aenv
Group=aenv
SupplementaryGroups=kvm
EnvironmentFile=/etc/aenv/aenv.env
ExecStart=/usr/local/bin/server
RuntimeDirectory=aenv
RuntimeDirectoryMode=0750
AmbientCapabilities=CAP_NET_ADMIN CAP_SYS_ADMIN
CapabilityBoundingSet=CAP_NET_ADMIN CAP_SYS_ADMIN
NoNewPrivileges=true
UMask=0027
LimitNOFILE=1048576
LimitMEMLOCK=infinity
Restart=on-failure
RestartSec=5
KillMode=process
TimeoutStopSec=30
[Install]
WantedBy=multi-user.target
'''


def install_recipe(*,roles,assets,state,upgrade=False,images=None,observed_steps=None):
    roles_exact(roles,ROLE_COMPONENTS)
    if state=='partial' and observed_steps is not None:
        from .common import missing_steps
        full=install_recipe(roles=roles,assets=assets,state='absent',images=images)
        dependencies={}
        if 'server' in roles:
            dependencies.update({
                'ownership:config':{'file:config.toml','account:aenv'},
                'ownership:aenv':{'archive:runtime-assets','file:api-key','file:uvm-ublk-daemon','account:aenv'},
                'setup:assets':{'file:server','file:config.toml','file:aenv.env','archive:runtime-assets','file:uvm-ublk-daemon','ownership:aenv'},
                'setup:host':{'setup:assets','account:aenv'},
                'start:server':{'setup:host','file:aenv.service','file:api-key','file:aenv.env','file:server'}})
        for role in ('scheduler','gateway'):
            if role in roles:dependencies['container:'+role]={'file:'+role+'.json','file:api-key-control'}|({'image:'+role} if 'image:'+role in assets else set())
        return missing_steps(full,observed_steps,dependencies=dependencies,
            replay_safe={'account:aenv','ownership:config','ownership:aenv','setup:assets','setup:host'})
    if installation_state(state,upgrade):return ()
    steps=[]
    if 'server' in roles:
        steps.append(archive_step(assets,'runtime-assets','/var/lib/aenv'))
        steps.append(command('account:aenv','useradd','--system','--user-group','--home-dir','/var/lib/aenv','--create-home','--shell','/usr/sbin/nologin','aenv'))
        steps.extend([file_step(assets,'config.toml','/etc/aenv/config.toml',mode=0o640),
            file_step(assets,'aenv.env','/etc/aenv/aenv.env',mode=0o600),
            file_step(assets,'api-key','/var/lib/aenv/secrets/api-key',mode=0o600),
            command('ownership:config','chown','root:aenv','/etc/aenv/config.toml')])
        steps.extend(file_step(assets,name,'/usr/local/bin/'+name) for name in ('server','aenv','aenv-buildctl'))
        steps.extend([file_step(assets,'uvm-ublk-daemon','/var/lib/aenv/ublk/uvm-ublk-daemon'),
            command('ownership:aenv','chown','-R','aenv:aenv','/var/lib/aenv'),
            command('setup:assets','/usr/local/bin/server','--setup-only',network='none',
                environment={'AENV_HOME_PATH':'/var/lib/aenv','AENV_CONFIG_PATH':'/etc/aenv/config.toml','AENV_UBLK_DAEMON_BINARY_PATH':'/var/lib/aenv/ublk/uvm-ublk-daemon'}),
            command('setup:host','/usr/local/bin/server','--setup-host','--runtime-user','aenv','--runtime-group','aenv',network='none',
                environment={'AENV_HOME_PATH':'/var/lib/aenv','AENV_CONFIG_PATH':'/etc/aenv/config.toml','AENV_UBLK_DAEMON_BINARY_PATH':'/var/lib/aenv/ublk/uvm-ublk-daemon'}),
            file_step(assets,'aenv.service','/etc/systemd/system/aenv.service',mode=0o644),
            command('systemd:reload','systemctl','daemon-reload'),command('start:server','systemctl','start','aenv.service')])
    for role in ('scheduler','gateway'):
        if role in roles:
            image=pinned_image((images or {}).get(role))
            if 'image:'+role in assets:
                from .common import image_load_step
                steps.append(image_load_step(assets,role,image))
            if not any(step.step_id=='file:api-key-control' for step in steps):
                from dataclasses import replace
                steps.append(replace(file_step(assets,'api-key','/etc/aenv/api-key',mode=0o600),step_id='file:api-key-control'))
            steps.append(file_step(assets,role+'.json','/etc/aenv/'+role+'.json',mode=0o644))
            steps.append(RecipeStep('container:'+role,'owned-container',(),target='aenv-'+role,
                environment={'schema_version':1,'product':'agentenv','role':role,'name':'aenv-'+role,
                    'image':image,'mounts':[{'source':'/etc/aenv/'+role+'.json','target':'/config/default.json'},
                        {'source':'/etc/aenv/api-key','target':'/run/secrets/api-key'}],
                    'args':['-config','/config/default.json']}))
    return tuple(steps)


def control_config(nodes,*,multi_host):
    ids=[row['id'] for row in nodes]
    if not ids or len(set(ids))!=len(ids):raise ValueError('unique nonempty node IDs required')
    for row in nodes:
        if set(row)!={'id','endpoint'}:raise ValueError('upstream node fields are id and endpoint')
        url=urlsplit(row['endpoint'])
        if url.scheme not in ('http','https') or not url.hostname or url.username or url.password:
            raise ValueError('explicit non-secret node URL required')
        if multi_host and url.hostname in {'localhost','127.0.0.1','::1','server','agentenv'}:
            raise ValueError('cross-host node endpoint required')
    return {'scheduler':{'discovery':{'mode':'static'},'nodes':[dict(row) for row in nodes]}}
