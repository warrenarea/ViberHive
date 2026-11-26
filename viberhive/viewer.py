"""Hover preview and resize handles for the frameless picker."""

from math import ceil

from PyQt6.QtCore import QRect, QRectF, QSize, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPen,
    QTextCharFormat,
    QTextCursor,
    QTextOption,
)
from PyQt6.QtWidgets import QFrame, QSizePolicy, QTextEdit, QVBoxLayout, QWidget

from .clipplay import ClipPlayerWidget


class PreviewImage(QWidget):
    def paintEvent(self, event):
        preview = self.parentWidget()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        pixmap = preview.pixmap
        if pixmap is not None and not pixmap.isNull():
            size = pixmap.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            dest = QRect(self.rect().topLeft(), size)
            dest.moveCenter(self.rect().center())
            p.drawPixmap(dest, pixmap)
        else:
            p.setPen(QColor("#c2c3ce"))
            p.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self.accessibleDescription(),
            )


class HoverPreview(QWidget):
    def __init__(self):
        super().__init__()
        self.item = self.pixmap = None
        self.related_pane = None
        self.note = ""
        self.setMinimumWidth(145)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAccessibleName("GIF preview")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(5)
        self.image = PreviewImage(self)
        self.image.setAccessibleDescription("Hover over a result to preview it")
        layout.addWidget(self.image, 1)
        self.clip_player = ClipPlayerWidget(self)
        layout.addWidget(self.clip_player)
        self.clip_player.hide()
        self.clip_player.activeChanged.connect(self.playback_changed)
        self.caption = QTextEdit()
        self.caption.setAccessibleName("Preview title and source")
        self.caption.setReadOnly(True)
        self.caption.setFrameShape(QFrame.Shape.NoFrame)
        self.caption.setStyleSheet("QTextEdit { background:transparent; border:0; padding:0; }")
        self.caption.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.caption.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.caption.document().setDocumentMargin(0)
        self.caption.document().documentLayout().documentSizeChanged.connect(self.fit_caption)
        layout.addWidget(self.caption)
        self.caption.hide()

    def install_related(self, pane):
        self.related_pane = pane
        self.layout().addWidget(pane, 1)
        pane.hide()

    def set_related_visible(self, visible):
        self.related_pane.setVisible(visible)
        self.image.setVisible(not visible and not self.clip_player.active)
        self.clip_player.setVisible(not visible and self.item is not None and self.item.kind == "clip")
        self.caption.setVisible(not visible and self.item is not None)
        normal_name = f"Preview of {self.item.title}" if self.item else "GIF preview"
        self.setAccessibleName("Similar GIFs" if visible else normal_name)

    def playback_changed(self, active):
        showing_related = self.related_pane is not None and not self.related_pane.isHidden()
        self.image.setVisible(not active and not showing_related)
        self.layout().setStretch(1, int(active))

    def fit_caption(self, *_):
        # Leave the image room even for unusually long publisher titles. The text
        # remains readable using the caption's own scrollbar in that case.
        needed = ceil(self.caption.document().size().height()) + 2
        height = min(needed, max(42, self.height() * 2 // 5))
        if self.caption.height() != height:
            self.caption.setFixedHeight(height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_caption()

    def set_preview(self, item=None, pixmap=None, note=""):
        if self.related_pane is not None and not self.related_pane.isHidden():
            return
        if item and item.kind == "clip" and pixmap is None:
            note = ""  # A missing poster says nothing about whether the clip can play.
        changed = item is not self.item or note != self.note
        self.item, self.pixmap, self.note = item, pixmap, note
        self.image.update()
        if not changed:
            return
        self.setAccessibleName(f"Preview of {item.title}" if item else "GIF preview")
        if item is None:
            prompt = "Hover over a result to preview it"
        elif item.kind == "clip":
            prompt = "Play clip to preview"
        else:
            prompt = note or "Loading preview…"
        self.image.setAccessibleDescription(prompt)
        self.clip_player.setVisible(bool(item and item.kind == "clip"))
        self.caption.setVisible(item is not None)
        self.caption.clear()
        if item is None:
            return
        cursor = QTextCursor(self.caption.document())
        title = QTextCharFormat()
        title.setFontWeight(QFont.Weight.Bold)
        title.setForeground(QColor("#eeeef3"))
        cursor.insertText(item.title, title)
        cursor.insertBlock()
        detail = QTextCharFormat()
        detail.setForeground(QColor("#b6b8c7"))
        detail.setFontWeight(QFont.Weight.Normal)
        source = (
            "Still image · Click to find related GIFs" if item.kind == "reference" else item.source
        )
        cursor.insertText(" · ".join(filter(None, (source, note))), detail)
        self.fit_caption()
        self.caption.verticalScrollBar().setValue(0)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor("#44454f")))
        p.setBrush(QColor("#28292f"))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)


class ResizeGrip(QWidget):
    """Prefer native resizing. Keep the opposite edge anchored during a fallback drag."""

    def __init__(self, picker, edges, cursor):
        super().__init__(picker)
        self.picker = picker
        self.edges = edges
        self.press = self.original = None
        self.setCursor(cursor)
        self.setToolTip("Drag to resize ViberHive")

    @classmethod
    def install(cls, picker):
        edge, cursor = Qt.Edge, Qt.CursorShape
        return [
            cls(picker, edges, shape)
            for edges, shape in (
                (edge.LeftEdge, cursor.SizeHorCursor),
                (edge.RightEdge, cursor.SizeHorCursor),
                (edge.TopEdge, cursor.SizeVerCursor),
                (edge.BottomEdge, cursor.SizeVerCursor),
                (edge.TopEdge | edge.LeftEdge, cursor.SizeFDiagCursor),
                (edge.BottomEdge | edge.RightEdge, cursor.SizeFDiagCursor),
                (edge.TopEdge | edge.RightEdge, cursor.SizeBDiagCursor),
                (edge.BottomEdge | edge.LeftEdge, cursor.SizeBDiagCursor),
            )
        ]

    def fit(self, size):
        edge = Qt.Edge
        horizontal = self.edges & (edge.LeftEdge | edge.RightEdge)
        vertical = self.edges & (edge.TopEdge | edge.BottomEdge)
        corner, strip = 13, 6
        w = (corner if vertical else strip) if horizontal else size.width() - corner * 2
        h = (corner if horizontal else strip) if vertical else size.height() - corner * 2
        x = (0 if self.edges & edge.LeftEdge else size.width() - w) if horizontal else corner
        y = (0 if self.edges & edge.TopEdge else size.height() - h) if vertical else corner
        self.setGeometry(x, y, w, h)
        self.raise_()

    def paintEvent(self, event):
        if self.edges == Qt.Edge.BottomEdge | Qt.Edge.RightEdge:
            p = QPainter(self)
            p.setPen(QPen(QColor("#898b99")))
            p.drawLine(2, 10, 10, 2)
            p.drawLine(6, 10, 10, 6)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.picker.drag_active = True
        self.press = self.original = None
        window = self.picker.windowHandle()
        if window is None or not window.startSystemResize(self.edges):
            self.press = event.globalPosition().toPoint()
            self.original = self.picker.geometry()
        event.accept()

    def mouseMoveEvent(self, event):
        if self.press is None or not event.buttons() & Qt.MouseButton.LeftButton:
            return super().mouseMoveEvent(event)
        rect = QRect(self.original)
        delta = event.globalPosition().toPoint() - self.press
        minimum = (
            self.picker.minimumSizeHint()
            .expandedTo(self.picker.minimumSize())
            .expandedTo(QSize(180, 180))
        )
        edge = Qt.Edge
        if self.edges & edge.LeftEdge:
            rect.setLeft(min(rect.right() - minimum.width() + 1, rect.left() + delta.x()))
        if self.edges & edge.RightEdge:
            rect.setWidth(max(minimum.width(), rect.width() + delta.x()))
        if self.edges & edge.TopEdge:
            rect.setTop(min(rect.bottom() - minimum.height() + 1, rect.top() + delta.y()))
        if self.edges & edge.BottomEdge:
            rect.setHeight(max(minimum.height(), rect.height() + delta.y()))
        self.picker.setGeometry(rect)
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.press = self.original = None
            self.picker.remember_move()
            self.picker.drag_active = False
            event.accept()
        else:
            super().mouseReleaseEvent(event)
