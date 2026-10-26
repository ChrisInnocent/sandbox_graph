"""Complete, bounded case-file generation. No template overlay or hidden base."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
from itertools import product
import json
from pathlib import Path
import re

import yaml

from .conditions import SpecProfile
from .contracts import CaseContract
from .identity import measurement_contract_hash
from sandbox_graph.domain.models import canonical_hash
from sandbox_graph.environment.declarations import _UniqueKeyLoader, _unique_json_object

CONDITIONS=('method_id','method_version','spec','completion','load','density','background',
    'resources','resource_observation_waived','repeat_id','topology','endpoint_kind','readiness_signal',
    'ttl_s','cleanup_timeout_s','cache_profile','clone_count','source_must_run','target_node',
    'workload_ref','declared_metrics','declared_conditions')
MAX_CASES=1024


def normalize_spec(value):
    if isinstance(value,str) or isinstance(value,dict) and 'memory_gib' in value:
        spec=SpecProfile.model_validate(value)
        return {'vcpus':float(spec.vcpus),'memory_bytes':spec.memory_bytes}
    return deepcopy(value)


@dataclass(frozen=True)
class GeneratedCase:
    case: CaseContract
    provenance: dict

    @property
    def template_ref(self):
        return 'casefile:'+canonical_hash(self.provenance)

    def register(self,*,config,priority,capability,adapter_verified,conflict_domains):
        from sandbox_graph.planning.compiler import Case
        case=self.case
        planning=Case(case_id=self.template_ref,group_id=case.group_id,sandbox_type=case.product,
            scenario_id=case.method_id.split('.')[0],method_id=case.method_id,
            method_version=case.method_version,repeat_id=case.repeat_id,priority=priority,
            conditions=({'case_template_ref':self.template_ref},),capability=capability,
            adapter_verified=adapter_verified,conflict_domains=conflict_domains,
            contract_ref=case.contract_ref,environment_ref=case.environment_ref,
            artifact_kind=case.source.artifact_kind,launch_path=case.source.launch_path,
            producer_step=case.source.producer_step,topology=case.topology,
            endpoint_kind=case.endpoint_kind,readiness_signal=case.readiness_signal,endpoint_ref=case.endpoint_ref)
        return planning,{'case':case.model_dump(mode='json'),
            'config':deepcopy(config)|{'case_input_provenance':deepcopy(self.provenance)}}


def generate_cases(raw,*,file_sha256):
    if not isinstance(raw,dict) or not re.fullmatch('[a-f0-9]{64}',file_sha256):
        raise ValueError('complete case mapping and source SHA256 required')
    body=deepcopy(raw);axes=body.pop('axes',{})
    if not isinstance(axes,dict) or set(axes)-{'specs','concurrency','requests','repeat_ids'}:
        raise ValueError('unknown case matrix axis')
    # Existing CaseContract alone defines required fields and method rules.
    if 'spec' in body: body['spec']=normalize_spec(body['spec'])
    base=CaseContract.model_validate_json(json.dumps(body))
    for name,values in axes.items():
        if not isinstance(values,list) or not values or len({canonical_hash(v) for v in values})!=len(values):
            raise ValueError('matrix axes must be nonempty unique lists')
        if name!='specs' and any(type(v) is not int or v<1 for v in values):
            raise ValueError('matrix counts must be positive integers')
    sizes=1
    for values in axes.values(): sizes*=len(values)
    if sizes>MAX_CASES: raise ValueError('case matrix exceeds finite 1024-case budget')
    combinations=product(axes.get('specs',[body['spec']]),axes.get('concurrency',[base.load.concurrency]),
        axes.get('requests',[base.load.requests]),axes.get('repeat_ids',[base.repeat_id]))
    result=[];seen=set()
    for index,(spec,concurrency,requests,repeat) in enumerate(combinations):
        value=deepcopy(body);value['spec']=normalize_spec(spec)
        value['load'].update(concurrency=concurrency,requests=requests);value['repeat_id']=repeat
        if axes:
            value['work_id']=base.work_id+':cell:'+str(index)
        case=CaseContract.model_validate_json(json.dumps(value))
        conditions={field:case.model_dump(mode='json')[field] for field in CONDITIONS}
        identity=canonical_hash(conditions)
        if identity in seen: raise ValueError('matrix axes produce duplicate normalized conditions')
        seen.add(identity)
        provenance={'schema_version':'case_input.v1','file_sha256':file_sha256,
            'case_sha256':case.digest(),'measurement_hash':measurement_contract_hash(case),
            'conditions':conditions,'conditions_hash':identity,'matrix_index':index,'matrix_size':sizes}
        result.append(GeneratedCase(case,provenance))
    return tuple(result)


def load_case_file(path):
    source=Path(path);payload=source.read_bytes()
    if len(payload)>1048576: raise ValueError('case file exceeds 1 MiB')
    text=payload.decode('utf-8')
    if source.suffix.lower()=='.json':
        raw=json.loads(text,object_pairs_hook=_unique_json_object)
    elif source.suffix.lower() in {'.yaml','.yml'}:
        raw=yaml.load(text,Loader=_UniqueKeyLoader)
    else: raise ValueError('case file must be JSON or YAML')
    return generate_cases(raw,file_sha256=hashlib.sha256(payload).hexdigest())


def single_case_file(path):
    items=load_case_file(path)
    if len(items)!=1: raise ValueError('matrix has multiple cases; generate explicit cells before single-case execution')
    return items[0]


def verify_provenance(case,provenance,*,allow_work_binding=False):
    if (not isinstance(provenance,dict) or provenance.get('schema_version')!='case_input.v1'
            or not re.fullmatch('[a-f0-9]{64}',str(provenance.get('file_sha256','')))
            or provenance.get('measurement_hash')!=measurement_contract_hash(case)
            or provenance.get('conditions_hash')!=canonical_hash(provenance.get('conditions'))
            or any(case.model_dump(mode='json')[field]!=provenance['conditions'].get(field) for field in CONDITIONS)
            or (not allow_work_binding and provenance.get('case_sha256')!=case.digest())):
        raise ValueError('case input provenance differs from frozen measurement conditions')
    return deepcopy(provenance)
