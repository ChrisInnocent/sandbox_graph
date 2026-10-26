"""Operator-owned, data-only lifecycle assembly for ordinary deployed workers.

The separate worker configuration can bind a run after its ID is allocated.
It does not alter the frozen measurement runtime or grant policy permissions.
"""
import json
from ..catalog.repair_actions import standard_repair_catalog
from ..services.agent_lifecycle import decode


def assemble_lifecycle(repository,config,*,store,resolver):
    if config is None:return {}
    if (not isinstance(config,dict) or set(config)-{'schema_version','bindings','model','reconciliation','evolution'}
            or config.get('schema_version')!=1 or not isinstance(config.get('bindings'),list)):
        raise ValueError('explicit versioned lifecycle runtime required')
    result={'lifecycle_bindings':config['bindings']}
    if config.get('model') is not None:
        from .agent_bridge import BridgeConfig,HttpAgentProvider
        from ..services.agent_team import AgentTeamService,ModelTeamConfig
        from ..services.lifecycle_context import LifecycleContextService
        value=config['model']
        if set(value)!={'bridge','credential_ref','team','context'}:raise ValueError('explicit model bridge/team/context required')
        if resolver is None or not value['credential_ref'].startswith('secret://'):
            raise ValueError('private provider credential reference required')
        path=resolver.resolve(value['credential_ref'])
        with path.open('rb') as stream:secret=stream.read(4097)
        if len(secret)>4096:raise ValueError('provider credential exceeds limit')
        secret=secret.decode().strip()
        if 'bearer_token' in value['bridge']:raise ValueError('inline provider credential forbidden')
        bridge=BridgeConfig.model_validate_json(json.dumps(value['bridge']|{'bearer_token':secret}))
        team=decode(ModelTeamConfig,value['team'])
        if bridge.provider_id!=team.provider_id:raise ValueError('model provider identity differs')
        context=LifecycleContextService(repository,store,secret_values=(secret,),**value['context'])
        catalog=standard_repair_catalog()
        result.update(agent_provider=HttpAgentProvider(bridge),repair_catalog=catalog,
            model_team=AgentTeamService(repository,context,config=team,repair_catalog=catalog))
    if config.get('reconciliation') is not None:
        from ..services.lifecycle_reconciliation import ReconciliationConfig,LifecycleReconciliationService
        value=config['reconciliation']
        if set(value)!={'config','authorizations'}:raise ValueError('explicit safety reconciliation config and authority required')
        catalog=result.setdefault('repair_catalog',standard_repair_catalog())
        result['reconciliation']=LifecycleReconciliationService(repository,
            config=decode(ReconciliationConfig,value['config']),authorizations=value['authorizations'],repair_catalog=catalog)
    if config.get('evolution') is not None:
        result['evolution']=assemble_evolution(repository,config['evolution'],resolver=resolver)
    return result


def _provider(value,resolver):
    from .agent_bridge import BridgeConfig,HttpAgentProvider
    if set(value)!={'bridge','credential_ref'} or 'bearer_token' in value['bridge']:
        raise ValueError('private provider bridge binding required')
    if resolver is None or not value['credential_ref'].startswith('secret://'):raise ValueError('private provider reference required')
    with resolver.resolve(value['credential_ref']).open('rb') as stream:raw=stream.read(4097)
    if len(raw)>4096:raise ValueError('provider credential exceeds byte budget')
    bridge=BridgeConfig.model_validate_json(json.dumps(value['bridge']|{'bearer_token':raw.decode().strip()}))
    return HttpAgentProvider(bridge),bridge


def assemble_evolution(repository,config,*,resolver):
    from .candidate_workspace import CandidateWorkspace
    from .candidate_worker import CandidateWorker,CodingConfig
    from .candidate_review_worker import CandidateReviewWorker
    from .candidate_isolation import BwrapBackend
    from .candidate_runtime import CandidateGateRuntime
    from .evolution_dispatch import EvolutionCoordinator
    from .agent_bridge import UsageCeiling
    from ..services.evolution import EvolutionService
    from ..domain.evolution import ChangeProposal,TargetBinding
    required={'repository','candidate_root','bindings','coding','review','gates','lease_seconds','owner'}
    if not required<=set(config) or set(config)-required-{'isolation'}:raise ValueError('explicit candidate runtime configuration required')
    service=EvolutionService(repository,CandidateWorkspace(config['repository'],config['candidate_root']))
    def worker_config(value):
        if set(value)!={'provider','config','identity'}:raise ValueError('explicit role provider/config/identity required')
        provider,bridge=_provider(value['provider'],resolver)
        data=dict(value['config']);data['usage_ceiling']=UsageCeiling.model_validate(data['usage_ceiling'])
        typed=CodingConfig(**data)
        if bridge.provider_id!=typed.provider_id:raise ValueError('role provider identity differs')
        return provider,typed,bridge
    coder,coding,coding_bridge=worker_config(config['coding'])
    reviewer,review,review_bridge=worker_config(config['review'])
    bindings=[]
    for row in config['bindings']:
        if set(row)-{'proposal','target','revision_targets'}:raise ValueError('unknown candidate binding field')
        proposal=decode(ChangeProposal,row['proposal']);target=decode(TargetBinding,row['target'])
        if any(bridge.scope.kind!='candidate' or bridge.scope.ref!=proposal.proposal_id for bridge in (coding_bridge,review_bridge)):
            raise PermissionError('candidate role bridge scope differs from proposal')
        targets={}
        for revision,value in row.get('revision_targets',{}).items():
            if not isinstance(revision,str) or not revision.isdecimal() or not 1<=int(revision)<=1000:
                raise ValueError('bounded explicit revision target required')
            targets[int(revision)]=decode(TargetBinding,value)
        bindings.append({'proposal':proposal,'target':target,'revision_targets':targets})
    backend=None;backend_id=None
    if config.get('isolation'):
        isolation=dict(config['isolation']);backend_id=isolation.pop('backend_id')
        backend=BwrapBackend(**isolation)
    gates=CandidateGateRuntime(service,config['gates'],backend=backend,backend_id=backend_id)
    review_worker=CandidateReviewWorker(service,reviewer,config=review,
        reviewer_identity=config['review']['identity'],coder_identity=config['coding']['identity'])
    worker=CandidateWorker(service,coder,config=coding,verify=gates,reviewer=review_worker)
    return EvolutionCoordinator(service,worker,bindings=bindings,lease_seconds=config['lease_seconds'],owner=config['owner'],
        isolations={backend_id:backend} if backend else {})
