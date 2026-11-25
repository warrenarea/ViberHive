"""Static, softly edged stills for category cards on the GIF landing page."""

import json
from collections import OrderedDict
from pathlib import Path
from time import monotonic

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPixmap

from .gallery import cover
from .model import Item


def reaction_examples():
    path = Path(__file__).resolve().parents[1] / "assets" / "reaction_previews.json"
    try:
        with path.open(encoding="utf-8") as file:
            return {name.casefold(): Item(source="KLIPY", media="", **row) for name, row in json.load(file).items()}
    except (OSError, ValueError):
        return {}


def soft_square(data):
    source = QPixmap()
    if not data or not source.loadFromData(data, "PNG"):
        return None
    side = 192
    tile = QPixmap(side, side)
    tile.fill(Qt.GlobalColor.transparent)
    painter = QPainter(tile)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    cover(painter, tile.rect(), source)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
    for end in (QPointF(side, 0), QPointF(0, side)):
        fade = QLinearGradient(QPointF(0, 0), end)
        for position, alpha in ((0, 0), (0.035, 22), (0.08, 119), (0.135, 226), (0.18, 255)):
            tone = QColor(0, 0, 0, alpha)
            fade.setColorAt(position, tone)
            fade.setColorAt(1 - position, tone)
        painter.fillRect(tile.rect(), fade)
    painter.end()
    return tile


class CategoryArt:
    def __init__(self, picker):
        self.picker = picker
        self.examples = reaction_examples()
        self.covers = None
        self.revision = 0
        self.finding = False
        self.pending = set()
        self.cache = OrderedDict()

    def refresh(self):
        self.revision += 1
        self.covers = None
        self.picker.board.category_images.clear()

    def fill_visible(self, top, bottom):
        ui = self.picker
        board = ui.board
        if self.covers is None:
            if self.finding:
                return
            self.finding = True
            revision = self.revision
            names = [tile[0] for tile in board.home_tiles]

            def found(result, error):
                self.finding = False
                if revision == self.revision:
                    self.covers = {} if error else result
                if board.home:
                    ui.schedule_posters()

            ui.jobs.run("category", lambda: ui.store.category_covers(names), found)
            return
        for index, (title, *_) in enumerate(board.home_tiles, len(board.items)):
            rect = board.positions[index]
            if rect.bottom() < top or rect.top() > bottom:
                board.category_images.pop(title, None)
                continue
            if title in board.category_images:
                continue
            key = title.casefold()
            for item in (self.covers.get(key), self.examples.get(key)):
                if item is None:
                    continue
                identity = (item.key, item.thumb)
                cached = self.cache.get(identity)
                if cached:
                    image, timestamp = cached
                    if image is not None:
                        self.cache.move_to_end(identity)
                        board.category_images[title] = image
                        board.update(rect)
                        break
                    if monotonic() - timestamp < 60:
                        continue
                if identity not in self.pending and len(self.pending) < 2:
                    self.load(item, identity)
                break

    def load(self, item, identity):
        ui = self.picker
        self.pending.add(identity)

        def loaded(data, error):
            self.pending.discard(identity)
            self.cache[identity] = (soft_square(data) if not error else None, monotonic())
            self.cache.move_to_end(identity)
            if len(self.cache) > 64:
                self.cache.popitem(last=False)
            if ui.board.home:
                ui.schedule_posters()

        ui.jobs.run("category", lambda: ui.svc.poster(item), loaded)
