"""ViberHive for Windows. Launch with --tray to start hidden."""

import os
import sys
from pathlib import Path

from viberhive import __version__, windows
from viberhive.shortcut import APP_USER_MODEL_ID


def check_browser(app):
    """Load a bundled page and execute JavaScript in the actual Chromium renderer."""
    from PyQt6 import sip
    from PyQt6.QtCore import QEventLoop, QTimer, QUrl
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile

    profile = QWebEngineProfile(app)
    page = QWebEnginePage(profile)
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    success = False

    def read_title(title):
        nonlocal success
        success = title == "ViberHive browser check"
        loop.quit()

    def loaded(ok):
        if ok:
            page.runJavaScript("document.title", read_title)
        else:
            loop.quit()

    try:
        page.loadFinished.connect(loaded)
        timer.start(20000)
        page.setHtml("<title>ViberHive browser check</title>", QUrl("about:blank"))
        loop.exec()
    finally:
        timer.stop()
        sip.delete(page)
        sip.delete(profile)
    if not success:
        raise RuntimeError("The Google Images browser could not render its local test page.")


def check_video():
    """Confirm that Qt can actually deliver an H.264 frame on this installation."""
    from PyQt6.QtCore import QBuffer, QEventLoop, QIODevice, QTimer, QUrl
    from PyQt6.QtMultimedia import QMediaPlayer, QVideoSink

    loop = QEventLoop()
    timer = QTimer(singleShot=True, interval=10000)
    buffer = QBuffer()
    buffer.setData((Path(__file__).parent / "assets" / "playback-check.mp4").read_bytes())
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    player = QMediaPlayer()
    sink = QVideoSink()
    success = False

    def received(frame):
        nonlocal success
        if frame.isValid() and frame.width() == 48 and frame.height() == 32:
            success = True
            loop.quit()

    try:
        sink.videoFrameChanged.connect(received)
        player.errorOccurred.connect(lambda *_: loop.quit())
        timer.timeout.connect(loop.quit)
        player.setVideoSink(sink)
        player.setSourceDevice(buffer)
        timer.start()
        player.play()
        if not success and player.error() == QMediaPlayer.Error.NoError:
            loop.exec()
    finally:
        timer.stop()
        player.stop()
        player.setSource(QUrl())
        player.setVideoSink(None)
        buffer.close()
    if not success:
        raise RuntimeError("The Qt video player could not decode the bundled H.264 test clip.")


def main():
    windows.set_app_user_model_id(APP_USER_MODEL_ID)
    windows.dpi_awareness()
    # Clips are supplied as self-contained memory buffers. FFmpeg does not
    # need any file or network protocol for nested media references.
    os.environ["QT_FFMPEG_PROTOCOL_WHITELIST"] = "none"
    # Use the supported default renderer sandbox, including during the installation check.
    # Browser flags from unrelated Qt applications can override it or expose debugging ports.
    for name in (
        "QTWEBENGINE_DISABLE_SANDBOX",
        "QTWEBENGINE_CHROMIUM_FLAGS",
        "QTWEBENGINE_REMOTE_DEBUGGING",
    ):
        os.environ.pop(name, None)
    if (
        "--check" in sys.argv
        and sys.platform.startswith("linux")
        and not any(os.getenv(name) for name in ("DISPLAY", "WAYLAND_DISPLAY"))
    ):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from viberhive.activation import activate_running, listen_for_launch
    from viberhive.service import Service
    from viberhive.settings import Settings
    from viberhive.store import Store
    from viberhive.theme import STYLE, icon
    from viberhive.ui import Picker
    from PyQt6.QtCore import QLockFile, QStandardPaths, Qt, QTimer
    from PyQt6.QtGui import QFont
    from PyQt6.QtNetwork import QNetworkProxyFactory
    from PyQt6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication([sys.argv[0]])
    QNetworkProxyFactory.setUseSystemConfiguration(True)
    app.setApplicationName("ViberHive")
    app.setApplicationVersion(__version__)
    if "--check" in sys.argv:
        from io import BytesIO

        from viberhive.service import rebuild
        from PIL import Image

        source = BytesIO()
        first = Image.new("RGB", (12, 9), "darkblue")
        first.save(
            source,
            "GIF",
            save_all=True,
            append_images=[Image.new("RGB", first.size, "gold")],
            duration=100,
            loop=0,
        )
        output = Image.open(BytesIO(rebuild(source.getvalue(), "gif")))
        if output.n_frames != 2:
            raise RuntimeError("The media decoder check failed.")
        if windows.WINDOWS and windows.dpapi(windows.dpapi(b"ViberHive"), True) != b"ViberHive":
            raise RuntimeError("The Windows key storage check failed.")
        check_browser(app)
        check_video()
        print("The browser renderer, GIF decoder, H.264 player and platform checks passed.")
        return 0
    app.setApplicationDisplayName("ViberHive")
    app.setQuitOnLastWindowClosed(False)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(STYLE)
    app.setWindowIcon(icon())
    root = (
        Path(os.environ["LOCALAPPDATA"]) / "ViberHive"
        if os.name == "nt"
        else Path(
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
        )
    )
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "running.lock"
    lock = QLockFile(str(lock_path))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        if "--tray" not in sys.argv and not activate_running(lock, lock_path):
            QMessageBox.information(
                None,
                "ViberHive",
                "A running copy could not be raised. Open it from its tray icon or with its configured hotkey.",
            )
        return 0
    prefs = Settings(root)
    store = Store(root, budget=prefs.values["cache_mib"] * 1024**2)
    picker = Picker(Service(store, prefs))
    listen_for_launch(lock_path, picker)
    cleanup = QTimer(app, interval=60 * 60 * 1000)
    cleanup.timeout.connect(lambda: picker.jobs.run("action", store.prune, lambda *_: None))
    cleanup.start()
    if "--tray" not in sys.argv or not QSystemTrayIcon.isSystemTrayAvailable():
        QTimer.singleShot(0, picker.open_picker)
    try:
        return app.exec()
    finally:
        picker.jobs.stop()
        # Let any in-progress cache transaction complete before another instance can open the store.
        for pool in picker.jobs.pools.values():
            pool.shutdown(wait=True, cancel_futures=True)
        store.db.close()
        lock.unlock()


if __name__ == "__main__":
    sys.exit(main())
