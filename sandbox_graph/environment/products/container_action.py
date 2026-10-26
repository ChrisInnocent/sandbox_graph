"""Fixed owned control-container activation and offline image loading.

Invoked as one journaled child, so controller loss keeps the original operation
unknown. It never pulls images or removes a container selected only by name.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from .common import absolute,pinned_image


def _json(runner,argv):
    result=runner(argv,check=False,capture_output=True,text=True,timeout=30,shell=False)
    if result.returncode:raise ValueError('required Docker observation unavailable')
    if len(result.stdout)>1024*1024:raise ValueError('Docker observation byte limit')
    value=json.loads(result.stdout)
    if not isinstance(value,list) or len(value)!=1:raise ValueError('unique Docker identity required')
    return value[0]


def image_identity(image,runner):
    pinned_image(image)
    row=_json(runner,['docker','image','inspect',image])
    if (row.get('Id')!=image if image.startswith('sha256:') else image not in row.get('RepoDigests',[])) or not row.get('Id'):raise ValueError('local image digest differs')
    return row['Id']


def activate(spec,*,expected_id,runner=subprocess.run):
    if set(spec)!={'schema_version','product','role','name','image','mounts','args'} or spec['schema_version']!=1:
        raise ValueError('exact owned container spec required')
    if spec['product']!='agentenv' or spec['role'] not in {'gateway','scheduler'} or spec['name']!='aenv-'+spec['role']:
        raise ValueError('registered control container required')
    if spec['args']!=['-config','/config/default.json']:raise ValueError('fixed control entrypoint required')
    expected_mounts=[{'source':'/etc/aenv/'+spec['role']+'.json','target':'/config/default.json'},
        {'source':'/etc/aenv/api-key','target':'/run/secrets/api-key'}]
    if spec['mounts']!=expected_mounts:raise ValueError('registered read-only mounts required')
    if expected_id is not None and (not isinstance(expected_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',expected_id)):
        raise ValueError('frozen container identity required')
    image=image_identity(spec['image'],runner)
    response=runner(['docker','inspect',spec['name']],check=False,capture_output=True,text=True,timeout=30,shell=False)
    # An inspect error is not absence evidence: independently enumerate exact names.
    if response.returncode:
        listed=runner(['docker','container','ls','--all','--format','{{.Names}}'],check=False,capture_output=True,text=True,timeout=30,shell=False)
        if listed.returncode or len(listed.stdout)>1024*1024 or spec['name'] in listed.stdout.splitlines():
            raise ValueError('container absence unconfirmed')
        current=None
    else:
        if len(response.stdout)>1024*1024:raise ValueError('container observation byte budget')
        rows=json.loads(response.stdout)
        if len(rows)!=1:raise ValueError('ambiguous container identity')
        current=rows[0]
    if (current or {}).get('Id')!=expected_id:raise ValueError('container compare-and-swap differs')
    labels={'sandbox-graph.product':spec['product'],'sandbox-graph.role':spec['role']}
    if current:
        if current.get('Name')!='/'+spec['name'] or any(current.get('Config',{}).get('Labels',{}).get(k)!=v for k,v in labels.items()):
            raise PermissionError('refusing replacement of an unowned container')
        removed=runner(['docker','rm','--force',expected_id],check=False,capture_output=True,text=True,timeout=30,shell=False)
        if removed.returncode:raise RuntimeError('original container removal unresolved')
    argv=['docker','run','--detach','--pull=never','--name',spec['name'],'--restart','unless-stopped','--network','host']
    for key,value in labels.items():argv+=['--label',key+'='+value]
    for mount in spec['mounts']:
        argv+=['--mount','type=bind,src='+mount['source']+',dst='+mount['target']+',readonly']
    argv+=[spec['image'],*spec['args']]
    launched=runner(argv,check=False,capture_output=True,text=True,timeout=60,shell=False)
    if launched.returncode:raise RuntimeError('container creation unresolved')
    try:observed=_json(runner,['docker','inspect',spec['name']])
    except Exception as exc:raise RuntimeError('post-create observation unresolved') from exc
    if (observed.get('Id')!=launched.stdout.strip() or observed.get('Image')!=image
            or observed.get('State',{}).get('Running') is not True
            or any(observed.get('Config',{}).get('Labels',{}).get(k)!=v for k,v in labels.items())):
        raise RuntimeError('new container identity not confirmed')
    return {'container_id':observed['Id'],'image_id':image,'status':'completed'}


def load_image(spec,runner=subprocess.run):
    if set(spec)!={'archive','sha256','image'}:raise ValueError('exact offline image spec required')
    pinned_image(spec['image']);path=Path(absolute(spec['archive']))
    if path.resolve()!=path or not path.is_file():raise ValueError('canonical image archive required')
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    if digest.hexdigest()!=spec['sha256']:raise ValueError('image archive changed')
    try:return {'status':'completed','image_id':image_identity(spec['image'],runner),'reused':True}
    except ValueError:pass
    result=runner(['docker','load','--input',str(path)],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=300,shell=False)
    if result.returncode:raise RuntimeError('image load unresolved')
    try:identity=image_identity(spec['image'],runner)
    except Exception as exc:raise RuntimeError('post-load observation unresolved') from exc
    return {'status':'completed','image_id':identity,'reused':False}


def main():
    try:
        request=json.loads(sys.argv[1])
        if request.get('kind') not in {'docker-image','owned-container'}:raise ValueError('unknown action kind')
        result=load_image(request['spec']) if request['kind']=='docker-image' else activate(request['spec'],expected_id=request['expected_id'])
        print(json.dumps(result));return 0
    except (ValueError,PermissionError):return 2
    except Exception:return 75  # mutation may have reached Docker; original operation must reconcile.

if __name__=='__main__':raise SystemExit(main())
