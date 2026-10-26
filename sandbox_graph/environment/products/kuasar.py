"""Native Kuasar services, zero semantics and file-based group import."""
import copy
from .common import absolute,archive_step,command,file_step,installation_state,roles_exact

AUTH_HEADER='X-API-Key'
ROLE_COMPONENTS={'conductor':('node-ctl',),'proxy':('node-ctl',),'runtime':('sandbox-ctl','connector'),
    'registry':('cluster-ctl',),'router':('cluster-ctl',),'placer':('cluster-ctl',),
    'store':('store-ctl',),'cache':('cache-ctl',)}
UNITS={'conductor':'node-ctl','proxy':'node-proxy','registry':'cluster-registry',
    'router':'cluster-router','placer':'cluster-placer','store':'store-ctl','cache':'cache-ctl'}


def effective_limits(config):
    capacity=config.get('sandbox',{}).get('capacity',0)
    admission=config.get('resource_listen',{}).get('admission',{})
    execution=config.get('builder',{}).get('admission',{}).get('execution',{})
    builds=execution.get('max_builds',2)
    if type(builds) is not int or builds<=0:raise ValueError('builder max_builds must be positive')
    if type(capacity) is not int or capacity<0:raise ValueError('invalid sandbox capacity')
    result={'sandbox_capacity':capacity or None,'admission_rate':admission.get('rate',0) or 4,
        'admission_burst':admission.get('burst',0) or 16,'queue_max_depth':admission.get('queue_max_depth',0) or 256,
        'max_builds':builds}
    if any(type(value) is not int or value<=0 for key,value in result.items() if value is not None):
        raise ValueError('invalid admission configuration')
    return result


def install_recipe(*,roles,assets,state,upgrade=False,observed_steps=None):
    roles_exact(roles,ROLE_COMPONENTS)
    if state=='partial' and observed_steps is not None:
        from .common import missing_steps
        full=install_recipe(roles=roles,assets=assets,state='absent')
        ids={step.step_id for step in full}
        dependencies={}
        for role in roles:
            if role not in UNITS:continue
            inputs={'file:'+name for name in ROLE_COMPONENTS[role]}
            inputs.update({'file:'+role+'.yaml','file:'+UNITS[role]+'.service'})
            validator='validate:'+role
            if validator in ids:
                dependencies[validator]=inputs.copy()
                inputs.add(validator)
            dependencies['start:'+role]=inputs
        return missing_steps(full,observed_steps,dependencies=dependencies)
    if installation_state(state,upgrade):return ()
    binaries=set()
    for role in roles:binaries.update(ROLE_COMPONENTS[role])
    steps=[file_step(assets,name,'/usr/bin/'+name) for name in sorted(binaries)]
    if 'runtime' in roles:
        steps.extend([file_step(assets,'guest-runtime','/var/lib/kuasar/guest-runtime'),
            archive_step(assets,'runtime-assets','/var/lib/kuasar/assets')])
    # No store generation is initialized by an ordinary install/re-entry.
    for role in ('store','cache','registry','placer','router','conductor','proxy'):
        if role not in roles:continue
        unit=UNITS[role]+'.service'
        directory='/etc/node-ctl' if role in ('conductor','proxy') else '/etc/cluster-ctl' if role in ('registry','router','placer') else '/etc/'+role+'-ctl'
        steps.append(file_step(assets,role+'.yaml',directory+'/'+role+'.yaml',mode=0o600))
        steps.append(file_step(assets,unit,'/etc/systemd/system/'+unit,mode=0o644))
        if role in ('conductor','proxy'):
            steps.append(command('validate:'+role,'/usr/bin/node-ctl','config',role,'--config','/etc/node-ctl/'+role+'.yaml','--resolve'))
        elif role in ('registry','router','placer'):
            steps.append(command('validate:'+role,'/usr/bin/cluster-ctl','config',role,'--config',directory+'/'+role+'.yaml'))
    steps.append(command('systemd:reload','systemctl','daemon-reload'))
    for role in ('store','cache','registry','placer','router','conductor','proxy'):
        if role in roles:steps.append(command('start:'+role,'systemctl','start',UNITS[role]+'.service'))
    return tuple(steps)


def group_import(config,*,source_id,directory):
    absolute(directory)
    if not source_id or any(c.isspace() for c in source_id):raise ValueError('source identity required')
    result=copy.deepcopy(config);rows=result.setdefault('import_groups',[])
    identities=[row['source_id'] for row in rows]
    if len(set(identities))!=len(identities):raise ValueError('duplicate group source')
    desired={'source_type':'file','source_id':source_id,'path':directory}
    if source_id in identities:
        if rows[identities.index(source_id)]!=desired:raise ValueError('existing group source differs')
    else:rows.append(desired)
    return result


def cluster_group_recipe(*,config,groups_by_source,record,pair,source_id,directory,
                         config_target,expected_before,output):
    """Freeze a complete owned file source, then activate it through config CAS.

    Input snapshots must include every configured source. This renderer does not
    claim that placer has imported or distributed the keys; an independent
    protected canary remains mandatory after validation and service restart.
    """
    import hashlib
    import json
    from pathlib import Path
    import re
    from ...domain.models import canonical_hash
    from .common import RecipeStep
    from .server_auth import kuasar_group,private_asset
    absolute(directory);absolute(config_target)
    if not isinstance(source_id,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',source_id):
        raise ValueError('explicit file source identity required')
    if expected_before is not None and not re.fullmatch('[0-9a-f]{64}',expected_before):
        raise ValueError('frozen placer config digest required')
    updated=copy.deepcopy(config);sources=updated.setdefault('import_groups',[])
    original_sources=copy.deepcopy(sources)
    identities=[row['source_id'] for row in sources]
    if (len(set(identities))!=len(identities) or set(groups_by_source)!=set(identities)
            or any(row.get('source_type')!='file' for row in sources)):
        raise ValueError('complete unique file source snapshots required')
    seen={};owned=[]
    for identity,records in groups_by_source.items():
        if not isinstance(records,list) or len(records)>4096:raise ValueError('bounded group snapshot required')
        for value in records:
            group=value.get('group')
            if not isinstance(group,str) or not group or group in seen:raise ValueError('duplicate or missing group identity')
            seen[group]=identity
        if identity==source_id:owned=copy.deepcopy(records)
    desired=kuasar_group(record,pair=pair,existing_groups=[])
    group=desired['group']
    if group in seen:
        if seen[group]!=source_id or next(row for row in owned if row['group']==group)!=desired:
            raise ValueError('existing group differs; explicit rotation or migration required')
    else:owned.append(desired)
    records={hashlib.sha256(row['group'].encode()).hexdigest()+'.json':row for row in owned}
    payload=json.dumps(records,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    generation=hashlib.sha256(payload).hexdigest()
    destination=str(Path(directory)/generation)
    entry={'source_id':source_id,'source_type':'file','path':destination}
    if source_id in identities:sources[identities.index(source_id)]=entry
    else:sources.append(entry)
    root=Path(output)
    # All conflicts are checked before creating private assets.
    group_asset=private_asset(root/'groups.json',payload)
    activation={'configuration':updated,'sources':[{'path':row['path'],'records':groups_by_source[row['source_id']]}
        for row in original_sources],'generation':{'target':destination,'asset':group_asset}}
    config_asset=private_asset(root/'activation.json',json.dumps(activation,sort_keys=True,allow_nan=False).encode())
    steps=(RecipeStep('publish:kuasar-groups','kuasar-groups',(),asset=group_asset,target=destination,mode=0o700),
        RecipeStep('file:placer-groups','kuasar-group-config',(),asset=config_asset,target=config_target,mode=0o600),
        command('validate:placer','/usr/bin/cluster-ctl','config','placer','--config',config_target),
        command('restart:placer','systemctl','restart','cluster-placer.service'))
    return {'steps':steps,'expected_before':{destination:None,config_target:expected_before},
        'source_id':source_id,'generation':generation,'config_hash':canonical_hash(updated),
        'evidence_level':'rendered_only','requires':['protected_canary','effective_source_probe']}
