"""Opt-in EAT-30 SSH/proc/systemd/KVM observations, without product mutations."""
import asyncio
from dataclasses import fields
import hashlib
import json
import math
import os
from pathlib import Path
from ..environment.probes.host import HostFacts,assess_role
from ..environment.transport import SSHRequest,SSHTransport,validate_helper_path


def validate_contract(value):
    required={'schema_version','ssh','helper_path','machine_id','role','require_pss','output','timeout_seconds'}
    if not isinstance(value,dict) or set(value)!=required or value['schema_version']!=1:
        raise ValueError('exact versioned host acceptance contract required')
    if (set(value['ssh'])!={'address','port','user','known_hosts','identity_file'}
            or value['role'] not in {'compute','runtime','server','worker','control','conductor','proxy','scheduler','gateway','registry','router','placer','store','cache'}
            or not isinstance(value['machine_id'],str) or not value['machine_id']
            or type(value['require_pss']) is not bool or type(value['timeout_seconds']) not in (float,int)
            or not math.isfinite(value['timeout_seconds']) or not 0<value['timeout_seconds']<=30):
        raise ValueError('bounded explicit role, host identity and SSH contract required')
    validate_helper_path(value['helper_path'])
    output=Path(value['output'])
    if not output.is_absolute() or output.resolve()!=output:raise ValueError('canonical new private output required')
    return value


def _facts(row):
    value=row['facts']
    if set(value)-{field.name for field in fields(HostFacts)}:raise ValueError('unknown host observation fields')
    for key in ('cpu_count','memory_bytes','disk_bytes'):
        if type(value.get(key)) is not int or value[key]<0:raise ValueError('invalid host observation number')
    for key in ('kvm_device_exists','kvm_module_loaded','kvm_openable','systemd_available','pss_readable'):
        if type(value.get(key)) is not bool:raise ValueError('invalid host observation boolean')
    return HostFacts(**value)


async def run_host_acceptance(value,*,transport=None):
    value=validate_contract(value);output=Path(value['output']);output.mkdir(mode=0o700,parents=True,exist_ok=False)
    transport=transport or SSHTransport(helper_paths={value['ssh']['address']:value['helper_path']})
    rows={};result={'status':'blocked','evidence_scope':'host_primitives_only','role':value['role']}
    def private(name,body):
        raw=json.dumps(body,sort_keys=True,allow_nan=False,indent=2).encode()+b'\n'
        fd=os.open(output/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        return hashlib.sha256(raw).hexdigest()
    async def request(action,payload):
        response=await asyncio.wait_for(transport.request(SSHRequest(**value['ssh'],
            payload={'action':action,'input':payload},timeout_seconds=value['timeout_seconds'])),value['timeout_seconds'])
        if response.returncode or len(response.stdout)>1024*1024:raise ValueError('bounded remote probe did not complete')
        row=json.loads(response.stdout)
        if not isinstance(row,dict):raise ValueError('remote observation object required')
        return row
    try:
        before=rows['before']=await request('probe.access.v1',{'read_only':True})
        if (before.get('machine_id')!=value['machine_id'] or not before.get('boot_id')
                or before.get('can_exec') is not True or type(before.get('uid')) is not int):
            result['reason']='identity_unconfirmed';return result
        host=rows['host']=await request('probe.host.v1',{'role':value['role']})
        after=rows['after']=await request('probe.access.v1',{'read_only':True})
        if any(after.get(key)!=before.get(key) for key in ('machine_id','boot_id','uid','can_exec')):
            result['reason']='identity_changed';return result
        observed=_facts(host)
        # Compute requirements on the controller; remote requirements_met is
        # not accepted as the verdict. This still does not create a guest VM.
        if assess_role(value['role'],observed)['requirements_met'] is not True:
            result['reason']='host_requirements_not_met';return result
        if value['require_pss'] and not observed.pss_readable:
            result['reason']='pss_unavailable';return result
        result.update(status='passed',machine_id=before['machine_id'],boot_id=before['boot_id'])
        return result
    except (OSError,ValueError,KeyError,TypeError,TimeoutError) as exc:
        result.update(reason='host_probe_incomplete',error_type=type(exc).__name__);return result
    finally:
        result['observations_sha256']=private('observations.json',rows)
        private('summary.json',result)
