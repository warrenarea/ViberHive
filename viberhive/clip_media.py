"""Resolve a publisher's advertised video and admit bounded, self-contained MP4 clips."""

from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from . import clipcafe
from .net import HTTPProblem, Problem, fetch, host_of, web_url
from .providers import YARN_HOSTS, yarn_link

MAX_BYTES = 24 * 1024**2
YARN_MEDIA = frozenset({*YARN_HOSTS, "y.yarn.co", "y.getyarn.io"})


class VideoSources(HTMLParser):
    def __init__(self, page, hosts):
        super().__init__()
        self.page = page
        self.hosts = hosts
        self.urls = []
        self.in_video = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        url = ""
        if tag == "video":
            self.in_video = True
            url = attrs.get("src")
        elif tag == "source" and self.in_video:
            url = attrs.get("src")
        elif tag == "meta" and (
            attrs.get("property") or attrs.get("itemprop") or attrs.get("name")
        ) in (
            "og:video",
            "og:video:secure_url",
            "og:video:url",
            "contentUrl",
            "twitter:player:stream",
        ):
            url = attrs.get("content")
        if not url:
            return
        try:
            url = urljoin(self.page, url)
        except ValueError:
            return
        if (
            len(self.urls) < 8
            and web_url(url)
            and host_of(url) in self.hosts
            and urlsplit(url).path.lower().endswith(".mp4")
            and url not in self.urls
        ):
            self.urls.append(url)

    def handle_endtag(self, tag):
        if tag == "video":
            self.in_video = False


def _boxes(data):
    """Walk just the expected ISO BMFF hierarchy, without copying media data."""
    start = count = 0
    while start < len(data):
        if len(data) - start < 8 or count >= 512:
            raise ValueError
        end = start + int.from_bytes(data[start : start + 4], "big")
        if not start + 8 <= end <= len(data):
            raise ValueError
        yield bytes(data[start + 4 : start + 8]), data[start + 8 : end]
        start = end
        count += 1


def _one(data, name):
    matches = [body for typ, body in _boxes(data) if typ == name]
    if len(matches) != 1:
        raise ValueError
    return matches[0]


def _track_kind(track):
    media = _one(track, b"mdia")
    handler = bytes(_one(media, b"hdlr")[8:12])
    info = _one(media, b"minf")
    references = _one(_one(info, b"dinf"), b"dref")
    # Each track must read from this exact buffer. A URL with any payload, or
    # another reference type (even if unused), makes the clip ineligible.
    if bytes(references) != bytes.fromhex(
        "00000000 00000001 0000000c 75726c20 00000001"
    ):
        raise ValueError
    samples = _one(_one(info, b"stbl"), b"stsd")
    if bytes(samples[:8]) != bytes.fromhex("00000000 00000001"):
        raise ValueError
    [(codec, sample)] = _boxes(samples[8:])
    if bytes(sample[6:8]) != b"\x00\x01":
        raise ValueError
    if handler == b"vide" and codec in (b"avc1", b"avc3"):
        width = int.from_bytes(sample[24:26], "big")
        height = int.from_bytes(sample[26:28], "big")
        if (
            0 < min(width, height)
            and max(width, height) <= 1920
            and width * height <= 1920 * 1080
        ):
            return handler
    if (
        handler == b"soun"
        and codec == b"mp4a"
        and 0 < int.from_bytes(sample[16:18], "big") <= 8
    ):
        return handler
    raise ValueError


def validate_mp4(raw):
    try:
        if not 32 <= len(raw) <= MAX_BYTES:
            raise ValueError
        data = memoryview(raw)
        top = list(_boxes(data))
        if top[0][0] != b"ftyp" or len(top[0][1]) < 8:
            raise ValueError
        if any(
            typ not in {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide"}
            for typ, _ in top
        ):
            raise ValueError
        if not _one(data, b"mdat"):
            raise ValueError
        movie = _one(data, b"moov")
        header = _one(movie, b"mvhd")
        if len(header) < 36 or header[0] not in (0, 1):
            raise ValueError
        offset = 20 if header[0] else 12
        scale = int.from_bytes(header[offset : offset + 4], "big")
        duration = int.from_bytes(
            header[offset + 4 : offset + (12 if header[0] else 8)], "big"
        )
        if not scale or not 0 < duration <= 180 * scale:
            raise ValueError
        kinds = [_track_kind(body) for name, body in _boxes(movie) if name == b"trak"]
        if kinds.count(b"vide") != 1 or kinds.count(b"soun") > 1:
            raise ValueError
    except (IndexError, ValueError):
        raise Problem(
            "This clip does not meet the in-app MP4 playback limits. Use Watch on website."
        ) from None


def get_video(page, cancelled=lambda: False):
    if clipcafe.clip_url(page):
        sites, hosts, name = {clipcafe.HOST}, {clipcafe.HOST}, clipcafe.NAME
    elif yarn_link(page):
        sites, hosts, name = YARN_HOSTS, YARN_MEDIA, "Yarn"
    else:
        raise Problem("This clip has no supported publisher page.")
    try:
        raw, _ = fetch(page, 2 * 1024**2, hosts=sites, accept="text/html", deadline=12)
        if cancelled():
            return None
        doc = VideoSources(page, hosts)
        doc.feed(raw.decode("utf-8", "replace"))
        doc.close()
        if not doc.urls:
            raise Problem(
                f"{name} did not publish a playable MP4 here. Use Watch on website."
            )
        raw, mime = fetch(
            doc.urls[0], MAX_BYTES, hosts=hosts, accept="video/mp4", deadline=25
        )
    except HTTPProblem as exc:
        if exc.code in (401, 403, 429):
            raise Problem(
                f"{name} declined video playback on this connection. Use Watch on website."
            ) from None
        raise
    if cancelled():
        return None
    if mime not in ("video/mp4", "application/mp4", "application/octet-stream"):
        raise Problem(
            f"{name} returned an unsupported video response. Use Watch on website."
        )
    validate_mp4(raw)
    return raw
