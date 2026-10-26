#!/usr/bin/env python3
"""Render a private group-import recipe and version-bound Kuasar client config."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from sandbox_graph.environment.cli import read_json
from sandbox_graph.environment.config import load_config
from sandbox_graph.environment.products.kuasar import cluster_group_recipe
from sandbox_graph.environment.products.server_auth import kuasar_pair,private_asset
from sandbox_graph.environment.secrets import SecretStore
from sandbox_graph.domain.models import canonical_hash


def render(value):
    required={'secret_root','scope','manifest_key_ref','api_secret_name','config_snapshot','groups_by_source',
        'record','source_id','directory','config_target','output','api_host','domain'}
    if not required<=set(value) or set(value)-required-{'api_secret_ref'}:
        raise ValueError('explicit group snapshot and scoped credential input required')
    store=SecretStore(value['secret_root']);scope=value['scope']
    version=value['manifest_key_ref'].rsplit('/',1)[1]
    api_secret=None
    if 'api_secret_ref' in value:
        reference=value['api_secret_ref']
        api_secret=store.resolve(reference,scope=scope)
        if reference.rsplit('/',1)[1]!=version:
            raise ValueError('manifest and API secret version must match')
    pair=kuasar_pair(store.resolve(value['manifest_key_ref'],scope=scope),api_secret)
    source=Path(value['config_snapshot'])
    if source.resolve()!=source or not source.is_file():raise ValueError('canonical observed config snapshot required')
    with source.open('rb') as stream:raw=stream.read(1024*1024+1)
    if len(raw)>1024*1024:raise ValueError('config snapshot exceeds budget')
    config=load_config(source)
    if source.read_bytes()!=raw:raise ValueError('config snapshot changed')
    plan=cluster_group_recipe(config=config,groups_by_source=value['groups_by_source'],record=value['record'],
        pair=pair,source_id=value['source_id'],directory=value['directory'],config_target=value['config_target'],
        expected_before=hashlib.sha256(raw).hexdigest(),output=value['output'])
    item=store.create_if_absent(scope,value['api_secret_name'],lambda:pair['api_secret'],version=version)
    if store.resolve(item.ref,scope=scope)!=pair['api_secret']:
        raise ValueError('existing API secret differs; explicit credential rotation required')
    steps=[asdict(step) for step in plan.pop('steps')]
    return plan|{'steps':steps,'recipe_hash':canonical_hash(steps),'client':{'kuasar_cluster':{
        'api_host':value['api_host'],'domain':value['domain'],'group':value['record']['group'],
        'credential':{'auth_ref':item.ref,'auth_scope':scope,'auth_version':version,
            'server_credential_version':version,'secret_store_path':str(store.root)}}}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True);parser.add_argument('--manifest',type=Path,required=True)
    args=parser.parse_args()
    try:
        manifest=render(read_json(args.input))
        private_asset(args.manifest,(json.dumps(manifest,sort_keys=True,indent=2)+'\n').encode())
    except Exception as exc:
        print(json.dumps({'status':'blocked','error':type(exc).__name__}));return 2
    print(json.dumps({'status':'rendered','manifest':str(args.manifest),'evidence_level':'rendered_only'}));return 0


if __name__=='__main__':raise SystemExit(main())
