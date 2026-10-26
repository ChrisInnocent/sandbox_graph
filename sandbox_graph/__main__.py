"""Minimal operational CLI; all business commands share typed service contracts."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
from .catalog.registry import standard_registry
from .planning.compiler import Case,Selection,PlanCompiler


def main():
    import sys
    if sys.argv[1:2]==['env']:
        from .environment.cli import main as environment_main
        raise SystemExit(environment_main(sys.argv[2:]))
    if sys.argv[1:2]==['eval']:
        from .runtime.evaluation_cli import main as evaluation_main
        raise SystemExit(evaluation_main(sys.argv[2:]))
    if sys.argv[1:2]==['lifecycle']:
        from .runtime.lifecycle_cli import main as lifecycle_main
        raise SystemExit(lifecycle_main(sys.argv[2:]))
    parser=argparse.ArgumentParser(prog='python -m sandbox_graph')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('env',help='environment check/plan/apply/worker/status/reconcile/report')
    sub.add_parser('lifecycle',help='fenced lifecycle controls and fixed gate profiles')
    sub.add_parser('eval',help='persistent benchmark plan/start/worker/status')
    sub.add_parser('catalog',help='show definitions and unverified adapter status')
    sub.add_parser('migrate',help='provision PostgreSQL ledger and saver tables')
    status=sub.add_parser('status');status.add_argument('run_id')
    dump=sub.add_parser('export-analysis',help='dump versioned analysis export files, including facts.xlsx')
    dump.add_argument('--run-id');dump.add_argument('--analysis-ref')
    dump.add_argument('--out',type=Path,required=True)
    dump.add_argument('--artifacts-root',type=Path,default=os.environ.get('SANDBOX_GRAPH_ARTIFACTS'))
    cases=sub.add_parser('export-cases',help='dump sealed case evidence (data/raw/resource samples) for a run')
    cases.add_argument('--run-id',required=True)
    cases.add_argument('--out',type=Path,required=True)
    cases.add_argument('--artifacts-root',type=Path,default=os.environ.get('SANDBOX_GRAPH_ARTIFACTS'))
    reclaim=sub.add_parser('reclaim-leases',help='list or reclaim quarantined resource leases (issue #3)')
    reclaim.add_argument('--scope',default=None);reclaim.add_argument('--domain',action='append',default=[])
    reclaim.add_argument('--evidence-ref',default='');reclaim.add_argument('--reason',default='operator reconciliation')
    reclaim.add_argument('--apply',action='store_true')
    preview=sub.add_parser('preview');preview.add_argument('input',type=Path)
    args=parser.parse_args()
    if args.command=='catalog':
        registry=standard_registry();result={'nodes':[asdict(n) for n in registry.nodes.values()],'methods':list(registry.methods.values())}
    elif args.command=='preview':
        data=json.loads(args.input.read_text());compiler=PlanCompiler([Case.model_validate(c) for c in data['catalog']])
        plan=compiler.compile(Selection.model_validate(data['selection']),authorized_groups=set(data['authorized_groups']),evidence=data.get('evidence'),graph_ref=data.get('graph_ref','default:1'),contract_ref=data.get('contract_ref',''))
        result={'plan_hash':plan.hash,'plan':plan.model_dump(mode='json'),'persisted':False}
    else:
        from .persistence.repository import PostgresRepository
        dsn=os.environ.get('SANDBOX_GRAPH_POSTGRES_DSN')
        if not dsn: parser.error('SANDBOX_GRAPH_POSTGRES_DSN is required; no fallback backend')
        repository=PostgresRepository(dsn)
        if args.command=='migrate':
            repository.migrate()
            result={'migration':'complete','backend':'PostgreSQL'}
        elif args.command=='export-analysis':
            from .analysis_dump import dump_with_root
            if not args.artifacts_root: parser.error('--artifacts-root or SANDBOX_GRAPH_ARTIFACTS is required')
            result={'written':dump_with_root(repository,args.artifacts_root,out=args.out,run_id=args.run_id,analysis_ref=args.analysis_ref)}
        elif args.command=='export-cases':
            from .analysis_dump import dump_cases_with_root
            if not args.artifacts_root: parser.error('--artifacts-root or SANDBOX_GRAPH_ARTIFACTS is required')
            result={'written':dump_cases_with_root(repository,args.artifacts_root,out=args.out,run_id=args.run_id)}
        elif args.command=='reclaim-leases':
            from .runtime.leases import LeaseService
            service=LeaseService(repository);candidates=service.reclaim_candidates()
            if not args.apply:
                result={'candidates':candidates,'applied':False}
            else:
                groups={}
                for item in candidates:
                    if (not args.domain or item['domain'] in args.domain) and item['reclaimable'] \
                            and (not args.scope or item['scope']==args.scope):
                        groups.setdefault(item['scope'],[]).append(item['domain'])
                reclaimed=[]
                for scope,domains in groups.items():
                    token=service.acquire_thread(f'lease-reclaim:{scope}','operator',60)
                    reclaimed+=service.reclaim(domains,scope=scope,token=token,
                        evidence_ref=args.evidence_ref,reason=args.reason)
                result={'reclaimed':reclaimed,'applied':True,
                        'skipped':[c for c in candidates if c['domain'] not in set(reclaimed)]}
        else:
            from .interfaces.service import QueryService
            result=QueryService(repository).status(args.run_id)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))

if __name__=='__main__': main()
