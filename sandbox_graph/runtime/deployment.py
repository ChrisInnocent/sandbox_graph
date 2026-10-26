"""Production assembly for accepted environments and frozen benchmark case files.

No model-supplied Python/module/shell is loaded. Environment preparation runs in
its own environment:1 graph; this deployment consumes its verified acceptance.
"""
import hashlib
from pathlib import Path
from types import SimpleNamespace
from ..domain.models import ScriptRef,canonical_hash
from ..planning.compiler import PlanCompiler,Selection
from ..interfaces.service import RunService,QueryService
from ..artifacts.store import FileArtifactStore
from ..services.reviews import ReviewService
from ..services.environment import EnvironmentHandlers,EnvironmentSnapshot
from ..environment.acceptance import AcceptanceService,EnvironmentAcceptanceGate,PREFIX
from ..environment.cli import LocalReferences
from ..environment.probes.environment import RemoteEnvironmentProbe
from ..environment.probes.registration import RuntimeRegistrationProbe
from ..environment.verification import ProductVerification
from ..environment.transport import SSHTransport
from .runner import OperationService
from .bootstrap import build_job_handlers
from .worker import Worker,WorkerPolicy


class PreparedEnvironmentProbe:
    def __init__(self,gate):self.gate=gate
    async def connect(self,scope,environment_ref):return await self.inspect(scope,environment_ref)
    async def inspect(self,scope,environment_ref):
        try:
            body=self.gate.service.read(environment_ref)
            if body['declaration']['group_id']!=scope:raise PermissionError('environment group differs')
            await self.gate.check(None,[SimpleNamespace(environment_ref=environment_ref)])
            return EnvironmentSnapshot(scope=scope,connected=True,authenticated=True,identity_confirmed=True,
                state='satisfied',source_ref=environment_ref,
                roles=tuple(sorted({role for node in body['declaration']['nodes'] for role in node['roles']})))
        except (OSError,ValueError,PermissionError):
            return EnvironmentSnapshot(scope=scope,connected=False,authenticated=False,identity_confirmed=False,
                state='unknown',source_ref=environment_ref,diagnostics=('fresh_environment_acceptance_failed',))


class EvaluationDeployment:
    def __init__(self,repository,runtime,*,saver_factory,lifecycle_runtime=None,bundle_ref=None):
        if not getattr(repository,'is_test_fixture',False) and not getattr(repository,'dsn',None):
            raise ValueError('production evaluation requires PostgreSQL')
        required={'schema_version','graph_ref','contract_ref','authorized_groups','cases','environment_runtime','session_root','artifact_root'}
        if not required<=set(runtime) or runtime['schema_version']!=1:raise ValueError('incomplete versioned evaluation runtime')
        if not runtime['cases'] or not runtime['authorized_groups']:raise ValueError('explicit finite case/group selection required')
        self.repository=repository;self.runtime=runtime;self.runtime_hash=canonical_hash(runtime)
        self.store=FileArtifactStore(runtime['artifact_root']);self.reviews=ReviewService(repository,self.store)
        from sandbox_bench.casegen import load_case_file
        from sandbox_bench.bridge import BenchGateway,delivery_finalizer,work_handlers
        from sandbox_bench.bindings import RegisteredBenchmarks
        from sandbox_bench.workflow_services import AnalysisReportServices
        import sandbox_bench
        root=Path(sandbox_bench.__file__).parent
        digest=canonical_hash({path.relative_to(root).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(root.rglob('*.py'))})
        script=ScriptRef(script_id='benchmark.step',version='1',digest=digest)
        gateway=BenchGateway({},script);operations=OperationService(repository,gateway,{(script.script_id,script.version):script})
        cases=[];templates={}
        for row in runtime['cases']:
            for loaded in load_case_file(row['case_file']):
                if (loaded.case.graph_ref!=runtime['graph_ref'] or loaded.case.contract_ref!=runtime['contract_ref']
                        or not loaded.case.environment_ref.startswith(PREFIX)):
                    raise ValueError('case must bind the deployed graph/contract and versioned environment acceptance')
                if row['config'].get('environment_evidence',{}).get('evidence_kind') in {'synthetic','fixture'}:
                    raise ValueError('synthetic product evidence is not a live evaluation binding')
                case,template=loaded.register(config=row['config'],priority=row['priority'],capability=row['capability'],
                    adapter_verified=row['adapter_verified'],conflict_domains=tuple(row['conflict_domains']))
                if loaded.template_ref in templates:raise ValueError('duplicate case template')
                cases.append(case);templates[loaded.template_ref]=template
        import sandbox_graph
        controller=Path(sandbox_graph.__file__).parent
        code_hash=canonical_hash({path.relative_to(controller).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(controller.rglob('*.py'))})
        self.fingerprint=canonical_hash({'runtime':self.runtime_hash,'templates':templates,
            'benchmark':digest,'controller':code_hash})
        self.cases=tuple(cases);self.api=RunService(repository,PlanCompiler(cases))
        registry=RegisteredBenchmarks(repository,gateway,templates,runtime['session_root'])
        acceptance=AcceptanceService(repository,store=self.store)
        env=runtime['environment_runtime'];resolver=LocalReferences(env['references']);transport=SSHTransport(helper_paths=env.get('helper_paths'))
        def probe_factory(scope):
            probe=RemoteEnvironmentProbe(transport=transport,resolver=resolver,store=self.store,scope=scope)
            registered=RuntimeRegistrationProbe(probe=probe,bindings=env['registration']) if env.get('registration') else None
            probe.verification=ProductVerification(probe=probe,repository=repository,scope=scope,store=self.store,runtime=registered)
            return probe
        self.gate=EnvironmentAcceptanceGate(acceptance,probe_factory=probe_factory)
        environment=PreparedEnvironmentProbe(self.gate)
        def services(token,activation):
            if canonical_hash(self.runtime)!=self.runtime_hash:raise ValueError('evaluation deployment changed after startup')
            with repository.transaction() as tx:
                bound=tx.get('evaluation_deployments',activation.run_id)
                if not bound or bound['body']['runtime_hash']!=self.fingerprint:raise PermissionError('worker deployment differs from frozen run')
                work=tx.get('work_instances',activation.scope)
                run=tx.get('runs',activation.run_id)['body']
            deadline=self.activation_deadline(activation)
            mapping=EnvironmentHandlers(repository,environment,deadline=deadline).mapping()
            mapping.update(AnalysisReportServices(repository,self.reviews,self.store,deadline=deadline).mapping())
            mapping['T2']=registry.validate_conditions
            if work:
                session=registry.session(activation.scope)
                mapping.update(work_handlers(operations,token,session,run_id=activation.run_id,scope=activation.scope,
                    dispatch_generation=run['dispatch_generation'],deadline=deadline))
            return mapping
        from .lifecycle_assembly import assemble_lifecycle
        lifecycle=assemble_lifecycle(repository,lifecycle_runtime,store=self.store,resolver=resolver)
        handlers=build_job_handlers(repository,graph_ref=runtime['graph_ref'],saver_factory=saver_factory,
            node_service_factory=services,review_service=self.reviews,deadline=self.activation_deadline,
            lease_seconds=60,segment_seconds=3600,delivery_finalizer=delivery_finalizer(gateway.sessions,self.store),
            operation_service=operations,max_work_concurrency=runtime.get('max_work_concurrency',1),environment_gate=self.gate,**lifecycle)
        bundle_service=None
        if bundle_ref:
            from ..services.worker_bundles import WorkerBundleService
            with repository.transaction() as tx:bound=tx.get('worker_bundles',bundle_ref)
            if not bound:raise ValueError('worker bundle is not registered')
            bundle_service=WorkerBundleService(repository,root=Path(bound['body']['path']).parent)
            if bundle_service.verify(bundle_ref)['graph_ref']!=runtime['graph_ref']:raise PermissionError('deployed graph differs from bundle')
        self.worker=Worker(repository,handlers,owner=runtime.get('worker_id','evaluation-worker'),
            bundle_service=bundle_service,bundle_ref=bundle_ref,
            policy=WorkerPolicy(claim_seconds=60,job_seconds=3650,poll_seconds=1,
                accepted_job_kinds=('graph:all','graph:check','graph:analysis','graph:report','run','group','work','analysis','report',
                    'resume','cleanup','reconcile','lifecycle_stop','supervision',*tuple(k for k in ('lifecycle','agent_team','candidate','candidate_cleanup') if k in handlers))))

        if bundle_ref:self.register_candidate_deployment()

    def register_candidate_deployment(self):
        """Only a worker that imported this immutable bundle may bind its runtime."""
        from ..domain.evolution import CandidateRevision
        from ..services.agent_lifecycle import decode
        from ..services.evolution import EvolutionService
        from ..planning.measurement_generation import load_plan
        self.worker.attest_bundle()
        bundle=self.worker.bundle_service.verify(self.worker.bundle_ref)
        with self.repository.transaction() as tx:
            tx.assert_fence(self.worker.bundle_token)
            candidate=decode(CandidateRevision,tx.get('candidate_revisions',bundle['candidate_ref'])['body'])
            EvolutionService.verify_targets_in(tx,candidate)
            plan=load_plan(tx.get('plans',candidate.target_plan_ref)['body'])
            cases={case.case_id:case for case in self.cases}
            if (plan.graph_ref!=self.runtime['graph_ref'] or plan.contract_ref!=self.runtime['contract_ref']
                    or not set(case.group_id for case in plan.cases)<=set(self.runtime['authorized_groups'])
                    or not plan.cases or any(cases.get(case.case_id)!=case for case in plan.cases)):
                raise PermissionError('candidate deployment does not implement frozen target plan')
            seconds=self.runtime.get('execution_seconds',300.)
            if type(seconds) not in (int,float) or not 0<seconds<=86400:raise ValueError('bounded successor execution_seconds required')
            from ..services.worker_bundles import WorkerBundleService
            WorkerBundleService.register_deployment_in(tx,candidate.candidate_hash,{
                'candidate_hash':candidate.candidate_hash,'bundle_ref':self.worker.bundle_ref,'bundle_hash':canonical_hash(bundle),
                'plan_ref':candidate.target_plan_ref,'contract_ref':candidate.target_contract_ref,'auth_ref':candidate.target_auth_ref,
                'graph_ref':candidate.graph_ref,'runtime_hash':self.fingerprint,'execution_seconds':seconds,
                'worker_id':self.worker.owner})

    def activation_deadline(self,activation):
        with self.repository.transaction() as tx:
            row=tx.get('evaluation_deployments',activation.run_id)
            if not row or row['body']['runtime_hash']!=self.fingerprint:raise PermissionError('run deployment binding changed')
            stage=tx.get('evaluation_stage_deadlines',activation.activation_id)
            if stage:
                if stage['body']['run_id']!=activation.run_id:raise PermissionError('stage execution scope differs')
                return stage['body']['deadline']
            from .tasks import business_job_kind
            jobs=[item['body'] for item in tx.scan('jobs') if item['body'].get('activation_id')==activation.activation_id
                and business_job_kind(item['body'].get('job_kind','')) in {'analysis','report'}]
            if jobs:
                if (len(jobs)!=1 or jobs[0]['run_id']!=activation.run_id
                        or jobs[0]['payload'].get('graph_ref')!=activation.graph_ref
                        or jobs[0]['payload'].get('plan_ref')!=activation.plan_ref):
                    raise PermissionError('publication activation deadline has another scope/version')
                return jobs[0]['deadline']
            return row['body']['deadline']

    def preview(self,selection,*,evidence=None):
        return self.api.preview(Selection.model_validate(selection),authorized_groups=set(self.runtime['authorized_groups']),
            graph_ref=self.runtime['graph_ref'],contract_ref=self.runtime['contract_ref'],evidence=evidence)

    async def start(self,request_id,plan_hash,*,seconds,evidence=None):
        if not 0<seconds<=86400:raise ValueError('explicit bounded evaluation duration required')
        with self.repository.transaction() as tx:
            row=tx.get('plans',plan_hash)
            if not row:raise ValueError('previewed evaluation plan required')
            from ..planning.measurement_generation import load_plan
            plan=load_plan(row['body'])
            replay=tx.get('commands',request_id)
        if not replay and plan.stage not in {'analysis','report'}:await self.gate.check(None,plan.cases)
        run=self.api.start(request_id,plan_hash,authorized_groups=set(self.runtime['authorized_groups']),execution_seconds=seconds,
            evidence=evidence,deployment_binding={'runtime_hash':self.fingerprint,'graph_ref':self.runtime['graph_ref']})
        return run

    def status(self,run_id):return QueryService(self.repository).status(run_id)
