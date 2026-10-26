"""Audited parameter-to-file edits; never mutate guest specs or infer unlimited.

Outputs are preparation assets for the existing file CAS/recipe/grant pipeline.
A rendered file is not runtime-effective evidence: independent probes remain required.
"""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import yaml
import re
import tomllib
from .config import _flatten,_set_path,load_config
from .contracts import TargetValue
from .parameters import ParameterRule,resolve_parameter
from .products.common import file_step
from .products.literal_env import render_literal_env,parse_literal_env

# Paths follow the source-locked product profiles, not an arbitrary model patch.
RULES={
 ('kuasar','conductor'):{
    'sandbox_capacity':('sandbox.capacity','count',0,2**31-1),
    'admission_rate':('resource_listen.admission.rate','requests/s',0,2**31-1),
    'admission_burst':('resource_listen.admission.burst','count',0,2**31-1),
    'queue_max_depth':('resource_listen.admission.queue_max_depth','count',0,2**31-1),
    'max_builds':('builder.admission.execution.max_builds','count',1,2**31-1),
    'runner_pool_size':('units.runner_pool_size','count',0,2**31-1),
    'builder_pool_size':('units.builder_pool_size','count',0,2**31-1)},
 ('cubesandbox','cubelet'):{
    'quota_cpu':('host.quota.mcpu_limit','mCPU',0,2**31-1),
    'max_sandboxes':('host.quota.mvm_limit','count',0,2**31-1),
    'create_concurrency':('host.quota.creation_concurrent_num','count',0,2**31-1)},
 ('cubesandbox','CubeAPI'):{
    'api_rate':('RATE_LIMIT_PER_SEC','requests/s',0,2**32-1),
    'worker_threads':('WORKER_THREADS','count',0,2**31-1)},
 ('cubesandbox','CubeMaster'):{
    'max_sandboxes':('scheduler.node_max_mvm_num','count',0,2**31-1),
    'reserve_ratio':('scheduler.node_max_mvm_num_reserve_num_percent','ratio',0,1),
    'reserved_memory':('scheduler.node_max_mem_reserved_in_mb','MiB',0,2**31-1),
    'create_concurrency':('cubelet_conf.create_concurrent_limit','count',0,2**31-1)},
 ('agentenv','server'):{
    'max_builds':('template_build.max_concurrent_builds','count',1,2**31-1),
    'firecracker_pool_concurrency':('pool.firecracker.fill_concurrency','count',1,2**31-1)},
 ('agentenv','scheduler'):{
    name:('scheduler.node_resource_limit.'+name,unit,0,maximum) for name,unit,maximum in (
       ('max_sandbox_count','count',2**32-1),('max_sandbox_starting_count','count',2**32-1),
       ('max_cpu_used_percent','%',2**32-1),('max_cpu_allocated_percent','%',2**32-1),
       ('max_memory_used_percent','%',2**32-1),('max_memory_allocated_percent','%',2**32-1),
       ('max_sandbox_count_including_paused','count',2**32-1),
       ('max_allocated_cpu_including_paused','CPU',2**32-1),
       ('max_allocated_memory_bytes_including_paused','B',2**64-1))}}


def tune(product,component,values,targets,*,budgets=None):
    rules=RULES.get((product,component))
    if rules is None or set(targets)-rules.keys():raise ValueError('unsupported audited component parameter')
    result=deepcopy(values);flat=_flatten(values);resolutions=[]
    for name,raw in targets.items():
        target=TargetValue.model_validate(raw)
        path,unit,low,high=rules[name]
        # Preserve includes omission/nil, which are meaningful product defaults.
        if target.mode=='preserve':continue
        rule=ParameterRule(path,'platform_limit',unit,low,high)
        budget=None
        if target.mode=='relax_for_plan':
            budget=(budgets or {}).get(target.budget_ref)
            if not budget or budget.get('budget_ref')!=target.budget_ref:
                raise ValueError('exact recorded relaxation budget required')
        resolved=resolve_parameter(rule,mode=target.mode,value={'value':target.value,'unit':target.unit or unit},
            budget=budget,observed={'value':flat[path],'unit':unit} if type(flat.get(path)) in (int,float) else None)
        if unit=='ratio':value=float(resolved.value)
        else:
            if type(resolved.value) is not int and not resolved.value.is_integer():raise ValueError('product field requires an integer')
            value=int(resolved.value)
        if (product,component)==('cubesandbox','CubeAPI'):value=str(value)
        # A nil scheduler limit means absent restrictions, not a malformed object.
        if product=='agentenv' and result.get('scheduler',{}).get('node_resource_limit','absent') is None:
            result['scheduler']['node_resource_limit']={}
        _set_path(result,path,value);resolutions.append(asdict(resolved))
    return {'values':result,'resolved':resolutions,'evidence_level':'rendered_only'}


def _patch_toml(raw,values,paths):
    lines=raw.decode().splitlines(keepends=True);flat=_flatten(values)
    if lines and not lines[-1].endswith('\n'):lines[-1]+='\n'
    for path in paths:
        prefix,key=path.rsplit('.',1);table=None;found=False;insert_at=None
        for index,line in enumerate(lines):
            match=re.match(r'^\s*\[([^\[\]]+)\]\s*(?:#.*)?$',line.strip())
            if match:
                if table==prefix and insert_at is None:insert_at=index
                table=match[1].strip()
            if table==prefix and re.match(r'^\s*'+re.escape(key)+r'\s*=',line):
                comment=(' #'+line.split('#',1)[1].rstrip()) if '#' in line else ''
                lines[index]=key+' = '+json.dumps(flat[path])+comment+'\n';found=True;break
        if not found:
            if insert_at is not None:lines.insert(insert_at,key+' = '+json.dumps(flat[path])+'\n')
            elif table==prefix:lines.append(key+' = '+json.dumps(flat[path])+'\n')
            else:lines.extend(['\n['+prefix+']\n',key+' = '+json.dumps(flat[path])+'\n'])
    result=''.join(lines).encode()
    if tomllib.loads(result.decode())!=values:raise ValueError('native TOML edit did not preserve the parsed configuration')
    return result


def prepare_asset(product,component,source,output,*,targets,budgets=None,remote_path):
    """Write a private digest-locked staging asset and return its CAS recipe input."""
    source=Path(source);output=Path(output)
    if source.is_symlink() or source.resolve()!=source.absolute():raise ValueError('canonical source file required')
    with source.open('rb') as stream:before=stream.read(1048577)
    if len(before)>1048576:raise ValueError('configuration source exceeds byte limit')
    is_env=(product,component)==('cubesandbox','CubeAPI')
    values=parse_literal_env(before) if is_env else load_config(source)
    result=tune(product,component,values,targets,budgets=budgets)
    if is_env:payload=render_literal_env(result['values'])
    elif source.suffix=='.json':payload=(json.dumps(result['values'],sort_keys=True,indent=2,allow_nan=False)+'\n').encode()
    elif source.suffix in {'.yaml','.yml'}:payload=yaml.safe_dump(result['values'],sort_keys=True).encode()
    elif source.suffix=='.toml':payload=_patch_toml(before,result['values'],[row['path'] for row in result['resolved']])
    else:raise ValueError('this audited component requires JSON/YAML or literal env input')
    with source.open('rb') as stream:current=stream.read(1048577)
    if current!=before:raise ValueError('configuration source changed during rendering')
    output=output.absolute();output.parent.mkdir(parents=True,exist_ok=True)
    if output.is_symlink() or output.resolve()!=output:raise ValueError('canonical output required')
    if output==source:raise ValueError('staging output cannot overwrite source')
    from .products.server_auth import private_asset
    asset=private_asset(output,payload)
    step=file_step({'tuned-config':asset},'tuned-config',remote_path,mode=0o600)
    return {'asset':asset,'step':asdict(step),'expected_before':{remote_path:hashlib.sha256(before).hexdigest()},
        'resolved':result['resolved'],'evidence_level':'rendered_only'}


def prepare_ublk_asset(output,*,target,expected_config_sha256=None):
    """Persist the next-load setting separately; activation refuses loaded drift.

    The caller supplies this recipe through the existing grant/CAS pipeline.
    No service stop, device deletion or module unload is implied.
    """
    from .products.server_auth import private_asset
    from .products.common import RecipeStep
    if type(target) is not int or not 1<=target<2**32:raise ValueError('positive finite ublk limit required')
    if expected_config_sha256 is not None and not re.fullmatch('[a-f0-9]{64}',expected_config_sha256):
        raise ValueError('explicit current configuration hash required')
    asset=private_asset(Path(output).absolute(),('options ublk_drv ublks_max='+str(target)+'\n').encode())
    destination='/etc/modprobe.d/sandbox-graph-ublk.conf'
    steps=(file_step({'ublk-config':asset},'ublk-config',destination,mode=0o644),
        RecipeStep('ublk:activate','ublk-limit',(),target=str(target),timeout_s=60))
    return {'steps':[asdict(step) for step in steps],'expected_before':{destination:expected_config_sha256},
        'evidence_level':'rendered_only','activation_requires':'module_absent_or_current_limit_equal'}
