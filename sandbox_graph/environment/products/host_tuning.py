"""Closed, journaled host tuning command. Never unload an existing ublk module."""
import json
import subprocess
import sys
from ..probes.ublk import inspect_ublk


def activate_ublk(target,*,probe=inspect_ublk,runner=subprocess.run):
    if type(target) is not int or not 1<=target<2**32:raise ValueError('finite positive ublk device limit required')
    current=probe()
    if type(current.get('loaded')) is not bool:raise ValueError('module presence is unknown')
    if current['loaded']:
        if current.get('ublks_max')!=target:raise ValueError('loaded ublk differs; separate maintenance required')
        return {'status':'completed','reused':True,'observed':current}
    supported=runner(['modinfo','-p','ublk_drv'],capture_output=True,text=True,check=False,timeout=10,shell=False)
    if supported.returncode or len(supported.stdout)>65536 or not any(line.startswith('ublks_max:') for line in supported.stdout.splitlines()):
        raise ValueError('ublks_max unsupported or unobservable on this kernel')
    result=runner(['modprobe','ublk_drv','ublks_max='+str(target)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        check=False,timeout=30,shell=False)
    if result.returncode:raise RuntimeError('original module load requires reconciliation')
    try:observed=probe()
    except Exception as exc:raise RuntimeError('post-load observation unavailable') from exc
    if not observed['loaded'] or observed.get('ublks_max')!=target:raise RuntimeError('module load outcome differs')
    return {'status':'completed','reused':False,'observed':observed}


def main():
    try:result=activate_ublk(json.loads(sys.argv[1]));print(json.dumps(result));return 0
    except ValueError:return 2
    except Exception:return 75

if __name__=='__main__':raise SystemExit(main())
