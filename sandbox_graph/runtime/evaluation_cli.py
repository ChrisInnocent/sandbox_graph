"""Persistent evaluation entrypoint; environment preparation precedes eval start."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from ..environment.cli import read_json
from .deployment import EvaluationDeployment


def parser():
    root=argparse.ArgumentParser(prog='python -m sandbox_graph eval')
    sub=root.add_subparsers(dest='command',required=True)
    for name in ('plan','start','worker','bundle-worker','status'):
        command=sub.add_parser(name)
        command.add_argument('--runtime',type=Path,required=True)
        if name in {'plan','start'}:command.add_argument('--evidence',type=Path)
        if name=='plan':command.add_argument('--selection',type=Path,required=True)
        if name=='start':
            command.add_argument('--plan-ref',required=True);command.add_argument('--request-id',required=True)
            command.add_argument('--seconds',type=float,required=True)
        if name in {'worker','bundle-worker'}:
            command.add_argument('--once',action='store_true');command.add_argument('--lifecycle-runtime',type=Path);command.add_argument('--bundle-ref',required=name=='bundle-worker')
        if name=='status':command.add_argument('--run-id',required=True)
    return root


def assemble(runtime,dsn,*,lifecycle_runtime=None,bundle_ref=None):
    from psycopg_pool import ConnectionPool
    from ..persistence.repository import PostgresRepository
    from ..persistence.checkpointer import FencedPostgresSaver
    if not dsn:raise ValueError('explicit PostgreSQL DSN required')
    repository=PostgresRepository(dsn)
    pool=ConnectionPool(dsn,kwargs={'autocommit':True},min_size=1,max_size=4)
    try:deployment=EvaluationDeployment(repository,runtime,saver_factory=lambda token:FencedPostgresSaver(pool,token),lifecycle_runtime=lifecycle_runtime,bundle_ref=bundle_ref)
    except BaseException:pool.close();raise
    return deployment,pool


async def execute(args):
    if args.command=='bundle-worker':
        from ..persistence.repository import PostgresRepository
        from .bundle_launcher import launch
        dsn=os.environ.get('SANDBOX_GRAPH_POSTGRES_DSN')
        if not dsn:raise ValueError('explicit PostgreSQL DSN required')
        launch(PostgresRepository(dsn),bundle_ref=args.bundle_ref,runtime=args.runtime,
            lifecycle_runtime=args.lifecycle_runtime,once=args.once)
        raise RuntimeError('worker exec unexpectedly returned')
    deployment,pool=assemble(read_json(args.runtime),os.environ.get('SANDBOX_GRAPH_POSTGRES_DSN'),
        **({'lifecycle_runtime':read_json(args.lifecycle_runtime)} if getattr(args,'lifecycle_runtime',None) else {}),
        **({'bundle_ref':args.bundle_ref} if getattr(args,'bundle_ref',None) else {}))
    try:
        if args.command=='plan':
            plan=deployment.preview(read_json(args.selection),evidence=read_json(args.evidence) if args.evidence else None)
            return {'plan_ref':plan.hash,'plan':plan.model_dump(mode='json')}
        if args.command=='start':return (await deployment.start(args.request_id,args.plan_ref,seconds=args.seconds,
            evidence=read_json(args.evidence) if args.evidence else None)).model_dump(mode='json')
        if args.command=='status':return deployment.status(args.run_id)
        if args.once:return {'worked':await deployment.worker.run_once()}
        while True:
            if not await deployment.worker.run_once():await asyncio.sleep(1)
    finally:pool.close()


def main(argv=None):
    args=parser().parse_args(argv)
    try:result=asyncio.run(execute(args));code=0
    except Exception as exc:result={'status':'blocked','error':type(exc).__name__};code=2
    print(json.dumps(result,sort_keys=True,ensure_ascii=False,default=str));return code
