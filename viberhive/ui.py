import os
import sqlite3
from collections import defaultdict, deque
from concurrent.futures import CancelledError, ThreadPoolExecutor
from copy import deepcopy
from html import escape
from itertools import islice
from urllib.parse import unquote, urlsplit

from PyQt6.QtCore import (
    QEvent,
    QMimeData,
    QObject,
    QPoint,
    QPointF,
    QRectF,
    QSignalBlocker,
    QSize,
    Qt,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QInputDevice,
    QKeySequence,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyle,
    QStyleOptionComboBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from . import klipy_web, placement, windows
from . import providers as p
from .anchor import locate
from .animations import HOVER_WORKERS, Animations
from .category_art import CategoryArt
from .clipplay import ClipPlayback
from .discord_paste import DiscordPaste
from .folder_bar import FolderBar
from .gallery import Gallery, MAX_COLUMNS, MAX_FREQUENT_ROWS
from .google_query import search_url
from .google_results import GoogleResults
from .hotkeys import GlobalHotkey
from .model import Item, Page
from .navigation import History, Location
from .net import Problem, host_of, web_url
from .preview_search import related_query
from .related_pane import RelatedPane
from .search_previews import SearchPreviews
from .service import MIB, THUMBNAIL_WORKERS
from .theme import ASSETS, control_icon, icon
from .viewer import HoverPreview, ResizeGrip


class Jobs(QObject):
    done = pyqtSignal(object, object, object)

    def __init__(self):
        super().__init__()
        self.pools = {
            name: ThreadPoolExecutor(max_workers=n, thread_name_prefix=name)
            for name, n in (
                ("search", 2),
                ("preview", 3),
                ("google-still", THUMBNAIL_WORKERS),
                ("side-still", THUMBNAIL_WORKERS - 1),
                ("category", 2),
                ("motion", HOVER_WORKERS),
                ("motion-disk", 1),
                ("cover-read", 1),
                ("cover-write", 1),
                ("warm", 2),
                ("action", 2),
                ("anchor", 1),
                ("clips", 1),
            )
        }
        self.done.connect(self.deliver, Qt.ConnectionType.QueuedConnection)
        self.closed = False

    def run(self, lane, work, callback):
        if self.closed:
            return
        future = self.pools[lane].submit(work)

        def finish(f):
            try:
                value, error = f.result(), ""
            except CancelledError:
                return
            except Problem as e:
                value, error = None, str(e)
            except OSError:
                value, error = (
                    None,
                    "Could not read or write the local data, or contact the service.",
                )
            except Exception:  # noqa: BLE001 -- service details can contain credentials.
                value, error = (
                    None,
                    "This operation could not finish. Check the provider and try again.",
                )
            if not self.closed:
                self.done.emit(callback, value, error)

        future.add_done_callback(finish)
        return future

    def deliver(self, callback, value, error):
        if not self.closed:
            callback(value, error)

    def stop(self):
        self.closed = True
        for pool in self.pools.values():
            pool.shutdown(wait=False, cancel_futures=True)


def button(text, fn, quiet=False):
    b = QPushButton(text)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setProperty("quiet", quiet)
    b.clicked.connect(fn)
    return b


def label(text, name="small"):
    l = QLabel(text)
    l.setTextFormat(Qt.TextFormat.PlainText)
    l.setObjectName(name)
    return l


def plain_tooltip(text):
    # A result title or a provider error can contain angle brackets.
    return f"<qt>{escape(text)}</qt>" if "<" in text else text


class PickerStatus(QLabel):
    """A single-line status that keeps the last user action above background updates."""

    def __init__(self):
        super().__init__()
        self.setObjectName("small")
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setAccessibleName("Status")
        self.search_text = ""
        self.showing_action = False
        self.timer = QTimer(self, singleShot=True)
        self.timer.timeout.connect(self.clear_action)

    def sizeHint(self):
        margins = self.contentsMargins()
        return QSize(0, self.fontMetrics().height() + margins.top() + margins.bottom())

    def minimumSizeHint(self):
        return self.sizeHint()

    def setText(self, text):
        self.search_text = text
        if not self.showing_action:
            self.display_message(text)

    def display_message(self, text):
        super().setText(text)
        self.setAccessibleDescription(text)
        self.setToolTip(plain_tooltip(text))

    def display_text(self):
        line = " ".join(self.text().splitlines())
        return self.fontMetrics().elidedText(
            line, Qt.TextElideMode.ElideRight, self.contentsRect().width()
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        self.style().drawItemText(
            painter, self.contentsRect(), self.alignment(), self.palette(),
            self.isEnabled(), self.display_text(), QPalette.ColorRole.WindowText,
        )

    def action(self, text, duration=7000):
        self.showing_action = True
        self.timer.stop()
        self.display_message(text)
        if duration:
            self.timer.start(duration)

    def clear_action(self):
        self.showing_action = False
        self.timer.stop()
        self.display_message(self.search_text)


class ProviderBox(QComboBox):
    """Size the closed control to its selection, and the menu to its longest choice."""

    def __init__(self):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.currentIndexChanged.connect(self.updateGeometry)

    def sizeHint(self):
        hint = super().sizeHint()
        # Qt's active style reserves the drop-down and stylesheet padding separately.
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        option.rect.setWidth(300)
        field = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox, option,
            QStyle.SubControl.SC_ComboBoxEditField, self,
        )
        margins = option.rect.width() - field.width()
        hint.setWidth(self.fontMetrics().horizontalAdvance(self.currentText()) + margins + 6)
        return hint

    def minimumSizeHint(self):
        return self.sizeHint()

    def showPopup(self):
        longest = max((self.fontMetrics().horizontalAdvance(self.itemText(i))
                       for i in range(self.count())), default=0)
        self.view().setMinimumWidth(longest + 40)
        super().showPopup()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#ced0df"), 1.6))
        x = self.width() - 15
        y = self.height() / 2 - 2
        painter.drawLine(QPointF(x - 3, y), QPointF(x, y + 3))
        painter.drawLine(QPointF(x, y + 3), QPointF(x + 3, y))


class MoveHeader(QWidget):
    """Let the window manager handle dragging when the platform supports it."""

    def __init__(self, picker):
        super().__init__(picker)
        self.picker = picker
        self.press = self.offset = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Drag to move ViberHive. Double-click for full screen or to restore the window.")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.wrapped = None

    def set_controls(self, navigation, selectors, close):
        self.navigation, self.selectors, self.close_button = navigation, selectors, close
        stack = QVBoxLayout(self)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.first_row = QHBoxLayout()
        self.first_row.setSpacing(6)
        self.first_row.addWidget(navigation)
        self.first_row.addStretch(1)
        self.first_row.addWidget(close)
        self.second_row = QHBoxLayout()
        self.second_row.setSpacing(0)
        self.second_row.addStretch(1)
        stack.addLayout(self.first_row)
        stack.addLayout(self.second_row)
        self.arrange_controls()

    def minimumSizeHint(self):
        if not hasattr(self, "selectors"):
            return super().minimumSizeHint()
        width = max(self.navigation.minimumSizeHint().width() + self.close_button.width() + 6,
                    self.selectors.minimumSizeHint().width())
        return QSize(width, self.layout().minimumSize().height())

    def arrange_controls(self):
        required = self.navigation.sizeHint().width() + self.selectors.sizeHint().width() + self.close_button.width()
        wrapped = self.width() < required + 18
        if wrapped == self.wrapped:
            return
        self.wrapped = wrapped
        if wrapped:
            self.first_row.removeWidget(self.selectors)
            self.second_row.addWidget(self.selectors)
        else:
            self.second_row.removeWidget(self.selectors)
            self.first_row.insertWidget(2, self.selectors)
        self.layout().setSpacing(5 if wrapped else 0)
        self.layout().activate()
        self.updateGeometry()

    def event(self, event):
        result = super().event(event)
        if event.type() in (QEvent.Type.Resize, QEvent.Type.LayoutRequest) and hasattr(self, "second_row"):
            self.arrange_controls()
        return result

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.press = self.offset = None
        if not self.picker.isFullScreen():
            self.press = event.globalPosition().toPoint()
            self.offset = self.press - self.picker.frameGeometry().topLeft()
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mouseDoubleClickEvent(event)
        self.press = self.offset = None
        self.picker.toggle_fullscreen()
        event.accept()

    def mouseMoveEvent(self, event):
        if self.offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            event.accept()
            point = event.globalPosition().toPoint()
            if self.press is not None:
                # Starting a native move on the first press can consume the double-click on Windows.
                if (point - self.press).manhattanLength() < QApplication.startDragDistance():
                    return
                self.press = None
                self.picker.drag_active = True
                handle = self.picker.windowHandle()
                if handle is not None and handle.startSystemMove():
                    self.offset = None
                    return
            self.picker.move(point - self.offset)
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.press = self.offset = None
            self.picker.remember_move()
            self.picker.drag_active = False
        super().mouseReleaseEvent(event)


class PickerDialog(QDialog):
    def __init__(self, picker):
        # The tray can open Settings while the picker (and its native window) is hidden.
        super().__init__(picker if picker.isVisible() else None)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.present)

    def present(self):
        if self.isVisible():
            self.raise_()
            self.activateWindow()


def magnifier():
    pix = QPixmap(20, 20)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor("#bbbcca"), 1.5))
    painter.drawEllipse(QRectF(2.5, 2.5, 11, 11))
    painter.drawLine(QPointF(12, 12), QPointF(17.5, 17.5))
    painter.end()
    return pix


class Preferences(PickerDialog):
    def __init__(self, parent):
        super().__init__(parent)
        version = QApplication.applicationVersion()
        self.setWindowTitle(f"ViberHive {version} Settings" if version else "ViberHive Settings")
        self.setMinimumWidth(435)
        self.picker = parent
        settings = parent.settings
        form = QFormLayout(self)
        form.setContentsMargins(18, 18, 18, 18)
        form.setSpacing(12)
        self.keys = {}
        for field, title, env in (
            ("serp", "SerpApi key", "SERPAPI_API_KEY"),
            ("klipy", "KLIPY app key", "KLIPY_APP_KEY"),
        ):
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            if os.getenv(env):
                edit.setPlaceholderText(f"Provided by {env}")
                edit.setEnabled(False)
            else:
                edit.setText(settings.secrets.get(field, ""))
                edit.setPlaceholderText("Optional")
            edit.setMaxLength(256)
            self.keys[field] = edit
            form.addRow(title, edit)
        note = label(
            "Leave the keys empty to search public listings. A KLIPY app key enables API pagination. "
            "A SerpApi key selects API search for Google and Yarn. Each uncached SerpApi request can use one credit."
        )
        note.setWordWrap(True)
        form.addRow(note)
        self.hotkey = QComboBox()
        self.hotkey.addItems(["Ctrl+F12", "Ctrl+Shift+G"])
        self.hotkey.setCurrentText(settings.values["hotkey"])
        form.addRow("Global shortcut", self.hotkey)
        hotkey_status = label(parent.hotkey.status)
        hotkey_status.setWordWrap(True)
        parent.hotkey.statusChanged.connect(hotkey_status.setText)
        form.addRow(hotkey_status)
        self.reset_position = QCheckBox("Return to the Discord send arrow on next open")
        if settings.values.get("manual_position"):
            form.addRow("Window position", self.reset_position)
        self.related = QCheckBox("Find related Google images automatically")
        self.related.setChecked(settings.values.get("related", True))
        form.addRow(self.related)
        self.limit = QSpinBox()
        self.limit.setRange(64, 4096)
        self.limit.setSuffix(" MiB")
        self.limit.setValue(settings.values["cache_mib"])
        form.addRow("Media cache target", self.limit)
        form.addRow(
            label(
                f"Currently using {parent.store.usage() / MIB:.1f} MiB. Favorites and recent clipboard files are protected."
            )
        )
        controls = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        controls.accepted.connect(self.accept)
        controls.rejected.connect(self.reject)
        form.addRow(controls)

    def accept(self):
        s = self.picker.settings
        try:
            for name, edit in self.keys.items():
                if edit.isEnabled():
                    s.set_key(name, edit.text().strip())
            s.values.update(
                hotkey=self.hotkey.currentText(),
                cache_mib=self.limit.value(),
                related=self.related.isChecked(),
            )
            if self.reset_position.isChecked():
                s.values.pop("manual_position", None)
            s.save()
        except OSError:
            QMessageBox.warning(
                self, "Settings", "Windows could not save the settings or protect the key."
            )
            return
        self.picker.store.budget = self.limit.value() * MIB
        self.picker.jobs.run("action", self.picker.store.prune, lambda *_: None)
        super().accept()


class Picker(QWidget):
    firstFrameReady = pyqtSignal(str, bytes)

    def __init__(self, service):
        super().__init__(
            None,
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint,
        )
        self.svc = service
        self.store = service.store
        self.settings = service.settings
        self.jobs = Jobs()
        self.epoch = 0
        self.open_id = 0
        self.opening = False
        self.raised_target = None
        self.raise_timer = QTimer(self, singleShot=True, interval=450)
        self.raise_timer.timeout.connect(self.finish_summon)
        self.clip_id = 0
        self.clipboard_revision = 0
        QApplication.clipboard().dataChanged.connect(self.clipboard_changed)
        self.preview_tasks = set()
        self.google_still_tasks = set()
        self.google_stills_tried = set()
        self.saving_covers = set()
        self.firstFrameReady.connect(self.show_first_frame, Qt.ConnectionType.QueuedConnection)
        if hasattr(service, "first_frame_ready"):
            service.first_frame_ready = self.firstFrameReady.emit
        self.favorite_tasks = set()
        self.search_job = None
        self.busy = False
        self.failed = False
        self.query = ""
        self.displayed_visit = None
        self.restoring_scroll = False
        self._wheel_remainder = 0
        self.link_offer = None
        self.tab = "GIFs"
        self.browse = p.Browse()
        self.buffered = deque()
        self.animations = Animations(self)
        self.motion_target = -1
        self.previous = 0
        self.drag_active = False
        self.maximized_before_fullscreen = False
        self.position_dirty = False
        self.position_timer = QTimer(self, singleShot=True, interval=220)
        self.position_timer.timeout.connect(self.remember_move)
        self.search_delay = QTimer(self, singleShot=True, interval=5000)
        self.search_delay.setTimerType(Qt.TimerType.PreciseTimer)
        self.search_delay.timeout.connect(self.search_draft)
        self.setWindowTitle("ViberHive")
        self.setWindowIcon(icon())
        self.setObjectName("window")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.discord_paste = DiscordPaste(self)
        self.hotkey = GlobalHotkey(self)
        self._layout()
        self.category_art = CategoryArt(self)
        self.clips = ClipPlayback(self)
        self.board.favorite_keys = self.store.favorite_ids()
        self.tab_changed("GIFs")
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray_menu = QMenu()
        self.tray_menu.addAction("Open picker", self.open_picker)
        self.tray_menu.addAction("Settings…", self.preferences)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction("Quit ViberHive", QApplication.instance().quit)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self.tray_action)
        self.tray.setToolTip("ViberHive  •  " + self.settings.values["hotkey"])
        self.hotkey.activated.connect(self.open_picker)
        self.hotkey.statusChanged.connect(
            lambda status: self.tray.setToolTip(f"ViberHive  •  {status}")
        )
        self.hotkey.configure(self.settings.values["hotkey"])
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self.escape = QShortcut(QKeySequence("Escape"), self)
        self.escape.activated.connect(self.dismiss)
        self.find_shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        self.find_shortcut.activated.connect(self.focus_search)
        QApplication.instance().aboutToQuit.connect(self.google.shutdown)
        QApplication.instance().aboutToQuit.connect(self.hotkey.stop)
        QApplication.instance().aboutToQuit.connect(self.discord_paste.cancel)
        QApplication.instance().aboutToQuit.connect(self.jobs.stop)

    def _layout(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel = QFrame()
        panel.setObjectName("panel")
        outer.addWidget(panel)
        base = QVBoxLayout(panel)
        base.setContentsMargins(15, 12, 15, 9)
        base.setSpacing(10)
        self.header = MoveHeader(self)
        self.header.setObjectName("head")
        navigation = QWidget()
        row = QHBoxLayout(navigation)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        for artwork, width, accessible_name in (
            ("emblem", 32, ""),
            ("wordmark", 118, "ViberHive"),
        ):
            pixmap = QPixmap(str(ASSETS / f"viberhive-{artwork}.png"))
            logo = QLabel()
            logo.setObjectName(artwork)
            logo.setAccessibleName(accessible_name)
            logo.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            logo.setFixedSize(pixmap.size().scaled(width, 32, Qt.AspectRatioMode.KeepAspectRatio))
            logo.setScaledContents(True)
            logo.setPixmap(pixmap)
            row.addWidget(logo)
        row.addSpacing(9)
        self.tabs = {}
        group = QButtonGroup(self)
        for text in ("GIFs", "Clips", "Favorites"):
            b = button(text, lambda _, t=text: self.tab_changed(t))
            b.setCheckable(True)
            b.setProperty("tab", True)
            group.addButton(b)
            self.tabs[text] = b
            row.addWidget(b)
        self.favorite_kind = ProviderBox()
        self.favorite_kind.setAccessibleName("Favorites collection")
        self.favorite_kind.setToolTip("Choose saved GIFs or saved clips")
        self.favorite_kind.addItem("GIFs", "gif")
        self.favorite_kind.addItem("Clips", "clip")
        self.favorite_kind.currentIndexChanged.connect(self.favorite_kind_changed)
        row.addWidget(self.favorite_kind)
        selectors = QWidget()
        filters = QHBoxLayout(selectors)
        filters.setContentsMargins(0, 0, 0, 0)
        filters.setSpacing(6)
        self.source = ProviderBox()
        self.source.setCursor(Qt.CursorShape.ArrowCursor)
        self.source.setAccessibleName("GIF search provider")
        self.source.addItems([p.GOOGLE, p.COMMONS, p.KLIPY])
        self.source.setCurrentText(self.settings.values["source"])
        self.source.currentTextChanged.connect(self.source_changed)
        filters.addWidget(self.source)
        self.clip_source = ProviderBox()
        self.clip_source.setAccessibleName("Clip search provider")
        self.clip_source.setToolTip("Clip search provider")
        self.clip_source.addItem("Auto · clip sources", p.CLIPS)
        self.clip_source.addItem("Yarn.co", p.YARN)
        self.clip_source.addItem("Clip.Cafe", p.CLIP_CAFE)
        saved_source = self.settings.values.get("clip_source", p.CLIPS)
        self.clip_source.setCurrentIndex(max(0, self.clip_source.findData(saved_source)))
        self.clip_source.currentIndexChanged.connect(self.clip_source_changed)
        filters.addWidget(self.clip_source)
        self.google_mode = ProviderBox()
        self.google_mode.setAccessibleName("Google image search mode")
        self.google_mode.addItems(["GIFs only", "Explore all images"])
        self.google_mode.setToolTip("Explore ordinary images and choose one to find related GIFs.")
        self.google_mode.currentIndexChanged.connect(self.google_mode_changed)
        filters.addWidget(self.google_mode)
        self.preview_side = ProviderBox()
        self.preview_side.setAccessibleName("Preview position")
        for side in ("left", "right", "off"):
            self.preview_side.addItem(f"Preview {side}", side)
        saved_side = self.settings.values.get("preview_side", "left")
        self.preview_side.setCurrentIndex(max(0, self.preview_side.findData(saved_side)))
        self.preview_side.currentIndexChanged.connect(self.preview_side_changed)
        filters.addWidget(self.preview_side)
        close = button("", self.dismiss, True)
        close.setIcon(control_icon("close"))
        close.setIconSize(QSize(24, 24))
        close.setFixedSize(36, 34)
        close.setToolTip("Hide picker (Esc)")
        close.setAccessibleName("Hide picker")
        close.setObjectName("closeButton")
        self.header.set_controls(navigation, selectors, close)
        self.header_options = filters
        base.addWidget(self.header)
        search_box = QFrame()
        search_box.setObjectName("searchBox")
        search_row = QHBoxLayout(search_box)
        search_row.setContentsMargins(12, 4, 5, 4)
        search_row.setSpacing(9)
        search_icon = QLabel()
        search_icon.setPixmap(magnifier())
        search_row.addWidget(search_icon)
        self.edit = QLineEdit()
        self.edit.setObjectName("search")
        self.edit.setMaxLength(p.TITLE_LIMIT)
        self.edit.textChanged.connect(self.text_changed)
        self.edit.returnPressed.connect(self.submit)
        search_row.addWidget(self.edit, 1)
        self.clear_search = button("×", self.clear_search_field, True)
        self.clear_search.setObjectName("clearSearch")
        self.clear_search.setAccessibleName("Clear search")
        self.clear_search.setToolTip("Clear search")
        self.clear_search.setFixedSize(26, 26)
        self.clear_search.hide()
        search_row.addWidget(self.clear_search)
        self.add_url = button("Add GIF by URL…", self.add_favorite_url, True)
        search_row.addWidget(self.add_url)
        self.search_button = button("Search", self.submit)
        self.search_button.setObjectName("accent")
        self.search_button.setMinimumWidth(106)
        search_row.addWidget(self.search_button)
        base.addWidget(search_box)
        self.auto_animate = QCheckBox("Auto animate")
        self.auto_animate.setToolTip("Play all visible GIFs")
        self.auto_animate.setChecked(self.settings.values.get("auto_animate") is True)
        self.auto_animate.toggled.connect(self.auto_animate_changed)
        self.clip_autoplay = QCheckBox("Autoplay")
        self.clip_autoplay.setToolTip("Play a clip after hovering for two seconds")
        self.clip_autoplay.setChecked(self.settings.values.get("clip_autoplay") is True)
        self.clip_autoplay.toggled.connect(
            lambda checked: self.save_preference("clip_autoplay", checked)
        )
        self.folder_bar = FolderBar(self, ProviderBox())
        self.folder_bar.changed.connect(self.refresh_saved_view)
        self.folder_bar.folderRequested.connect(self.jump_to_folder)
        base.addWidget(self.folder_bar)
        self.scroll = QScrollArea()
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.board = Gallery()
        rows = self.settings.values.get("frequent_rows", 2)
        if type(rows) is int:
            self.board.frequent_rows = max(1, min(MAX_FREQUENT_ROWS, rows))
        self.board.frequentRowsChanged.connect(lambda rows: self.save_preference("frequent_rows", rows))
        self.board.setObjectName("board")
        self.scroll.setWidget(self.board)
        self.install_website_actions()
        self.preview = HoverPreview()
        self.related_pane = RelatedPane(self)
        self.search_previews = SearchPreviews(self)
        for gallery in (self.board, self.related_pane.board):
            gallery.browserCovers.connect(self.save_browser_covers)
        self.firstFrameReady.connect(self.related_pane.show_first_frame, Qt.ConnectionType.QueuedConnection)
        self.preview.install_related(self.related_pane)
        self.related_pane.closeRequested.connect(self.close_related_preview)
        QApplication.instance().aboutToQuit.connect(self.related_pane.shutdown)
        self.results_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.results_splitter.setObjectName("resultsSplitter")
        self.results_splitter.setChildrenCollapsible(False)
        self.results_splitter.setHandleWidth(18)
        self.results_splitter.addWidget(self.preview)
        self.results_splitter.addWidget(self.scroll)
        self.results_splitter.setStretchFactor(0, 1)
        self.results_splitter.setStretchFactor(1, 1)
        self.preview_fraction = 0.42
        self.results_splitter.setSizes([420, 580])
        self.results_splitter.splitterMoved.connect(self.remember_preview_width)
        self.results_splitter.handle(1).setToolTip("Drag to resize the preview")
        self.preview.hide()
        base.addWidget(self.results_splitter, 1)
        footer = QFrame()
        footer.setObjectName("footer")
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(0, 5, 0, 0)
        foot.setSpacing(4)
        self.back_button = button("", lambda: self.navigate(-1), True)
        self.forward_button = button("", lambda: self.navigate(1), True)
        for control, title in ((self.back_button, "Back"), (self.forward_button, "Forward")):
            control.setAccessibleName(title)
            control.setToolTip(title + (" (Alt+Left)" if title == "Back" else " (Alt+Right)"))
            control.setObjectName("navigationButton")
            control.setIcon(control_icon(title.lower()))
            control.setIconSize(QSize(21, 21))
            control.setFixedSize(32, 29)
            foot.addWidget(control)
        self.status = PickerStatus()
        foot.addWidget(self.status, 1)
        self.paste_link = button("Paste source link", self.paste_offered_link)
        self.paste_link.hide()
        foot.addWidget(self.paste_link)
        self.google_website = button("Open in browser ↗", self.open_google_website)
        self.google_website.setToolTip("Open this Google Images search in your default browser")
        self.google_website.hide()
        foot.addWidget(self.google_website)
        self.more = button("Load more", self.more_clicked)
        foot.addWidget(self.more)
        for action in (self.paste_link, self.google_website, self.more):
            action.setProperty("footerAction", True)
        self.footer_options = QHBoxLayout()
        self.footer_options.setSpacing(12)
        self.footer_options.addWidget(self.auto_animate)
        self.footer_options.addWidget(self.folder_bar.auto)
        foot.addLayout(self.footer_options)
        self.result_count = label("", "resultCount")
        foot.addWidget(self.result_count)
        self.settings_button = button("", self.preferences, True)
        self.settings_button.setAccessibleName("Settings")
        self.settings_button.setToolTip("Settings and API keys")
        self.settings_button.setObjectName("iconButton")
        self.settings_button.setIcon(control_icon("settings"))
        self.settings_button.setIconSize(QSize(25, 25))
        self.settings_button.setFixedSize(36, 34)
        foot.addWidget(self.settings_button)
        base.addWidget(footer)
        self.board.activate.connect(self.choose)
        self.board.star.connect(self.toggle_star)
        self.board.related.connect(self.open_related)
        self.board.previewRelated.connect(self.open_related_preview)
        self.board.menu.connect(self.card_menu)
        self.board.hovered.connect(self.hover)
        self.scroll.verticalScrollBar().valueChanged.connect(self.scrolled)
        self.scroll.verticalScrollBar().valueChanged.connect(self.board.tile_tip.scrolled)
        self.scroll.verticalScrollBar().actionTriggered.connect(self.scroll_intent)
        self.scroll.verticalScrollBar().sliderMoved.connect(self.scroll_intent)
        self.scroll.viewport().installEventFilter(self)
        self.scroll.verticalScrollBar().installEventFilter(self)
        self.board.installEventFilter(self)
        self.paint_timer = QTimer(self, singleShot=True, interval=25)
        self.paint_timer.timeout.connect(self.load_posters)
        self.board.layoutChanged.connect(self.schedule_posters)
        self.board.layoutChanged.connect(self.sync_preview_panel)
        self.board.layoutChanged.connect(self.update_animation_visibility)
        self.scroll_armed = False
        self.prefetch = QTimer(self, singleShot=True, interval=550)
        self.prefetch.timeout.connect(self.warm_next_batch)
        self.google = GoogleResults(self)
        self.resize_grips = ResizeGrip.install(self)
        self.history = History(self)
        self.history.navigate.connect(self.navigate)

    def is_google_tab(self):
        return self.tab == "GIFs" and self.provider() == p.GOOGLE

    def sync_navigation(self):
        google = self.is_google_tab()
        self.board.related_enabled = self.tab == "GIFs" or (
            self.tab == "Favorites" and self.favorite_kind.currentData() == "gif"
        )
        self.google_mode.setVisible(google)
        for control, delta in ((self.back_button, -1), (self.forward_button, 1)):
            control.setEnabled(self.can_navigate(delta))

    def can_navigate(self, delta):
        return self.history.can_go(delta)

    def google_mode_changed(self, index):
        self.set_placeholder()
        location = self.displayed_visit.location if self.displayed_visit else None
        if (
            self.is_google_tab() and location and location.seed
            and " ".join(self.edit.text().split()) == location.query
        ):
            self.show_google(Location(location.query, bool(index), location.seed, location.native_branch))
        else:
            self.submit(record_search=False)

    def remember_visit(self):
        visit = self.displayed_visit
        self.displayed_visit = None
        if visit is None:
            return
        visit.scroll = self.scroll.verticalScrollBar().value()
        visit.selected = self.board.selected
        if not visit.location.query or visit.location.tab == "Favorites":
            return
        visit.api_state = None
        visit.snapshot = None
        visit.website_more = self.klipy_website_more
        visit.failed = self.failed
        visit.google_signin = self.google.active and self.google.blocked == "signin"
        if self.board.items or (not self.google.active and not self.busy and not self.failed):
            visit.snapshot = Page(
                list(self.board.items),
                int(self.has_more(visit.location.provider)),
                getattr(self.google, "note", "") if self.google.active else "",
            )
            if not self.google.active:
                visit.api_state = (deepcopy(self.browse), deque(self.buffered), self.last_request)

    def navigate(self, delta):
        if not self.can_navigate(delta):
            return
        self.remember_visit()
        visit = self.history.go(delta)
        location = visit.location
        if location.tab == "GIFs":
            with QSignalBlocker(self.source), QSignalBlocker(self.google_mode):
                self.source.setCurrentText(location.provider)
                self.google_mode.setCurrentIndex(int(location.references))
        elif location.tab == "Clips":
            with QSignalBlocker(self.clip_source):
                self.clip_source.setCurrentIndex(self.clip_source.findData(location.provider))
        else:
            with QSignalBlocker(self.favorite_kind):
                self.favorite_kind.setCurrentIndex(self.favorite_kind.findData(location.collection))
        self.tab_changed(location.tab, search=location.query, restore=visit)

    def restore_position(self, visit):
        if not visit:
            return
        self.board.selected = visit.selected if visit.selected < len(self.board.positions) else -1
        position, token = visit.scroll, self.epoch

        def restore():
            if self.epoch == token:
                self.restoring_scroll = True
                try:
                    self.scroll.verticalScrollBar().setValue(position)
                finally:
                    self.restoring_scroll = False

        QTimer.singleShot(0, restore)
        self.paint_timer.start()

    def restore_results(self, visit):
        self.browse, self.buffered, self.last_request = deepcopy(visit.api_state)
        self.klipy_website_more = visit.website_more
        self.failed = visit.failed
        self.board.extend(visit.snapshot.items)
        self.board.empty = ("No matching results", "Try a different phrase or another source.")
        self.controls()
        self.status.setText(f"Restored {visit.location.provider} results.")

    def open_related(self, item):
        if item.kind not in ("gif", "reference"):
            return
        title = related_query(self, item)
        if not title:
            self.status.action("This image has no title or saved search phrase.")
            return
        if self.source.currentText() == p.GOOGLE:
            with QSignalBlocker(self.google_mode):
                self.google_mode.setCurrentIndex(0)
        self.tab_changed("GIFs", search=title)

    def open_related_preview(self, item):
        if item.kind != "gif" or self.is_clip_view():
            return
        query = related_query(self, item)
        if not query:
            self.status.action("This GIF has no title or saved search phrase.")
            return
        self.related_pane.open_search(item, query, self.source.currentText())
        self.sync_preview_panel()

    def close_related_preview(self):
        if self.related_pane.active:
            self.related_pane.clear()
            self.sync_preview_panel()
            self.refresh_preview()

    def open_visual_matches(self, item):
        if not self.is_google_tab() or not self.query or self.board.home:
            return
        refs = bool(self.displayed_visit and self.displayed_visit.location.references)
        if self.google.active:
            self.google.keep_page_for_branch()
        self.show_google(Location(self.query, refs, item, self.google.active))

    def provider(self):
        return self.clip_source.currentData() if self.tab == "Clips" else self.source.currentText()

    def clip_source_changed(self, _index):
        self.save_preference("clip_source", self.clip_source.currentData())
        if self.tab == "Clips":
            self.submit()

    def focus_search(self):
        self.edit.setFocus()
        self.edit.selectAll()

    def clear_search_field(self):
        self.edit.clear()
        self.edit.setFocus()

    def set_search_text(self, text):
        previous = self.edit.blockSignals(True)
        self.edit.setText(text)
        self.edit.blockSignals(previous)
        self.clear_search.setVisible(bool(self.edit.text()))

    def tray_action(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_picker()

    def present_window(self, target, origin, token):
        self.finish_summon()
        if target.isMinimized():
            target.setWindowState(target.windowState() & ~Qt.WindowState.WindowMinimized)
        target.show()
        hwnd = int(target.winId())
        if windows.raise_for_summon(hwnd, origin):
            self.raised_target = (target, hwnd)
            target.installEventFilter(self)
            self.raise_timer.start()
        target.raise_()
        target.activateWindow()
        if target is self:
            self.focus_search()
        if windows.focus(hwnd, origin, keyboard=True):
            return

        def retry():
            # Restoration and activation can finish on the next Windows message cycle.
            # A later summon, dismissal, dialog or foreground switch supersedes this one.
            if self.open_id != token or (QApplication.activeModalWidget() or self) is not target:
                return
            if target.isVisible() and not target.isMinimized() and windows.foreground() in (
                origin, hwnd, 0, None
            ):
                if not windows.focus(hwnd, origin, keyboard=True):
                    QApplication.alert(target, 2500)

        timer = QTimer(target, singleShot=True, interval=90)
        timer.timeout.connect(retry)
        timer.timeout.connect(timer.deleteLater)
        timer.start()

    def finish_summon(self, *, yield_to_active=False):
        if self.raised_target:
            target, hwnd = self.raised_target
            self.raised_target = None
            self.raise_timer.stop()
            target.removeEventFilter(self)
            windows.finish_summon(hwnd, yield_to_active=yield_to_active)

    def toggle_fullscreen(self):
        # A pending Discord anchor belongs to the old placement, even after a quick round trip.
        self.open_id += 1
        self.remember_move()
        self.drag_active = False
        if self.isFullScreen():
            if self.maximized_before_fullscreen:
                self.showMaximized()
            else:
                self.showNormal()
        else:
            self.maximized_before_fullscreen = self.isMaximized()
            self.showFullScreen()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            for grip in self.resize_grips:
                grip.setVisible(not self.isFullScreen())

    def open_picker(self):
        origin = windows.foreground()
        self.open_id += 1
        token = self.open_id
        popup = QApplication.activePopupWidget()
        if popup:
            popup.close()
        modal = QApplication.activeModalWidget()
        if modal:
            self.present_window(modal, origin, token)
            return
        # Qt can still report an active picker while its native activation event
        # is queued. Use the real foreground at this press, even on repeat summons.
        if origin and not windows.own_window(origin):
            self.previous = origin
        if self.isVisible() or self.isFullScreen():
            hidden = self.isHidden()
            self.present_window(self, origin, token)
            if hidden:
                self.schedule_draft()
            return
        self.opening = True

        def show_at(rect):
            if self.open_id != token:
                return
            self.opening = False
            self.drag_active = False
            self.setGeometry(rect)
            self.present_window(self, origin, token)
            self.paint_timer.start()
            self.schedule_draft()

        saved = placement.manual_rect(self.settings.values.get("manual_position"))
        if saved is not None:
            show_at(saved)
            return
        area, pixels, mapping = placement.capture(origin)

        def ready(hit, _):
            point = placement.logical(hit, mapping) if hit else None
            show_at(placement.popup_rect(area, point))

        if pixels is None:
            ready(None, "")
        else:
            # Open now. Template matching may take longer on high-resolution desktops.
            ready(None, "")

            def align(hit, _):
                if (
                    hit
                    and self.isVisible()
                    and self.open_id == token
                    and not self.drag_active
                    and not self.settings.values.get("manual_position")
                ):
                    self.move(placement.popup_rect(area, placement.logical(hit, mapping)).topLeft())

            self.jobs.run("anchor", lambda: locate(pixels, mapping[4]), align)

    def moveEvent(self, event):
        super().moveEvent(event)
        if self.drag_active and self.isVisible():
            self.position_dirty = True
            self.position_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        for grip in self.resize_grips:
            grip.fit(event.size())
        if self.drag_active and self.isVisible():
            self.position_dirty = True
            self.position_timer.start()

    def remember_move(self):
        # Some window managers consume the mouse release during a native move.
        # moveEvent also saves the final position after movement settles.
        self.position_timer.stop()
        if not self.position_dirty:
            return
        self.position_dirty = False
        screen = QApplication.screenAt(self.frameGeometry().center()) or self.screen()
        area = screen.availableGeometry()
        rect = self.frameGeometry()
        self.settings.values["manual_position"] = {
            "screen": screen.name(),
            "x": rect.x() - area.x(),
            "y": rect.y() - area.y(),
            "w": rect.width(),
            "h": rect.height(),
        }
        try:
            self.settings.save()
        except OSError:
            self.status.setText("Window position will be remembered for this session.")

    def dismiss(self):
        active = self.isActiveWindow()
        self.open_id += 1
        self.opening = False
        self.clip_id += 1
        self.hide()
        if active:
            windows.restore(self.previous)

    def hideEvent(self, event):
        self.close_related_preview()
        self.clips.clear()
        self.remember_move()
        self.drag_active = False
        self.clip_id += 1
        self.discord_paste.cancel()
        self.status.clear_action()
        self.prefetch.stop()
        self.search_delay.stop()
        self.hover(-1)
        self.animations.suspend()
        self.board.hot = -1
        self.google.visibility_changed(False)
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self.google.visibility_changed(True)
        self.paint_timer.start()

    def closeEvent(self, event):
        event.ignore()
        self.dismiss()

    def preferences(self):
        active = QApplication.activeModalWidget()
        if active:
            active.raise_()
            active.activateWindow()
            return
        self.open_id += 1
        self.opening = False
        self.remember_move()
        self.drag_active = False
        self.search_delay.stop()
        self.discord_paste.cancel()
        self.clip_id += 1
        self.status.clear_action()
        old_source_key = (self.settings.serp, self.settings.klipy)
        dialog = Preferences(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.hotkey.configure(self.settings.values["hotkey"])
            old_key = old_source_key[1] if self.provider() == p.KLIPY else old_source_key[0]
            new_key = self.settings.klipy if self.provider() == p.KLIPY else self.settings.serp
            if self.query and self.tab != "Favorites" and self.isVisible() and old_key != new_key:
                self.submit(record_search=False)
        dialog.deleteLater()
        self.schedule_draft()

    def add_favorite_url(self):
        dialog = PickerDialog(self)
        dialog.setWindowTitle("Save a GIF by URL")
        dialog.setMinimumWidth(405)
        layout = QFormLayout(dialog)
        link = QLineEdit()
        link.setMaxLength(8192)
        link.setPlaceholderText("https://…")
        name = QLineEdit()
        name.setPlaceholderText("Optional name for Favorites")
        name.setMaxLength(180)
        instructions = label(
            "In Google Images, right-click the full-size image and choose Copy image address. Paste the address below."
        )
        instructions.setWordWrap(True)
        layout.addRow(instructions)
        layout.addRow("Image URL", link)
        layout.addRow("Name", name)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        url = web_url(link.text().strip())
        if not url:
            self.status.setText("Enter a public HTTPS image URL.")
            return
        title = name.text().strip() or unquote(urlsplit(url).path.split("/")[-1]) or host_of(url)
        item = Item(p.label(title), url, "", "", "Saved URL")
        self.status.setText("Checking the animation and saving the favorite…")

        def complete(_, error):
            self.favorites_changed()
            self.status.setText(error or "Favorite saved.")

        self.jobs.run("action", lambda: self.svc.favorite(item, title), complete)

    def install_website_actions(self):
        self.edit.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.edit.customContextMenuRequested.connect(self.search_menu)
        # The gallery paints its empty title and explanation above this action.
        # Keeping it in the scrolling content preserves the footer on compact screens.
        self.empty_website = button(
            "Open search on website ↗", lambda: self.open_website(self.query), True
        )
        self.empty_website.setAccessibleName("Open search on website")
        self.empty_website.hide()
        self.account_search = button("", lambda: self.google.search_alternative())
        self.account_search.setObjectName("accent")
        self.account_search.hide()
        layout = QVBoxLayout(self.board)
        layout.setContentsMargins(0, 188, 0, 0)
        layout.addWidget(self.account_search, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.empty_website, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()

    def sync_website_action(self):
        active = self.google.active
        finished = (
            bool(self.google.blocked) or self.google.exhausted
            if active
            else not self.busy and bool(self.board.empty[0])
        )
        visible = bool(
            self.query and self.tab != "Favorites" and not self.board.home
            and not self.board.items and finished
        )
        account = active and self.google.blocked == "signin"
        self.google_website.setVisible(account)
        if visible and account:
            self.account_search.setText(f"Search {self.google.alternative_provider}")
        self.account_search.setVisible(visible and account)
        self.empty_website.setVisible(visible and not account)

    def search_menu(self, point):
        menu = self.edit.createStandardContextMenu()
        if self.tab != "Favorites":
            menu.addSeparator()
            self.add_search_website_action(menu)
        menu.exec(self.edit.mapToGlobal(point))
        menu.deleteLater()

    def add_search_website_action(self, menu, query=None, *, clip_source=None):
        # Automatic search can replace the selected branch while its menu is open.
        url = self.website_url(query, clip_source=clip_source)
        if url:
            menu.addAction("Open search on website ↗", lambda: QDesktopServices.openUrl(QUrl(url)))

    def open_website(self, query=None):
        if url := self.website_url(query):
            QDesktopServices.openUrl(QUrl(url))

    def open_google_website(self):
        google = self.google
        if google.active and google.blocked == "signin":
            url = search_url(google.query, references=google.references, seed=google.branch)
            QDesktopServices.openUrl(QUrl(url))

    def website_url(self, query=None, *, clip_source=None):
        if self.tab == "Favorites":
            return ""
        if query is None:
            query = " ".join(self.edit.text().split())
        if self.is_google_tab():
            seed = (
                self.displayed_visit.location.seed
                if self.displayed_visit and query == self.query
                else None
            )
            url = search_url(query, references=self.google_mode.currentIndex() == 1, seed=seed)
        elif self.tab == "Clips":
            source = self.provider()
            if source == p.CLIPS:
                if clip_source in (p.YARN, p.CLIP_CAFE):
                    source = clip_source
                elif self.board.items and query == self.query:
                    source = self.board.items[0].source
            url = p.clips_website(query, source)
        else:
            url = p.website(self.provider(), query)
        return url

    def source_changed(self, text):
        self.save_preference("source", text)
        self.set_placeholder()
        self.sync_navigation()
        if self.tab == "GIFs":
            if self.edit.text().strip():
                self.submit(record_search=False)
            else:
                self.home()

    def set_placeholder(self):
        self.edit.setPlaceholderText(
            ("Search favorite clips" if self.favorite_kind.currentData() == "clip" else "Search favorite GIFs")
            if self.tab == "Favorites"
            else "Find a clip by quote…"
            if self.tab == "Clips"
            else "Search KLIPY"
            if self.provider() == p.KLIPY
            else "Find images to use as starting points…"
            if self.is_google_tab() and self.google_mode.currentIndex() == 1
            else f"Search {self.provider()} for GIFs…"
        )

    def favorite_kind_changed(self):
        if self.tab == "Favorites":
            self.tab_changed("Favorites")

    def tab_changed(self, name, *, search="", restore=None):
        self.close_related_preview()
        self.remember_visit()
        self.tab = name
        self.tabs[name].setChecked(True)
        self.source.setVisible(name == "GIFs")
        self.clip_source.setVisible(name == "Clips")
        self.favorite_kind.setVisible(name == "Favorites")
        options = self.footer_options if name == "Favorites" else self.header_options
        if options.indexOf(self.clip_autoplay) < 0:
            options.insertWidget(0, self.clip_autoplay)
        clip_view = self.is_clip_view()
        self.auto_animate.setVisible(not clip_view)
        self.clip_autoplay.setVisible(clip_view)
        self.folder_bar.auto.setVisible(name == "Favorites")
        self.search_button.setToolTip("Filter the selected favorites folder" if name == "Favorites" else "Search now")
        self.add_url.setVisible(name == "Favorites" and not clip_view)
        self.folder_bar.setVisible(name == "Favorites")
        self.set_search_text(search)
        self.set_placeholder()
        self.sync_navigation()
        if name == "Favorites":
            self.folder_bar.set_kind(self.favorite_kind.currentData())
            if restore:
                self.folder_bar.reload(restore.location.folder)
            self.show_favorites(restore=restore)
        elif search:
            self.submit(record_search=restore is None, restore=restore)
        else:
            self.home(restore=restore)
        self.edit.setFocus()

    def reset_view(self):
        self.clips.clear()
        self.remember_visit()
        self.clear_link_offer()
        self.google.reset()
        self.search_delay.stop()
        self.prefetch.stop()
        self.discord_paste.cancel()
        self.status.clear_action()
        self.epoch += 1
        self.google_stills_tried.clear()
        self.clip_id += 1
        if self.search_job:
            self.search_job.cancel()
        self.busy = self.failed = False
        self.klipy_website_more = False
        self.browse = p.Browse()
        self.buffered.clear()
        self.last_request = (0, "")
        self.cover_seen = False
        self.scroll_armed = False
        self.hover(-1)
        self.scroll.verticalScrollBar().setValue(0)

    def home(self, *, record=True, restore=None):
        self.reset_view()
        self.query = ""
        self.displayed_visit = restore or self.history.visit(
            Location("", self.is_google_tab() and self.google_mode.currentIndex() == 1,
                     tab=self.tab, provider=self.provider()), transient=not record
        )
        self.sync_navigation()
        if self.tab == "Clips":
            clips = self.store.favorites(kind="clip")
            self.board.reset(items=clips, sections={0: "Saved clips"} if clips else None)
            self.board.empty = (
                "No saved clips yet",
                "Search for a quote and star a clip to keep it here.",
            )
            self.board.update()
            self.status.setText("Search for a quote, or click a saved clip to pin its preview.")
        else:
            self.show_gif_home()
            self.status.setText("Choose a GIF or a search category, or enter a search.")
        self.more.hide()
        self.update_result_count()
        self.restore_position(restore)

    def text_changed(self, text):
        self.close_related_preview()
        self.clear_search.setVisible(bool(text))
        self.clear_link_offer()
        self.search_delay.stop()
        self.clip_id += 1
        self.discord_paste.cancel()
        self.status.clear_action()
        if self.tab == "Favorites":
            self.show_favorites()
        elif not text.strip():
            self.home(record=False)
        elif " ".join(text.split()) != self.query:
            self.prefetch.stop()
            self.schedule_draft()
        elif self.status.search_text.startswith("Search starts after five seconds"):
            self.status.setText(f"Showing results for {self.query}.")

    def has_draft(self):
        query = " ".join(self.edit.text().split())
        return (
            self.isVisible()
            and not QApplication.activeModalWidget()
            and self.tab != "Favorites"
            and bool(query)
            and query != self.query
        )

    def schedule_draft(self):
        if self.has_draft():
            self.search_delay.start()
            self.status.setText("Search starts after five seconds. Press Enter to search now.")

    def search_draft(self):
        if self.has_draft():
            self.submit()

    def jump_to_folder(self, folder_id):
        selector = self.folder_bar.selector
        index = selector.findData(folder_id)
        if index < 0:
            return
        scrollbar = self.scroll.verticalScrollBar()
        if selector.currentData() is None:
            title = selector.itemText(index)
            for rect, heading in self.board.section_rows:
                if heading == title:
                    scrollbar.setValue(rect.top() - 4)
                    return
        # Newest-first views and folders with no matching section use the normal folder filter.
        selector.setCurrentIndex(index)
        scrollbar.setValue(0)

    def show_favorites(self, *, restore=None):
        self.reset_view()
        self.query = self.edit.text().strip()
        folder_id = self.folder_bar.selector.currentData()
        kind = self.favorite_kind.currentData()
        location = Location(self.query, tab="Favorites", provider="", collection=kind, folder=folder_id)
        self.displayed_visit = restore or self.history.visit(location)
        self.displayed_visit.location = location  # A folder may have been deleted since this visit.
        self.sync_navigation()
        noun = "clips" if kind == "clip" else "GIFs"
        entries = self.store.favorite_entries(self.query, kind=kind, folder_id=folder_id)
        items, sections = [], {}
        if self.folder_bar.auto.isChecked():
            grouped = defaultdict(list)
            for item, folder in entries:
                grouped[folder].append(item)
            for folder in sorted(grouped, key=lambda name: (name is None, (name or "").casefold())):
                sections[len(items)] = folder or "Unfiled"
                items.extend(grouped[folder])
        else:
            items = [item for item, _ in entries]
        self.board.reset(items=items, sections=sections, favorite_headings=True)
        self.board.empty = (
            ("No matching favorites", "Try a title, provider or the words you used to find it.")
            if self.query
            else ("This folder is empty", "Use Move to folder on a saved favorite to file it here.")
            if folder_id
            else (f"No unfiled {noun}", f"Saved {noun} without a folder appear here.")
            if folder_id == 0
            else (f"No favorite {noun} yet", "Select the star on a search result to save it here.")
        )
        self.status.setText("Click a clip to pin its preview. Right-click to move it to a folder." if kind == "clip"
                            else "Right-click a GIF to move it to a folder or search for more like it.")
        self.more.hide()
        self.update_result_count()
        self.paint_timer.start()
        self.restore_position(restore)

    def refresh_saved_view(self):
        if self.tab == "Favorites":
            self.show_favorites()
        elif self.tab == "Clips" and not self.edit.text().strip():
            self.home()
        elif self.tab == "GIFs" and self.board.home:
            self.show_gif_home()

    def show_gif_home(self):
        self.hover(-1)
        self.category_art.refresh()
        capacity = MAX_FREQUENT_ROWS * MAX_COLUMNS
        self.board.show_categories(self.store.search_categories(), self.store.frequent_gifs(capacity))

    def favorites_changed(self):
        self.board.favorite_keys = self.store.favorite_ids()
        self.related_pane.favorites_changed()
        if self.tab == "Favorites":
            self.folder_bar.reload()
            self.show_favorites(restore=self.displayed_visit)
        else:
            self.refresh_saved_view()
        self.board.update()

    def submit(self, *, record_search=True, restore=None):
        self.close_related_preview()
        self.search_delay.stop()
        if self.tab == "Favorites":
            return self.show_favorites()
        query = " ".join(self.edit.text().split())
        if not query:
            return self.home()
        if record_search and self.tab == "GIFs":
            try:
                self.store.record_gif_search(query)
            except sqlite3.Error:
                self.status.action("Search started. This phrase could not be saved for search categories.")
        if self.is_google_tab():
            self.show_google(restore.location if restore else Location(query, self.google_mode.currentIndex() == 1), restore)
        else:
            self.reset_view()
            self.query = query
            self.displayed_visit = restore or self.history.visit(Location(query, tab=self.tab, provider=self.provider()))
            self.sync_navigation()
            self.board.reset()
            self.board.empty = ("Searching…", f"{self.provider()}  ·  {query}")
            self.update_result_count()
            if restore and restore.api_state:
                self.restore_results(restore)
            elif restore and self.provider() == p.YARN and (restore.snapshot or restore.google_signin):
                self.google.start(query, yarn_clips=True, snapshot=restore.snapshot, signin_required=restore.google_signin)
            elif self.provider() != p.YARN or not self.google.restore_recent_yarn(query):
                self.get_page(0, "")
            self.restore_position(restore)

    def show_google(self, location, restore=None):
        self.close_related_preview()
        self.reset_view()
        self.query = location.query
        if self.edit.text() != self.query:
            self.set_search_text(self.query)
        self.displayed_visit = restore or self.history.visit(location)
        self.sync_navigation()
        self.board.reset()
        self.board.empty = ("Searching…", location.seed.title if location.seed else location.query)
        self.update_result_count()
        snapshot = restore.snapshot if restore else None
        if restore and restore.api_state and snapshot:
            self.restore_results(restore)
        elif (
            location.references
            or location.native_branch
            or not self.settings.serp
            or snapshot
            or (restore and restore.google_signin)
            or (location.seed and not location.seed.seed)
        ):
            if not self.google.start(
                location.query,
                references=location.references,
                seed=location.seed,
                snapshot=snapshot,
                signin_required=bool(restore and restore.google_signin),
            ):
                if location.references or location.seed:
                    self.board.empty = (
                        "Google browser unavailable",
                        "Run Setup.cmd to install the browser component.",
                    )
                    self.board.update()
                    self.status.setText(self.board.empty[1])
                else:
                    self.get_page(0, "")
        else:
            if location.seed:
                self.browse.branch_seed = location.seed.seed
            self.get_page(0, location.seed.seed if location.seed else "")
        self.restore_position(restore)

    def has_more(self, provider=None):
        if self.google.active:
            return self.google.has_more()
        return bool(
            self.buffered
            or self.browse.next_request(
                (provider or self.provider()) == p.GOOGLE and self.settings.values.get("related", True)
            )
        )

    def update_result_count(self):
        """Count the tiles currently in this view, including a filtered Favorites view."""
        saved_clips = self.tab == "Clips" and not self.query and bool(self.board.items)
        visible = self.tab == "Favorites" or bool(self.query) or saved_clips
        self.result_count.setVisible(visible)
        if visible:
            count = len(self.board.items)
            text = f"{count:,} {'result' if count == 1 else 'results'}"
            location = self.displayed_visit.location if self.displayed_visit else None
            if location and location.seed:
                detail = f"More like “{location.seed.title}” · {self.google_mode.currentText()}"
            else:
                if self.tab == "Favorites":
                    source = self.tab
                elif saved_clips:
                    source = "Saved clips"
                elif self.tab == "Clips" and self.provider() == p.CLIPS:
                    source = self.board.items[0].source if count else "Quote clips"
                else:
                    source = self.provider()
                detail = f"{source} · {self.query}" if self.query else source
            self.result_count.setText(text)
            self.result_count.setAccessibleName(text)
            self.result_count.setAccessibleDescription(detail)
            self.result_count.setToolTip(plain_tooltip(detail))
        self.sync_website_action()

    def controls(self):
        if self.google.active:
            self.google.sync()
            return
        on_results = bool(self.query and self.tab != "Favorites")
        has_more = self.has_more()
        self.more.setVisible(on_results and (self.busy or self.failed or has_more or self.klipy_website_more))
        self.more.setEnabled(not self.busy)
        branch = bool(self.displayed_visit and self.displayed_visit.location.seed)
        self.more.setText(
            "Searching…" if self.busy else "Retry" if self.failed
            else "More on KLIPY ↗" if self.klipy_website_more and not has_more
            else "More results" if branch else "Load more"
        )
        self.update_result_count()

    def get_page(self, page, seed):
        if self.busy:
            return
        self.busy = True
        self.failed = False
        self.last_request = (page, seed)
        self.scroll_armed = False
        token = self.epoch
        source, query = self.provider(), self.query
        name = "quote clips" if source == p.CLIPS else source
        branch = self.displayed_visit.location.seed if self.displayed_visit else None
        self.status.setText(
            "Loading more similar GIFs…" if branch and page != 0
            else "Finding related GIFs…" if seed else f"Searching {name}…"
        )
        self.controls()

        def complete(answer, error):
            if token != self.epoch:
                return
            self.busy = False
            if source == p.GOOGLE and (error == p.GOOGLE_SIGNIN or (answer and answer[1] == p.GOOGLE_SIGNIN)):
                cached = answer[0].items if answer else []
                snapshot = Page(list(self.board.items) + cached, 1)
                self.board.reset()
                self.google.start(query, references=self.google_mode.currentIndex() == 1,
                                  seed=branch, snapshot=snapshot, signin_required=True)
                return
            if error:
                if branch and page == 0 and self.google.start(query, seed=branch):
                    return
                if (
                    source == p.YARN and page == 0 and not self.board.items
                    and self.google.start(query, yarn_clips=True)
                ):
                    return
                if source == p.GOOGLE and not self.settings.serp and self.google.browser is None:
                    error = (
                        "Live Google Images needs the browser component. Quit ViberHive and run Setup.cmd in this version’s folder. "
                        + error
                    )
                self.failed = True
                self.status.setText(error)
                if not self.board.items:
                    self.board.empty = ("Search unavailable", error)
            else:
                result, note = answer
                if source == p.KLIPY and result.more == klipy_web.WEBSITE_MORE:
                    self.klipy_website_more = True
                    result = Page(result.items, None)
                if branch:
                    result = Page([row for row in result.items if row.key != branch.key], result.more)
                    if (page == 0 and not result.items and result.more is None
                            and self.google.start(query, seed=branch)):
                        return
                fresh = self.browse.accept(result, seed)
                # A four-row batch keeps large provider pages local until needed.
                if seed and not branch:
                    for item in reversed(fresh):
                        self.buffered.appendleft(item)
                else:
                    self.buffered.extend(fresh)
                self.append_batch()
                self.failed = False
                if not self.board.items:
                    if branch:
                        self.board.empty = (
                            ("Looking for similar GIFs…", "Checking this image’s More results page.")
                            if page == 0 and self.has_more()
                            else ("No GIFs in this batch", "Choose More results to continue.")
                            if self.has_more()
                            else ("No similar GIFs", "Google returned no GIFs for this image.")
                        )
                    else:
                        self.board.empty = (
                            "No matching results",
                            "Try a different phrase, another source, or Load more when available.",
                        )
                msg = (
                    "Related GIFs added."
                    if seed and fresh
                    else "More results are available for this image."
                    if branch and self.has_more()
                    else "End of this image’s related GIFs."
                    if branch
                    else "No new GIFs in that panel."
                    if seed
                    else "Click a clip to pin its preview. Use Paste into Discord to share it."
                    if self.tab == "Clips"
                    else "Powered by KLIPY  ·  Click to paste a link into Discord. Hover to preview."
                    if source == p.KLIPY
                    else "Hover to play. Click to paste into Discord. Right-click for Copy GIF."
                )
                self.status.setText((note + "  ·  " if note else "") + msg)
                if page == 0 and self.has_more() and (branch or (not seed and result.items)):
                    self.prefetch.start()
            self.board.update()
            self.controls()
            self.paint_timer.start()

        self.search_job = self.jobs.run(
            "search", lambda: self.svc.search(source, query, page, seed), complete
        )

    def append_batch(self):
        batch = [self.buffered.popleft() for _ in range(min(24, len(self.buffered)))]
        if batch:
            self.consider_search_cover(batch)
            self.extend_search_results(self.board, batch)

    def consider_search_cover(self, items):
        visit = self.displayed_visit
        if self.cover_seen or not visit or self.tab != "GIFs" or visit.location.seed or visit.location.references:
            return
        candidate = next((item for item in items if item.kind == "gif"), None)
        if candidate:
            self.cover_seen = True
            try:
                self.store.remember_search_cover(visit.location.query, candidate)
            except sqlite3.Error:
                pass  # Optional artwork must never prevent a search from displaying.

    def more_clicked(self):
        if self.klipy_website_more and not self.busy and not self.failed and not self.has_more():
            self.open_website(self.query)
        else:
            self.load_more()

    def load_more(self):
        self.prefetch.stop()
        if self.google.active:
            self.google.more()
            return
        if self.busy or not self.query or self.tab == "Favorites":
            return
        if self.failed:
            return self.get_page(*self.last_request)
        choice = self.browse.next_request(
            self.provider() == p.GOOGLE and self.settings.values.get("related", True)
        )
        # Interleave original local batches with genuinely new panels, beginning with the first panel.
        if self.buffered and (
            self.browse.branch_seed or not choice or not choice[1]
            or getattr(self, "last_request", (0, ""))[1]
        ):
            self.append_batch()
            self.last_request = (0, "")
            self.controls()
            return
        if choice:
            self.get_page(*choice)

    def warm_next_batch(self):
        if (
            self.isVisible()
            and not self.busy
            and not self.failed
            and self.query
            and self.tab != "Favorites"
            and not self.has_draft()
            and not QApplication.activeModalWidget()
            and not self.google.active
        ):
            self.load_more()

    def scroll_intent(self, *_):
        self.scroll_armed = True
        QTimer.singleShot(0, lambda: self.scrolled(self.scroll.verticalScrollBar().value()))

    def eventFilter(self, watched, event):
        if self.raised_target and watched is self.raised_target[0] and event.type() in (
            QEvent.Type.Hide, QEvent.Type.WindowDeactivate,
        ):
            self.finish_summon(yield_to_active=True)
        if watched is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            self.schedule_posters()
        if event.type() == QEvent.Type.Wheel and watched in (
            self.board, self.scroll.viewport(), self.scroll.verticalScrollBar()
        ):
            if self.scroll_results_wheel(event):
                return True
        return super().eventFilter(watched, event)

    def scroll_results_wheel(self, event):
        bar = self.scroll.verticalScrollBar()
        if event.modifiers() or not self.scroll.isEnabled() or not bar.isEnabled():
            return False
        pixels = event.pixelDelta()
        delta = event.angleDelta() if pixels.isNull() else pixels
        if not delta.y() or abs(delta.x()) > abs(delta.y()):
            return False
        down = delta.y() < 0
        if not pixels.isNull():
            # Pixel deltas already include the touchpad's speed and scroll direction.
            distance = -pixels.y()
            self._wheel_remainder = 0
        else:
            lines = QApplication.wheelScrollLines()
            if lines <= 0:
                return False
            if (
                event.phase() == Qt.ScrollPhase.NoScrollPhase
                and event.device().type() != QInputDevice.DeviceType.TouchPad
            ):
                lines += 3
            per_notch = min(lines * bar.singleStep(), max(1, bar.pageStep() // 2))
            # Qt uses 120 angle units per notch. Keep subpixel movement from fine wheels.
            amount = -delta.y() * per_notch
            if (amount > 0) == (self._wheel_remainder > 0):
                amount += self._wheel_remainder
            distance = int(amount / 120)
            self._wheel_remainder = amount - distance * 120
        self.scroll_armed = down
        if down and bar.value() == bar.maximum():
            # No valueChanged signal at the bottom. Allow the next batch to load.
            self.scroll_intent()
        else:
            bar.setValue(bar.value() + distance)
        event.accept()
        return True

    def scrolled(self, value):
        if not self.clips.pinned:
            self.clips.clear()
        self.schedule_posters()
        self.board.cancel_press()
        if self.motion_target >= 0:
            self.hover(-1)
        self.update_animation_visibility()
        self.board.hot = -1
        if self.google.active:
            if not self.restoring_scroll:
                self.google.on_scroll(value)
            return
        bar = self.scroll.verticalScrollBar()
        if (
            not self.scroll_armed
            or self.busy
            or self.failed
            or not self.isVisible()
            or self.tab == "Favorites"
        ):
            return
        if (
            value >= bar.maximum() - self.scroll.viewport().height()
            and self.query
            and self.has_more()
        ):
            self.scroll_armed = False
            self.load_more()

    def schedule_posters(self):
        # Continuous wheel input should still allow each paint cycle to start new previews.
        if not self.paint_timer.isActive():
            self.paint_timer.start()

    def save_preference(self, key, value):
        self.settings.values[key] = value
        try:
            self.settings.save()
        except OSError:
            self.status.action("This preference applies for this session. Windows could not save it.")

    def auto_animate_changed(self, checked):
        self.save_preference("auto_animate", checked)
        self.update_animation_visibility()
        self.related_pane.animations.viewport_changed()
        self.search_previews.schedule()

    def remember_preview_width(self, *_):
        sizes = self.results_splitter.sizes()
        if not self.preview.isHidden() and sum(sizes):
            self.preview_fraction = sizes[self.results_splitter.indexOf(self.preview)] / sum(sizes)

    def preview_side_changed(self, _index):
        self.save_preference("preview_side", self.preview_side.currentData())
        if self.preview_side.currentData() == "off":
            self.close_related_preview()
        self.sync_preview_panel()

    def is_clip_view(self):
        return self.tab == "Clips" or (self.tab == "Favorites" and self.favorite_kind.currentData() == "clip")

    def sync_preview_panel(self):
        # Showing the panel on entry to a result set keeps the tile under the pointer stable.
        side = self.preview_side.currentData()
        self.clip_autoplay.setVisible(self.is_clip_view())
        self.clip_autoplay.setEnabled(side != "off")
        if self.related_pane.active and side == "off":
            side = "left"
        if not self.related_pane.active and (side == "off" or self.board.home or not self.board.items):
            self.preview.hide()
            return
        new_index = 0 if side == "left" else 1
        splitter = self.results_splitter
        moved = splitter.indexOf(self.preview) != new_index
        requires_sizes = moved or self.preview.isHidden()
        if moved:
            splitter.insertWidget(new_index, self.preview)
        self.preview.show()
        if requires_sizes:
            preview_size = round(1000 * self.preview_fraction)
            sizes = [preview_size, 1000 - preview_size]
            splitter.setSizes(sizes if new_index == 0 else sizes[::-1])

    def refresh_preview(self):
        if self.related_pane.active:
            return
        i = self.motion_target
        if hasattr(self, "clips") and self.clips.item is not None:
            i = self.clips.index
        if self.board.home or not 0 <= i < len(self.board.items):
            self.preview.set_preview()
            return
        pix = self.board.image_for(i)
        note = ""
        if i in self.board.motion_errors:
            note = "Animation unavailable"
        elif i in self.board.fail and pix is None:
            note = "Preview unavailable"
        self.preview.set_preview(self.board.items[i], pix, note)

    def update_animation_visibility(self):
        self.animations.viewport_changed()

    def save_browser_covers(self, frames):
        if self.jobs.closed:
            return
        # A fast sequence of searches can paint immediately and skip optional disk writes
        # whenever the queue already holds 48 thumbnails.
        capacity = 48 - len(self.saving_covers)
        batch = dict(islice(((k, v) for k, v in frames.items() if k not in self.saving_covers), capacity))
        if batch:
            self.saving_covers.update(batch)
            self.jobs.run("cover-write", lambda: self.svc.save_browser_covers(batch),
                          lambda *_: self.saving_covers.difference_update(batch))

    def extend_search_results(self, board, items):
        board.extend_search(items)
        self.search_previews.schedule()

    def restore_search_covers(self, board, items):
        # Local stills can paint independently of thumbnail and animation transfers.
        current = board.items
        wanted = [it for it in items if it.kind == "gif" and not it.link_only and it.key not in board.google_thumbs]

        def paint(covers, error):
            if current is not board.items:
                return
            board.cover_batches -= 1
            self.search_previews.schedule()
            if error or not isinstance(covers, dict) or not covers:
                return
            for key, png in covers.items():
                if key not in board.google_thumbs:
                    pix = QPixmap()
                    if pix.loadFromData(png, "PNG"):
                        board.remember_thumbnail(key, pix)
            board.update()
            if board is self.board:
                self.refresh_preview()

        for offset in range(0, len(wanted), 6):
            group = wanted[offset:offset + 6]
            future = self.jobs.run("cover-read", lambda group=group: self.svc.cached_covers(group), paint)
            if future is not None:
                board.cover_batches += 1
                board.cover_reads.add(future)

    def show_first_frame(self, key, png):
        if self.jobs.closed or not self.isVisible():
            return
        indices = [i for i, visible_key in self.animations.visible.items() if visible_key == key]
        if not indices:
            return
        pix = QPixmap()
        if not pix.loadFromData(png, "PNG"):
            return
        for i in indices:
            self.board.pix[i] = pix
            self.board.fail.pop(i, None)
            self.board.update(self.board.positions[i])
        if self.motion_target in indices:
            self.refresh_preview()

    def load_posters(self):
        self.update_animation_visibility()
        self.search_previews.schedule()
        if not self.isVisible():
            return
        viewport = self.scroll.viewport()
        top = max(0, self.board.mapFrom(viewport, QPoint(0, 0)).y())
        height = max(100, viewport.height())
        bottom = top + height
        if self.board.home:
            self.category_art.fill_visible(top, bottom)
        if not self.board.items:
            return
        candidates = sorted(
            (
                i
                for i, r in enumerate(islice(self.board.positions, len(self.board.items)))
                if r.bottom() >= top - height // 2 and r.top() <= bottom + height
            ),
            key=lambda i: (
                i != self.motion_target,
                max(0, top - self.board.positions[i].bottom(), self.board.positions[i].top() - bottom),
                bool(self.board.google_thumbs) and self.board.items[i].key in self.board.google_thumbs,
            ),
        )
        for i in list(self.board.pix):
            r = self.board.positions[i]
            if r.bottom() < top - 2 * height or r.top() > top + 3 * height:
                del self.board.pix[i]
        has_google_poster = getattr(self.svc, "has_google_poster", None)
        for i in candidates:
            if i in self.board.search_gifs:
                continue
            item = self.board.items[i]
            ticket = (self.epoch, i, item.key)
            if i in self.board.pix or ticket in self.google_still_tasks:
                continue
            if item.kind == "clip" and not item.thumb:
                continue
            fast = (item.source == p.GOOGLE and ticket[2] not in self.google_stills_tried
                    and has_google_poster is not None and has_google_poster(item) is True)
            if fast:
                # Keep one thumbnail worker available for a newly hovered result.
                if len(self.google_still_tasks) >= THUMBNAIL_WORKERS - int(i != self.motion_target):
                    continue
                tasks, lane = self.google_still_tasks, "google-still"
                self.google_stills_tried.add(ticket[2])
                fetch_poster = self.svc.google_poster
            else:
                if ticket in self.preview_tasks or len(self.preview_tasks) >= 3:
                    continue
                tasks, lane = self.preview_tasks, "preview"
                if i in self.board.fail:
                    if not self.google.take_poster_retry(item):
                        continue
                    fetch_poster = self.svc.retry_inline_poster
                else:
                    fetch_poster = self.svc.poster
            tasks.add(ticket)

            def complete(data, error, t=ticket, pending=tasks, quick=fast):
                pending.discard(t)
                if (t[0] == self.epoch and t[1] < len(self.board.items)
                        and self.board.items[t[1]].key == t[2]):
                    idx = t[1]
                    pix = QPixmap()
                    if data and not error and pix.loadFromData(data, "PNG"):
                        self.board.fail.pop(idx, None)
                        self.board.pix.setdefault(idx, pix)
                    elif not quick and idx not in self.board.pix:
                        self.board.fail[idx] = error or "Preview unavailable"
                    self.board.update(self.board.positions[idx])
                    if idx == self.motion_target:
                        self.refresh_preview()
                self.schedule_posters()

            self.jobs.run(lane, lambda it=item, fetch=fetch_poster: fetch(it), complete)

    def hover(self, i):
        if hasattr(self, "clips"):
            self.clips.hover(i)
        if self.motion_target == i:
            return
        self.motion_target = i
        if 0 <= i < len(self.board.items):
            self.animations.failures.pop(self.board.items[i].key, None)
        self.animations.selection_changed()
        self.refresh_preview()
        if 0 <= i < len(self.board.items) and i not in self.board.pix:
            self.schedule_posters()

    def choose(self, i):
        if self.board.home and i >= len(self.board.items) and i < len(self.board.positions):
            category = i - len(self.board.items)
            if category == 0:
                self.favorite_kind.setCurrentIndex(0)
                self.tab_changed("Favorites")
            else:
                self.edit.setText(self.board.home_tiles[category][0])
                self.submit()
            return
        if i < 0 or i >= len(self.board.items):
            return
        item = self.board.items[i]
        if item.kind == "reference":
            self.open_visual_matches(item)
        elif item.kind == "clip":
            self.clips.play_here(i, item)
        else:
            self.copy_item(item, paste=True)

    def clipboard_changed(self):
        self.clipboard_revision += 1

    def clear_link_offer(self):
        self.link_offer = None
        self.paste_link.hide()

    def paste_offered_link(self):
        if self.link_offer is not None:
            self.copy_item(self.link_offer, paste=True, as_link=True)

    def record_use(self, item):
        if item.kind == "gif":
            try:
                self.store.record_gif_use(item)
                if self.tab == "GIFs" and self.board.home:
                    self.show_gif_home()
            except sqlite3.Error:
                return " The usage count could not be saved."
        return ""

    def copy_url(self, item, url):
        QApplication.clipboard().setText(url)
        self.status.action("Link copied." + self.record_use(item))

    def copy_item(self, item, *, paste=False, as_link=False):
        from .service import share_link

        if item.kind == "reference":
            return
        self.clear_link_offer()
        self.clip_id += 1
        token = self.clip_id
        self.discord_paste.cancel()
        revision = self.clipboard_revision
        origin = self.discord_paste.foreground() if paste else 0
        previous = self.previous
        if paste:
            # Opening Discord and preparing a large GIF are independent. The worker
            # waits for both and never has permission to paste before the file is ready.
            self.discord_paste.present(previous, origin=origin)

        def show_failure_in_tray(message):
            if (
                paste
                and not self.isActiveWindow()
                and self.tray.isVisible()
                and QSystemTrayIcon.supportsMessages()
            ):
                self.tray.showMessage(
                    "ViberHive", message, QSystemTrayIcon.MessageIcon.Information, 5500
                )

        def pasted(success, message):
            if token == self.clip_id:
                if success:
                    message += self.record_use(item)
                self.status.action(message, 7000 if success else 0)
                if not success:
                    show_failure_in_tray(message)

        def complete(copied, error):
            if token != self.clip_id or not self.isVisible():
                return
            if error:
                self.discord_paste.cancel()
                if paste and share_link(item):
                    self.link_offer = item
                    self.paste_link.show()
                    self.status.action(error + "  You can paste a link instead.", 0)
                    show_failure_in_tray(error + " Open ViberHive to choose Paste source link.")
                else:
                    self.status.action(error, 12000)
                    show_failure_in_tray(error)
                return
            if revision != self.clipboard_revision:
                self.discord_paste.cancel()
                message = "The clipboard changed while preparing the GIF. Select it again to copy it."
                self.status.action(message)
                show_failure_in_tray(message)
                return
            if item.link_only or as_link:
                QApplication.clipboard().setText(copied)
            else:
                mime = QMimeData()
                mime.setUrls([QUrl.fromLocalFile(str(copied))])
                QApplication.clipboard().setMimeData(mime)
                for index, row in enumerate(self.board.items):
                    if row.key == item.key:
                        self.board.fail.pop(index, None)
                        recovered = self.board.motion_errors.pop(index, None)
                        self.board.update(self.board.positions[index])
                        if recovered and self.motion_target == index:
                            self.refresh_preview()
                            self.animations.failures.pop(item.key, None)
                            self.update_animation_visibility()
                        self.schedule_posters()
                        break
            if paste:
                self.status.action("Copied. Waiting for the Discord message field…", 0)
                self.discord_paste.start(previous, copied, pasted, origin=origin)
            else:
                message = "Link copied." if item.link_only or as_link else "GIF file copied."
                self.status.action(message + self.record_use(item))

        if as_link:
            link = share_link(item)
            if link:
                complete(link, "")
        elif item.link_only:
            complete(item.media or item.page, "")
        else:
            self.status.action("Opening Discord and preparing GIF…" if paste else "Preparing GIF…", 0)
            self.jobs.run("action", lambda: self.svc.full(item, lease=86400), complete)

    def toggle_star(self, i):
        if i < 0 or i >= len(self.board.items):
            return
        item = self.board.items[i]
        self.set_favorite(item, remove=item.key in self.board.favorite_keys)

    def set_favorite(self, item, remove, *, words=None):
        if item.kind == "reference":
            return
        key = item.key
        if key in self.favorite_tasks:
            return
        if words is None:
            words = self.query
        self.favorite_tasks.add(key)
        self.status.action("Removing favorite…" if remove else "Saving favorite…")

        def complete(_, error):
            self.favorite_tasks.discard(key)
            self.favorites_changed()
            self.status.action(error or ("Favorite removed." if remove else "Favorite saved."))

        self.jobs.run(
            "action",
            (lambda: self.store.remove(key))
            if remove
            else (lambda: self.svc.favorite(item, words)),
            complete,
        )

    def card_menu(self, i, point):
        from .service import share_link

        item = self.board.items[i]
        menu = QMenu(self)
        if item.kind != "reference":
            menu.addAction(
                "Remove favorite" if item.key in self.board.favorite_keys else "Add to favorites",
                lambda: self.set_favorite(item, remove=item.key in self.board.favorite_keys),
            )
        if self.tab == "Favorites" and item.kind in ("gif", "clip"):
            self.folder_bar.move_menu(menu, item)
        if item.kind == "clip":
            menu.addAction(
                "Play in preview", lambda: QTimer.singleShot(0, lambda: self.clips.play_here(i, item))
            )
        if item.kind != "reference":
            menu.addAction(
                "Paste into Discord",
                lambda: QTimer.singleShot(0, lambda: self.copy_item(item, paste=True)),
            )
            menu.addAction(
                "Copy clip link"
                if item.kind == "clip"
                else "Copy GIF link"
                if item.link_only
                else "Copy GIF file",
                lambda: self.copy_item(item),
            )
            if not item.link_only and share_link(item):
                menu.addAction(
                    "Paste source link into Discord",
                    lambda: QTimer.singleShot(
                        0, lambda: self.copy_item(item, paste=True, as_link=True)
                    ),
                )
            if item.media and not item.link_only:
                menu.addAction(
                    "Copy original GIF URL", lambda: self.copy_url(item, item.media)
                )
        if self.board.can_relate(i):
            menu.addAction("More like this", lambda: self.open_related(item))
            if self.is_google_tab() and self.query and not self.board.home:
                menu.addAction("Visually similar images", lambda: self.open_visual_matches(item))
        page = web_url(item.page)
        media = web_url(item.media)
        if page or media or (self.query and self.tab != "Favorites"):
            menu.addSeparator()
        if page:
            name = (
                f"Watch on {item.source} ↗"
                if item.kind == "clip" and p.clip_link(page)
                else "Open source website ↗"
            )
            menu.addAction(name, lambda: QDesktopServices.openUrl(QUrl(page)))
            menu.addAction("Copy source page link", lambda: self.copy_url(item, page))
        elif media and item.kind != "clip":
            menu.addAction("Open original image ↗", lambda: QDesktopServices.openUrl(QUrl(media)))
        if self.query and self.tab != "Favorites":
            self.add_search_website_action(menu, self.query, clip_source=item.source)
        menu.exec(point)
        menu.deleteLater()
