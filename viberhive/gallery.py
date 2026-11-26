"""A painted masonry grid. Only visible tiles need pixmaps or remote previews."""

from collections import OrderedDict
from time import monotonic
from weakref import WeakSet

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QApplication, QScrollArea, QWidget

from .model import GOOGLE_THUMB_EDGE
from .tile_tip import TileTip

A = Qt.AlignmentFlag
NOPEN = Qt.PenStyle.NoPen
SECTION_FLAGS = A.AlignLeft | A.AlignVCenter | Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextWrapAnywhere
MAX_COLUMNS = 6
MAX_FREQUENT_ROWS = 8
HOME = [
    ("Favorites", "★", "#6455c9", "#38325f"),
    ("Hello", "👋", "#546d85", "#303d55"),
    ("Laughing", "😂", "#926549", "#4a3946"),
    ("Love", "♥", "#914f79", "#4b3556"),
    ("Happy birthday", "🎂", "#735791", "#413554"),
    ("Thank you", "💌", "#517977", "#2d4853"),
    ("Excited", "⚡", "#927338", "#554332"),
    ("Yes", "✔", "#567c63", "#30483f"),
    ("No", "✕", "#935956", "#523743"),
    ("Happy", "☺", "#837140", "#424438"),
    ("Sorry", "☂", "#526e96", "#333e59"),
    ("Thumbs up", "👍", "#706693", "#3c3856"),
    ("Good morning", "☀", "#966d4d", "#554438"),
    ("Sad", "😢", "#556f83", "#36465b"),
    ("Surprised", "😮", "#926e82", "#523c54"),
]


def color(hexcode):
    return QColor(hexcode)


def font(pt=10, bold=False, family="Segoe UI"):
    f = QFont(family)
    f.setPointSizeF(pt)
    f.setBold(bold)
    return f


def cover(painter, rect, pix):
    target = QRectF(rect)
    ratio = target.width() / target.height()
    w, h = pix.width(), pix.height()
    if w / h > ratio:
        sw = h * ratio
        source = QRectF((w - sw) / 2, 0, sw, h)
    else:
        sh = w / ratio
        source = QRectF(0, (h - sh) / 2, w, sh)
    painter.drawPixmap(target, pix, source)


class FrequentDivider(QWidget):
    """A section edge that snaps to whole rows of used GIFs."""

    def __init__(self, gallery):
        super().__init__(gallery)
        self.gallery = gallery
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setAccessibleName("Resize Frequently Used")
        self.setToolTip("Drag down to show more rows or up to show fewer. You can also use the Down and Up keys.")
        self.setAccessibleDescription(self.toolTip())
        self.setFixedHeight(24)
        self.start_point = None
        self.edge_scroll = QTimer(self, interval=30)
        self.edge_scroll.timeout.connect(self.scroll_at_edge)
        self.hide()

    def scroll_area(self):
        viewport = self.gallery.parentWidget()
        area = viewport.parentWidget() if viewport else None
        return area if isinstance(area, QScrollArea) else None

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.start_point = event.globalPosition().toPoint()
        self.pointer = self.start_point
        self.dragged = False
        self.direction = 0
        self.start_y = self.gallery.mapFromGlobal(self.start_point).y()
        self.start_rows = self.gallery.shown_frequent_rows()
        self.update()
        event.accept()

    def follow_pointer(self):
        distance = self.gallery.mapFromGlobal(self.pointer).y() - self.start_y
        pitch = self.gallery.frequent_row_pitch
        steps = (abs(distance) + pitch // 2) // pitch
        rows = self.start_rows + (steps if distance >= 0 else -steps)
        self.gallery.resize_frequent_section(rows)

    def mouseMoveEvent(self, event):
        if self.start_point is None:
            return super().mouseMoveEvent(event)
        point = event.globalPosition().toPoint()
        if point.y() != self.pointer.y():
            self.direction = point.y() - self.pointer.y()
        self.pointer = point
        if abs(point.y() - self.start_point.y()) >= QApplication.startDragDistance():
            self.dragged = True
        if self.dragged:
            self.follow_pointer()
            if not self.edge_scroll.isActive():
                self.edge_scroll.start()
        event.accept()

    def scroll_at_edge(self):
        area = self.scroll_area()
        if area is not None:
            viewport = area.viewport()
            y = viewport.mapFromGlobal(self.pointer).y()
            bar = area.verticalScrollBar()
            value = bar.value()
            rows = self.gallery.shown_frequent_rows()
            if (self.direction > 0 and y >= viewport.height() - 24
                    and rows < self.gallery.available_frequent_rows()):
                bar.setValue(value + 12)
            elif self.direction < 0 and y <= 24 and rows > 1:
                bar.setValue(value - 12)
            if value != bar.value():
                self.follow_pointer()
                return
        self.edge_scroll.stop()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self.start_point is None:
            return super().mouseReleaseEvent(event)
        self.pointer = event.globalPosition().toPoint()
        self.follow_pointer()
        self.start_point = None
        self.edge_scroll.stop()
        self.keep_visible()
        self.update()
        event.accept()

    def hideEvent(self, event):
        self.start_point = None
        self.edge_scroll.stop()
        super().hideEvent(event)

    def keep_visible(self):
        area = self.scroll_area()
        if area is not None:
            area.ensureWidgetVisible(self, 0, 12)

    def keyPressEvent(self, event):
        rows = self.gallery.shown_frequent_rows()
        sizes = {Qt.Key.Key_Up: rows - 1, Qt.Key.Key_Down: rows + 1,
                 Qt.Key.Key_Home: 1, Qt.Key.Key_End: self.gallery.available_frequent_rows()}
        if event.key() not in sizes:
            return super().keyPressEvent(event)
        self.gallery.resize_frequent_section(sizes[event.key()])
        self.keep_visible()
        event.accept()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        active = self.hasFocus() or self.underMouse() or self.start_point is not None
        middle = self.height() // 2
        rows = self.gallery.shown_frequent_rows()
        label = f"{rows} row" if rows == 1 else f"{rows} rows"
        p.setFont(font(9, True))
        width = p.fontMetrics().horizontalAdvance(label) + 32
        grip = QRect((self.width() - width) // 2, 2, width, self.height() - 4)
        p.setPen(color("#939bc9" if active else "#575a69"))
        p.drawLine(2, middle, grip.left() - 4, middle)
        p.drawLine(grip.right() + 4, middle, self.width() - 2, middle)
        p.setBrush(color("#464a61" if active else "#353741"))
        p.drawRoundedRect(grip, 6, 6)
        p.setPen(color("#f4f4ff" if active else "#c5c7d3"))
        x = grip.left() + 11
        for dy, tip in ((-2, -5), (2, 5)):
            p.drawLine(x - 3, middle + dy, x, middle + tip)
            p.drawLine(x, middle + tip, x + 3, middle + dy)
        p.drawText(grip.adjusted(19, 0, -4, 0), A.AlignCenter, label)


class Gallery(QWidget):
    card_width_target = 178
    activate = pyqtSignal(int)
    star = pyqtSignal(int)
    related = pyqtSignal(object)
    previewRelated = pyqtSignal(object)
    menu = pyqtSignal(int, QPoint)
    hovered = pyqtSignal(int)
    artPainted = pyqtSignal()
    browserCovers = pyqtSignal(object)
    layoutChanged = pyqtSignal()
    frequentRowsChanged = pyqtSignal(int)

    def __init__(self, *, thumbnail_cache=None):
        super().__init__()
        self.tile_tip = TileTip(self)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(
            "Search results. Use arrow keys to browse. Enter pastes a GIF or finds related GIFs "
            "for a still image. Enter on a clip pins its preview. Shift+Enter searches an image’s title."
        )
        self.related_enabled = False
        self.home = True
        self.home_tiles = HOME
        self.category_images = {}
        self.frequent_pool = []
        self.frequent_rows = 2
        self.row_divider = FrequentDivider(self)
        self.personal_count = 0
        self.frequent_hint = QRect()
        self.items = []
        self.positions = []
        self.sections = {}
        self.section_rows = []
        self.favorite_headings = False
        self.pix = {}
        self.cover_reads = WeakSet()
        self.cover_batches = 0
        self.search_gifs = set()
        self.painted_covers = set()
        self.checked_covers = set()
        self.cover_attempts = {}
        self.original_attempts = set()
        self.autoplay_retries = {}
        self.cover_grace = 0.0
        self.google_thumbs = thumbnail_cache if thumbnail_cache is not None else OrderedDict()
        self.fail = {}
        self.motion_errors = {}
        self.pressed = None
        self.favorite_keys = set()
        self.hot = self.selected = -1
        self.motion_frames = {}
        self.empty = ("", "")
        self.setMinimumWidth(180)

    def reset(self, home=False, items=(), sections=None, *, favorite_headings=False):
        for future in list(self.cover_reads):
            future.cancel()
        self.cover_reads.clear()
        self.cover_batches = 0
        self.search_gifs.clear()
        self.painted_covers.clear()
        self.checked_covers.clear()
        self.cover_attempts.clear()
        self.original_attempts.clear()
        self.autoplay_retries.clear()
        self.cover_grace = 0.0
        self.tile_tip.cancel()
        self.home = home
        if not home:
            self.category_images.clear()
        self.items = list(items)
        self.frequent_pool = self.items if home else []
        self.sections = sections or {}
        self.favorite_headings = favorite_headings
        self.pix.clear()
        self.fail.clear()
        self.motion_errors.clear()
        self.cancel_press()
        self.hot = self.selected = -1
        self.motion_frames = {}
        self.empty = ("", "")
        self.reflow()

    def extend(self, items):
        self.items.extend(items)
        self.reflow()

    def extend_search(self, items):
        indices = {i for i, it in enumerate(items, len(self.items))
                   if it.kind == "gif" and not it.link_only}
        if indices:
            self.search_gifs.update(indices)
            self.cover_grace = monotonic() + 0.65
        self.extend(items)

    def retry_covers(self, keys):
        for i in list(self.cover_attempts):
            if self.items[i].key in keys:
                self.cover_attempts.pop(i)

    def update_sources(self, items):
        for index, item in enumerate(self.items):
            if item.key in items and not item.thumb:
                self.items[index] = items[item.key]
                self.fail.pop(index, None)

    def offer_google_pixels(self, frames):
        """Display the browser's bounded pixel buffers without decoding remote files in Qt."""
        accepted = {}
        matching = {item.key: item for item in self.items if item.key in frames}
        for key, frame in frames.items():
            if not isinstance(frame, (list, tuple)) or len(frame) != 3:
                continue
            w, h, rgba = frame
            if (type(w) is not int or type(h) is not int or not isinstance(rgba, bytes)
                    or not (8 <= w <= GOOGLE_THUMB_EDGE and 8 <= h <= GOOGLE_THUMB_EDGE)
                    or len(rgba) != 4 * w * h):
                continue
            if key not in matching or not matching[key].accepts_thumbnail(w, h):
                continue
            image = QImage(rgba, w, h, 4 * w, QImage.Format.Format_RGBA8888)
            self.remember_thumbnail(key, QPixmap.fromImage(image.copy()))
            accepted[key] = frame
        if accepted:
            self.update()
            cacheable = {k: frame for k, frame in accepted.items() if matching[k].kind == "gif" and not matching[k].link_only}
            if cacheable:
                self.browserCovers.emit(cacheable)
        return bool(accepted)

    def remember_thumbnail(self, key, pix):
        self.google_thumbs[key] = pix
        self.google_thumbs.move_to_end(key)
        while len(self.google_thumbs) > 192:
            self.google_thumbs.popitem(last=False)

    def image_for(self, index):
        pix = self.motion_frames.get(index, self.pix.get(index))
        if pix is None and self.google_thumbs:
            key = self.items[index].key
            pix = self.google_thumbs.get(key)
            if pix is not None:
                self.google_thumbs.move_to_end(key)
        return pix

    def show_categories(self, topics, frequent=()):
        built_in = {tile[0].casefold() for tile in HOME}
        personal = [(topic, "🔎", "#526693", "#393952") for topic in topics
                    if topic.casefold() not in built_in]
        self.home_tiles = [HOME[0], *personal, *HOME[1:]]
        self.personal_count = len(personal)
        self.reset(home=True, items=frequent)

    def available_frequent_rows(self):
        return min(MAX_FREQUENT_ROWS, (len(self.frequent_pool) + self.cols - 1) // self.cols)

    def shown_frequent_rows(self):
        return min(self.frequent_rows, self.available_frequent_rows())

    def resize_frequent_section(self, rows):
        rows = max(1, min(self.available_frequent_rows(), rows))
        if self.home and rows != self.shown_frequent_rows():
            self.frequent_rows = rows
            self.reflow()
            self.frequentRowsChanged.emit(rows)

    def reflow(self):
        gap = 9
        width = max(150, self.width() - 2)
        self.cols = max(1, min(MAX_COLUMNS, (width + gap) // (self.card_width_target + gap)))
        cardw = (width - (self.cols - 1) * gap) // self.cols
        tops = [1] * self.cols
        self.positions = []
        self.section_rows = []
        self.frequent_hint = QRect()
        show_divider = self.home and self.available_frequent_rows() > 1
        self.row_divider.setVisible(show_divider)
        large = self.home or self.favorite_headings
        self.section_font = font(21 if large else 10, True)
        metrics = QFontMetrics(self.section_font, self)

        def heading(title, top):
            rect = QRect(3, top, max(1, width - 5), 25)
            if large:
                rect.translate(0, 4)
                rect.setHeight(metrics.boundingRect(QRect(0, 0, rect.width(), 10000),
                                                    SECTION_FLAGS, title).height())
            self.section_rows.append((rect, title))
            return rect.bottom() + (15 if large else 7)

        changed_capacity = False
        if self.home:
            self.frequent_row_pitch = max(99, min(152, round(cardw * 0.62))) + gap
            count = min(len(self.frequent_pool), self.frequent_rows * self.cols)
            changed_capacity = len(self.items) != count
            if changed_capacity:
                self.items = self.frequent_pool[:count]
                self.pix = {i: pix for i, pix in self.pix.items() if i < count}
                self.hot = self.selected = -1
                self.cancel_press()
                self.tile_tip.cancel()
            self.sections = {}
            if count:
                self.sections[0] = "Frequently Used"
            else:
                top = heading("Frequently Used", 1)
                self.frequent_hint = QRect(4, top, width - 6, 32)
                tops = [self.frequent_hint.bottom() + gap] * self.cols
            self.sections[count] = "Favorites and Search Categories"
            self.sections[count + 1 + self.personal_count] = "Reactions"
        total = len(self.items) + (len(self.home_tiles) if self.home else 0)
        for i in range(total):
            if show_divider and i == len(self.items):
                self.row_divider.setGeometry(1, max(tops) - 3, width, self.row_divider.height())
                self.row_divider.update()
                tops = [self.row_divider.geometry().bottom() + 4] * self.cols
            if i in self.sections:
                tops = [heading(self.sections[i], max(tops))] * self.cols
            col = min(range(self.cols), key=tops.__getitem__)
            if self.home:
                h = self.frequent_row_pitch - gap
            else:
                item = self.items[i]
                ratio = (item.height / item.width) if item.width and item.height else 0.68
                h = round(cardw * min(1.22, max(0.50, ratio))) + 30
            self.positions.append(QRect(1 + col * (cardw + gap), tops[col], cardw, h))
            tops[col] += h + gap
        minimum = max(tops) + 1 if self.positions else 225
        if self.minimumHeight() != minimum:
            self.setMinimumHeight(minimum)
        if changed_capacity:
            self.hovered.emit(-1)
        self.layoutChanged.emit()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self.reflow()

    def hit(self, point):
        for i, rect in enumerate(self.positions):
            if rect.contains(point):
                return i
        return -1

    def can_relate(self, i):
        return self.related_enabled and 0 <= i < len(self.items) and self.items[i].kind in ("gif", "reference")

    @staticmethod
    def action_box(rect, action):
        if action == "related":
            left = rect.left() + 5
        elif action == "star":
            left = rect.right() - 62
        else:
            left = rect.right() - 31
        return QRect(left, rect.top() + 5, 27, 25)

    def action_at(self, i, point):
        if not 0 <= i < len(self.items) or point.y() >= self.positions[i].top() + 35:
            return ""
        for action in ("menu", "star", "related"):
            if action == "star" and self.items[i].kind == "reference":
                continue
            if action == "related" and not self.can_relate(i):
                continue
            box = self.action_box(self.positions[i], action)
            if box.adjusted(-4, -5, 5, 5).contains(point):
                return action
        return ""

    def mouseMoveEvent(self, event):
        xy = event.position().toPoint()
        if (
            self.pressed
            and (xy - self.pressed[2]).manhattanLength() >= QApplication.startDragDistance()
        ):
            self.cancel_press()
        pos = self.hit(xy)
        if pos != self.hot:
            self.hot = pos
            self.hovered.emit(pos)
            self.update()
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if pos >= 0 else Qt.CursorShape.ArrowCursor
        )
        self.tile_tip.observe(xy)

    def hideEvent(self, event):
        self.tile_tip.cancel()
        super().hideEvent(event)

    def leaveEvent(self, event):
        self.tile_tip.cancel()
        self.cancel_press()
        self.hot = -1
        self.hovered.emit(-1)
        self.update()
        super().leaveEvent(event)

    def cancel_press(self):
        self.pressed = None

    def mousePressEvent(self, event):
        self.tile_tip.cancel()
        xy = event.position().toPoint()
        tile = self.hit(xy)
        button = event.button()
        if tile >= 0 and (
            button in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton)
            or (button == Qt.MouseButton.MiddleButton and tile < len(self.items) and self.items[tile].kind == "gif")
        ):
            self.pressed = (tile, event.button(), xy, self.action_at(tile, xy))
            event.accept()
        else:
            self.cancel_press()
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        xy = event.position().toPoint()
        i = self.hit(xy)
        pressed = self.pressed
        self.cancel_press()
        if (
            not pressed
            or pressed[0] != i
            or pressed[1] != event.button()
            or (xy - pressed[2]).manhattanLength() >= QApplication.startDragDistance()
        ):
            return
        action = self.action_at(i, xy)
        if event.button() == Qt.MouseButton.LeftButton and action != pressed[3]:
            return
        self.selected = i
        if event.button() == Qt.MouseButton.MiddleButton:
            self.previewRelated.emit(self.items[i])
        elif i < len(self.items) and (event.button() == Qt.MouseButton.RightButton or action == "menu"):
            self.menu.emit(i, event.globalPosition().toPoint())
        elif event.button() == Qt.MouseButton.LeftButton:
            if action == "related":
                self.related.emit(self.items[i])
            elif action == "star":
                self.star.emit(i)
            else:
                self.activate.emit(i)
        self.update()

    def keyPressEvent(self, event):
        keys = Qt.Key
        if not self.positions:
            return super().keyPressEvent(event)
        distances = {
            keys.Key_Left: -1,
            keys.Key_Right: 1,
            keys.Key_Up: -self.cols,
            keys.Key_Down: self.cols,
        }
        if event.key() in distances:
            self.selected = max(
                0, min(len(self.positions) - 1, self.selected + distances[event.key()])
            )
            self.update()
            viewport = self.parentWidget()
            parent = viewport.parentWidget() if viewport else None
            if hasattr(parent, "ensureVisible"):
                r = self.positions[self.selected]
                parent.ensureVisible(r.center().x(), r.center().y(), 5, r.height() // 2)
            self.hovered.emit(self.selected)
        elif event.key() in (keys.Key_Enter, keys.Key_Return, keys.Key_Space):
            if not event.isAutoRepeat():
                i = max(0, self.selected)
                if (
                    event.key() != keys.Key_Space
                    and event.modifiers() == Qt.KeyboardModifier.ShiftModifier
                ):
                    if self.can_relate(i):
                        self.selected = i
                        self.related.emit(self.items[i])
                else:
                    self.activate.emit(i)
        elif (
            event.key() == keys.Key_Menu
            or (event.key() == keys.Key_F10 and event.modifiers() == Qt.KeyboardModifier.ShiftModifier)
        ) and 0 <= self.selected < len(self.items):
            if not event.isAutoRepeat():
                self.menu.emit(
                    self.selected, self.mapToGlobal(self.positions[self.selected].center())
                )
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        if not self.positions and self.empty[0]:
            rect = QRect(22, 34, max(100, self.width() - 44), 50)
            p.setFont(font(15, True))
            p.setPen(color("#f1f2f7"))
            p.drawText(rect, A.AlignCenter, self.empty[0])
            p.setFont(font(10))
            p.setPen(color("#b9bbc6"))
            p.drawText(
                QRect(34, 92, max(100, self.width() - 68), 94),
                A.AlignHCenter | A.AlignTop | Qt.TextFlag.TextWordWrap,
                self.empty[1],
            )
        if self.section_rows:
            large = self.home or self.favorite_headings
            p.setFont(self.section_font)
            p.setPen(color("#ffffff" if large else "#cfd0dc"))
            for rect, title in self.section_rows:
                if rect.adjusted(0, 0, 0, 8).intersects(event.rect()):
                    p.drawText(rect, SECTION_FLAGS if large else A.AlignLeft | A.AlignVCenter, title)
                    if large:
                        p.fillRect(1, rect.bottom() + 5, self.width() - 2, 2, color("#ffffff"))
        if self.frequent_hint.intersects(event.rect()):
            p.setFont(font(10))
            p.setPen(color("#c4c5d0"))
            p.drawText(self.frequent_hint, A.AlignLeft | A.AlignVCenter, "GIFs you copy or paste will appear here.")
        for i, r in enumerate(self.positions):
            if not r.intersects(event.rect()):
                continue
            p.save()
            path = QPainterPath()
            path.addRoundedRect(QRectF(r), 9, 9)
            p.setClipPath(path)
            p.setPen(NOPEN)
            if self.home and i >= len(self.items):
                self.paint_category(p, r, i - len(self.items))
            else:
                self.paint_result(p, r, i)
            p.restore()
            if self.hot == i or (self.hasFocus() and self.selected == i):
                p.setPen(QPen(color("#aeb5ff"), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(QRectF(r).adjusted(1, 1, -1, -1), 8, 8)

    def paint_category(self, p, r, i):
        title, mark, light, dark = self.home_tiles[i]
        g = QLinearGradient(QPointF(r.topRight()), QPointF(r.bottomLeft()))
        g.setColorAt(0, color(light))
        g.setColorAt(1, color(dark))
        p.fillRect(r, g)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 20), 1.4))
        for radius in (31, 55, 79):
            p.drawEllipse(QPointF(r.right() - 35, r.top() + 18), radius, radius)
        thumbnail = self.category_images.get(title)
        if thumbnail is not None:
            side = min(round(r.width() * 0.49), r.height() - 32)
            target = QRect(r.left() + 8, r.top() + (r.height() - 25 - side) // 2, side, side)
            p.save()
            p.setOpacity(0.83)
            p.drawPixmap(target, thumbnail)
            p.restore()
        p.setPen(QColor(255, 255, 255, 175))
        p.setFont(font(41, False, "Segoe UI Emoji"))
        p.drawText(r.adjusted(10, 0, -13, -17), A.AlignRight | A.AlignVCenter, mark)
        p.setFont(font(11.5, True))
        p.setPen(color("#ffffff"))
        caption = r.adjusted(13, 5, -6, -10)
        p.drawText(caption, A.AlignBottom | A.AlignLeft,
                   p.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, caption.width()))

    def paint_result(self, p, r, i):
        item = self.items[i]
        interacting = i in (self.hot, self.selected)
        related = interacting and self.can_relate(i)
        img = r.adjusted(0, 0, 0, -30)
        pix = self.image_for(i)
        quote_card = item.kind == "clip" and not item.thumb and not pix
        p.fillRect(r, color("#26272e"))
        if pix and not pix.isNull():
            cover(p, img, pix)
            if i in self.search_gifs and i not in self.painted_covers:
                self.painted_covers.add(i)
                self.artPainted.emit()
            if i in self.motion_errors and i in (self.hot, self.selected):
                strip = img.adjusted(0, img.height() - 24, 0, 0)
                p.fillRect(strip, QColor(27, 28, 36, 218))
                p.setPen(color("#f1f2f8"))
                p.setFont(font(9))
                p.drawText(strip, A.AlignCenter, "Animation unavailable")
        else:
            p.fillRect(img, color(("#32343e", "#363540", "#353944")[i % 3]))
            p.setPen(color("#bdc0ce"))
            p.setFont(font(10))
            placeholder = (
                "▶  Clip"
                if item.kind == "clip"
                else "Preview unavailable"
                if i in self.fail
                else "Loading preview…"
            )
            if quote_card:
                quote = img.adjusted(12, min(30, img.height() // 4), -12, -5)
                p.drawText(quote, A.AlignCenter | Qt.TextFlag.TextWordWrap, item.title)
            else:
                p.drawText(img, A.AlignCenter, placeholder)
        if item.kind == "reference":
            p.setFont(font(8, True))
            text = "Still image"
            badge = QRect(
                img.left() + 5, img.top() + 5, p.fontMetrics().horizontalAdvance(text) + 14, 23
            )
            if related:
                badge.moveLeft(self.action_box(r, "related").right() + 5)
            p.setPen(NOPEN)
            p.setBrush(QColor(31, 32, 42, 235))
            p.drawRoundedRect(badge, 5, 5)
            p.setPen(color("#e5e6f0"))
            p.drawText(badge, A.AlignCenter, text)
        bar = r.adjusted(8, r.height() - 30, -8, 0)
        p.setPen(color("#e4e5ea"))
        p.setFont(font(9, True))
        p.drawText(
            bar,
            A.AlignLeft | A.AlignVCenter,
            p.fontMetrics().elidedText(
                f"▶  {item.source}" if quote_card else item.title, Qt.TextElideMode.ElideRight, bar.width()
            ),
        )
        if interacting or item.key in self.favorite_keys:
            buttons = []
            if related:
                buttons.append(("related", ""))
            if item.kind != "reference":
                buttons.append(("star", "★" if item.key in self.favorite_keys else "☆"))
            if interacting:
                buttons.append(("menu", "⋯"))
            for action, text in buttons:
                box = self.action_box(r, action)
                p.setPen(NOPEN)
                p.setBrush(QColor(27, 28, 36, 222))
                p.drawRoundedRect(box, 6, 6)
                if action == "related":
                    self.draw_related_icon(p, box)
                    continue
                p.setPen(color("#ffd87d" if text == "★" else "#f1f2f8"))
                p.setFont(font(12, True))
                p.drawText(box, A.AlignCenter, text)

    @staticmethod
    def draw_related_icon(painter, box):
        # An image outline and a magnifier remain legible without an icon font.
        painter.save()
        painter.translate(QPointF(box.center()))
        painter.setPen(QPen(color("#f1f2f8"), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(-8, -6.5, 12.5, 10), 1.8, 1.8)
        painter.drawLine(QPointF(-7, 2.5), QPointF(-2.5, -2))
        painter.setBrush(color("#252630"))
        painter.drawEllipse(QPointF(3.3, 2), 3.8, 3.8)
        painter.drawLine(QPointF(6.2, 4.9), QPointF(9, 7.7))
        painter.restore()
