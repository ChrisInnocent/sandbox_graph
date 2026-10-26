"""Read-only file and service discovery. Readiness needs independent group proofs."""
import hashlib
import json
import stat
import subprocess
import re
from pathlib import Path

from . import product_adapter
from .common import absolute,roles_exact

UNITS={
    'cubesandbox':{'control':('cube-sandbox-cubeops.service','cube-sandbox-cubemaster.service',
        'cube-sandbox-cube-api.service','cube-sandbox-cube-proxy.service','cube-sandbox-cube-templatecenter.service'),
        'compute':('cube-sandbox-cubelet.service',)},
    'kuasar':{'conductor':('node-ctl.service',),'proxy':('node-proxy.service',),
        'runtime':(),'registry':('cluster-registry.service',),'router':('cluster-router.service',),
        'placer':('cluster-placer.service',),'store':('store-ctl.service',),'cache':('cache-ctl.service',)},
    'agentenv':{'server':('aenv.service',),'scheduler':(),'gateway':()}}


def _file(path,expected):
    if path is None:return {'status':'unknown','reason':'explicit_component_path_required'}
    absolute(path);target=Path(path)
    try:
        info=target.lstat()
        if not stat.S_ISREG(info.st_mode) or target.resolve()!=target:
            return {'status':'unknown','reason':'component_path_not_regular'}
        checksum=hashlib.sha256()
        with target.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):checksum.update(chunk)
        if target.stat().st_mtime_ns!=info.st_mtime_ns:return {'status':'unknown','reason':'file_changed_during_probe'}
        actual=checksum.hexdigest()
        return {'status':'matched' if expected==actual else 'unverified_version' if expected is None else 'mismatch',
            'sha256':actual,'size':info.st_size,'mode':stat.S_IMODE(info.st_mode),'path':path}
    except FileNotFoundError:return {'status':'missing','path':path}
    except OSError:return {'status':'unknown','reason':'file_read_unavailable','path':path}


def probe_product(product,*,roles,paths,expected_hashes,runner=subprocess.run):
    adapter=product_adapter(product);roles_exact(roles,adapter.ROLE_COMPONENTS)
    file_roles=[role for role in roles if product!='agentenv' or role=='server']
    files={name:_file(paths.get(name),expected_hashes.get(name))
        for name in sorted({name for role in file_roles for name in adapter.ROLE_COMPONENTS[role]})
        if not (product=='cubesandbox' and name=='cubeproxy')}
    services=[]
    for unit in sorted({unit for role in roles for unit in UNITS[product][role]}):
        try:
            result=runner(['systemctl','show',unit,'--property=LoadState,ActiveState,SubState,MainPID'],
                check=False,capture_output=True,text=True,timeout=10,shell=False)
            fields=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
            if result.returncode or not {'LoadState','ActiveState','MainPID'}<=fields.keys():
                services.append({'unit':unit,'status':'unknown'});continue
            services.append({'unit':unit,'status':'running' if fields['ActiveState']=='active' else
                'missing' if fields['LoadState']=='not-found' else 'stopped',
                'pid':int(fields['MainPID'])})
        except (OSError,ValueError,subprocess.TimeoutExpired):services.append({'unit':unit,'status':'unknown'})
    containers=[]
    container_roles=[role for role in ('scheduler','gateway') if product=='agentenv' and role in roles]
    if product=='cubesandbox' and 'control' in roles:container_roles.append('cubeproxy')
    if container_roles:
        for role in container_roles:
            try:
                name=paths.get('container:'+role,'cube-proxy' if role=='cubeproxy' else 'aenv-'+role)
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',name):raise ValueError('explicit safe container name required')
                result=runner(['docker','inspect',name],
                    check=False,capture_output=True,text=True,timeout=10,shell=False)
                if result.returncode:
                    containers.append({'role':role,'status':'unknown'});continue
                if len(result.stdout)>1024*1024:raise ValueError('container response exceeds byte budget')
                rows=json.loads(result.stdout)
                if not isinstance(rows,list) or len(rows)!=1:raise ValueError('ambiguous container identity')
                actual=rows[0];image=actual['Image'];expected=expected_hashes.get(role)
                matched=False
                if expected and '@sha256:' in expected:
                    inspected=runner(['docker','image','inspect',image],check=False,capture_output=True,text=True,timeout=10,shell=False)
                    details=json.loads(inspected.stdout) if inspected.returncode==0 else []
                    matched=bool(len(details)==1 and details[0].get('Id')==image and expected in details[0].get('RepoDigests',[]))
                elif expected:matched=image==(expected if expected.startswith('sha256:') else 'sha256:'+expected)
                state=('unverified_version' if expected is None else 'mismatch' if not matched else
                    'running' if actual['State']['Status']=='running' else 'stopped')
                containers.append({'role':role,'status':state,'image':image,'expected_image':expected,
                    'container_name':name,'container_id':actual.get('Id'),'pid':actual.get('State',{}).get('Pid')})
            except (OSError,ValueError,KeyError,TypeError,subprocess.TimeoutExpired):containers.append({'role':role,'status':'unknown'})
    states=[value['status'] for value in files.values()]+[row['status'] for row in services+containers]
    installation=('unknown' if 'unknown' in states else 'absent' if states and all(x=='missing' for x in states)
        else 'installed' if states and all(x in {'matched','running'} for x in states) else 'partial')
    return {'product':product,'roles':list(roles),'installation':installation,'functional_ready':False,
        'files':files,'services':services,'containers':containers,
        'remaining_proofs':['host_prerequisites','effective_configuration','registration','authentication','canary_cleanup']}
