"""Explicit, version-pinned assembly of business services and graph workers."""
from ..graphs.factory import build_work,build_definition
from ..services.workflows import (WorkflowHandlers,run_definition,run_routers,
    group_definition,group_routers,analysis_definition,report_definition,linear_routers)
from .worker import GraphJobHandler,ResumeJobHandler,RecoveryJobHandler


def build_job_handlers(repository,*,graph_ref,saver_factory,node_service_factory,
                       review_service,deadline,lease_seconds,segment_seconds,
                       owner='worker',max_work_concurrency=1,
                       delivery_finalizer=None,operation_service=None):
    """Build all five graphs for one immutable deployed graph version.

    node_service_factory(token, activation) supplies registered environment,
    benchmark, analysis, and report adapters for this version. The factory is
    evaluated only after acquiring the actual owner lease. Missing services
    fail graph validation; no successful adapter is synthesized. Use a separate
    version-pinned worker set to drain historical versions.
    """
    def factory(token,activation):
        if activation.graph_ref!=graph_ref:
            raise ValueError('worker graph version does not match activation')
        services=node_service_factory(token,activation)
        return WorkflowHandlers(repository,deadline=deadline,node_services=services,
            review_service=review_service,max_work_concurrency=max_work_concurrency).mapping()
    definitions={'run':(run_definition(),run_routers()),
                 'group':(group_definition(),group_routers()),
                 'analysis':(analysis_definition(),linear_routers(analysis_definition().nodes)),
                 'report':(report_definition(),linear_routers(report_definition().nodes))}
    handlers={}
    for kind,(definition,routers) in definitions.items():
        def builder(executor,checkpointer,definition=definition,routers=routers):
            return build_definition(definition,executor,routers=routers,checkpointer=checkpointer)
        handlers[kind]=GraphJobHandler(repository,builder,saver_factory,factory,
            lease_seconds=lease_seconds,segment_seconds=segment_seconds,owner=owner)
    handlers['work']=GraphJobHandler(repository,build_work,saver_factory,factory,
        lease_seconds=lease_seconds,segment_seconds=segment_seconds,owner=owner,
        delivery_finalizer=delivery_finalizer)
    handlers['resume']=ResumeJobHandler(repository,handlers)
    if operation_service is not None:
        recovery=RecoveryJobHandler(repository,operation_service,lease_seconds=lease_seconds,owner=owner)
        handlers.update(cleanup=recovery,reconcile=recovery)
    return handlers
