"""Static case-template registry connecting selected work to benchmark sessions."""
from copy import deepcopy
import json
from pathlib import Path
from sandbox_graph.domain.models import NodeResult, canonical_hash
from sandbox_graph.services.workflows import token_for
from .contracts import CaseContract
from .runner import digest
from .identity import measurement_contract_hash


class RegisteredBenchmarks:
    """Templates/configs are supplied by deployment, never imported from a model.

    A finite plan condition selects `case_template_ref`. Only work identity and
    plan/group bindings are filled after expansion; measurement parameters remain
    frozen in the referenced template and configuration.
    """
    def __init__(self, repository, gateway, templates, root, *, session_factory=None):
        self.repository = repository
        self.gateway = gateway
        self.templates = deepcopy(templates)
        self.fingerprint = digest(self.templates)
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if session_factory is None:
            from .wiring import configured_session
            session_factory = configured_session
        self.factory = session_factory

    def session(self, work_id):
        if digest(self.templates) != self.fingerprint:
            raise ValueError('registered templates changed after deployment')
        with self.repository.transaction() as tx:
            row = tx.get('work_instances', work_id)
            if not row:
                raise KeyError('unregistered work')
            work = row['body']
        condition = work['condition']
        if set(condition) != {'case_template_ref'}:
            raise ValueError('measurement conditions must be frozen in a registered case template')
        ref = condition['case_template_ref']
        if ref not in self.templates:
            raise ValueError('case template not registered')
        template = self.templates[ref]
        body = deepcopy(template['case'])
        provenance=template['config'].get('case_input_provenance')
        if provenance is not None:
            from .casegen import verify_provenance
            verify_provenance(CaseContract.model_validate_json(json.dumps(body)),provenance)
        if (body['group_id'] != work['group_id'] or body['method_id'] != work['key']['method_id']
                or body['contract_ref'] != work['contract_ref']
                or body['method_version'] != work['key'].get('method_version', '1')
                or (work.get('environment_ref') and body['environment_ref'] != work['environment_ref'])):
            raise ValueError('template group/method/version/contract differs from plan')
        body.update(work_id=work_id, plan_ref=work['plan_ref'], repeat_id=work['key'].get('repeat_id',1))
        case = CaseContract.model_validate_json(json.dumps(body))
        binding={'work_id':work_id,'run_id':work['run_id'],'plan_ref':work['plan_ref'],
                 'case_hash':case.digest(),'measurement_contract_hash':measurement_contract_hash(case),
                 'script_ref':canonical_hash(self.gateway.script_ref)}
        with self.repository.transaction() as tx:
            tx.put('measurement_bindings',work_id,binding,immutable=True)
        existing = self.gateway.sessions.get(work_id)
        if existing:
            if existing.case != case or digest(existing.config) != digest(template['config']):
                raise ValueError('registered work session input changed')
            return existing
        # Work IDs are opaque; a stable hash is the sole local path component.
        session = self.factory(case, deepcopy(template['config']), self.root/canonical_hash(work_id))
        self.gateway.sessions[work_id] = session
        return session

    async def validate_conditions(self, context):
        with self.repository.transaction() as tx:
            rows = [r['body'] for r in tx.scan('work_instances')
                    if r['body']['run_id'] == context.run_ref and r['body']['plan_ref'] == context.plan_ref
                    and r['body']['group_id'] == context.scope and r['body']['status'] == 'pending']
        ready, gaps = [], []
        for work in rows:
            try:
                session = self.session(work['work_id'])
                ready.append({'work_ref':work['work_id'], 'case_hash':session.case.digest()})
            except (KeyError, ValueError, OSError) as exc:
                gap = {'work_ref':work['work_id'], 'code':'CASE_BINDING_MISSING_OR_INVALID',
                       'error_type':type(exc).__name__}
                gaps.append(gap)
                with self.repository.transaction() as tx:
                    tx.assert_fence(token_for(context))
                    row = tx.get('work_instances', work['work_id'])
                    if row['body']['status'] == 'pending':
                        tx.put('work_instances', work['work_id'], row['body'] | {
                            'status':'blocked', 'queue_reason':gap['code']})
                    tx.put('case_binding_gaps',work['work_id'],gap,immutable=True)
        record = {'run_id':context.run_ref, 'plan_ref':context.plan_ref, 'group_id':context.scope,
                  'registry_hash':self.fingerprint, 'ready':ready, 'gaps':gaps}
        identity = canonical_hash(record)
        with self.repository.transaction() as tx:
            tx.assert_fence(token_for(context))
            tx.put('case_bindings',identity,record,immutable=True)
        return NodeResult(execution_status='succeeded', data_status='unverified',
                          continuation='T4' if ready else 'T6',
                          output={'bindings_ref':identity, 'ready_count':len(ready), 'gap_count':len(gaps)})
