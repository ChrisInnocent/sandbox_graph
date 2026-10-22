"""Registered benchmark steps exposed through D11 RunnerGateway.

BenchGateway is a single-host execution endpoint. OperationService must authorize
and fence graph dispatch before calling it. It never creates a new session from
arbitrary shell, a model message, or a request-supplied directory.
"""
from __future__ import annotations
import time
from sandbox_graph.domain.models import NodeResult, ErrorRecord, Operation, canonical_hash
from sandbox_graph.runtime.runner import RunnerStatus, RunnerCollection, CleanupResult


class BenchGateway:
    def __init__(self,sessions,script_ref):
        self.sessions=dict(sessions);self.script_ref=script_ref

    def _session(self,scope):
        if scope not in self.sessions:raise PermissionError('benchmark scope not registered')
        return self.sessions[scope]

    def _binding(self,operation_id,scope):
        session=self._session(scope)
        row=session.journal.state().get('gateway_operations',{}).get(operation_id)
        if row is None:raise KeyError('unknown operation')
        if row['scope']!=scope:raise PermissionError('operation scope mismatch')
        return session,row

    def _status(self,session,result,step=None):
        state=result.get('status','unknown')
        mapped={'completed':'completed','ready':'completed','valid':'completed','failed':'failed',
                'blocked':'failed','incomplete':'failed','cancelled':'cancelled','running':'running'}.get(state,'unknown')
        artifacts=tuple(str(session.folder/result['artifact_ref']) for _ in [0] if result.get('artifact_ref'))
        resources=tuple(f"{session.environment}:{r['kind']}:{r['resource_id']}"
                        for r in session.journal.owned(session.environment)) if step=='N05' else ()
        return RunnerStatus(state=mapped,authoritative=mapped in ('completed','failed','cancelled'),
            task_handle=session.case.work_id,resource_refs=resources,artifact_refs=artifacts,
            output={k:v for k,v in result.items() if k not in ('analysis','manifest','resources','cleanup')},
            reason=result.get('reason'))

    async def submit(self,operation):
        if operation.script_ref!=self.script_ref:raise PermissionError('script version/digest not registered')
        session=self._session(operation.scope)
        if set(operation.input)!={'step_id','case_hash'} or operation.input['case_hash']!=session.case.digest():
            raise ValueError('benchmark immutable input mismatch')
        step=operation.input['step_id']
        if step!=operation.node_id:raise ValueError('node/step mismatch')
        value={'scope':operation.scope,'input_hash':operation.input_hash,'step_id':step,
               'case_hash':session.case.digest(),'script':operation.script_ref.model_dump(mode='json')}
        with session.journal._transaction() as state:
            operations=state.setdefault('gateway_operations',{})
            if operation.operation_id in operations and operations[operation.operation_id]!=value:
                raise ValueError('operation ID reused with different input')
            operations[operation.operation_id]=value
        result=await session.apply_step(step,operation.input_hash,visit_id=operation.operation_id)
        return self._status(session,result,step)

    async def reconcile(self,operation_id,scope):
        session,row=self._binding(operation_id,scope)
        result=session.journal.receipt(row['step_id']+':'+operation_id,row['input_hash'])
        if result is None or result.get('status')=='unknown':
            result=await session.reconcile(row['step_id'],discover=True)
        return self._status(session,result,row['step_id'])

    async def cancel(self,operation_id,scope,reason):
        session,_=self._binding(operation_id,scope)
        await session.cancel(reason)
        return RunnerStatus(state='unknown',authoritative=False,reason='admission_stopped_external_effects_require_reconcile')

    async def collect(self,operation_id,scope,last_seq=0):
        session,_=self._binding(operation_id,scope)
        if type(last_seq) is not int or last_seq<0:raise ValueError('invalid cursor')
        events=session.collect(last_seq)
        latest=events[-1]['seq'] if events else last_seq
        return RunnerCollection(events=tuple(events),last_seq=latest)

    async def cleanup(self,scope,resource_ids):
        session=self._session(scope)
        allowed={f"{session.environment}:{r['kind']}:{r['resource_id']}" for r in session.journal.owned(session.environment)}
        if not set(resource_ids)<=allowed:raise PermissionError('cleanup foreign resource denied')
        # Dedicated recovery API uses a fresh receipt identity to catch late resources.
        # The ordinary N09 node also performs this ownership check through session ledger.
        result=await session._N09(resource_ids={x.removeprefix(session.environment+':') for x in resource_ids})
        remaining=tuple(f"{session.environment}:source:{x['resource_id']}" for x in result['cleanup'] if x['status']=='retained')
        remaining+=tuple(f"{session.environment}:sandbox:{x['resource_id']}" for x in result['cleanup'] if x['status']=='unknown')
        return CleanupResult(status='incomplete' if remaining else 'unknown',resource_ids=tuple(sorted(resource_ids)),
                             remaining=remaining,evidence_refs=(str(session.folder/'ledger/journal.json'),))


def work_handlers(operation_service,token,session,*,run_id,scope,dispatch_generation,deadline):
    """N00–N10 handlers; persistent OperationService guards every submitted step."""
    handlers={}
    for i in range(11):
        node=f'N{i:02}'
        async def handle(context,node=node):
            if context.scope!=scope:raise PermissionError('node scope mismatch')
            body={'step_id':node,'case_hash':session.case.digest()}
            operation=Operation(operation_id=canonical_hash({'visit':context.visit_id,'node':node,'scope':scope}),
                input_hash=canonical_hash(body),script_ref=operation_service.gateway.script_ref,
                scope=scope,input=body,deadline=deadline,node_id=node,run_id=run_id,
                dispatch_generation=dispatch_generation,
                submission_kind='cleanup' if node in ('N07','N09','N10') else 'ordinary')
            status=await operation_service.submit(operation,token)
            original=status.output.get('status')
            execution='succeeded' if status.state=='completed' else 'blocked' if original=='blocked' else 'cancelled' if status.state=='cancelled' else 'failed'
            return NodeResult(execution_status=execution,data_status=status.output.get('data_status','unverified'),
                coverage_status='attempted',cleanup_status='unknown' if node=='N09' else 'not_needed',
                operation_refs=(operation.operation_id,),artifact_refs=status.artifact_refs,
                resource_refs=status.resource_refs,
                error=None if execution=='succeeded' else ErrorRecord(code='BENCHMARK_STEP_'+status.state.upper(),
                    effect_certainty='unknown' if status.state=='unknown' else 'none',observed_at=time.time(),
                    message=status.reason or 'benchmark step did not succeed'),
                output=status.output)
        handlers[node]=handle
    return handlers


def delivery_finalizer(sessions,artifact_store,*,repository=None,script_ref=None,reuse_policies=None):
    """Copy verified sealed evidence into the graph store before publishing delivery.

    Work execution scope stays work-specific; artifact/review scope is its registered
    environment group. A directory path alone is never accepted as verified delivery.
    """
    from copy import deepcopy
    policies=deepcopy(reuse_policies or {})
    if policies and (repository is None or script_ref is None):
        raise ValueError('reuse producer requires the work repository and registered script identity')
    async def finalize(job,graph_result):
        from pathlib import Path
        import json
        from sandbox_bench.analysis import verify_case,recompute_case
        from sandbox_graph.domain.models import DeliveryManifest
        scope=job['scope_ref']
        if scope not in sessions:raise PermissionError('unregistered work session')
        session=sessions[scope];case=session.case
        if scope!=case.work_id or job['payload']['plan_ref']!=case.plan_ref:
            raise ValueError('job and benchmark identity differ')
        group=job['payload'].get('group_id',case.group_id)
        if group!=case.group_id:raise PermissionError('delivery group mismatch')
        await session._N10()
        root=session.folder/'evidence';manifest=verify_case(root)
        if manifest['case_id']!=case.work_id or any(manifest['metadata'].get(k)!=v for k,v in {
                'case_hash':case.digest(),'plan_ref':case.plan_ref,'contract_ref':case.contract_ref}.items()):
            raise ValueError('sealed evidence identity differs from current work/contract')
        summary=recompute_case(root)
        refs={};index={}
        for name in sorted(set(manifest['files'])|{'case_manifest.json'}):
            path=root/name
            with path.open('rb') as stream:
                ref=artifact_store.commit(iter(lambda:stream.read(1024*1024),b''),scope=group,
                    media_type='application/octet-stream',producer='benchmark.session.v1')
            refs[ref.artifact_id]=ref;index[name]=ref.model_dump(mode='json')
        archive_index={'case_hash':case.digest(),'case_id':case.work_id,'paths':index}
        index_ref=artifact_store.commit((json.dumps(archive_index,sort_keys=True).encode(),),scope=group,
            media_type='application/octet-stream',producer='benchmark.session.v1')
        refs[index_ref.artifact_id]=index_ref
        states=[r['execution_status'] for r in graph_result.get('results',{}).values()]
        execution='failed' if 'failed' in states else 'blocked' if 'blocked' in states else 'cancelled' if 'cancelled' in states else 'succeeded'
        data=summary.get('qualifications',{}).get('status','unverified')
        if data not in ('valid','invalid','insufficient','unverified'):data='unverified'
        output={'case_index_ref':index_ref.artifact_id,'case_hash':case.digest()}
        policy=policies.get(case.work_id)
        if policy is not None:
            from sandbox_graph.services.reuse import ReuseContract
            from .identity import measurement_contract_hash
            if (not isinstance(policy,dict) or set(policy)!={'case_hash','valid_until','evidence_ref'}
                    or policy['case_hash']!=case.digest()
                    or not isinstance(policy['evidence_ref'],str) or not policy['evidence_ref'].strip()
                    or type(policy['valid_until']) not in (float,int)):
                raise ValueError('reuse policy must bind this exact case and explicit freshness evidence')
            with repository.transaction() as tx:
                row=tx.get('work_instances',case.work_id)
                if not row:raise ValueError('reuse source work identity unavailable')
                work=row['body'];key=work['key']
                bound=tx.get('measurement_bindings',case.work_id)
                expected_binding={'work_id':case.work_id,'run_id':job.get('run_id'),'plan_ref':case.plan_ref,
                    'case_hash':case.digest(),'measurement_contract_hash':measurement_contract_hash(case),
                    'script_ref':canonical_hash(script_ref)}
                if not bound or bound['body']!=expected_binding:
                    raise ValueError('reuse producer requires the exact registered case/script binding')
                if (work.get('run_id')!=job.get('run_id') or work['plan_ref']!=case.plan_ref
                        or work['group_id']!=case.group_id or work.get('environment_ref')!=case.environment_ref
                        or key['method_id']!=case.method_id or key['method_version']!=case.method_version
                        or key['repeat_id']!=case.repeat_id or key['group_id']!=case.group_id
                        or not policy['valid_until']>tx.now):
                    raise ValueError('reuse policy does not bind the current unexpired work identity')
            contract=ReuseContract(environment_ref=case.environment_ref,spec_hash=canonical_hash(case.spec),
                method_ref=f'{case.method_id}:{case.method_version}',completion_event=case.completion.endpoint,
                time_domain=session.clock_domain,script_ref=canonical_hash(script_ref),
                condition_hash=key['condition_hash'],valid_until=policy['valid_until'],
                measurement_contract_hash=measurement_contract_hash(case))
            if execution=='succeeded' and data=='valid':
                proof={'case_hash':case.digest(),'work_key':key,'policy':policy,
                       'script':script_ref.model_dump(mode='json'),'contract':contract.model_dump(mode='json')}
                ref=artifact_store.commit((json.dumps(proof,sort_keys=True,allow_nan=False).encode(),),
                    scope=group,media_type='application/octet-stream',producer='benchmark.session.v1')
                refs[ref.artifact_id]=ref
                output.update(reuse_contract=contract.model_dump(mode='json'),reuse_policy_ref=ref.artifact_id)
            else:
                output['reuse_status']='ineligible_execution_or_data'
        operations=session.journal.state()['operations']
        unknown=any(op['status'] in ('submission_started','unknown','timeout') for op in operations.values())
        gaps=[]
        gaps.extend(summary.get('qualifications',{}).get('gaps',[]))
        if data!='valid':gaps.append('measurement_data_'+data)
        if unknown:gaps.append('external_operations_unresolved')
        cleanup=session.journal.state().get('cleanup',[])
        # issue #3：只有“未验证物理释放”或“有 journal 归属却无已核实释放行”才算 gap。
        # 同步删除（Cube/AgentENV）会得到 physical_release='verified'，不应再阻挡租约回收。
        released={row['resource_id'] for row in cleanup if row.get('physical_release')=='verified'}
        unresolved=[row for row in cleanup if row.get('physical_release')!='verified']
        owned_ids={row['resource_id'] for row in session.journal.owned(session.environment)}
        if unresolved or (owned_ids-released):gaps.append('physical_resource_release_unverified')
        result=NodeResult(execution_status=execution,data_status=data,coverage_status='attempted',
            cleanup_status='unknown' if gaps else 'not_needed',
            performance_status='not_evaluated',artifact_refs=tuple(refs),
            output=output)
        return DeliveryManifest(delivery_id=canonical_hash({'work_id':case.work_id,'case':case.digest()}),
            version=1,scope=group,plan_ref=case.plan_ref,contract_ref=case.contract_ref,
            work_refs=(case.work_id,),evidence=tuple(refs.values()),result=result,gaps=tuple(gaps))
    return finalize
