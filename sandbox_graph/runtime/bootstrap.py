"""Explicit, version-pinned assembly of business services and graph workers."""
from ..graphs.factory import build_work,build_definition,build_work_hold
from ..services.workflows import (WorkflowHandlers,run_definition,run_routers,
    group_definition,group_routers,analysis_definition,report_definition,linear_routers)
from .agents import AgentGateway,AgentSupervisor
from ..services.remediation import NodeRemediationService
from .worker import GraphJobHandler,ResumeJobHandler,RecoveryJobHandler,SupervisionJobHandler,RemedyJobHandler


def build_job_handlers(repository,*,graph_ref,saver_factory,node_service_factory,
                       review_service,deadline,lease_seconds,segment_seconds,
                       owner='worker',max_work_concurrency=1,
                       delivery_finalizer=None,operation_service=None,
                       agent_gateway=None,agent_allowed_fields=(),agent_budget=None,agent_allowlists=None,
                       agent_fields_by_node=None,agent_accessible_evidence=(),max_node_attempts=2,
                       lifecycle_bindings=None, model_team=None, agent_provider=None,reconciliation=None,repair_catalog=None,
                       evolution=None,environment_gate=None):
    """Build all five graphs for one immutable deployed graph version.

    node_service_factory(token, activation) supplies registered environment,
    benchmark, analysis, and report adapters for this version. The factory is
    evaluated only after acquiring the actual owner lease. Missing services
    fail graph validation; no successful adapter is synthesized. Use a separate
    version-pinned worker set to drain historical versions.
    """
    from ..catalog.repair_actions import standard_repair_catalog
    if graph_ref=='environment:1':
        from ..environment.workflow import environment_definition,environment_routers
        def environment_builder(executor,checkpointer):
            return build_definition(environment_definition(),executor,routers=environment_routers(),checkpointer=checkpointer)
        def environment_factory(token,activation):
            if activation.graph_ref!=graph_ref:raise ValueError('environment worker version differs')
            services=node_service_factory(token,activation)
            return services.mapping() if hasattr(services,'mapping') else services
        handler=GraphJobHandler(repository,environment_builder,saver_factory,environment_factory,
            lease_seconds=lease_seconds,segment_seconds=segment_seconds,owner=owner)
        return _lifecycle_handlers(repository,{'environment':handler,'resume':ResumeJobHandler(repository,{'environment':handler})},
            node_service_factory=node_service_factory,lifecycle_bindings=lifecycle_bindings,model_team=model_team,
            agent_provider=agent_provider,reconciliation=reconciliation,repair_catalog=repair_catalog or standard_repair_catalog(),
            evolution=evolution,operation_service=operation_service,lease_seconds=lease_seconds,owner=owner)
    repair_catalog = repair_catalog or (model_team.repair_catalog if model_team is not None
        else reconciliation.repair_catalog if reconciliation is not None else standard_repair_catalog())
    for service in (model_team, reconciliation):
        if service is not None and service.repair_catalog is not repair_catalog:
            raise ValueError('all lifecycle services require the same trusted repair catalog')
    def factory(token,activation):
        if activation.graph_ref!=graph_ref:
            raise ValueError('worker graph version does not match activation')
        services=node_service_factory(token,activation)
        return WorkflowHandlers(repository,deadline=deadline(activation) if callable(deadline) else deadline,node_services=services,
            review_service=review_service,max_work_concurrency=max_work_concurrency,environment_gate=environment_gate).mapping()
    definitions={'run':(run_definition(),run_routers()),
                 'group':(group_definition(),group_routers()),
                 'analysis':(analysis_definition(),linear_routers(analysis_definition().nodes)),
                 'report':(report_definition(),linear_routers(report_definition().nodes))}
    handlers={}
    for kind,(definition,routers) in definitions.items():
        def builder(executor,checkpointer,definition=definition,routers=routers):
            return build_definition(definition,executor,routers=routers,checkpointer=checkpointer)
        handlers[kind]=GraphJobHandler(repository,builder,saver_factory,factory,
            lease_seconds=lease_seconds,segment_seconds=segment_seconds,owner=owner,
            agent_allowlists=agent_allowlists,repair_catalog=repair_catalog)
    handlers['work']=GraphJobHandler(repository,build_work_hold if graph_ref=='hold:1' else build_work,saver_factory,factory,
        lease_seconds=lease_seconds,segment_seconds=segment_seconds,owner=owner,
        delivery_finalizer=delivery_finalizer,agent_allowlists=agent_allowlists,repair_catalog=repair_catalog)
    handlers['resume']=ResumeJobHandler(repository,handlers)
    supervisor=AgentSupervisor(repository,agent_gateway)
    handlers['supervision']=SupervisionJobHandler(supervisor,allowed_fields=agent_allowed_fields,budget=agent_budget,
        accessible_evidence=agent_accessible_evidence,fields_by_node=agent_fields_by_node,
        remediation=NodeRemediationService(repository,max_attempts=max_node_attempts,repair_catalog=repair_catalog))
    handlers['remedy']=RemedyJobHandler(repository,node_service_factory,
        lease_seconds=lease_seconds,segment_seconds=segment_seconds,repair_catalog=repair_catalog)
    return _lifecycle_handlers(repository,handlers,node_service_factory=node_service_factory,
        lifecycle_bindings=lifecycle_bindings,model_team=model_team,agent_provider=agent_provider,
        reconciliation=reconciliation,repair_catalog=repair_catalog,evolution=evolution,
        operation_service=operation_service,lease_seconds=lease_seconds,owner=owner)


def _lifecycle_handlers(repository,handlers,*,node_service_factory,lifecycle_bindings,model_team,
                        agent_provider,reconciliation,repair_catalog,evolution,operation_service,lease_seconds,owner):
    if lifecycle_bindings is not None:
        from .lifecycle_supervisor import LifecycleSupervisor
        handlers['lifecycle']=LifecycleSupervisor(repository,bindings=lifecycle_bindings,
            lease_seconds=lease_seconds,owner=owner,model_team=model_team,repair_catalog=repair_catalog,evolution=evolution)
    if evolution is not None:
        if lifecycle_bindings is None:raise ValueError('candidate evolution requires explicit lifecycle bindings')
        handlers['candidate']=evolution
        handlers['candidate_cleanup']=evolution.safety
    if model_team is not None:
        if lifecycle_bindings is None or agent_provider is None:
            raise ValueError('model team requires lifecycle bindings and explicit agent provider')
        from .agent_team import AgentTeamJobHandler
        handlers['agent_team']=AgentTeamJobHandler(model_team,agent_provider,
            lease_seconds=lease_seconds,owner=owner,evolution=evolution)
    if operation_service is not None:
        recovery=RecoveryJobHandler(repository,operation_service,lease_seconds=lease_seconds,owner=owner)
        handlers.update(cleanup=recovery,reconcile=recovery)
    if reconciliation is not None:
        if operation_service is None and agent_provider is None:
            raise ValueError('安全对账必须明确提供 OperationService 或原 provider')
        from .lifecycle_reconciliation import LifecycleReconciliationHandler
        handlers['reconcile']=LifecycleReconciliationHandler(reconciliation,
            operation_service=operation_service,agent_provider=agent_provider,
            lease_seconds=lease_seconds,owner=owner,legacy=handlers.get('reconcile'))
    return handlers
