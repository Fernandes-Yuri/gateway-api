"""Bloqueio entre processos para arquivos compartilhados do gateway."""
import fcntl
from pathlib import Path
from contextlib import contextmanager


@contextmanager
def file_lock(path):
    lock_path = Path(str(path) + '.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


