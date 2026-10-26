"""Local owner identity for the in-process observer (never a remote process kill)."""
import os
from pathlib import Path
import socket


def process_identity():
    # PID alone is not an identity across boot, exec or PID reuse. Fail closed
    # on hosts without the Linux identity sources used by this adapter.
    pid = os.getpid()
    stat = Path(f'/proc/{pid}/stat').read_text()
    return {'host': socket.gethostname(), 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'pid': pid, 'start_ticks': int(stat[stat.rfind(')') + 2:].split()[19])}
