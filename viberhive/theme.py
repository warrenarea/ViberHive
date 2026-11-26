from math import cos, pi, sin
from pathlib import Path

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

ASSETS = Path(__file__).resolve().parents[1] / "assets"

STYLE = """
QWidget { color:#ececf2; font-family:"Segoe UI", "DejaVu Sans", sans-serif; font-size:13px; }
QWidget#window, QDialog { background:#313238; }
QFrame#panel { background:#35363d; border:1px solid #51525e; border-radius:12px; }
QWidget#head { background:transparent; }
QLabel#resultCount { color:#c3c5d2; font-weight:600; padding:0 4px; }
QFrame#footer { border:0; border-top:1px solid #474851; }
QPushButton[footerAction="true"] { padding-left:5px; padding-right:5px; }
QLabel#small { font-size:12px; color:#b5b7c4; }
QPushButton, QToolButton, QComboBox {
 background:#42434c; color:#eaebf2; border:1px solid transparent;
 border-radius:6px; padding:6px 11px; font-weight:600;
}
QPushButton:hover, QToolButton:hover, QComboBox:hover { background:#50515c; }
QPushButton:focus, QToolButton:focus, QComboBox:focus { border-color:#a2aaff; }
QPushButton:disabled { color:#a4a4af; background:#3c3d44; }
QPushButton[tab="true"] { background:transparent; padding:7px 11px; }
QPushButton[tab="true"]:checked { background:#51525d; color:#fff; }
QPushButton[tab="true"]:hover { background:#474851; }
QPushButton[quiet="true"] { background:transparent; color:#c7c9d5; }
QPushButton[quiet="true"]:hover { background:#464750; color:white; }
QPushButton#iconButton { padding:0; }
QPushButton#iconButton:disabled { background:transparent; }
QPushButton#navigationButton { padding:0; background:#444651; border:1px solid #575a68; }
QPushButton#navigationButton:hover { background:#55596b; border-color:#8992c3; }
QPushButton#navigationButton:disabled { background:#393a43; border-color:#454650; }
QPushButton#closeButton { padding:0; background:transparent; }
QPushButton#closeButton:hover { background:#b83243; }
QPushButton#closeButton:pressed { background:#922535; }
QPushButton#clearSearch { padding:0 0 4px 0; color:#c5c6d2; font-size:22px; font-weight:600; background:transparent; }
QPushButton#clearSearch:hover { color:#ff9ba7; background:#682a37; border-color:#bb4355; }
QPushButton#clearSearch:pressed { color:white; background:#922535; }
QPushButton#accent { background:#5865f2; color:white; }
QPushButton#accent:hover { background:#6874ff; }
QLineEdit { padding:9px; border:1px solid #50515c; border-radius:7px; background:#2e2f35; color:white; selection-background-color:#5865f2; }
QLineEdit:focus { border-color:#8992ff; }
QFrame#searchBox { background:#2c2d33; border:1px solid #6f77cf; border-radius:8px; }
QLineEdit#search { padding:6px 0; border:0; background:transparent; font-size:16px; }
QComboBox::drop-down { border:0; width:18px; }
QComboBox { padding-right:5px; }
QAbstractItemView, QMenu { background:#27282f; color:#eeeeF4; selection-background-color:#4d526d; border:1px solid #4b4c59; }
QMenu { padding:5px; } QMenu::item { padding:7px 16px; } QMenu::item:selected { background:#4951a7; border-radius:4px; }
QScrollArea, QWidget#board, QScrollArea > QWidget > QWidget { background:#313238; border:0; }
QSplitter#resultsSplitter::handle:horizontal {
 background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0.41 #35363d,stop:0.42 #686a7b,
                            stop:0.58 #686a7b,stop:0.59 #35363d);
}
QSplitter#resultsSplitter::handle:horizontal:hover {
 background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0.40 #454657,stop:0.41 #a6adff,
                            stop:0.59 #a6adff,stop:0.60 #454657);
}
QScrollBar:vertical { background:#303138; width:8px; margin:2px 0; }
QScrollBar::handle:vertical { background:#666873; border-radius:4px; min-height:30px; }
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height:0; }
QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical { background:transparent; }
QToolTip { color:white; background:#25262d; border:1px solid #555767; padding:4px; }
QSpinBox { background:#292a30; border:1px solid #50515c; padding:5px; }
QLabel#clipClock { color:#c8cad5; font-size:11px; }
QPushButton#clipAction { padding:4px 6px; font-size:12px; }
QSlider#clipTimeline { min-height:20px; }
QSlider#clipTimeline::groove:horizontal { height:4px; border-radius:2px; background:#515361; }
QSlider#clipTimeline::sub-page:horizontal { background:#8992ff; border-radius:2px; }
QSlider#clipTimeline::handle:horizontal { width:12px; margin:-4px 0; border-radius:6px; background:#e6e8ff; }
QSlider#clipTimeline::handle:horizontal:hover { background:white; }
"""


def icon():
    return QIcon(str(ASSETS / "viberhive.ico"))


def control_icon(name):
    """Resolution-independent toolbar glyphs with consistent stroke weights."""
    pix = QPixmap(72, 72)
    pix.fill(Qt.GlobalColor.transparent)
    pix.setDevicePixelRatio(3)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor("#d7d8e3")
    pen = QPen(color, 2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    path = QPainterPath()
    if name == "settings":
        for i in range(8):
            for offset, radius in ((-0.5, 7.2), (-0.28, 7.5), (-0.20, 10),
                                   (0.20, 10), (0.28, 7.5)):
                angle = (i + offset) * pi / 4
                point = QPointF(12 + radius * cos(angle), 12 + radius * sin(angle))
                if path.elementCount():
                    path.lineTo(point)
                else:
                    path.moveTo(point)
        path.closeSubpath()
        path.addEllipse(QPointF(12, 12), 3.5, 3.5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
    elif name == "close":
        path.moveTo(6, 6)
        path.lineTo(18, 18)
        path.moveTo(18, 6)
        path.lineTo(6, 18)
    else:
        tip, bend, tail = (4, 11, 20) if name == "back" else (20, 13, 4)
        path.moveTo(bend, 5)
        path.lineTo(tip, 12)
        path.lineTo(bend, 19)
        path.moveTo(tip, 12)
        path.lineTo(tail, 12)
    painter.drawPath(path)
    painter.end()
    return QIcon(pix)
