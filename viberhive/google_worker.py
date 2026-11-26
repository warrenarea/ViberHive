"""Private pipes connect the picker to a restartable Google WebEngine process."""

import base64
import json
import os
import re
import sys
import threading
from dataclasses import asdict
from pathlib import Path

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, Qt, QTimer, pyqtSignal

from viberhive import windows
from viberhive.model import THUMB_BATCH_BYTES, THUMB_BATCH_COUNT, GoogleBatch, Item, unpack_thumbnail_pixels

MAX_REPLY = 12 * 1024**2
MAX_COMMAND = 96 * 1024


def line(message):
    return json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"


def unpack_thumbnails(records):
    frames = {}
    remaining = THUMB_BATCH_BYTES
    if isinstance(records, dict):
        for index, (key, record) in enumerate(records.items()):
            if index >= THUMB_BATCH_COUNT or remaining < 256:
                break
            if not re.fullmatch(r"[0-9a-f]{64}", key):
                continue
            frame = unpack_thumbnail_pixels(record, remaining)
            if frame:
                frames[key] = frame
                remaining -= len(frame[2])
    return frames


def pack_thumbnails(frames):
    return {key: (w, h, base64.b64encode(data).decode("ascii")) for key, (w, h, data) in frames.items()}


class GoogleClient(QObject):
    """The picker owns only this pipe and the native grid. No browser window owns the picker."""

    resultsReady = pyqtSignal(object)
    batchFinished = pyqtSignal(object)
    thumbnailsReady = pyqtSignal(str, object)
    verificationResumed = pyqtSignal()
    returnToPicker = pyqtSignal()
    alternativeRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = None
        self.worker_pid = 0
        self.query = ""
        self.generation = 0
        self.buffer = bytearray()
        self.timer = QTimer(self, singleShot=True)
        self.timer.timeout.connect(self._timed_out)

    @staticmethod
    def _program():
        exe = Path(sys.executable)
        if windows.WINDOWS:
            exe = exe.with_name("python.exe")
        return str(exe), ["-I", "-B", str(Path(__file__).resolve())]

    def _launch(self):
        p = QProcess(self)
        self.process = p
        self.worker_pid = 0
        self.buffer.clear()
        exe, args = self._program()
        p.setProgram(exe)
        p.setArguments(args)
        env = QProcessEnvironment.systemEnvironment()
        for name in (
            "SERPAPI_API_KEY",
            "KLIPY_APP_KEY",
            "QTWEBENGINE_DISABLE_SANDBOX",
            "QTWEBENGINE_CHROMIUM_FLAGS",
            "QTWEBENGINE_REMOTE_DEBUGGING",
        ):
            env.remove(name)
        p.setProcessEnvironment(env)
        p.setStandardErrorFile(QProcess.nullDevice())
        p.readyReadStandardOutput.connect(lambda: self._read(p))
        p.finished.connect(lambda *_: self._lost(p))
        p.errorOccurred.connect(
            lambda kind: self._lost(p) if kind == QProcess.ProcessError.FailedToStart else None
        )
        p.start()

    def _send(self, op, **fields):
        p = self.process
        if p is None:
            return False
        data = line({"id": self.generation, "op": op, **fields})
        return len(data) <= MAX_COMMAND and p.write(data) == len(data)

    def search(self, query, *, references=False, seed=None):
        self.generation += 1
        self.query = query
        if self.process is None:
            self._launch()
        self.timer.start(45000)
        return self._send(
            "search", query=query, references=references, seed=asdict(seed) if seed else None
        )

    def more_results(self, item=None, *, related=True):
        self.timer.start(24000)
        return self._send("more", item=asdict(item) if item else None, related=related)

    def open_verification(self):
        if not self.process:
            return False
        windows.grant_foreground(self.worker_pid)
        parent = self.parent()
        center = parent.frameGeometry().center() if hasattr(parent, "frameGeometry") else None
        # Stop on the window acknowledgment. Human interaction has no timeout.
        self.timer.start(10000)
        return self._send(
            "verify",
            owner=os.getpid(),
            center=[center.x(), center.y()] if center is not None else None,
        )

    def pause(self):
        self.generation += 1
        self.timer.stop()
        self._send("pause")

    def stop_search(self):
        self.generation += 1
        self.query = ""
        self.timer.stop()
        self._send("stop")

    def dispose(self):
        self.query = ""
        self.timer.stop()
        p, self.process = self.process, None
        if p is not None:
            # EOF is the worker's shutdown signal, including if the picker itself exits.
            p.closeWriteChannel()
            if p.state() != QProcess.ProcessState.NotRunning and not p.waitForFinished(1200):
                p.kill()
                p.waitForFinished(400)
            p.deleteLater()

    def _timed_out(self):
        if self.process:
            self._lost(
                self.process, "Google stopped responding. Choose Retry to reopen this search."
            )

    def _lost(
        self,
        p,
        message="The Google browser closed unexpectedly. Choose Retry to reopen this search.",
    ):
        if p is not self.process:
            return
        self.process = None
        self.timer.stop()
        self.buffer.clear()
        if p.state() != QProcess.ProcessState.NotRunning:
            p.closeWriteChannel()
            p.kill()
        p.deleteLater()
        if self.query:
            self.batchFinished.emit(GoogleBatch(self.query, "session", "error", [], message))

    def _read(self, p):
        if p is not self.process:
            return
        # Any bytes retained from the last readyRead are already known to have no newline.
        start = len(self.buffer)
        self.buffer.extend(bytes(p.readAllStandardOutput()))
        try:
            while True:
                end = self.buffer.find(b"\n", start)
                if end < 0:
                    break
                start = 0
                if end > MAX_REPLY:
                    raise ValueError()
                packet = json.loads(self.buffer[:end])
                del self.buffer[: end + 1]
                if packet.get("event") == "started":
                    pid = packet["pid"]
                    if type(pid) is not int or not 0 < pid < 2**32 - 1:
                        raise ValueError()
                    self.worker_pid = pid
                    continue
                if packet["id"] != self.generation or not self.query:
                    continue
                if packet["query"] != self.query:
                    continue
                if packet.get("event") == "return":
                    self.returnToPicker.emit()
                    continue
                if packet.get("event") == "alternative":
                    self.timer.stop()
                    self.alternativeRequested.emit()
                    continue
                if packet.get("event") == "opened":
                    self.timer.stop()
                    continue
                if packet.get("event") == "resuming":
                    self.timer.start(45000)
                    self.verificationResumed.emit()
                    continue
                if packet.get("event") == "thumbnails":
                    self.thumbnailsReady.emit(self.query, unpack_thumbnails(packet.get("rendered")))
                    if p is not self.process:
                        return
                    continue
                batch = GoogleBatch(
                    self.query,
                    packet["kind"],
                    packet["status"],
                    [Item(**item) for item in packet["items"]],
                    packet["message"],
                    {
                        key: base64.b64decode(val, validate=True)
                        for key, val in packet["posters"].items()
                    },
                    packet["can_expand"],
                    packet.get("parent_key", ""),
                    packet.get("parent_identity", ""),
                    unpack_thumbnails(packet.get("rendered")),
                )
                if batch.status == "partial":
                    self.resultsReady.emit(batch)
                else:
                    self.timer.stop()
                    self.batchFinished.emit(batch)
                if p is not self.process:
                    return
            if len(self.buffer) > MAX_REPLY:
                raise ValueError()
        except (KeyError, TypeError, ValueError, RecursionError, AttributeError):
            self._lost(
                p, "The Google browser returned unreadable results. Choose Retry to reopen it."
            )


class Commands(QObject):
    received = pyqtSignal(object)

    def read(self):
        # A pipe is not select()-able on Windows. Only the Qt thread touches the web view.
        pending = bytearray()
        try:
            while chunk := os.read(0, 8192):
                pending.extend(chunk)
                while b"\n" in pending:
                    end = pending.index(10)
                    if end > MAX_COMMAND:
                        return
                    self.received.emit(json.loads(pending[:end]))
                    del pending[: end + 1]
                if len(pending) > MAX_COMMAND:
                    return
        except (OSError, ValueError):
            pass
        finally:
            self.received.emit(None)


def run_worker(browser_factory=None):
    from PyQt6.QtNetwork import QNetworkProxyFactory
    from PyQt6.QtWidgets import QApplication

    from viberhive.google_browser import GoogleBrowser
    from viberhive.theme import icon

    windows.dpi_awareness()
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(["ViberHive Google Images"])
    app.setWindowIcon(icon())
    app.setQuitOnLastWindowClosed(False)
    QNetworkProxyFactory.setUseSystemConfiguration(True)
    browser = (browser_factory or GoogleBrowser)()
    commands = Commands()
    generation = 0
    picker_pid = 0

    def reply(batch):
        packet = {
            "id": generation,
            "query": batch.query,
            "kind": batch.kind,
            "status": batch.status,
            "message": batch.message,
            "items": [asdict(item) for item in batch.items],
            "posters": {
                k: base64.b64encode(v).decode("ascii")
                for k, v in batch.inline_thumbnails.items()
            },
            "can_expand": batch.can_expand,
            "parent_key": batch.parent_key,
            "parent_identity": batch.parent_identity,
        }
        if batch.rendered_thumbnails:
            packet["rendered"] = pack_thumbnails(batch.rendered_thumbnails)
        data = line(packet)
        if len(data) > MAX_REPLY and "rendered" in packet:
            # A large metadata batch still reaches the picker with its normal poster sources.
            del packet["rendered"]
            data = line(packet)
        if len(data) > MAX_REPLY:
            data = line(
                {
                    "id": generation,
                    "query": batch.query,
                    "kind": batch.kind,
                    "status": "error",
                    "message": "Google returned too much data in one batch.",
                    "items": [],
                    "posters": {},
                    "can_expand": False,
                }
            )
        write(data)

    def write(data):
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except (BrokenPipeError, OSError):
            app.quit()

    def return_focus():
        windows.grant_foreground(picker_pid)
        write(line({"id": generation, "query": browser.query, "event": "return"}))

    def handle(command):
        nonlocal generation, picker_pid
        if command is None:
            app.quit()
            return
        generation, op = command["id"], command["op"]
        if op == "search":
            browser.search(
                command["query"],
                references=command["references"],
                seed=Item(**command["seed"]) if command["seed"] else None,
            )
        elif op == "more":
            item = Item(**command["item"]) if command["item"] else None
            if not browser.more_results(item, related=command["related"]):
                reply(
                    GoogleBatch(
                        browser.query,
                        "session",
                        "error",
                        [],
                        "Google could not continue. Choose Retry to reopen the search.",
                    )
                )
        elif op == "verify":
            picker_pid = command["owner"]
            if browser.open_verification(command["center"]):
                write(line({"id": generation, "query": browser.query, "event": "opened"}))
            else:
                reply(
                    GoogleBatch(
                        browser.query,
                        "session",
                        "error",
                        [],
                        "The Google prompt has expired. Choose Retry to reopen the search.",
                    )
                )
        elif op == "stop":
            browser.stop_search()
        elif op == "pause":
            browser.pause()

    commands.received.connect(handle, Qt.ConnectionType.QueuedConnection)
    if hasattr(browser, "resultsReady"):
        browser.resultsReady.connect(reply)
    browser.batchFinished.connect(reply)
    browser.thumbnailsReady.connect(
        lambda query, frames: write(line({"id": generation, "query": query, "event": "thumbnails",
                                          "rendered": pack_thumbnails(frames)}))
    )
    browser.returnToPicker.connect(return_focus)
    browser.verificationResumed.connect(
        lambda: write(line({"id": generation, "query": browser.query, "event": "resuming"}))
    )
    browser.alternativeRequested.connect(
        lambda: write(line({"id": generation, "query": browser.query, "event": "alternative"}))
    )
    write(line({"event": "started", "pid": os.getpid()}))
    threading.Thread(target=commands.read, name="picker-input", daemon=True).start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(run_worker())
