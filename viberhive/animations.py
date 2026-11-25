"""Prepare verified GIFs ahead of the pointer. Only visible tiles own live decoders."""

from collections import OrderedDict
from itertools import islice
from pathlib import Path
from time import monotonic

from PyQt6.QtCore import QBuffer, QIODevice, QObject, QPoint, QSize, Qt, QTimer
from PyQt6.QtGui import QImageReader, QMovie


HOVER_WORKERS = 2


class Animations(QObject):
    def __init__(self, picker):
        super().__init__(picker)
        self.ui = picker
        self.movies = {}
        self.ready = OrderedDict()
        self.items = {}
        self.visible = {}
        self.order = []
        self.wanted = {}
        self.focus = None
        self.checked = set()
        self.attempted = set()
        self.failures = {}
        self.checking = False
        self.fetching = {}
        self.dataset = None
        self.touched = OrderedDict()
        self.next_frame = QTimer(self, singleShot=True, interval=0)
        self.next_frame.timeout.connect(self.pump)

    def suspend(self):
        self.next_frame.stop()
        self.order = []
        self.wanted = {}
        self.set_focus(-1)
        self.visible.clear()
        for index in list(self.movies):
            self.stop(index)

    def viewport_changed(self):
        ui, board = self.ui, self.ui.board
        if not ui.isVisible() or ui.isMinimized() or not board.items:
            self.suspend()
            return
        if self.dataset is not board.items:
            self.dataset = board.items
            self.items = {}
            self.checked.clear()
            self.attempted.clear()
            self.failures.clear()
        # Cards are appended during paging. Hash and inspect each new item's source just once.
        if len(self.items) != len(board.items):
            for i in range(len(self.items), len(board.items)):
                item = board.items[i]
                self.items[i] = (
                    (item.key, item) if item.kind == "gif" and item.media else None
                )

        top = board.mapFrom(ui.scroll.viewport(), QPoint(0, 0)).y()
        bottom = top + ui.scroll.viewport().height() - 1
        candidates = [i for i, entry in self.items.items() if entry is not None]
        candidates.sort(
            key=lambda i: max(
                0, top - board.positions[i].bottom(), board.positions[i].top() - bottom
            )
        )
        previously_visible = set(self.visible.values())
        self.visible = {
            i: self.items[i][0]
            for i in candidates
            if board.positions[i].bottom() >= top and board.positions[i].top() <= bottom
        }
        for key in set(self.visible.values()) - previously_visible:
            if key not in self.ready:
                self.checked.discard(key)
                self.attempted.discard(key)
        self.order = [self.items[i] for i in candidates]
        self.selection_changed()

    def set_focus(self, index):
        key = self.visible.get(index)
        if key != self.focus:
            self.focus = key
            prioritize = getattr(self.ui.svc, "prioritize_motion", None)
            if prioritize is not None:
                prioritize(self.items[index][1].media if key is not None else None)

    def selection_changed(self):
        ui = self.ui
        i = ui.motion_target
        self.set_focus(i)
        self.wanted = {i: self.focus} if self.focus is not None else {}
        if ui.auto_animate.isChecked():
            self.wanted.update(self.visible)
        for index, (key, _) in list(self.movies.items()):
            if self.wanted.get(index) != key:
                self.stop(index)
        self.pump()

    def pump(self):
        if not self.order or self.ui.jobs.closed:
            return
        # A second hover can start while the previous publisher is still responding.
        # When both workers are busy, the next completion takes the current pointer only.
        deadline = monotonic() + 0.014
        for index, key in self.wanted.items():
            if index in self.movies:
                continue
            prepared = self.ready.get(key)
            if prepared:
                # If many cached decoders start together, give painting and input
                # a turn. The pointed-at card always starts first.
                if key != self.focus and monotonic() >= deadline:
                    self.next_frame.start()
                    break
                if self.ui.store.still_verified(prepared):
                    self.ready.move_to_end(key)
                    self.play(index, key, prepared)
                    continue
                self.ready.pop(key)
                self.checked.discard(key)
                self.attempted.discard(key)
            if (
                key == self.focus
                and key not in self.failures
                and key not in self.fetching
            ) and list(self.fetching.values()).count("motion") < HOVER_WORKERS:
                self.fetch(key, self.items[index][1], "motion")

        service = self.ui.svc
        # Probe the disk on an independent worker. Offline favorites start playing even
        # when an unrelated host occupies every network worker.
        if not self.checking and hasattr(service, "ready_motion"):
            missing = (
                (key, item)
                for key, item in self.order
                if key not in self.checked
                and key not in self.ready
                and not item.link_only
            )
            batch = list(islice(missing, 12))
            if batch:
                self.checking = True
                self.checked.update(key for key, _ in batch)

                def check():
                    return [
                        (key, service.ready_motion(item, lease=0))
                        for key, item in batch
                    ]

                def checked(found, _error):
                    self.checking = False
                    for key, prepared in found or ():
                        self.remember(key, prepared)
                    self.pump()

                self.ui.jobs.run("motion-disk", check, checked)

        # Existing transfers can finish. Don't start another speculative animation
        # while the pointed-at GIF is waiting for its first frame.
        if (
            self.focus is not None
            and self.ui.motion_target not in self.movies
            and self.focus not in self.failures
        ):
            return
        auto_keys = set(self.wanted.values())
        for key, item in self.order:
            if list(self.fetching.values()).count("warm") >= 2:
                break
            if key in self.ready or key in self.fetching or key in self.failures:
                continue
            if item.link_only:
                # KLIPY permits playback from transient bytes only on a requested tile.
                if key not in auto_keys or any(
                    k == key for k, _ in self.movies.values()
                ):
                    continue
            elif key in self.attempted or not hasattr(service, "prefetch_motion"):
                continue
            else:
                self.attempted.add(key)
            self.fetch(key, item, "warm")

    def remember(self, key, prepared):
        if isinstance(prepared, Path):
            self.ready[key] = prepared
            self.ready.move_to_end(key)
            while len(self.ready) > 384:
                self.ready.popitem(last=False)

    def fetch(self, key, item, lane):
        self.fetching[key] = lane
        service = self.ui.svc

        def prepare():
            if lane == "motion":
                return service.motion(item)
            if item.link_only:
                return service.motion(item, background=True)
            return service.prefetch_motion(item)

        def done(prepared, error):
            self.fetching.pop(key, None)
            self.remember(key, prepared)
            for i, wanted in self.wanted.items():
                if wanted != key:
                    continue
                if error or not isinstance(prepared, (Path, bytes)):
                    self.failures[key] = error or "Animation unavailable"
                    self.ui.board.motion_errors[i] = self.failures[key]
                    self.ui.board.update(self.ui.board.positions[i])
                    if i == self.ui.motion_target:
                        self.ui.refresh_preview()
                elif i not in self.movies:
                    if isinstance(prepared, Path) and not self.ui.store.still_verified(
                        prepared
                    ):
                        self.ready.pop(key, None)
                        self.failures[key] = (
                            "The cached animation changed. Hover again to reload it."
                        )
                        self.ui.board.motion_errors[i] = self.failures[key]
                    else:
                        self.play(i, key, prepared)
            self.pump()

        self.ui.jobs.run(lane, prepare, done)

    def play(self, i, key, source):
        ui, board = self.ui, self.ui.board
        if isinstance(source, bytes):
            buffer = QBuffer()
            buffer.setData(source)
            buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            movie = QMovie(buffer, b"gif", self)
            buffer.setParent(movie)
        else:
            movie = QMovie(str(source), b"gif", self)

        # Keep only the current frame. Limit the display surfaces, preserving the aspect
        # ratio from the verified GIF header even when search metadata omits dimensions.
        if isinstance(source, Path):
            reader = QImageReader(str(source), b"gif")
            size = reader.size()
        else:
            size = QSize(
                int.from_bytes(source[6:8], "little"),
                int.from_bytes(source[8:10], "little"),
            )
        if size.isValid() and max(size.width(), size.height()) > 720:
            movie.setScaledSize(
                size.scaled(720, 720, Qt.AspectRatioMode.KeepAspectRatio)
            )
        if not movie.isValid():
            movie.deleteLater()
            self.ready.pop(key, None)
            self.failures[key] = "The animation could not be played."
            board.motion_errors[i] = self.failures[key]
            board.update(board.positions[i])
            if i == ui.motion_target:
                ui.refresh_preview()
            return
        self.movies[i] = (key, movie)
        if isinstance(source, Path):
            now = monotonic()
            if now - self.touched.get(key, -3600) >= 3600:
                # A cache probe does not extend retention. A real playback does.
                item = self.items[i][1]
                self.touched[key] = now
                self.touched.move_to_end(key)
                if len(self.touched) > 384:
                    self.touched.popitem(last=False)
                ui.jobs.run(
                    "motion-disk", lambda: ui.svc.ready_motion(item), lambda *_: None
                )

        def frame(number):
            pix = movie.currentPixmap()
            if pix.isNull():
                return
            board.motion_frames[i] = pix
            board.motion_errors.pop(i, None)
            if number == 0 and i not in board.pix:
                board.pix[i] = (
                    pix.scaled(
                        360,
                        250,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    if pix.width() > 360 or pix.height() > 250
                    else pix
                )
                board.fail.pop(i, None)
            board.update(board.positions[i])
            if i == ui.motion_target:
                ui.refresh_preview()

        movie.frameChanged.connect(frame)

        def replay():
            if movie.lastError() == QImageReader.ImageReaderError.UnknownError:
                movie.start()

        movie.finished.connect(replay)
        movie.start()

    def stop(self, i):
        _, movie = self.movies.pop(i)
        movie.stop()
        movie.deleteLater()
        board = self.ui.board
        board.motion_frames.pop(i, None)
        if i < len(board.positions):
            board.update(board.positions[i])
