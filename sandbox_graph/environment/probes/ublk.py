"""Read the loaded kernel's actual ublk limit, never the modprobe config target."""
from pathlib import Path
import stat


def inspect_ublk(*,sys_root='/sys',dev_root='/dev'):
    module=Path(sys_root)/'module/ublk_drv';device=Path(dev_root)/'ublk-control'
    try:loaded=stat.S_ISDIR(module.stat().st_mode)
    except FileNotFoundError:loaded=False
    except OSError:loaded=None
    value={'loaded':loaded,'ublks_max':None,'evidence_level':'unavailable',
        'control_device_exists':False,'reason':None}
    try:value['control_device_exists']=stat.S_ISCHR(device.stat().st_mode)
    except FileNotFoundError:pass
    except OSError:value['reason']='control_device_observation_unavailable'
    if value['loaded']:
        try:
            with (module/'parameters/ublks_max').open() as stream:raw=stream.read(65)
            if len(raw)>64 or not raw.strip().isdigit():raise ValueError('invalid kernel parameter')
            value['ublks_max']=int(raw);value['evidence_level']='runtime_effective'
        except FileNotFoundError:value['reason']='parameter_not_exposed_by_loaded_kernel'
        except (OSError,ValueError):value['reason']='kernel_parameter_unreadable'
    else:value['reason']='module_not_loaded' if loaded is False else 'module_observation_unavailable'
    return value
