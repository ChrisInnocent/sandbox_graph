#!/usr/bin/env python3
"""Render private server auth assets; install with the registered recipe path."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from sandbox_graph.environment.cli import read_json
from sandbox_graph.environment.products.server_auth import prepare_auth,private_asset,deployment_recipe
from sandbox_graph.environment.secrets import SecretStore


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--manifest',type=Path,required=True)
    args=parser.parse_args();value=read_json(args.input)
    deployment=value.pop('deployment',None)
    if set(value)!={'product','secret_root','scope','credential_ref','observed','output'}:
        raise ValueError('explicit scoped server auth rendering input required')
    root=value.pop('secret_root')
    manifest=prepare_auth(store=SecretStore(root),**value)
    if deployment is not None:
        manifest['deployment']=deployment_recipe(manifest,scope=value['scope'],secret_root=root,**deployment)
    private_asset(args.manifest,(json.dumps(manifest,indent=2,sort_keys=True)+'\n').encode())
    print(json.dumps({'status':'rendered','manifest':str(args.manifest),'evidence_level':'rendered_only'}))


if __name__=='__main__':main()
