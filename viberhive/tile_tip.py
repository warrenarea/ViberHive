"""Delayed result descriptions, anchored to the visible bottom of the tile."""

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer
from PyQt6.QtGui import QColor, QCursor, QPainter, QPalette, QTextDocument, QTextOption
from PyQt6.QtWidgets import QApplication, QWidget


class TileTip(QWidget):
    def __init__(self, gallery):
        super().__init__(
            gallery,
            Qt.WindowType.ToolTip
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowTransparentForInput,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.gallery = gallery
        self.target = None
        self.delay = QTimer(self, singleShot=True, interval=5000)
        self.delay.setTimerType(Qt.TimerType.PreciseTimer)
        self.delay.timeout.connect(self.reveal)
        self.expire = QTimer(self, singleShot=True, interval=10000)
        self.expire.timeout.connect(self.hide)
        self.doc = QTextDocument(self)
        self.doc.setDocumentMargin(7)
        self.doc.setDefaultFont(self.font())
        opt = self.doc.defaultTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.doc.setDefaultTextOption(opt)

    def cancel(self):
        self.target = None
        self.delay.stop()
        self.expire.stop()
        self.hide()

    def target_at(self, pos):
        board = self.gallery
        if not board.visibleRegion().contains(pos):
            return None
        i = board.hit(pos)
        if not 0 <= i < len(board.items):
            return None
        action = board.action_at(i, pos)
        return i, {"menu": "More actions", "star": "Toggle favorite", "related": "More like this"}.get(action, "")

    def observe(self, pos):
        target = self.target_at(pos)
        if target != self.target:
            self.cancel()
            self.target = target
            if target is not None:
                self.delay.start()

    def scrolled(self, *_):
        self.cancel()
        if self.gallery.underMouse():
            self.observe(self.gallery.mapFromGlobal(QCursor.pos()))

    def reveal(self):
        board = self.gallery
        pos = board.mapFromGlobal(QCursor.pos())
        if (
            not self.target
            or self.target != self.target_at(pos)
            or QApplication.activePopupWidget()
            or QApplication.activeModalWidget()
            or QApplication.widgetAt(QCursor.pos()) is not board
        ):
            self.cancel()
            return
        i, action = self.target
        detail = board.motion_errors.get(i) or board.fail.get(i)
        text = action or "\n".join(filter(None, (board.items[i].title, detail)))
        if not text:
            return
        self.setAccessibleName(text)
        self.doc.setPlainText(text)
        visible = board.positions[i].intersected(board.visibleRegion().boundingRect())
        tile = QRect(board.mapToGlobal(visible.topLeft()), visible.size())
        screen = QApplication.screenAt(board.mapToGlobal(pos)) or board.screen()
        area = screen.availableGeometry()
        self.doc.setTextWidth(-1)
        self.doc.setTextWidth(min(self.doc.idealWidth() + 1, 370, area.width()))
        self.resize(
            min(370, self.doc.size().toSize().width()),
            min(240, self.doc.size().toSize().height()),
        )
        left = min(max(area.left(), tile.left()), area.right() - self.width() + 1)
        top = min(tile.bottom() + 2, area.bottom() - self.height() + 1)
        # If the tile is flush with the monitor bottom, keep the animation unobstructed
        # by placing the tooltip alongside the caption when there is room.
        if top <= tile.bottom():
            if tile.right() + self.width() + 3 <= area.right():
                left = tile.right() + 3
            elif tile.left() - self.width() - 3 >= area.left():
                left = tile.left() - self.width() - 3
        self.move(QPoint(left, max(area.top(), top)))
        self.show()
        self.expire.start()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#26272e"))
        painter.setPen(QColor("#646675"))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        layout = self.doc.documentLayout()
        context = layout.PaintContext()
        context.palette.setColor(QPalette.ColorRole.Text, QColor("#f2f2f5"))
        layout.draw(painter, context)
