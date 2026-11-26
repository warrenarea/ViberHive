"""An independently scrollable set of title-search results in the preview pane."""

from html import escape

from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from .animations import Animations
from .gallery import Gallery
from .preview_search import PreviewSearch
from .service import THUMBNAIL_WORKERS
from .theme import control_icon


class RelatedPane(QWidget):
    closeRequested = pyqtSignal()

    def __init__(self, picker):
        super().__init__()
        self.picker = picker
        self.active = False
        self.generation = 0
        self.poster_jobs = {}
        self.thumbnail_jobs = {}
        self.thumbnail_attempts = set()
        self.motion_target = -1
        self.hover_index = -1
        self.setAccessibleName("Similar GIF results")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        header = QHBoxLayout()
        heading = QLabel("More like this")
        heading.setStyleSheet("font-weight: 700;")
        header.addWidget(heading, 1)
        close = QPushButton()
        close.setObjectName("iconButton")
        close.setFixedSize(28, 28)
        close.setIcon(control_icon("close"))
        close.setAccessibleName("Return to hover preview")
        close.setToolTip("Return to hover preview")
        close.clicked.connect(self.closeRequested)
        header.addWidget(close)
        layout.addLayout(header)
        self.title = QLabel()
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.title)
        self.scroll = QScrollArea()
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.board = Gallery(thumbnail_cache=picker.board.google_thumbs)
        self.board.card_width_target = 132
        self.board.setAccessibleName("Similar GIFs. Arrow keys select a GIF. Enter pastes it into Discord.")
        self.board.setMinimumWidth(100)
        self.board.related_enabled = True
        self.scroll.setWidget(self.board)
        self.board.installEventFilter(self)
        layout.addWidget(self.scroll, 1)
        self.notice = QLabel()
        self.notice.setObjectName("small")
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        self.notice.setMinimumWidth(0)
        layout.addWidget(self.notice)
        self.more = QPushButton()
        self.more.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.more)

        self.search = PreviewSearch(picker)
        self.search.cleared.connect(self.clear_results)
        self.search.itemsAdded.connect(self.append)
        self.search.sourcesReady.connect(self.update_sources)
        self.search.thumbnailsReady.connect(self.board.offer_google_pixels)
        self.search.inlinePostersReady.connect(self.inline_posters_ready)
        self.search.changed.connect(self.show_state)
        self.more.clicked.connect(self.search.more)
        self.posters = QTimer(self, interval=20, singleShot=True)
        self.posters.timeout.connect(self.load_posters)
        self.animations = Animations(picker, self)
        self.dwell = QTimer(self, interval=90, singleShot=True)
        self.dwell.timeout.connect(self.play_hover)
        self.board.hovered.connect(self.hover)
        self.board.activate.connect(self.activate)
        self.board.star.connect(self.toggle_star)
        self.board.related.connect(self.explore)
        self.board.previewRelated.connect(self.explore)
        self.board.menu.connect(self.show_menu)
        self.board.layoutChanged.connect(self.schedule_posters)
        self.scroll.verticalScrollBar().valueChanged.connect(self.scrolled)
        self.scroll.viewport().installEventFilter(self)

    def open_search(self, item, query, provider):
        self.active = True
        self.picker.preview.set_related_visible(True)
        self.favorites_changed()
        self.search.start(provider, query)
        self.show_state()

    def clear(self):
        if self.active:
            self.active = False
            self.search.reset()
            self.picker.preview.set_related_visible(False)

    def shutdown(self):
        self.clear()
        self.search.shutdown()

    def clear_results(self):
        self.generation += 1
        self.animations.suspend()
        for pending in (self.poster_jobs, self.thumbnail_jobs):
            for job in pending.values():
                if job is not None:
                    job.cancel()
            pending.clear()
        self.thumbnail_attempts.clear()
        self.hover(-1)
        self.posters.stop()
        self.board.reset()
        self.scroll.verticalScrollBar().setValue(0)

    def show_state(self):
        if not self.active:
            return
        has_items = bool(self.board.items)
        message = self.search.message
        self.title.setText(self.search.query)
        self.title.setToolTip(f"<qt>{escape(self.search.query)}</qt>")
        self.notice.setText(message)
        self.notice.setAlignment(Qt.AlignmentFlag.AlignLeft if has_items else Qt.AlignmentFlag.AlignCenter)
        self.notice.setVisible(bool(message))
        self.scroll.setVisible(has_items)
        self.layout().setStretch(self.layout().indexOf(self.notice), 0 if has_items else 1)
        self.more.setText(self.search.action_text)
        self.more.setVisible(bool(self.search.action_text))
        self.more.setEnabled(not self.search.busy)
        self.schedule_posters()

    def append(self, items):
        if self.active and items:
            self.picker.extend_search_results(self.board, items)
            self.show_state()

    def update_sources(self, items):
        self.board.update_sources(items)
        self.inline_posters_ready(items)

    def favorites_changed(self):
        self.board.favorite_keys = self.picker.board.favorite_keys
        self.board.update()

    def explore(self, item):
        if item.kind == "gif":
            self.open_search(item, item.title.strip() or self.search.query, self.search.provider)

    def activate(self, index):
        if 0 <= index < len(self.board.items):
            self.picker.copy_item(self.board.items[index], paste=True)

    def toggle_star(self, index):
        if 0 <= index < len(self.board.items):
            item = self.board.items[index]
            self.picker.set_favorite(item, item.key in self.board.favorite_keys, words=self.search.query)

    def show_menu(self, index, point):
        item = self.board.items[index]
        menu = QMenu(self)
        menu.addAction("Remove favorite" if item.key in self.board.favorite_keys else "Add to favorites",
                       lambda: self.toggle_star(index))
        menu.addAction("Paste into Discord", lambda: self.activate(index))
        menu.addAction("Copy GIF link" if item.link_only else "Copy GIF file",
                       lambda: self.picker.copy_item(item))
        menu.addAction("More like this in preview", lambda: self.explore(item))
        menu.exec(point)
        menu.deleteLater()

    def scrolled(self, position):
        self.hover(-1)
        self.board.hot = -1
        self.board.cancel_press()
        self.board.tile_tip.scrolled()
        self.schedule_posters()
        bar = self.scroll.verticalScrollBar()
        threshold = bar.pageStep() if self.board.search_gifs else bar.pageStep() // 2
        if self.active and position > 0 and bar.maximum() - position < threshold:
            self.search.load_more()

    def eventFilter(self, watched, event):
        if watched is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            self.schedule_posters()
        if watched is self.board and event.type() == QEvent.Type.Wheel and not event.modifiers():
            # Keep wheel input on a painted tile in this pane, even when the main grid has focus.
            bar = self.scroll.verticalScrollBar()
            if event.pixelDelta().y():
                bar.setValue(bar.value() - event.pixelDelta().y())
            else:
                QApplication.sendEvent(bar, event)
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def schedule_posters(self):
        if self.active and not self.posters.isActive():
            self.posters.start()

    def showEvent(self, event):
        super().showEvent(event)
        self.schedule_posters()

    def inline_posters_ready(self, keys):
        self.board.retry_covers(keys)
        self.thumbnail_attempts.difference_update(keys)
        self.schedule_posters()

    def show_first_frame(self, key, png):
        if not self.active or not self.isVisible() or self.picker.jobs.closed:
            return
        top = self.scroll.verticalScrollBar().value()
        bottom = top + self.scroll.viewport().height()
        for i, rect in enumerate(self.board.positions):
            if rect.top() <= bottom and rect.bottom() >= top and self.board.items[i].key == key:
                pix = QPixmap()
                if pix.loadFromData(png, "PNG"):
                    self.board.pix[i] = pix
                    self.board.fail.pop(i, None)
                    self.board.update(rect)
                return

    def request_thumbnail(self, i, item):
        key = item.key
        capacity = THUMBNAIL_WORKERS - 1 if self.search.web_google else 2
        if (key in self.board.google_thumbs or key in self.thumbnail_attempts
                or key in self.thumbnail_jobs or len(self.thumbnail_jobs) >= capacity):
            return
        self.thumbnail_attempts.add(key)
        generation = self.generation

        def complete(data, error):
            if generation != self.generation:
                return
            self.thumbnail_jobs.pop(key, None)
            pix = QPixmap()
            if isinstance(data, bytes) and not error and pix.loadFromData(data, "PNG"):
                self.board.remember_thumbnail(key, pix)
                self.board.update(self.board.positions[i])
            self.schedule_posters()

        self.thumbnail_jobs[key] = self.picker.jobs.run("side-still", lambda: self.picker.svc.quick_cover(item), complete)

    def load_posters(self):
        if not self.active or not self.isVisible() or self.picker.jobs.closed:
            return
        self.animations.viewport_changed()
        self.picker.search_previews.schedule()
        top = self.scroll.verticalScrollBar().value()
        height = self.scroll.viewport().height()
        for i in list(self.board.pix):
            rect = self.board.positions[i]
            if rect.bottom() < top - 2 * height or rect.top() > top + 3 * height:
                del self.board.pix[i]
        for i, rect in enumerate(self.board.positions):
            item = self.board.items[i]
            if rect.bottom() < top or rect.top() > top + height * 1.5 or i in self.board.pix:
                continue
            if i in self.board.search_gifs:
                continue
            self.request_thumbnail(i, item)
            if i in self.poster_jobs or i in self.board.fail or len(self.poster_jobs) >= 2:
                continue

            def complete(data, error, index=i, generation=self.generation):
                if generation != self.generation:
                    return
                self.poster_jobs.pop(index, None)
                pix = QPixmap()
                if isinstance(data, bytes) and not error and pix.loadFromData(data, "PNG"):
                    # Hover can supply the actual frame while a fallback is in flight.
                    self.board.pix.setdefault(index, pix)
                    self.board.fail.pop(index, None)
                elif index not in self.board.pix:
                    self.board.fail[index] = error or "Preview unavailable"
                self.board.update(self.board.positions[index])
                self.schedule_posters()

            self.poster_jobs[i] = self.picker.jobs.run(
                "preview", lambda it=item: self.picker.svc.related_poster(it), complete)

    def hover(self, index):
        if index == self.hover_index:
            return
        self.dwell.stop()
        self.hover_index = index
        self.motion_target = -1
        self.animations.selection_changed()
        if 0 <= index < len(self.board.items):
            self.dwell.start()

    def play_hover(self):
        index = self.hover_index
        if self.active and 0 <= index < len(self.board.items):
            self.motion_target = index
            self.animations.failures.pop(self.board.items[index].key, None)
            self.animations.viewport_changed()

    def hideEvent(self, event):
        self.dwell.stop()
        self.hover_index = self.motion_target = -1
        self.animations.suspend()
        super().hideEvent(event)
