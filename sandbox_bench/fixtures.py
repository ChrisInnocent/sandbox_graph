"""Fixture assertions, independent of measured request timing and source claims."""
import fcntl
import json
from pathlib import Path
from .runner import atomic_json,digest


def verify_effective_spec(expected,observed):
    keys=('vcpus','memory_bytes')
    if any(observed.get(k) is None for k in keys):return {'status':'unverified','reason':'effective_spec_evidence_missing'}
    mismatch=[key for key in keys if expected.get(key)!=observed[key]]
    return {'status':'invalid' if mismatch else 'valid','mismatch':mismatch,'expected':expected,'observed':observed}


def verify_continuity(before,after,*,mode):
    if mode not in ('memory','disk'):raise ValueError('unknown continuity mode')
    fields=('nonce_sha256','memory_sha256','allocated_bytes') if mode=='memory' else ('files',)
    valid=all(key in before and key in after and before[key]==after[key] for key in fields)
    if mode=='disk' and not before.get('files'):valid=False
    return {'valid':valid,'mode':mode,'assertion':'resident_nonce_and_bytes' if mode=='memory' else 'file_hashes',
            'before_digest':digest(before),'after_digest':digest(after)}


def verify_source(source,evidence,*,environment_ref,cache_profile):
    required={'source_id':source.id,'source_digest':source.digest,'environment_ref':environment_ref,
              'cache_profile':cache_profile}
    mismatch=[k for k,v in required.items() if evidence.get(k)!=v]
    if not evidence.get('evidence_ref'):mismatch.append('evidence_ref')
    return {'status':'valid' if not mismatch else 'invalid','mismatch':mismatch,'evidence_ref':evidence.get('evidence_ref')}


class FirstCommandLedger:
    """Reserve eligibility before a command; unknown replies never restore FIRST."""
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)

    def reserve(self,sandbox_id,generation):
        if not sandbox_id or not generation:raise ValueError('instance generation required')
        key=digest([sandbox_id,generation])
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            state=json.loads(self.path.read_text()) if self.path.exists() else {}
            if key in state:raise ValueError('FIRST already consumed or unknown')
            state[key]={'sandbox_id':sandbox_id,'generation':generation,'status':'reserved_before_command'}
            atomic_json(self.path,state)


def verify_request_spec(case,config):
    """Only audited resource carriers; ignored top-level fields are not evidence."""
    import re
    body=config.get('create_body',{})
    actual=None
    if case.product=='kuasar':
        try:
            value=json.loads(body.get('metadata',{})['kuasar-sandbox.resource'])['capacity']
            memory=value['memory'];match=re.fullmatch(r'(\d+)(KiB|MiB|GiB)',memory)
            if match is None:raise ValueError('explicit binary memory unit required')
            factor={'KiB':1024,'MiB':1024**2,'GiB':1024**3}[match[2]]
            actual={'vcpus':value['cpu'],'memory_bytes':int(match[1])*factor}
        except (KeyError,TypeError,ValueError):
            return {'status':'unverified','reason':'kuasar_explicit_capacity_metadata_required'}
    elif case.product=='agentenv' and case.source.kind=='image':
        memory=body.get('memoryMB');cpu=body.get('cpuCount')
        actual={'vcpus':cpu,'memory_bytes':memory*1024**2 if type(memory) is int else None}
    elif case.product=='cubesandbox' and case.method_id=='B02.FULL':
        build=config.get('build_body',{});cpu=build.get('cpu');memory=build.get('memory')
        actual={'vcpus':cpu/1000 if type(cpu) is int else None,
                'memory_bytes':memory*1024**2 if type(memory) is int else None}
    if actual is None:return {'status':'valid','basis':'source_effective_spec_checked_separately'}
    return verify_effective_spec(case.spec.model_dump(),actual)
