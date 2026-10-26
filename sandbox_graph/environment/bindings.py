"""Environment acceptance and executor-only private credential resolution."""
from datetime import datetime,timezone
from pathlib import Path
import hashlib
import json
import re

from .contracts import EnvironmentAcceptance,EnvironmentDeclaration
from .secrets import SecretStore
from ..domain.models import EvidenceRef,canonical_hash


def resolve_benchmark_credential(config):
    ref=config.get('auth_ref')
    if ref is None:return None
    if config.get('api_key_env'):raise ValueError('auth_ref and api_key_env are mutually exclusive')
    match=re.fullmatch(r'secret://([^/]+)/([^/]+)/([^/]+)',ref)
    if not match:raise ValueError('invalid benchmark credential reference')
    scope,_,version=match.groups()
    if scope!=config.get('auth_scope'):raise PermissionError('benchmark credential scope mismatch')
    if version!=config.get('auth_version') or version!=config.get('server_credential_version'):
        raise ValueError('server/client credential versions differ')
    root=config.get('secret_store_path')
    if not isinstance(root,str) or not Path(root).is_absolute() or not Path(root).is_dir():
        raise ValueError('explicit existing private secret store required')
    return SecretStore(root).resolve(ref,scope=scope)


def scope_fingerprint(declaration,scope):
    declaration=EnvironmentDeclaration.model_validate(declaration)
    scope=set(scope);nodes={node.node_id:node for node in declaration.nodes}
    if not scope or not scope<=nodes.keys():raise ValueError('acceptance node scope is undeclared')
    # Cross-host endpoints and authentication are group inputs; other nodes' private
    # SSH/path changes do not invalidate an unchanged selected node.
    return canonical_hash({'product':declaration.product,'version':declaration.product_version,
        'profile':declaration.profile,'sources':declaration.source_set,
        'artifacts':[item.model_dump(mode='json') for item in declaration.artifacts],
        'nodes':[nodes[key].model_dump(mode='json') for key in sorted(scope)],'targets':declaration.targets.model_dump(mode='json'),
        'endpoints':declaration.service_endpoints,'auth':[item.model_dump(mode='json') for item in declaration.auth],
        'policy':declaration.policy})


def verify_acceptance(acceptance,*,frozen_declaration,current_declaration,scope,current_fingerprint,
                      credential_versions,resolve_report,now=None,resolve_evidence=None,
                      evidence_scope=None,trusted_producers=()):
    acceptance=EnvironmentAcceptance.model_validate(acceptance)
    frozen=EnvironmentDeclaration.model_validate(frozen_declaration)
    current=EnvironmentDeclaration.model_validate(current_declaration)
    if acceptance.declaration_hash!=frozen.declaration_hash:raise ValueError('acceptance declaration digest mismatch')
    if not set(scope)<=set(acceptance.scope) or not scope:raise PermissionError('acceptance does not cover selected nodes')
    for key in ('product','product_version','profile'):
        if getattr(acceptance,key)!=getattr(current,key):raise ValueError('accepted product/version/profile differs')
    until=datetime.fromisoformat(acceptance.valid_until.replace('Z','+00:00'))
    if until.tzinfo is None or until<=(now or datetime.now(timezone.utc)):raise ValueError('environment acceptance expired')
    if scope_fingerprint(frozen,scope)!=scope_fingerprint(current,scope):raise ValueError('selected environment inputs changed')
    if acceptance.credential_versions!=credential_versions:raise ValueError('accepted credential version changed')
    report=resolve_report(acceptance.report_manifest_ref)
    if not isinstance(report,dict) or report.get('declaration_hash')!=frozen.declaration_hash:
        raise ValueError('accepted report binding mismatch')
    expected=acceptance.invalidation_fingerprint
    if set(scope)!=set(acceptance.scope):
        from .acceptance import observed_fingerprint
        if observed_fingerprint(report,acceptance.scope)!=expected:raise ValueError('accepted full report fingerprint differs')
        expected=observed_fingerprint(report,scope)
    if expected!=current_fingerprint:raise ValueError('observed environment drift')
    if any(report.get('readiness',{}).get(node)!='ready' for node in scope):raise ValueError('accepted nodes are not ready')
    if not acceptance.functional_verification_refs or not acceptance.cleanup_refs:
        raise ValueError('functional verification and cleanup evidence required')
    if resolve_evidence is None or not evidence_scope or not trusted_producers:
        raise ValueError('verified raw functional/cleanup evidence resolver required')
    all_refs=acceptance.functional_verification_refs+acceptance.cleanup_refs
    if len(set(all_refs))!=len(all_refs):raise ValueError('acceptance evidence identities must be distinct')
    resources={};coverage={}
    for kind,refs,status in [('functional',acceptance.functional_verification_refs,'passed'),
                             ('cleanup',acceptance.cleanup_refs,'complete')]:
        resources[kind]=set();coverage[kind]=set()
        for ref in refs:
            resolved=resolve_evidence(ref)
            if not isinstance(resolved,tuple) or len(resolved)!=2:raise ValueError('acceptance raw evidence missing')
            artifact,raw=resolved;artifact=EvidenceRef.model_validate(artifact)
            if (ref!='artifact://'+artifact.uri or artifact.scope!=evidence_scope
                    or artifact.producer not in trusted_producers):
                raise PermissionError('acceptance evidence identity/scope/producer differs')
            if (not isinstance(raw,bytes) or len(raw)>1024*1024 or len(raw)!=artifact.size
                    or hashlib.sha256(raw).hexdigest()!=artifact.digest):
                raise ValueError('acceptance evidence bytes do not match immutable artifact')
            from .declarations import _unique_json_object
            proof=json.loads(raw,object_pairs_hook=_unique_json_object)
            if (not isinstance(proof,dict) or proof.get('kind')!=kind or proof.get('status')!=status
                    or proof.get('declaration_hash')!=frozen.declaration_hash):
                raise ValueError('acceptance functional/cleanup proof failed or unbound')
            nodes=proof.get('scope');owned=proof.get('resource_ids')
            if (not isinstance(nodes,list) or not nodes or not all(isinstance(x,str) for x in nodes)
                    or not set(nodes)<=set(acceptance.scope) or not isinstance(owned,list)
                    or any(not isinstance(x,str) or not x for x in owned) or len(set(owned))!=len(owned)):
                raise ValueError('acceptance evidence scope/resource identity invalid')
            resources[kind].update(owned);coverage[kind].update(nodes)
        if not set(scope)<=coverage[kind]:raise ValueError('acceptance evidence node coverage incomplete')
    if resources['functional']!=resources['cleanup']:
        raise ValueError('canary resources are not exactly covered by cleanup proof')
    return acceptance
