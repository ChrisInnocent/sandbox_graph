import pytest
from sandbox_graph.artifacts.store import FileArtifactStore


def test_lifecycle_assembly_requires_explicit_binding_and_never_loads_provider_from_code(repository,tmp_path):
    from sandbox_graph.runtime.lifecycle_assembly import assemble_lifecycle
    store=FileArtifactStore(tmp_path/'artifacts')
    assert assemble_lifecycle(repository,None,store=store,resolver=None)=={}
    assert assemble_lifecycle(repository,{'schema_version':1,'bindings':[]},store=store,resolver=None)=={'lifecycle_bindings':[]}
    with pytest.raises(ValueError):
        assemble_lifecycle(repository,{'schema_version':1,'bindings':[],'factory':'evil:entry'},store=store,resolver=None)


def test_evaluation_worker_actual_entrypoint_accepts_lifecycle_dispatch(repository,tmp_path):
    from sandbox_graph.runtime.deployment import EvaluationDeployment
    from langgraph.checkpoint.memory import InMemorySaver
    from tests.orchestration.unit.test_evaluation_deployment import runtime
    deployed=EvaluationDeployment(repository,runtime(tmp_path),saver_factory=lambda _:InMemorySaver(),
        lifecycle_runtime={'schema_version':1,'bindings':[]})
    assert 'lifecycle' in deployed.worker.handlers
    assert 'lifecycle' in deployed.worker.policy.accepted_job_kinds
