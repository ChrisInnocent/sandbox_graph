#!/usr/bin/env python3
"""Fetch one sealed original operation log into a private local file."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from sandbox_graph.environment.cli import read_json
from sandbox_graph.environment.log_export import download
from sandbox_graph.environment.transport import SSHRequest,SSHTransport


async def export(config,output):
    if set(config)!={'ssh','helper_path','machine_id','log'}:raise ValueError('explicit log export contract required')
    ssh=config['ssh']
    if set(ssh)!={'address','port','user','known_hosts','identity_file'}:raise ValueError('exact SSH identity required')
    transport=SSHTransport(helper_paths={ssh['address']:config['helper_path']})
    async def fetch(data):
        response=await transport.request(SSHRequest(**ssh,payload={'action':'operation.logs.read.v1','input':data}))
        if response.returncode:raise ValueError('remote sealed log unavailable')
        return json.loads(response.stdout)
    return await asyncio.wait_for(download(fetch,config['log'],output=output,machine_id=config['machine_id']),300)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        result=asyncio.run(export(read_json(args.input),args.output));code=0
    except Exception as exc:result={'status':'blocked','reason':type(exc).__name__};code=2
    print(json.dumps(result));return code

if __name__=='__main__':raise SystemExit(main())
