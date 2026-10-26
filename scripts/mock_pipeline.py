#!/usr/bin/env python3
"""离线 mock 全链路执行器（参数化，可复用）。

真图/真 worker/真证据/真 Excel，假产品 API。用于在笔记本上按指定“密度”压图。
`run_mock(progress=cb)`：每完成一个 worker tick 调 `cb(status)`。
"""
import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import InMemorySaver
from tests.orchestration.conftest import MemoryRepository, FakeClock
from tests.benchmark.unit.test_contract_models import sample
from tests.benchmark.unit.test_session import Scenario
from sandbox_bench.session import BenchSession
from sandbox_bench.load import OperationResult
from sandbox_bench.bindings import RegisteredBenchmarks
from sandbox_bench.bridge import BenchGateway, work_handlers, delivery_finalizer
from sandbox_bench.workflow_services import AnalysisReportServices
from sandbox_graph.domain.models import ScriptRef
from sandbox_graph.planning.compiler import Case, Selection, PlanCompiler
from sandbox_graph.interfaces.service import RunService, QueryService
from sandbox_graph.artifacts.store import FileArtifactStore
from sandbox_graph.services.reviews import ReviewService
from sandbox_graph.services.environment import EnvironmentHandlers, EnvironmentSnapshot
from sandbox_graph.runtime.runner import OperationService, RunnerStatus
from sandbox_graph.runtime.bootstrap import build_job_handlers
from sandbox_graph.runtime.worker import Worker, WorkerPolicy
from sandbox_graph.runtime.agents import ContextBudget, AgentGateway
from sandbox_graph.analysis_dump import dump_case_evidence, dump_analysis_exports
from sandbox_graph.viz import graph_nodes

NODE_INDEX = graph_nodes()
WORK_NODES = tuple(f'N{i:02}' for i in range(11))

# 资源观测合同（脚本化采集，真走 N04/N06/N07 与 metrics/resources）
RESOURCE_CONTRACT = {'interval_s': 0.01, 'window_s': 0.05, 'max_wait_s': 1.0,
                     'max_gap_s': 0.05, 'min_samples': 2, 'inventory_ref': 'scripted-inventory'}


def scripted_inventory(environment_ref):
    return {'node': 'node-1', 'boot_id': 'boot-1', 'evidence_ref': 'scripted-inventory',
            'processes': [
                {'identity': {'pid': 4000 + i, 'environment': environment_ref, 'node': 'node-1',
                              'boot_id': 'boot-1', 'start_ticks': 100 + i},
                 'roles': ['data'], 'components': ['cube-runtime'],
                 'sandbox_id': f's{i}', 'generation': 'gen-1',
                 'pss_bytes': (48 + 8 * i) * 1024 * 1024, 'cpu_seconds': 0.05 + 0.01 * i}
                for i in range(2)]}


class ScriptedResourceSampler:
    """脚本化采样子：产出真实形状的 resource_sample（PSS/CPU），走真封存与分析路径。"""
    def __init__(self, processes, clock_domain, run_id):
        self.inventory = list(processes)      # 与 ResourceSampler 一致：inventory 是进程列表
        self.clock_domain = clock_domain
        self.run_id = run_id
        self.safe_to_submit = True
        self._seq = 1          # 真采样器 next_seq 从 1 开始；records() 要求从 1 连续
        self._pending = []

    def sample_once(self):
        now = time.monotonic()
        seq = self._seq
        self._seq += 1
        processes = []
        for item in self.inventory:
            processes.append({**item, 'expected_identity': item['identity'], 'ended_s': now,
                              'pss_bytes': item['pss_bytes'], 'cpu_seconds': item['cpu_seconds'] + seq * 0.01})
        row = {'schema_version': 'resource_sample.v1', 'kind': 'resource_sample', 'seq': seq,
               'sample_id': f'{self.run_id}:{seq}', 'clock_domain': self.clock_domain,
               'started_s': now, 'ended_s': now, 't_s': now, 'processes': processes,
               'sampler_wall_seconds': 0.0, 'sampler_cpu_seconds': 0.0,
               'poll_requests': 0, 'renewal_requests': 0, 'inventory_complete': True,
               'inventory_version': 'scripted:1', 'inventory_source_ref': 'scripted',
               'snapshot_cohort_ids': [p['sandbox_id'] for p in processes]}
        self._pending = [row]
        return row

    def reconcile(self, sink):
        ok = all(bool(sink(row)) for row in self._pending)
        self._pending = []
        return {'status': 'reconciled' if ok else 'failed'}

    def close(self):
        return None


async def scripted_steady(sampler, resources):
    """脚本化稳态检查：返回已达成稳态窗口（真代码会读采样序列判稳）。"""
    return {'status': 'completed', 'window_kind': 'steady', 'samples': max(1, resources.min_samples),
            'clock_domain': sampler.clock_domain}


def snapshot_status(repo):
    status = {g: {} for g in NODE_INDEX}
    with repo.transaction() as tx:
        for row in tx.scan('receipts'):
            ident = row['body']['identity']
            node = ident['node_id']
            st = row['body']['result']['execution_status']
            for g, nodes in NODE_INDEX.items():
                if node in nodes:
                    prev = status[g].get(node)
                    status[g][node] = st if prev in (None, 'succeeded') else prev
    return status


class EnvironmentFixture:
    def __init__(self):
        self.configured = set()
        self.calls = []

    def snapshot(self, scope, ref):
        ok = scope in self.configured
        return EnvironmentSnapshot(scope=scope, connected=True, authenticated=True,
            identity_confirmed=True, state='satisfied' if ok else 'drift', source_ref=ref,
            differences=() if ok else ('fixture configuration absent',))

    async def connect(self, scope, ref):
        self.calls.append(('connect', scope))
        return self.snapshot(scope, ref)

    async def inspect(self, scope, ref):
        self.calls.append(('inspect', scope))
        return self.snapshot(scope, ref)

    async def submit(self, operation):
        self.calls.append(('configure', operation.scope))
        self.configured.add(operation.scope)
        return RunnerStatus(state='completed', authoritative=True)


class FailedScenario(Scenario):
    async def execute(self, index):
        self.calls.append(index)
        return OperationResult('failed', value={'operation_id': str(index), 'status': 'failed', 'elapsed_s': .1,
            'completion': {'status': 'failed', 'reason': 'api_rejected', 'latency': None}})  # 被接纳前拒绝


class MockScenario(Scenario):
    """带争用的合成场景：并发越高单次耗时越高（合成模型，非真实产品）。

    用于让规格矩阵在不同 C/Q 下产生**可区分**的格子；数值是建模出来的，不是真测量。
    """
    def __init__(self):
        super().__init__()
        self._active = 0

    async def execute(self, index):
        self.calls.append(index)
        self._active += 1
        try:
            await asyncio.sleep(0.002)              # 让并发真正重叠
            elapsed = 0.01 + 0.01 * self._active    # 基础 10ms + 每活跃实例 10ms 争用
            return OperationResult('success', value={'operation_id': str(index),
                'elapsed_s': round(elapsed, 4), 'status': 'success',
                'completion': {'status': 'success', 'reason': None, 'latency': round(elapsed, 4), 'polls': []}})
        finally:
            self._active -= 1


async def run_mock(progress=None, out=None, store_root=None, *, groups=2, works=2, requests=5,
                   concurrency=2, max_work_concurrency=2, vcpus=2, memory_bytes=2 * 1024 ** 3,
                   fail_last_group=False, on_tick=None, model_gateway=None, case_files=None,
                   agent_allowed_fields=(), agent_fields_by_node=None, agent_accessible_evidence=()):
    store_root = Path(store_root or tempfile.mkdtemp(prefix='sandbox-mock-store-'))
    repo = MemoryRepository(FakeClock())
    store = FileArtifactStore(store_root / 'artifacts')
    reviews = ReviewService(repo, store)
    bench_script = ScriptRef(script_id='benchmark.step', version='1', digest='a' * 64)
    config_script = ScriptRef(script_id='fixture.configure', version='1', digest='b' * 64)
    gateway = BenchGateway({}, bench_script)
    environment = EnvironmentFixture()
    operations = OperationService(repo, gateway, {('benchmark.step', '1'): bench_script})
    configs = OperationService(repo, environment, {('fixture.configure', '1'): config_script})

    generated=[]
    if case_files is not None:
        from sandbox_bench.casegen import load_case_file
        if not case_files: raise ValueError('at least one complete case file required')
        generated=[item for path in case_files for item in load_case_file(path)]
        if any(item.case.graph_ref!='graph1' for item in generated):
            raise ValueError('mock runtime implements graph1; select that explicit graph_ref')
        group_names=list(dict.fromkeys(item.case.group_id for item in generated))
        works=len(generated);groups=len(group_names)
        inputs=[(group_names.index(item.case.group_id),item.case.group_id,index,item)
            for index,item in enumerate(generated)]
    else:
        group_names = [f'G_{i + 1}' for i in range(groups)]
        base, extra = divmod(works, groups)
        inputs=[(gi,group,wi,None) for gi,group in enumerate(group_names)
            for wi in range(base+(1 if gi<extra else 0))]

    cases, templates, scenarios = [], {}, {}
    for gi,group,wi,item in inputs:
        ref = f'{group}-w{wi}'
        if item is None:
            body = sample()
            body.update(group_id=group, environment_ref='fixture:' + group, work_id='template:' + ref,
                spec={'vcpus': vcpus, 'memory_bytes': memory_bytes},resources=RESOURCE_CONTRACT,
                load={**sample()['load'], 'requests': requests, 'concurrency': concurrency})
        else:
            body=item.case.model_dump(mode='json')
        config = {'product':body['product'], 'api_url':'http://localhost:1', 'data_url':'http://localhost:1',
            'source':{'kind':body['source']['kind'],'id':body['source']['id']},
            'create_body':{'timeout':body['ttl_s'],'metadata':{'kuasar-sandbox.resource':
                json.dumps({'capacity':{'cpu':body['spec']['vcpus'],
                    'memory':f"{body['spec']['memory_bytes'] // (1024**3)}GiB"}})}},
            'source_evidence':{'source_id':body['source']['id'],'source_digest':body['source']['digest'],
                'environment_ref':body['environment_ref'],'cache_profile':body['cache_profile'],
                'effective_spec':body['spec'],'evidence_ref':'synthetic-source'},
            'environment_evidence':{'ref':body['environment_ref'],'product_version':body['product_version'],
                'evidence_kind':'synthetic'}}
        if item is not None:
            planning,template=item.register(config=config,priority='base',capability='supported',
                adapter_verified=True,conflict_domains=(f'host:{group}:w{wi}',))
            cases.append(planning);templates[item.template_ref]=template
        else:
            templates[ref]={'case':body,'config':config}
            cases.append(Case(capability='supported',adapter_verified=True,case_id=ref,group_id=group,
                sandbox_type='kuasar',scenario_id='B01',method_id='B01.TEMPLATE',priority='base',
                conditions=({'case_template_ref':ref},),conflict_domains=(f'host:{group}:w{wi}',),
                contract_ref='contracthash',environment_ref='fixture:'+group,
                artifact_kind='sandbox_artifact',launch_path='cold_boot',producer_step='pre_existing'))

    def session_factory(case, config, folder):
        failing = fail_last_group and case.group_id == group_names[-1]
        scenario = FailedScenario() if failing else MockScenario()
        scenarios.setdefault(case.group_id, scenario)
        domain = f'scripted:{case.work_id}'
        sampler = ScriptedResourceSampler(scripted_inventory(case.environment_ref)['processes'], domain, case.work_id)
        return BenchSession(case, config, folder, scenario=scenario, sampler=sampler,
                            steady_check=scripted_steady, clock_domain=domain)

    registry = RegisteredBenchmarks(repo, gateway, templates, store_root / 'sessions', session_factory=session_factory)
    analysis = AnalysisReportServices(repo, reviews, store, deadline=3600)

    def services(token, activation):
        mapping = EnvironmentHandlers(repo, environment, deadline=3600, operation_service=configs,
            configuration_script=config_script, configuration_verified=True).mapping()
        mapping.update(analysis.mapping())
        mapping['T2'] = registry.validate_conditions
        if activation.scope in gateway.sessions:
            mapping.update(work_handlers(operations, token, registry.session(activation.scope),
                run_id=activation.run_id, scope=activation.scope, dispatch_generation=1, deadline=3600))
        return mapping

    saver = InMemorySaver()
    handlers = build_job_handlers(repo, graph_ref='graph1', saver_factory=lambda token: saver,
        node_service_factory=services, review_service=reviews, deadline=3600, lease_seconds=60,
        segment_seconds=30, delivery_finalizer=delivery_finalizer(gateway.sessions, store),
        operation_service=operations, max_work_concurrency=max_work_concurrency,
        agent_budget=ContextBudget(max_input_bytes=8192,max_output_bytes=8192,max_calls=2,max_tokens=2000,deadline=10**9),
        agent_gateway=AgentGateway(repo,model_gateway,registered_actions=set()) if model_gateway is not None else None,
        agent_allowed_fields=agent_allowed_fields,
        agent_fields_by_node=agent_fields_by_node,
        agent_accessible_evidence=agent_accessible_evidence)
    worker = Worker(repo, handlers, policy=WorkerPolicy(claim_seconds=60, job_seconds=55, poll_seconds=1))

    api = RunService(repo, PlanCompiler(cases))
    plan = api.preview(Selection(), authorized_groups=set(group_names), graph_ref='graph1', contract_ref='contracthash')
    run = api.start('start', plan.hash, authorized_groups=set(group_names))

    ticks, max_ticks, idle_streak, max_idle = 0, 40 + works * 6, 0, 30
    while ticks < max_ticks:
        ticks += 1
        progressed = await worker.run_once()
        if progressed:
            idle_streak = 0
            repo.clock.advance(.001)
        else:
            # 无到期任务：推进时钟让未来 due_at 的唤醒可领取，不提前收工。
            idle_streak += 1
            repo.clock.advance(1.0)
            if idle_streak > max_idle:
                break
        if progress:
            progress(snapshot_status(repo), ticks)
        if on_tick:
            await on_tick(ticks)
    worker.pump_outbox()
    status = snapshot_status(repo)
    if progress:
        progress(status, ticks)

    summary = {'run_id': run.run_id, 'plan_hash': plan.hash, 'ticks': ticks,
               'groups': group_names, 'works': works, 'requests_per_work': requests,
               'max_work_concurrency': max_work_concurrency,
               'spec': {'vcpus': vcpus, 'memory_bytes': memory_bytes},
               'artifact_store': str(store.root), 'status': status}
    if generated:
        summary['case_inputs']=[item.provenance for item in generated]
        summary.pop('requests_per_work');summary.pop('spec')
    with repo.transaction() as tx:
        summary['work_states'] = {r['key']: (r['body'].get('group_id'), r['body']['status'], r['body'].get('queue_reason')) for r in tx.scan('work_instances')}
        summary['jobs_detail'] = [{'kind': j['body']['job_kind'], 'status': j['body']['status'], 'scope': j['body'].get('scope_ref')} for j in tx.scan('jobs')]
        summary["leases"] = {r["key"]: (r["body"].get("scope"), r["body"].get("status"), r["body"].get("reason")) for r in tx.scan("resource_leases")}
        summary['waits'] = [{'wait': r['body']['record'].get('scope'), 'status': r['body']['status']} for r in tx.scan('waits')]
        summary['agent_supervision'] = len(tx.scan('agent_supervision'))
        summary['agent_reviews'] = [r['body'] for r in tx.scan('agent_reviews')]
        summary['node_reviews'] = [r['body'] for r in tx.scan('node_reviews')]
    if out is not None:
        out = Path(out)
        if out.exists():
            shutil.rmtree(out)
        out.mkdir(parents=True, exist_ok=True)
        cases_written = dump_case_evidence(repo, store, out=out / 'case-evidence', run_id=run.run_id)
        exports_written = dump_analysis_exports(repo, store, out=out / 'exports', run_id=run.run_id)
        q = QueryService(repo).status(run.run_id)
        summary.update({'jobs': [{'kind': j['job_kind'], 'status': j['status']} for j in q['jobs']],
                        'deliveries': q['deliveries'], 'analyses': q['analyses'], 'archives': q['archives'],
                        'written': {'case_evidence': sorted(cases_written), 'exports': sorted(exports_written)}})
        (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return summary
