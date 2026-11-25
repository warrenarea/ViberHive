"""A cancellable Windows Discord draft transfer. All Qt operations stay on the GUI thread."""

import ctypes
import json
import ntpath
import os
import sys
from ctypes import wintypes as w
from pathlib import Path

from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer
from PyQt6.QtWidgets import QApplication

from . import windows


def clipboard_matches(mime, copied):
    if mime is None:
        return False
    if isinstance(copied, Path):
        urls = mime.urls()
        if len(urls) != 1 or not urls[0].isLocalFile():
            return False
        return os.path.normcase(os.path.abspath(urls[0].toLocalFile())) == os.path.normcase(
            os.path.abspath(copied)
        )
    return isinstance(copied, str) and bool(copied) and mime.hasText() and mime.text() == copied


def rendered_clipboard_sequence(copied):
    """Render Qt's delayed CF_HDROP or Unicode data and validate it under one clipboard lock."""
    api = windows.user32
    api.OpenClipboard.argtypes = [w.HWND]
    api.CloseClipboard.argtypes = []
    api.GetClipboardOwner.argtypes = []
    api.GetClipboardOwner.restype = w.HWND
    api.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    api.GetClipboardData.argtypes = [w.UINT]
    api.GetClipboardData.restype = w.HANDLE
    api.GetClipboardSequenceNumber.argtypes = []
    api.GetClipboardSequenceNumber.restype = w.DWORD
    if not api.OpenClipboard(None):
        return 0
    try:
        # Qt publishes its data through an OLE window in this process. Avoid
        # requesting delayed data from a clipboard owner that has superseded us.
        owner = api.GetClipboardOwner()
        pid = w.DWORD()
        if (
            not owner
            or not api.GetWindowThreadProcessId(owner, ctypes.byref(pid))
            or pid.value != os.getpid()
        ):
            return 0
        handle = api.GetClipboardData(15 if isinstance(copied, Path) else 13)
        if not handle:
            return 0
        if isinstance(copied, Path):
            query = ctypes.WinDLL("shell32").DragQueryFileW
            query.argtypes = [w.HANDLE, w.UINT, w.LPWSTR, w.UINT]
            query.restype = w.UINT
            if query(handle, 0xFFFFFFFF, None, 0) != 1:
                return 0
            length = query(handle, 0, None, 0)
            if not 0 < length <= 32767:
                return 0
            path = ctypes.create_unicode_buffer(length + 1)
            if query(handle, 0, path, len(path)) != length or ntpath.normcase(
                path.value
            ) != ntpath.normcase(os.path.abspath(copied)):
                return 0
        else:
            kernel = ctypes.WinDLL("kernel32")
            kernel.GlobalSize.argtypes = [w.HANDLE]
            kernel.GlobalSize.restype = ctypes.c_size_t
            kernel.GlobalLock.argtypes = [w.HANDLE]
            kernel.GlobalLock.restype = ctypes.c_void_p
            kernel.GlobalUnlock.argtypes = [w.HANDLE]
            expected = copied.encode("utf-16-le") + b"\x00\x00"
            if kernel.GlobalSize(handle) < len(expected):
                return 0
            buffer = kernel.GlobalLock(handle)
            if not buffer:
                return 0
            try:
                if ctypes.string_at(buffer, len(expected)) != expected:
                    return 0
            finally:
                kernel.GlobalUnlock(handle)
        seq = api.GetClipboardSequenceNumber()
    finally:
        api.CloseClipboard()
    return seq if seq and seq == api.GetClipboardSequenceNumber() else 0


class DiscordPaste(QObject):
    """Open Discord on selection, then hand the worker a verified clipboard sequence."""

    def __init__(self, picker):
        super().__init__(picker)
        self.picker = picker
        self.process = None
        self.timeout = QTimer(self, singleShot=True)
        self.timeout.timeout.connect(self.expired)
        self.verify_timer = QTimer(self, singleShot=True, interval=60)
        self.verify_timer.timeout.connect(self.verify_clipboard)
        self.cancel()
        QApplication.instance().aboutToQuit.connect(self.cancel)

    def cancel(self):
        self.timeout.stop()
        self.verify_timer.stop()
        self.request = None
        self.ready = False
        self.sequence = 0
        self.copied = self.finished = self.outcome = None
        process, self.process = self.process, None
        if process and process.state() != QProcess.ProcessState.NotRunning:
            process.closeWriteChannel()
            process.kill()

    def record(self, ok, message):
        self.timeout.stop()
        self.verify_timer.stop()
        self.outcome = (ok, message)
        if self.finished:
            callback = self.finished
            self.cancel()
            callback(ok, message)

    def expired(self):
        process, self.process = self.process, None
        if process:
            process.closeWriteChannel()
            process.kill()
        self.record(
            False,
            "Discord did not confirm its message box in time. The copied item is ready for Ctrl+V.",
        )

    @staticmethod
    def foreground():
        return windows.foreground()

    @staticmethod
    def command(origin, previous, owner):
        exe = str(Path(sys.executable).with_name("python.exe"))
        script = str(Path(__file__).with_name("discord_native.py").resolve())
        return exe, [
            "-I",
            "-B",
            script,
            str(origin),
            str(int(previous or 0)),
            str(owner),
        ]

    def present(self, preferred_hwnd, *, origin):
        self.cancel()
        self.request = (preferred_hwnd, origin)
        if not windows.WINDOWS:
            self.record(
                False,
                "Automatic paste requires Windows. Paste the copied item with Ctrl+V.",
            )
            return
        current = self.foreground()
        if current and current == preferred_hwnd:
            origin = current
        if not origin or current != origin:
            self.record(
                False,
                "The foreground window changed. The copied item is ready for manual paste.",
            )
            return

        p = QProcess(self)
        self.process = p
        exe, args = self.command(origin, preferred_hwnd, int(self.picker.winId()))
        p.setProgram(exe)
        p.setArguments(args)
        env = QProcessEnvironment.systemEnvironment()
        for name in ("SERPAPI_API_KEY", "KLIPY_APP_KEY"):
            env.remove(name)
        p.setProcessEnvironment(env)
        granted = False
        result = None

        def receive():
            nonlocal granted, result
            while p is self.process and p.canReadLine():
                line = bytes(p.readLine(2048)).strip()
                if not granted:
                    if not line.isdigit() or not 0 < int(line) < 2**32 - 1:
                        p.kill()
                        return
                    windows.grant_foreground(int(line))
                    granted = True
                    p.write(b"open\n")
                elif line == b"ready" and not self.ready:
                    self.ready = True
                    self.timeout.stop()
                    self.send_when_ready()
                else:
                    try:
                        payload = json.loads(line)
                        if (
                            isinstance(payload, list)
                            and len(payload) == 2
                            and type(payload[0]) is bool
                            and isinstance(payload[1], str)
                        ):
                            result = payload
                    except (ValueError, UnicodeError):
                        pass

        def finish(code, status):
            if p is self.process:
                receive()
                self.process = None
                if code == 0 and status == QProcess.ExitStatus.NormalExit and result:
                    self.record(*result)
                else:
                    self.record(
                        False,
                        "Windows could not complete the draft transfer. The copied item is ready for Ctrl+V.",
                    )
            p.deleteLater()

        def error(kind):
            if kind == QProcess.ProcessError.FailedToStart:
                finish(-1, QProcess.ExitStatus.CrashExit)

        p.readyReadStandardOutput.connect(receive)
        p.finished.connect(finish)
        p.errorOccurred.connect(error)
        p.start()
        self.timeout.start(46000)

    def start(self, preferred_hwnd, copied, finished, *, origin):
        if self.request != (preferred_hwnd, origin):
            self.present(preferred_hwnd, origin=origin)
        self.copied = copied
        self.finished = finished
        if self.outcome:
            self.record(*self.outcome)
            return
        self.attempts = 0
        self.verify_clipboard()

    def verify_clipboard(self):
        if not clipboard_matches(QApplication.clipboard().mimeData(), self.copied):
            self.record(False, "The clipboard changed before the paste could start.")
            return
        self.sequence = rendered_clipboard_sequence(self.copied)
        if not self.sequence:
            # Discord and clipboard managers can briefly hold the clipboard after its update.
            self.attempts += 1
            if self.attempts < 10:
                self.verify_timer.start()
            else:
                self.record(
                    False,
                    "Windows could not verify the copied item. Check the clipboard before pasting.",
                )
            return
        self.send_when_ready()

    def send_when_ready(self):
        if self.process and self.ready and self.sequence:
            self.process.write(f"paste {self.sequence}\n".encode("ascii"))
            self.sequence = 0
            self.timeout.start(32000)
