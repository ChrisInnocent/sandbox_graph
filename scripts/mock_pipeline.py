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
        return OperationResult('failed', value={'operation_id': str(index), 'status': 'failed', 'elapsed_s': .1})


async def run_mock(progress=None, out=None, store_root=None, *, groups=2, works=2, requests=5,
                   max_work_concurrency=2, vcpus=2, memory_bytes=2 * 1024 ** 3,
                   fail_last_group=False, on_tick=None, model_gateway=None,
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

    group_names = [f'G_{i + 1}' for i in range(groups)]
    base, extra = divmod(works, groups)          # 12 个 / 3 组 => 每组 4 个
    per_group = [base + (1 if i < extra else 0) for i in range(groups)]

    cases, templates, scenarios = [], {}, {}
    for gi, group in enumerate(group_names):
        for wi in range(per_group[gi]):
            ref = f'{group}-w{wi}'
            body = sample()
            body.update(group_id=group, environment_ref='fixture:' + group, work_id='template:' + ref,
                        spec={'vcpus': vcpus, 'memory_bytes': memory_bytes},
                        load={**sample()['load'], 'requests': requests})
            config = {'product': 'kuasar', 'api_url': 'http://localhost:1', 'data_url': 'http://localhost:1',
                'source': {'kind': body['source']['kind'], 'id': body['source']['id']},
                'create_body': {'timeout': 100, 'metadata': {'kuasar-sandbox.resource':
                    json.dumps({'capacity': {'cpu': vcpus, 'memory': f'{memory_bytes // (1024**3)}GiB'}})}},
                'source_evidence': {'source_id': body['source']['id'], 'source_digest': body['source']['digest'],
                    'environment_ref': body['environment_ref'], 'cache_profile': body['cache_profile'],
                    'effective_spec': body['spec'], 'evidence_ref': 'synthetic-source'},
                'environment_evidence': {'ref': body['environment_ref'], 'product_version': body['product_version'],
                    'evidence_kind': 'synthetic'}}
            templates[ref] = {'case': body, 'config': config}
            # 每个 work 一个 Case、各自独立冲突域：避免同一 Case 的所有 work 共享域而被互相阻塞。
            cases.append(Case(capability='supported', adapter_verified=True, case_id=ref, group_id=group,
                sandbox_type='kuasar', scenario_id='B01', method_id='B01.TEMPLATE', priority='base',
                conditions=({'case_template_ref': ref},),
                conflict_domains=(f'host:{group}:w{wi}',),
                contract_ref='contracthash', environment_ref='fixture:' + group,
                artifact_kind='sandbox_artifact', launch_path='cold_boot', producer_step='pre_existing'))

    def session_factory(case, config, folder):
        failing = fail_last_group and case.group_id == group_names[-1]
        scenario = FailedScenario() if failing else Scenario()
        scenarios.setdefault(case.group_id, scenario)
        return BenchSession(case, config, folder, scenario=scenario)

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
