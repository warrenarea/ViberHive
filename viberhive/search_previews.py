"""Prioritize visible GIFs and stage one screen of results below each search viewport."""

from time import monotonic

from PyQt6.QtCore import QObject, QPoint, QTimer
from PyQt6.QtGui import QPixmap

from .model import site_icon_url
from .service import THUMBNAIL_WORKERS


# Normally prepare one GIF at a time. Autoplay can use the second warm worker
# for visible tiles while a slow original or its thumbnail is still in flight.
AUTOPLAY_GRACE = 1.2
RETRY_DELAY = 2.0


class SearchPreviews(QObject):
    def __init__(self, picker):
        super().__init__(picker)
        self.ui = picker
        self.transfers = {}
        self.last_board = None
        self.visible_since = {}
        self.thumbnails = set()
        self.timer = QTimer(self, singleShot=True)
        self.timer.timeout.connect(self.pump)
        for board in (picker.board, picker.related_pane.board):
            board.artPainted.connect(self.schedule)

    def schedule(self):
        if not self.ui.jobs.closed and self.timer.remainingTime() != 0:
            self.timer.start(0)

    def wake_at(self, deadline):
        delay = max(1, int((deadline - monotonic()) * 1000) + 1)
        if self.timer.remainingTime() < 0 or self.timer.remainingTime() > delay:
            self.timer.start(delay)

    def viewport_rows(self, board, scroll):
        visible, ahead = [], []
        if not board.search_gifs or not board.isVisible():
            return visible, ahead
        top = board.mapFrom(scroll.viewport(), QPoint(0, 0)).y()
        height = scroll.viewport().height()
        bottom = top + height
        for i, rect in enumerate(board.positions):
            if i not in board.search_gifs or rect.bottom() < top or rect.top() >= bottom + height:
                continue
            (visible if rect.top() < bottom else ahead).append(i)
        return visible, ahead

    def cover_status(self, board, rows, search, *, ahead=False):
        """Return the next covered and uncovered original, plus any pending thumbnail work."""
        ui = self.ui
        side = ui.related_pane
        if not board.cover_batches:
            limit = 6 if ahead else 24
            missing = [i for i in rows if i not in board.checked_covers and board.image_for(i) is None][:limit]
            if missing:
                board.checked_covers.update(missing)
                ui.restore_search_covers(board, [board.items[i] for i in missing])
        ready = overdue = fallback = None
        waiting = False
        now = monotonic()
        focus = side.hover_index if board is side.board else ui.motion_target
        for i in sorted(rows, key=lambda index: index != focus):
            item = board.items[i]
            autoplay = not ahead and ui.auto_animate.isChecked()
            retry = board.autoplay_retries.get(i)
            if autoplay and retry is not None:
                if now >= retry:
                    board.autoplay_retries[i] = None  # At most one automatic retry per result.
                    board.original_attempts.discard(i)
                else:
                    self.wake_at(retry)
            needs_original = i not in board.original_attempts and item.key not in self.transfers
            has_cover = board.image_for(i) is not None
            if has_cover and (ahead or i in board.painted_covers):
                if needs_original and ready is None:
                    ready = (board, i, item)
                continue
            if autoplay and needs_original:
                deadline = self.visible_since[board, item.key] + AUTOPLAY_GRACE
                if now >= deadline:
                    overdue = overdue or (board, i, item)
                else:
                    self.wake_at(deadline)
            if has_cover:
                waiting = True
                continue
            source = item.thumb if item.thumb != item.media and not site_icon_url(item.thumb) else ""
            inline = ui.svc.has_inline_poster(item.key) is True
            signature = (source, inline)
            if (source or inline) and board.cover_attempts.get(i) != signature:
                # A scroll into a different viewport must still have free thumbnail workers.
                capacity = 2 if ahead else THUMBNAIL_WORKERS - int(i != focus)
                if item.key not in self.thumbnails and len(self.thumbnails) < capacity:
                    self.fetch_thumbnail(board, i, item, signature)
                waiting = True
                continue
            if item.key in self.thumbnails or board.cover_batches or i not in board.checked_covers:
                waiting = True
                continue
            if not needs_original:
                continue
            if not ahead:
                if not source and not inline:
                    search.refresh_missing_covers()
                live = (search.web_google and search.busy and not search.web_ready
                        if board is side.board else search.active and search.inflight == "initial")
                deadline = board.cover_grace + (3.35 if live and not source else 0)
                if now < deadline:
                    waiting = True
                    self.wake_at(deadline)
                    continue
            if fallback is None:
                fallback = (board, i, item)
        return ready or overdue, fallback, waiting

    def pump(self):
        ui = self.ui
        if ui.jobs.closed or not ui.isVisible() or ui.isMinimized():
            return
        side = ui.related_pane
        upcoming = []
        visible_keys = set()
        now = monotonic()
        ready = fallback = None
        waiting = started = False
        views = [(side.board, side.scroll, side.search), (ui.board, ui.scroll, ui.google)]
        if self.last_board is side.board:
            views.reverse()
        for board, scroll, search in views:
            visible, ahead = self.viewport_rows(board, scroll)
            if not visible:
                continue
            # Revisited results can recover an evicted thumbnail or GIF from disk.
            for i in visible:
                key = (board, board.items[i].key)
                self.visible_since.setdefault(key, now)
                visible_keys.add(key)
            window = set(visible + ahead)
            for i in board.autoplay_retries.keys() - window:
                del board.autoplay_retries[i]
            board.checked_covers.intersection_update(window)
            board.original_attempts.intersection_update(window)
            for i in board.cover_attempts.keys() - window:
                del board.cover_attempts[i]
            covered, uncovered, pending = self.cover_status(board, visible, search)
            ready = ready or covered
            fallback = fallback or uncovered
            waiting |= pending
            started |= not board.original_attempts.isdisjoint(visible)
            if ahead:
                upcoming.append((board, ahead, search))

        for key in self.visible_since.keys() - visible_keys:
            del self.visible_since[key]
        current = ready or (fallback if not waiting else None)
        slot_available = not self.transfers
        if current and len(self.transfers) == 1 and ui.auto_animate.isChecked():
            deadline = next(iter(self.transfers.values())) + AUTOPLAY_GRACE
            slot_available = now >= deadline
            if not slot_available:
                self.wake_at(deadline)
        if current and slot_available:
            self.fetch_original(*current, autoplay=ui.auto_animate.isChecked())
            # Re-evaluate even if no cover or network callback arrives for another tile.
            if ui.auto_animate.isChecked() and len(self.transfers) == 1:
                self.wake_at(now + AUTOPLAY_GRACE)
            started = True
        if not started:
            return

        # Offscreen stills may overlap visible GIF transfers. Offscreen originals
        # wait for the current view, including covers still in flight in either pane.
        next_ready = next_fallback = None
        next_waiting = False
        for board, rows, search in upcoming:
            covered, uncovered, pending = self.cover_status(board, rows, search, ahead=True)
            next_ready = next_ready or covered
            next_fallback = next_fallback or uncovered
            next_waiting |= pending
        if not self.transfers and not (waiting or ready or fallback):
            next_item = next_ready or (next_fallback if not next_waiting else None)
            if next_item:
                self.fetch_original(*next_item)

    def fetch_thumbnail(self, board, index, item, signature):
        key = item.key
        dataset = board.items
        board.cover_attempts[index] = signature
        self.thumbnails.add(key)

        def done(data, error):
            self.thumbnails.discard(key)
            if dataset is board.items and not error and isinstance(data, bytes):
                pix = QPixmap()
                if pix.loadFromData(data, "PNG"):
                    if key not in board.google_thumbs:
                        board.remember_thumbnail(key, pix)
                    board.fail.pop(index, None)
                    # Both galleries share this in-memory image cache.
                    self.ui.board.update()
                    self.ui.related_pane.board.update()
                    self.ui.refresh_preview()
            self.schedule()

        self.ui.jobs.run("google-still", lambda: self.ui.svc.quick_cover(item), done)

    def fetch_original(self, board, index, item, *, autoplay=False):
        # No queue of future downloads: scrolling or changing searches updates the next choice.
        self.last_board = board
        self.transfers[item.key] = monotonic()
        dataset = board.items
        board.original_attempts.add(index)

        def done(path, error):
            self.transfers.pop(item.key, None)
            if dataset is board.items:
                if error:
                    if board.image_for(index) is None:
                        board.fail[index] = error
                    board.motion_errors[index] = error
                    if index not in board.autoplay_retries:
                        board.autoplay_retries[index] = monotonic() + RETRY_DELAY
                    board.update(board.positions[index])
                elif path:
                    if board.image_for(index) is None:
                        board.checked_covers.discard(index)
                    self.ui.animations.remember(item.key, path)
                    self.ui.animations.pump()
                    self.ui.related_pane.animations.pump()
            self.schedule()

        prepare = self.ui.svc.motion if autoplay else self.ui.svc.prefetch_motion
        self.ui.jobs.run("warm", lambda: prepare(item), done)
