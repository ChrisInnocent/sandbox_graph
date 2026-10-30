"""Versioned observation coverage; static derivation never becomes live evidence.

This catalog describes the audited built-in probe profile, not every possible
upstream extension. An unsupported requested field cannot silently pass or cause
an endless installation repair. New profiles require source and probe tests.
"""
from ..tuning import RULES

SOURCES={
 ('cubesandbox','cubelet'):('CubeSandbox/Cubelet/pkg/config/config.go','host quota/report'),
 ('cubesandbox','CubeAPI'):('CubeSandbox/CubeAPI/src/state.rs','AppState/rate_limiter'),
 ('cubesandbox','CubeMaster'):('CubeSandbox/CubeMaster/pkg/base/config/config.go','scheduler configuration'),
 ('kuasar','conductor'):('kuasar-components/orchestrator/config/config.go','resource admission/builder/units'),
 ('agentenv','server'):('AgentENV/src/cfg.rs','template_build/pool.firecracker'),
 ('agentenv','scheduler'):('AgentENV/services/scheduler/internal/metrics.go','exported scheduler observations'),
}
DIRECT={
 ('cubesandbox','cubelet'): {'quota_cpu','max_sandboxes','create_concurrency'},
 ('kuasar','conductor'): {'sandbox_capacity','max_builds'},
}


def catalog():
    return [{'product':product,'component':component,'parameter':name,'path':rule[0],
        'unit':rule[1],'profile':'audited-runtime-observation.v1',
        'observation':'management_registration' if name in DIRECT.get((product,component),()) else 'startup_binding_only',
        'source':('kuasar-components/orchestrator/internal/orch/cluster.go' if product=='kuasar' and name=='max_builds'
                  else SOURCES[(product,component)][0]),
        'source_scope':('ClusterNodeInfo / build_execution_capacity.max_builds' if product=='kuasar' and name=='max_builds'
                        else SOURCES[(product,component)][1])}
        for (product,component),rules in sorted(RULES.items()) for name,rule in sorted(rules.items())]


def capability(product,name,roles):
    rows=[row for row in catalog() if row['product']==product and row['parameter']==name]
    if not rows:return None
    component_roles={'cubelet':{'compute'},'CubeAPI':{'control'},'CubeMaster':{'control'},
        'conductor':{'conductor'},'server':{'server'},'scheduler':{'scheduler'}}
    selected=[r for r in rows if set(roles)&component_roles[r['component']]]
    if not selected:return None
    return {'profile':'audited-runtime-observation.v1',
        'supported':all(r['observation']=='management_registration' for r in selected),
        'reason':'direct_runtime_value_not_exposed_by_this_audited_probe_profile',
        'components':selected}
