"""A single embedded clip player. The media backend receives only a checked memory buffer."""

from threading import Event

from PyQt6.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QObject,
    QRect,
    QSignalBlocker,
    QSize,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QDesktopServices, QGuiApplication, QPainter
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QVBoxLayout,
    QWidget,
)

from .clip_media import get_video
from .providers import clip_link


class VideoFrame(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.image = None
        self.setMinimumSize(1, 1)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#1d1e23"))
        if self.image is not None:
            size = self.image.size().scaled(
                self.size(), Qt.AspectRatioMode.KeepAspectRatio
            )
            dest = QRect(self.rect().topLeft(), size)
            dest.moveCenter(self.rect().center())
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(dest, self.image)


class ClipTimeline(QSlider):
    """Make the whole height of the timeline clickable and draggable."""

    def scrub(self, x, *, begin=False):
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        control = QStyle.ComplexControl.CC_Slider
        sub = QStyle.SubControl
        handle = self.style().subControlRect(control, option, sub.SC_SliderHandle, self)
        groove = self.style().subControlRect(control, option, sub.SC_SliderGroove, self)
        if begin:
            center = handle.left() + handle.width() // 2
            self.grab_offset = x - center if handle.left() <= x <= handle.right() else 0
        offset = x - self.grab_offset - groove.left() - handle.width() // 2
        value = QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), offset, max(1, groove.width() - handle.width()),
            option.upsideDown,
        )
        self.setSliderPosition(value)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.setSliderDown(True)
        self.scrub(event.position().toPoint().x(), begin=True)
        event.accept()

    def mouseMoveEvent(self, event):
        if not self.isSliderDown() or not event.buttons() & Qt.MouseButton.LeftButton:
            return super().mouseMoveEvent(event)
        self.scrub(event.position().toPoint().x())
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.isSliderDown():
            return super().mouseReleaseEvent(event)
        self.scrub(event.position().toPoint().x())
        self.setSliderDown(False)
        event.accept()


def playback_time(ms):
    minutes, seconds = divmod(max(0, ms) // 1000, 60)
    return f"{minutes}:{seconds:02d}"


class ClipPlayerWidget(QWidget):
    activeChanged = pyqtSignal(bool)
    loadRequested = pyqtSignal()
    websiteRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.active = False
        self.buffer = self.player = self.audio = self.sink = None
        self.sound_preference = False
        self.canvas = VideoFrame(self)
        self.canvas.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.addWidget(self.canvas, 1)
        self.message = QLabel()
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.message.hide()
        layout.addWidget(self.message)
        self.transport = QWidget()
        timeline_row = QHBoxLayout(self.transport)
        timeline_row.setContentsMargins(0, 0, 0, 0)
        timeline_row.setSpacing(5)
        self.timeline = ClipTimeline(Qt.Orientation.Horizontal)
        self.timeline.setObjectName("clipTimeline")
        self.timeline.setAccessibleName("Clip timeline")
        self.timeline.setToolTip("Click or drag to seek. Arrow keys move one second.")
        self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.timeline.setSingleStep(1000)
        self.timeline.setPageStep(5000)
        self.timeline.setMinimumWidth(30)
        self.timeline.valueChanged.connect(self.seek_requested)
        self.timeline.sliderReleased.connect(self.commit_seek)
        timeline_row.addWidget(self.timeline, 1)
        self.clock = QLabel("0:00 / 0:00")
        self.clock.setObjectName("clipClock")
        self.clock.setAccessibleName("Playback time")
        timeline_row.addWidget(self.clock)
        layout.addWidget(self.transport)
        self.seek_timer = QTimer(self, singleShot=True, interval=50)
        self.seek_timer.timeout.connect(self.commit_seek)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.play_button = QPushButton("Play clip")
        self.play_button.clicked.connect(self.toggle_play)
        row.addWidget(self.play_button)
        self.sound = QPushButton("Mute")
        self.sound.setAccessibleName("Toggle clip sound")
        self.sound.clicked.connect(self.toggle_mute)
        self.sound.hide()
        row.addWidget(self.sound)
        row.addStretch()
        layout.addLayout(row)
        actions = QHBoxLayout()
        actions.setSpacing(4)
        self.paste = QPushButton("Paste into Discord")
        self.paste.setObjectName("clipAction")
        self.paste.setToolTip("Paste this clip’s link into Discord")
        actions.addWidget(self.paste)
        self.unpin = QPushButton("Unpin")
        self.unpin.setObjectName("clipAction")
        self.unpin.setAccessibleName("Unpin clip preview")
        self.unpin.setToolTip("Resume previewing clips by hover")
        self.unpin.hide()
        actions.addWidget(self.unpin)
        actions.addStretch()
        layout.addLayout(actions)
        self.website = QPushButton("Watch on website ↗")
        self.website.clicked.connect(self.websiteRequested)
        self.website.hide()
        layout.addWidget(self.website)
        self.hide()

    def sizeHint(self):
        hint = super().sizeHint()
        # The canvas uses the available pane, without increasing its minimum width.
        return QSize(min(135, hint.width()), hint.height())

    def _set_active(self, value):
        if value == self.active:
            return
        self.active = value
        self.canvas.setVisible(value)
        self.sound.setVisible(value and self.audio is not None)
        self.activeChanged.emit(value)

    def reset(self):
        self._set_active(False)
        self.seek_timer.stop()
        with QSignalBlocker(self.timeline):
            self.timeline.setSliderDown(False)
            self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.clock.setText("0:00 / 0:00")
        if self.player:
            self.player.stop()
            self.player.setSource(QUrl())
        if self.buffer:
            self.buffer.deleteLater()
            self.buffer = None
        self.canvas.image = None
        self.canvas.update()
        self.message.hide()
        self.website.hide()
        self.play_button.setEnabled(True)
        self.play_button.setText("Play clip")

    def loading(self):
        self.message.hide()
        self.website.hide()
        self.play_button.setText("Loading…")
        self.play_button.setEnabled(False)

    def failed(self, message):
        self.reset()
        self.message.setText(message)
        self.message.show()
        self.website.show()

    def toggle_mute(self):
        if self.audio:
            self.sound_preference = not self.audio.isMuted()
            self._mute(self.sound_preference)

    def _mute(self, muted):
        if self.audio:
            self.audio.setMuted(muted)
            self.sound.setText("Unmute" if muted else "Mute")

    def toggle_play(self):
        if not self.active:
            self.loadRequested.emit()
        elif self.player.isPlaying():
            self.player.pause()
        else:
            if (self.player.mediaStatus() == self.player.MediaStatus.EndOfMedia
                    and self.player.position() >= self.player.duration()):
                self.player.setPosition(0)
            self.player.play()

    def _setup_player(self):
        from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

        self.player = QMediaPlayer(self)
        self.sink = QVideoSink(self)
        self.player.setVideoSink(self.sink)
        if QGuiApplication.platformName() not in ("offscreen", "minimal"):
            self.audio = QAudioOutput(self)
            self.player.setAudioOutput(self.audio)
        self.sink.videoFrameChanged.connect(self.frame)
        self.player.playingChanged.connect(self.playing)
        self.player.durationChanged.connect(self.sync_timeline)
        self.player.seekableChanged.connect(self.sync_timeline)
        self.player.positionChanged.connect(self.update_position)
        self.player.errorOccurred.connect(self.playback_error)

    def sync_timeline(self, *_):
        if self.active:
            duration = max(0, min(self.player.duration(), 2**31 - 1))
            with QSignalBlocker(self.timeline):
                self.timeline.setRange(0, duration)
            self.timeline.setEnabled(duration > 0 and self.player.isSeekable())
            self.update_position(self.player.position())

    def update_position(self, position):
        if self.active and not self.timeline.isSliderDown():
            with QSignalBlocker(self.timeline):
                self.timeline.setValue(position)
            self.show_time(self.timeline.value())

    def show_time(self, position):
        self.clock.setText(f"{playback_time(position)} / {playback_time(self.timeline.maximum())}")

    def seek_requested(self, position):
        if not self.active or not self.timeline.isEnabled():
            return
        self.show_time(position)
        if self.timeline.isSliderDown():
            if not self.seek_timer.isActive():
                self.seek_timer.start()
        else:
            self.commit_seek()

    def commit_seek(self):
        self.seek_timer.stop()
        if self.active and self.timeline.isEnabled():
            self.player.setPosition(self.timeline.sliderPosition())

    def start(self, raw):
        self.reset()
        if not self.player:
            try:
                self._setup_player()
            except (ImportError, RuntimeError):
                self.failed("Qt video playback is unavailable. Use Watch on website.")
                return
        if not self.player.isAvailable():
            self.failed("The Qt video backend is unavailable. Use Watch on website.")
            return
        self.buffer = QBuffer(self)
        self.buffer.setData(QByteArray(raw))
        self.buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self._mute(self.sound_preference)
        self._set_active(True)
        self.play_button.setText("Pause")
        # No remote URL, path or playlist is ever handed to the multimedia engine.
        self.player.setSourceDevice(self.buffer)
        self.sync_timeline()
        self.player.play()

    def playing(self, playing):
        if self.active:
            self.play_button.setText("Pause" if playing else "Play")

    def frame(self, frame):
        if self.active and frame.isValid():
            image = frame.toImage()
            if not image.isNull():
                self.canvas.image = image
                self.canvas.update()

    def playback_error(self, error, message):
        if self.active:
            self.failed(
                "The installed video backend could not play this clip. Use Watch on website."
            )


class ClipPlayback(QObject):
    """Keep a clip selected when the pointer travels from a tile to Play/Pause."""

    def __init__(self, picker):
        super().__init__(picker)
        self.picker = picker
        self.widget = picker.preview.clip_player
        self.widget.loadRequested.connect(self.play)
        self.widget.websiteRequested.connect(self.open_page)
        self.widget.paste.clicked.connect(self.paste_link)
        self.widget.unpin.clicked.connect(self.unpin)
        self.item = None
        self.index = -1
        self.pending_index = -1
        self.pinned = False
        self.job = self.cancellation = None
        self.automatic = False
        self.dwell = QTimer(self, singleShot=True, interval=2000)
        self.dwell.setTimerType(Qt.TimerType.PreciseTimer)
        self.dwell.timeout.connect(self.dwell_finished)
        picker.clip_autoplay.toggled.connect(self.autoplay_changed)
        picker.preview_side.currentIndexChanged.connect(self.pane_changed)

    def open_page(self):
        if self.item and clip_link(self.item.page):
            QDesktopServices.openUrl(QUrl(self.item.page))

    def paste_link(self):
        if self.item is not None:
            self.picker.copy_item(self.item, paste=True)

    def unpin(self):
        self.pinned = False
        self.widget.unpin.hide()
        self.hover(self.picker.motion_target)

    def cancel(self):
        self.dwell.stop()
        self.pending_index = -1
        if self.cancellation:
            self.cancellation.set()
        if self.job:
            self.job.cancel()
        self.cancellation = self.job = None
        self.automatic = False
        self.widget.reset()

    def clear(self):
        if self.item is None:
            return
        previous = self.item
        self.cancel()
        self.pinned = False
        self.widget.unpin.hide()
        self.index = -1
        self.item = None
        # Scrolling can invalidate a clip while its tile is no longer hovered.
        # The ordinary hover callback then has no index change to repaint.
        if (
            self.picker.motion_target < 0
            and getattr(self.picker.preview, "item", None) is previous
        ):
            self.picker.preview.set_preview()

    def pane_changed(self):
        if self.picker.preview_side.currentData() == "off":
            self.clear()
        else:
            self.hover(self.picker.motion_target)

    def autoplay_changed(self, enabled):
        self.dwell.stop()
        self.pending_index = -1
        if enabled:
            self.hover(self.picker.motion_target)
        elif self.automatic and not self.pinned:
            self.cancel()

    def select(self, index):
        item = self.picker.board.items[index]
        if self.item is not item:
            self.clear()
            self.index, self.item = index, item

    def dwell_finished(self):
        index, self.pending_index = self.pending_index, -1
        if self.pinned or not 0 <= index < len(self.picker.board.items):
            return
        if self.index != index:
            self.select(index)
            self.picker.refresh_preview()
        if self.picker.clip_autoplay.isChecked():
            self.play(automatic=True)

    def hover(self, index):
        board = self.picker.board
        if (
            board.home
            or self.picker.preview_side.currentData() == "off"
            or not self.picker.isVisible()
        ):
            self.clear()
            return
        if self.pinned:
            return
        if index < 0:
            self.dwell.stop()
            self.pending_index = -1
            # Keep the caption/Play control stable when moving into the preview.
            # A departed hover can never start a pending automatic download or play.
            if self.automatic and self.cancellation:
                self.cancel()
            return
        if index >= len(board.items) or board.items[index].kind != "clip":
            if self.item is not None:
                self.clear()
            return
        if self.widget.active and index != self.index:
            # Crossing other tiles on the way to the player keeps the current clip
            # and caption. A deliberate hover on another tile selects it.
            self.pending_index = index
            self.dwell.start()
            return
        self.dwell.stop()
        self.pending_index = -1
        self.select(index)
        if self.picker.clip_autoplay.isChecked() and not self.widget.active and not self.cancellation:
            self.pending_index = index
            self.dwell.start()

    def play_here(self, index, item):
        picker = self.picker
        if not picker.isVisible() or not 0 <= index < len(picker.board.items):
            return
        if picker.board.items[index] is not item:
            return
        if picker.preview_side.currentData() == "off":
            picker.preview_side.setCurrentIndex(picker.preview_side.findData("left"))
        self.select(index)
        self.dwell.stop()
        self.pending_index = -1
        self.automatic = False
        self.pinned = True
        self.widget.unpin.show()
        picker.hover(index)
        picker.refresh_preview()
        if not self.widget.active:
            self.play()
        elif not self.widget.player.isPlaying():
            self.widget.toggle_play()

    def play(self, automatic=False):
        picker = self.picker
        if (
            self.item is None
            or not picker.isVisible()
            or picker.preview_side.currentData() == "off"
            or self.cancellation
            or self.widget.active
            or self.index < 0
            or self.index >= len(picker.board.items)
            or picker.board.items[self.index] is not self.item
        ):
            return
        self.dwell.stop()
        item = self.item
        epoch = picker.epoch
        cancelled = self.cancellation = Event()
        self.automatic = automatic
        self.widget.loading()

        def ready(raw, error):
            if (
                self.cancellation is not cancelled
                or epoch != picker.epoch
                or not picker.isVisible()
            ):
                return
            self.cancellation = self.job = None
            if error:
                self.widget.failed(error)
            elif raw:
                self.widget.start(raw)

        self.job = picker.jobs.run(
            "clips", lambda: get_video(item.page, cancelled.is_set), ready
        )
