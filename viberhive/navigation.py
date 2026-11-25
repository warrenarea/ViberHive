"""In-session page history for the picker and its child widgets."""

from dataclasses import dataclass

from PyQt6.QtCore import QEvent, QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QWidget

from .model import Item, Page
from .providers import GOOGLE


@dataclass(frozen=True)
class Location:
    query: str
    references: bool = False
    seed: Item | None = None
    native_branch: bool = False
    tab: str = "GIFs"
    provider: str = GOOGLE
    collection: str = "gif"
    folder: int | None = None


@dataclass
class Visit:
    location: Location
    snapshot: Page | None = None
    scroll: int = 0
    selected: int = -1
    api_state: tuple | None = None
    website_more: bool = False
    failed: bool = False
    google_signin: bool = False
    transient: bool = False


class History(QObject):
    navigate = pyqtSignal(int)

    def __init__(self, picker):
        super().__init__(picker)
        self.picker = picker
        self.entries = []
        self.position = -1
        QApplication.instance().installEventFilter(self)

    def can_go(self, delta):
        return 0 <= self.position + delta < len(self.entries)

    def go(self, delta):
        self.position += delta
        return self.entries[self.position]

    def visit(self, location, *, transient=False):
        current = self.entries[self.position] if self.entries else None
        if current and current.location == location:
            if not transient:
                current.transient = False
            return current
        del self.entries[self.position + 1 :]
        if current and location.query:
            old = current.location
            same_view = (old.tab, old.provider, old.collection, old.folder) == (
                location.tab, location.provider, location.collection, location.folder
            )
            # Treat a cleared search followed by new text as one new search.
            # One live Favorites filter also occupies one entry as it is typed.
            if same_view and (current.transient or (old.tab == "Favorites" and old.query)):
                self.entries.pop()
        self.entries.append(Visit(location, transient=transient))
        del self.entries[:-24]
        self.position = len(self.entries) - 1
        return self.entries[-1]

    def eventFilter(self, watched, event):
        kind = event.type()
        mouse = (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseButtonDblClick,
        )
        keys = (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
        if kind not in mouse + keys or not isinstance(watched, QWidget):
            return False
        picker = self.picker
        if (
            watched.window() is not picker
            or not picker.isVisible()
            or QApplication.activeModalWidget()
            or QApplication.activePopupWidget()
        ):
            return False
        if kind in mouse:
            direction = {Qt.MouseButton.BackButton: -1, Qt.MouseButton.ForwardButton: 1}.get(
                event.button()
            )
            trigger = kind == QEvent.Type.MouseButtonPress
        else:
            direction = {Qt.Key.Key_Back: -1, Qt.Key.Key_Forward: 1}.get(event.key())
            if event.modifiers() == Qt.KeyboardModifier.AltModifier:
                direction = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1}.get(event.key(), direction)
            trigger = kind == QEvent.Type.KeyPress and not event.isAutoRepeat()
        if direction is None:
            return False
        event.accept()
        if trigger and picker.can_navigate(direction):
            self.navigate.emit(direction)
        return True
