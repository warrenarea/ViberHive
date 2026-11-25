import base64
import json
from dataclasses import asdict, dataclass, field
from hashlib import sha256

from .net import host_of


def provider_page_key(provider, mode, query, page=0, seed=""):
    raw = json.dumps((provider, mode, query, page, seed))
    return sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class Item:
    title: str
    media: str
    thumb: str
    page: str
    source: str
    seed: str = ""
    kind: str = "gif"
    width: int = 0
    height: int = 0

    @property
    def key(self):
        return sha256((self.media or self.page).encode()).hexdigest()

    @property
    def panel_identity(self):
        # Two Google cards can show one media URL on different publisher pages.
        return sha256(f"{self.key}\0{self.seed}\0{self.page}".encode()).hexdigest()

    @property
    def link_only(self):
        return self.kind == "clip" or self.service_media

    @property
    def service_media(self):
        for url in (self.media, self.thumb, self.page):
            host = host_of(url)
            if host == "klipy.com" or host.endswith(".klipy.com"):
                return True
        return self.source == "KLIPY"

    @property
    def full_alias(self):
        return "gif:" + self.media

    @property
    def poster_alias(self):
        return "poster:" + (self.thumb or self.media or self.page)


@dataclass
class Page:
    items: list[Item] = field(default_factory=list)
    more: int | str | None = None
    note: str = ""

    def pack(self):
        value = {"items": [asdict(x) for x in self.items], "more": self.more}
        if self.note:
            value["note"] = self.note
        return value

    @classmethod
    def unpack(cls, value):
        return cls([Item(**i) for i in value["items"]], value["more"], value.get("note", ""))


@dataclass
class GoogleBatch:
    query: str
    kind: str
    status: str
    items: list[Item]
    message: str = ""
    inline_thumbnails: dict[str, bytes] = field(default_factory=dict)
    can_expand: bool = False
    parent_key: str = ""
    parent_identity: str = ""
    rendered_thumbnails: dict[str, tuple[int, int, bytes]] = field(default_factory=dict)


GOOGLE_THUMB_EDGE = 160
THUMB_BATCH_BYTES = 4 * 1024**2
THUMB_BATCH_COUNT = 128


def unpack_thumbnail_pixels(value, budget):
    """A tightly packed RGBA image. No image-format parser is needed in the picker."""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    w, h, encoded = value
    if not all(type(n) is int and 8 <= n <= GOOGLE_THUMB_EDGE for n in (w, h)):
        return None
    size = 4 * w * h
    if size > budget or not isinstance(encoded, str) or len(encoded) != ((size + 2) // 3) * 4:
        return None
    try:
        pixels = base64.b64decode(encoded, validate=True)
    except ValueError:
        return None
    return (w, h, pixels) if len(pixels) == size else None
