"""Independent process/file evidence; this does not qualify PostgreSQL recovery."""
import json
from pathlib import Path
import subprocess
import sys

SCRIPT=Path(__file__).with_name('controlled_runner.py')

def test_response_loss_preserves_exact_single_external_identity(tmp_path):
    first=subprocess.run([sys.executable,str(SCRIPT),str(tmp_path),'submit','op-1','--input-hash','fixture-hash','--crash-after-effect'],capture_output=True)
    assert first.returncode==71
    observed=json.loads(subprocess.check_output([sys.executable,str(SCRIPT),str(tmp_path),'reconcile','op-1']))
    assert observed['accepted'] is True and observed['completed'] is False
    assert observed['starts']==1 and observed['resources']==['op-1-resource']
    replay=json.loads(subprocess.check_output([sys.executable,str(SCRIPT),str(tmp_path),'submit','op-1','--input-hash','fixture-hash']))
    assert replay==observed
    assert len(list(tmp_path.glob('*.json')))==1


def test_cancel_ack_does_not_fabricate_resource_cleanup(tmp_path):
    subprocess.check_output([sys.executable,str(SCRIPT),str(tmp_path),'submit','op-2'])
    cancelled=json.loads(subprocess.check_output([sys.executable,str(SCRIPT),str(tmp_path),'cancel','op-2']))
    assert cancelled['cancelled'] is True
    assert cancelled['resources']==['op-2-resource']
