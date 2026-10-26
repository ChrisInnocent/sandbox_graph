import hashlib
import json
from pathlib import Path
import subprocess
import pytest


def bundle(tmp_path):
    from sandbox_graph.runtime.candidate_workspace import _tree
    from sandbox_graph.domain.models import canonical_hash
    folder=tmp_path/'bundle';tree=folder/'tree';(tree/'sandbox_graph/runtime').mkdir(parents=True)
    (tree/'sandbox_graph/__init__.py').write_text('')
    (tree/'sandbox_graph/runtime/__init__.py').write_text('')
    (tree/'sandbox_graph/runtime/evaluation_cli.py').write_text(
        'import json,sys,os\ndef main(argv):\n print(json.dumps({"loaded":__file__,"args":argv,"leak":os.environ.get("UNRELATED_SECRET")}));return 0\n')
    manifest={'path':str(folder),'files':_tree(tree)}
    (folder/'manifest.json').write_text(json.dumps(manifest))
    return folder,manifest


def test_bundle_launcher_starts_clean_interpreter_from_verified_tree(tmp_path,monkeypatch):
    from sandbox_graph.runtime.bundle_launcher import command
    folder,manifest=bundle(tmp_path);monkeypatch.setenv('UNRELATED_SECRET','do-not-inherit')
    runtime=tmp_path/'runtime.json';runtime.write_text('{}')
    argv,env=command(manifest,bundle_ref='worker-bundle:abc',runtime=runtime,once=True)
    assert 'do-not-inherit' not in repr((argv,env))
    run=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=15,cwd=tmp_path)
    assert run.returncode==0,run.stderr
    result=json.loads(run.stdout)
    assert result['loaded']==str(folder/'tree/sandbox_graph/runtime/evaluation_cli.py')
    assert result['leak'] is None and result['args']==['worker','--runtime',str(runtime),'--bundle-ref','worker-bundle:abc','--once']


def test_bundle_launcher_rejects_tree_or_runtime_change_before_import(tmp_path):
    from sandbox_graph.runtime.bundle_launcher import command
    folder,manifest=bundle(tmp_path);runtime=tmp_path/'runtime.json';runtime.write_text('{}')
    argv,env=command(manifest,bundle_ref='worker-bundle:abc',runtime=runtime)
    runtime.write_text('{"changed":true}')
    result=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode!=0 and 'loaded' not in result.stdout
    runtime.write_text('{}');argv,env=command(manifest,bundle_ref='worker-bundle:abc',runtime=runtime)
    (folder/'tree/sandbox_graph/__init__.py').write_text('raise RuntimeError("MUST_NOT_RUN")')
    result=subprocess.run(argv,env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode!=0 and 'MUST_NOT_RUN' not in result.stderr
