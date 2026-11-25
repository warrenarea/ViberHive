from PIL import Image
from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtGui import QCursor, QGuiApplication, QImage, QRegion

from . import windows


def capture(hwnd):
    """Return only the visible lower-right portion of the foreground window and its mapping."""
    info = windows.monitor_window(hwnd)
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    matched = False
    if info:
        name = info[0].lower().split(chr(92))[-1]
        for candidate in QGuiApplication.screens():
            if candidate.name().lower().split(chr(92))[-1] == name:
                screen, matched = candidate, True
                break
        if not matched:
            for candidate in QGuiApplication.screens():
                if (candidate.geometry().x(), candidate.geometry().y()) == info[1]:
                    screen, matched = candidate, True
                    break
    geo = screen.geometry()
    frame = screen.grabWindow(0)
    if frame.isNull():
        return screen.availableGeometry(), None, None
    sx, sy = frame.width() / geo.width(), frame.height() / geo.height()
    w, h = frame.width(), frame.height()
    x0, y0, x1, y1 = 0, 0, w, h
    if matched:
        ox, oy = info[1]
        left, top, right, bottom = info[2]
        x0, y0 = max(0, left - ox), max(0, top - oy)
        x1, y1 = min(w, right - ox), min(h, bottom - oy)
        if x1 <= x0 or y1 <= y0:
            x0, y0, x1, y1 = 0, 0, w, h
    x0 += int((x1 - x0) * 0.42)
    y0 += int((y1 - y0) * 0.52)
    # Take the crop in physical pixels, then convert that point back into this Qt screen's coordinates.
    img = (
        frame.toImage().copy(x0, y0, x1 - x0, y1 - y0).convertToFormat(QImage.Format.Format_RGB888)
    )
    if img.isNull():
        return screen.availableGeometry(), None, None
    buf = img.constBits().asstring(img.sizeInBytes())
    pixels = Image.frombytes(
        "RGB", (img.width(), img.height()), buf, "raw", "RGB", img.bytesPerLine()
    )
    return screen.availableGeometry(), pixels, (geo.x(), geo.y(), x0, y0, sx, sy)


def logical(hit, mapping):
    gx, gy, left, top, sx, sy = mapping
    return QPoint(round(gx + (left + hit[0]) / sx), round(gy + (top + hit[1]) / sy))


def popup_rect(area, point=None):
    margin = 10
    w = max(180, min(1040, area.width() - margin * 2))
    h = max(180, min(650, round(area.height() * 0.68), area.height() - margin * 2))
    if point:
        x = point.x() - w + 7
        y = point.y() - h - 32
    else:
        x = area.right() - w - 18
        y = area.bottom() - h - 72
    x = max(area.left() + margin, min(x, area.right() - w - margin + 1))
    y = max(area.top() + margin, min(y, area.bottom() - h - margin + 1))
    return QRect(x, y, w, h)


def manual_rect(record):
    """Restore a user move in Qt logical coordinates, including negative-origin monitors."""
    if not isinstance(record, dict) or not all(
        type(record.get(k)) is int for k in ("x", "y", "w", "h")
    ):
        return None
    if any(abs(record[key]) > 1_000_000 for key in ("x", "y", "w", "h")):
        return None
    screens = QGuiApplication.screens()
    for screen in screens:
        if screen.name() == record.get("screen"):
            area = screen.availableGeometry()
            # A picker deliberately stretched across adjacent monitors should reopen
            # across them while the full saved rectangle still fits the desktop.
            candidate = QRect(
                area.x() + record["x"], area.y() + record["y"], record["w"], record["h"]
            )
            work_area = QRegion()
            for display in screens:
                work_area |= QRegion(display.availableGeometry())
            if (
                candidate.width() >= 180
                and candidate.height() >= 180
                and QRegion(candidate).subtracted(work_area).isEmpty()
            ):
                return candidate
            w = min(area.width(), max(180, record["w"]))
            h = min(area.height(), max(180, record["h"]))
            x = area.left() + max(0, min(record["x"], area.width() - w))
            y = area.top() + max(0, min(record["y"], area.height() - h))
            return QRect(x, y, w, h)
    return None
