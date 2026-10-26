"""Stop only the original journaled process group; never replay installation."""
import fcntl
import os
from pathlib import Path
import signal
import time
from .probes.inventory import parse_proc_stat


def cancel_original(journal,*,operation_id,input_hash,lock_dir,timeout=5):
    if not 0<timeout<=10:raise ValueError('bounded cancellation required')
    row=journal.get(operation_id)
    if not row or row['input_hash']!=input_hash:raise PermissionError('original journal identity differs')
    if row['state'] in {'completed','failed','cancelled'}:return row
    owned=row.get('execution')
    if not owned:return row|{'cancel_observation':'original_process_identity_missing'}
    if (owned.get('machine_id')!=journal.machine_id or owned.get('boot_id')!=journal.boot_id
            or owned.get('pgid')!=owned.get('pid')):
        return row|{'cancel_observation':'original_process_identity_unverified'}
    if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'):
        return row|{'cancel_observation':'pidfd_unavailable'}
    def ticks(pid):return parse_proc_stat(Path('/proc',str(pid),'stat').read_text())['start_ticks']
    try:
        if ticks(owned['pid'])!=owned['start_ticks']:return row|{'cancel_observation':'pid_reused'}
    except FileNotFoundError:pass
    journal.mark_cancel(operation_id,input_hash)
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        members=[]
        for path in Path('/proc').iterdir():
            if not path.name.isdecimal():continue
            try:
                raw=(path/'stat').read_text();parts=raw[raw.rfind(')')+2:].split()
                if int(parts[2])==owned['pgid'] and parts[0]!='Z':members.append((int(path.name),int(parts[19])))
            except FileNotFoundError:continue
        for pid,start in members:
            fd=None
            try:
                fd=os.pidfd_open(pid)
                if ticks(pid)!=start:return journal.get(operation_id)|{'cancel_observation':'child_identity_changed'}
                signal.pidfd_send_signal(fd,signal.SIGKILL)
            except ProcessLookupError:pass
            finally:
                if fd is not None:os.close(fd)
        latest=journal.get(operation_id)
        if latest['state'] in {'completed','failed','cancelled'}:return latest
        if not members:
            # The helper or a descendant may still own the inherited OS lock.
            fd=os.open(Path(lock_dir)/'host.lock',os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
            try:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:pass
                else:return journal.finish_cancel(operation_id,input_hash)
            finally:os.close(fd)
        time.sleep(.02)
    return journal.get(operation_id)|{'cancel_observation':'stop_not_yet_confirmed'}


def compensate_original(journal,*,operation_id,input_hash,lock_dir,backup_dir):
    """CAS restore only a completed original file write, never a caller path."""
    from .file_transaction import FileMutationReceipt,compensate_file
    row=journal.get(operation_id)
    if not row or row['input_hash']!=input_hash:raise PermissionError('original file operation differs')
    value=(row.get('result') or {}).get('file')
    if row['state']!='completed' or not value:raise ValueError('completed original file receipt required')
    fd=os.open(Path(lock_dir)/'host.lock',os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return compensate_file(FileMutationReceipt(**value),backup_dir=backup_dir)
    finally:os.close(fd)
