import os
from pathlib import Path
from railassist.domain.errors import InstanceBusy


class InstanceLock:
    """OS advisory lock. The OS releases ownership even after a process crash."""

    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        handle = self.path.open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise InstanceBusy("此数据目录已有运行实例，请先退出该实例。") from exc
        self.handle = handle
        return self

    def __exit__(self, *args):
        if self.handle is not None:
            self.handle.close()
            self.handle = None

