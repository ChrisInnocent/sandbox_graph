#!/usr/bin/env python3
"""Build an offline helper bundle or deploy it using initial SSH credentials."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from sandbox_graph.environment.bootstrap import build_bundle,deploy
from sandbox_graph.environment.bootstrap_remote import atomic,encode,checksum
from sandbox_graph.environment.cli import read_json


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    build=sub.add_parser('build');build.add_argument('--input',type=Path,required=True)
    build.add_argument('--bundle',type=Path,required=True)
    apply=sub.add_parser('deploy');apply.add_argument('--input',type=Path,required=True)
    apply.add_argument('--bundle',type=Path,required=True);apply.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        config=read_json(args.input)
        if args.command=='build':
            if set(config)!={'files'} or not isinstance(config['files'],list):raise ValueError('asset inventory required')
            if any(set(row)!={'path','kind'} for row in config['files']):raise ValueError('exact file fields required')
            value=build_bundle([(row['path'],row['kind']) for row in config['files']],args.bundle)
            print(json.dumps({'status':'prepared','bundle_sha256':value['bundle_sha256'],'file_count':len(value['manifest']['files'])}))
        else:
            if set(config)!={'operation_id','machine_id','root','expected_launcher_sha256','ssh'}:
                raise ValueError('exact bootstrap request required')
            ssh=config.pop('ssh')
            if set(ssh)!={'address','port','user','known_hosts','identity_file'}:raise ValueError('exact initial SSH binding required')
            manifest=read_json(args.bundle/'manifest.json')
            request=dict(config,schema_version=1,manifest=manifest,bundle_sha256=checksum(encode(manifest)))
            result=asyncio.run(deploy(bundle=args.bundle,request=request,**ssh))
            atomic(args.output.absolute(),encode(result))
            print(json.dumps(result))
        return 0
    except Exception as error:
        print(json.dumps({'status':'blocked','reason':type(error).__name__}));return 2


if __name__=='__main__':raise SystemExit(main())
