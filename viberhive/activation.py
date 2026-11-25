"""Open the running picker when its Windows shortcut is launched again."""

import os
import time

from PyQt6.QtCore import QFileSystemWatcher, QTimer

from . import windows


def _request_path(lock_path, pid):
    # A request belongs to one lock acquisition and desktop session. An abandoned
    # request from a previous run cannot summon a future picker.
    generation = lock_path.stat().st_mtime_ns
    directory = lock_path.parent / "launcher"
    directory.mkdir(exist_ok=True)
    return directory / f"open-{windows.session_id()}-{pid}-{generation}"


def activate_running(lock, lock_path):
    found, pid, _, _ = lock.getLockInfo()
    if not found:
        return False
    try:
        request = _request_path(lock_path, pid)
        windows.grant_foreground(pid)
        request.touch()
        # Wait only in the new launcher. An older picker may own the lock
        # without supporting shortcut activation.
        deadline = time.monotonic() + 2
        while request.exists() and time.monotonic() < deadline:
            time.sleep(0.025)
        try:
            request.unlink()
        except FileNotFoundError:
            return True
    except OSError:
        pass
    return False


def listen_for_launch(lock_path, picker):
    request = _request_path(lock_path, os.getpid())
    watcher = QFileSystemWatcher([str(request.parent)], picker)

    def open_requested(*_, retry=True):
        try:
            request.unlink()
        except FileNotFoundError:
            return
        except OSError:
            # On Windows the directory notification may precede the launcher's
            # closing of its file handle.
            if retry:
                QTimer.singleShot(50, lambda: open_requested(retry=False))
            return
        picker.open_picker()

    watcher.directoryChanged.connect(open_requested)
    # A double-click can arrive during startup, before the watcher exists.
    QTimer.singleShot(0, open_requested)
