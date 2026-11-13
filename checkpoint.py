"""Moderator-only, atomic JSON checkpoints. No player communications here."""

import fcntl
import json
import math
import os
import stat
import tempfile
from pathlib import Path


class CheckpointError(RuntimeError):
    pass


class Checkpoint:
    def __init__(self, path):
        self.path = Path(path)
        self.lock_fd = None

    def acquire(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.path.parent.stat()
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise CheckpointError("Checkpoint directory must be owned by the moderator with mode 0700")
        self.lock_fd = os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.close()
            raise CheckpointError("Another moderator owns this checkpoint") from exc

    def load(self):
        if not self.path.exists():
            return None
        if self.path.is_symlink():
            raise CheckpointError("Refusing a symlink checkpoint")
        try:
            with self.path.open() as handle:
                record = json.load(handle)
            if not isinstance(record, dict) or record.get('schema') != 1:
                raise ValueError('unknown checkpoint schema')
            return record
        except (ValueError, OSError) as exc:
            raise CheckpointError("Checkpoint is unreadable; refusing to silently reset the match") from exc

    def save(self, record):
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix='.checkpoint-', dir=self.path.parent)
            with os.fdopen(fd, 'w') as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(record, handle, sort_keys=True, allow_nan=False)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except (OSError, ValueError) as exc:
            raise CheckpointError('Unable to commit checkpoint; stopping rather than acknowledging unsaved actions') from exc
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def close(self):
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
