"""Minimal operational CLI; all business commands share typed service contracts."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
from .catalog.registry import standard_registry
from .planning.compiler import Case,Selection,PlanCompiler


def main():
    parser=argparse.ArgumentParser(prog='python -m sandbox_graph')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('catalog',help='show definitions and unverified adapter status')
    sub.add_parser('migrate',help='provision PostgreSQL ledger and saver tables')
    status=sub.add_parser('status');status.add_argument('run_id')
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
            import psycopg
            from psycopg.rows import dict_row
            from langgraph.checkpoint.postgres import PostgresSaver
            repository.migrate()
            with psycopg.connect(dsn,autocommit=True,row_factory=dict_row) as conn: PostgresSaver(conn).setup()
            result={'migration':'complete','backend':'PostgreSQL'}
        else:
            from .interfaces.service import QueryService
            result=QueryService(repository).status(args.run_id)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))

if __name__=='__main__': main()
