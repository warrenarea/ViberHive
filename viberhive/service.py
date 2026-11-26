import json
import os
import re
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from hashlib import sha256
from pathlib import Path
from weakref import WeakValueDictionary

from . import providers as p
from .google_query import thumbnail_host
from .model import provider_page_key, site_icon_url
from .net import HTTPProblem, Problem, fetch, host_of, web_url

MIB = 1024**2
THUMBNAIL_WORKERS = 5  # Four ordinary stills plus one priority slot for the hovered GIF.
PNG = b"\x89PNG\r\n\x1a\n"
PUBLIC_PERMALINKS = {
    "tenor.com",
    "www.tenor.com",
    "giphy.com",
    "www.giphy.com",
    "commons.wikimedia.org",
}


class PublisherError(HTTPProblem):
    def __init__(self, url, code):
        self.code = code
        host = host_of(url)
        if code in (401, 403, 451):
            reason = "declined the download"
        elif code in (404, 410):
            reason = "no longer serves this file"
        elif code == 429:
            reason = "is temporarily limiting downloads"
        else:
            reason = "could not deliver this file"
        Problem.__init__(self, f"{host} {reason} (HTTP {code}).")


def share_link(item):
    """Return the source's public HTTPS link for an explicit link-sharing action."""
    page = web_url(item.page)
    if page:
        return page
    return item.media if p.gif_candidate(item.media, related=True) else ""


def rebuild(data, mode, *, on_poster=None):
    executable = Path(sys.executable)
    if os.name == "nt":
        executable = executable.with_name("python.exe")
    child_env = {
        k: v
        for k, v in os.environ.items()
        if k.upper()
        in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH")
    }
    command = [str(executable), "-I", "-B", str(Path(__file__).with_name("decoder.py")), mode]
    options = {
        "input": data,
        "timeout": 8 if mode == "poster" else 24,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
        "env": child_env,
    }
    try:
        if mode == "gif_with_poster" and on_poster is not None:
            return _rebuild_with_preview(command, options, on_poster)
        result = subprocess.run(command, capture_output=True, **options)
    except subprocess.TimeoutExpired:
        raise Problem("The media exceeded the decoder time limit.") from None
    if result.returncode or not result.stdout:
        message = result.stderr[:120].decode("ascii", "ignore").strip()
        raise Problem(message or "Unsupported, damaged, or oversized image.")
    if mode == "gif_with_poster":
        return split_animation(result.stdout)
    if (
        len(result.stdout) > 24 * MIB
        or (mode == "gif" and not result.stdout.startswith((b"GIF87a", b"GIF89a")))
        or (mode == "poster" and not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"))
    ):
        raise Problem("The media worker returned an invalid image.")
    return result.stdout


def split_animation(packet):
    return _read_animation(BytesIO(packet))


def _read_animation(stream, on_poster=None):
    invalid = "The media worker returned an invalid animation preview."
    header = stream.read(4)
    if len(header) != 4:
        raise Problem(invalid)
    size = int.from_bytes(header, "big")
    if not 45 <= size <= MIB:
        raise Problem(invalid)
    poster = stream.read(size)
    if (
        len(poster) != size
        or not poster.startswith(PNG + b"\x00\x00\x00\rIHDR")
        or not poster.endswith(b"\x00\x00\x00\x00IEND\xae\x42\x60\x82")
    ):
        raise Problem(invalid)
    width, height = struct.unpack_from(">II", poster, 16)
    if not (0 < width <= 360 and 0 < height <= 250):
        raise Problem(invalid)
    if on_poster is not None:
        on_poster(poster)
    gif = stream.read(24 * MIB + 1)
    if len(gif) > 24 * MIB or not gif.startswith((b"GIF87a", b"GIF89a")):
        raise Problem(invalid)
    return gif, poster


def _rebuild_with_preview(command, options, on_poster):
    # Read stdout alongside subprocess.run so Windows and POSIX keep the same
    # input, timeout and child cleanup behavior even when a pipe fills.
    read_fd, write_fd = os.pipe()
    with (
        os.fdopen(read_fd, "rb") as source,
        os.fdopen(write_fd, "wb") as sink,
        ThreadPoolExecutor(1, thread_name_prefix="gif-frame") as reader,
    ):
        def read():
            with source:
                return _read_animation(source, on_poster)

        output = reader.submit(read)
        with sink:
            result = subprocess.run(command, stdout=sink, stderr=subprocess.PIPE, **options)
        if result.returncode:
            message = result.stderr[:120].decode("ascii", "ignore").strip()
            raise Problem(message or "Unsupported, damaged, or oversized image.")
        return output.result()


class _MediaSlots:
    """Reserve one worker for playback and promote an existing request when the pointer moves."""

    def __init__(self, limit):
        self.limit = limit
        self.active = 0
        self.focus = None
        self.waiting = {}
        self.changed = threading.Condition()

    def prioritize(self, media):
        with self.changed:
            self.focus = media
            self.changed.notify_all()

    def _rank(self, ticket):
        media, requested = self.waiting[ticket]
        if self.focus is not None and media == self.focus:
            return 2
        return int(requested)

    def _can_start(self, ticket):
        if self.active >= self.limit or max(self.waiting, key=self._rank) is not ticket:
            return False
        rank = self._rank(ticket)
        return self.active < self.limit - 1 or rank == 2 or (rank == 1 and self.focus is None)

    @contextmanager
    def take(self, media=None, requested=False):
        ticket = object()
        with self.changed:
            self.waiting[ticket] = (media, requested)
            try:
                self.changed.wait_for(lambda: self._can_start(ticket))
                self.active += 1
            finally:
                del self.waiting[ticket]
                self.changed.notify_all()
        try:
            yield
        finally:
            with self.changed:
                self.active -= 1
                self.changed.notify_all()


class Service:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings
        self.locks = WeakValueDictionary()
        self.locks_guard = threading.Lock()
        self.download_slots = _MediaSlots(4)
        self.decoder_slots = _MediaSlots(2)
        self.first_frame_ready = None
        self.early_decoders = threading.BoundedSemaphore(2)
        self.invalid = {}
        self.refusals = {}
        # A poster can use the first frame before the user asks to rebuild the animation.
        # Keep originals only in bounded process memory until then. Unverified bytes
        # are never handed to Qt or written to the media cache.
        self.originals = OrderedDict()
        self.originals_lock = threading.Lock()
        self.original_bytes = 0
        self.inline_posters = OrderedDict()
        self.inline_bytes = 0
        self.thumbnail_downloads = _MediaSlots(THUMBNAIL_WORKERS)
        self.thumbnail_decoders = _MediaSlots(3)
        self.thumbnail_misses = {}

    def search(self, source, query, page, seed=""):
        key = self.settings.serp
        clips = source in (p.CLIPS, p.YARN, p.CLIP_CAFE)
        mode = "api" if key and source in (p.GOOGLE, p.CLIPS, p.YARN) else "direct"
        if source == p.GOOGLE and not key:
            mode = "public-v2"
        if source == p.GOOGLE and seed and key:
            # Version 1.4 treated every API panel as exhausted, even when Google
            # provided a More results link. Retrieve the continuation once online.
            mode = "api-panel-v2"

        def page_key(provider, search_mode=mode):
            return provider_page_key(provider, search_mode, query, page, seed)

        cache_key = ("clips-v3:" if clips else "") + page_key(source)
        old, age = self.store.read_page(cache_key) if source != p.KLIPY else (None, None)
        if old is not None and age < 86400 and (not clips or old.items):
            if clips:
                return old, f"Cached {old.items[0].source} clips"
            return old, "Cached search"
        try:
            if source == p.GOOGLE:
                result = (
                    p.google_api(query, page, seed, key)
                    if key
                    else p.google_public(query, page, seed)
                )
            elif source == p.COMMONS:
                result = p.commons(query, page)
            elif source == p.KLIPY:
                result = p.klipy(query, page, self.settings.klipy, self.settings.customer)
            elif clips:
                result = p.search_clips(source, query, page, key)
            else:
                raise Problem("Unknown search source.")
        except p.GoogleSignInRequired:
            if old and old.items:
                return p.Page(old.items), p.GOOGLE_SIGNIN
            raise
        except Problem:
            if old is not None and (not clips or old.items):
                return old, "Offline copy of search results"
            if mode == "api-panel-v2" and page == 0:
                legacy, _ = self.store.read_page(page_key(p.GOOGLE, "api"))
                if legacy is not None and legacy.items:
                    return legacy, "Previously cached related images"
            if clips and page == 0:
                # v1.3 stored both catalogues under Yarn. Old continuation numbers
                # belong to that routing scheme, so restore only the known items.
                legacy_key = page_key(p.YARN)
                for prefix in ("clips-v2:", ""):
                    legacy, _ = self.store.read_page(prefix + legacy_key)
                    if legacy and legacy.items and (
                        source == p.CLIPS or all(row.source == source for row in legacy.items)
                    ):
                        name = legacy.items[0].source
                        return p.Page(legacy.items, None), f"Previously cached {name} clips"
            raise
        if source != p.KLIPY and (not clips or result.items):
            self.store.save_page(cache_key, result)
        if clips:
            if result.note:
                return result, result.note
            if result.items:
                return result, f"Showing {result.items[0].source} quote matches"
            name = source if source != p.CLIPS else "The clip catalogues"
            return result, f"{name} found no matching dialogue"
        return result, ""

    def _lock(self, name):
        # Only requests for this exact asset wait on its transfer or rebuild.
        with self.locks_guard:
            lock = self.locks.get(name)
            if lock is None:
                lock = self.locks[name] = threading.RLock()
            return lock

    def prioritize_motion(self, media):
        self.download_slots.prioritize(media)
        self.decoder_slots.prioritize(media)
        self.thumbnail_downloads.prioritize(media)
        self.thumbnail_decoders.prioritize(media)

    _decode = staticmethod(rebuild)

    def _decode_media(self, data, mode, media=None, *, requested=False, on_poster=None):
        with self.decoder_slots.take(media, requested):
            if on_poster is not None:
                return self._decode(data, mode, on_poster=on_poster)
            return self._decode(data, mode)

    def _download_media(self, url, cap, media=None, *, requested=False, hosts=None):
        with self.download_slots.take(media, requested):
            if hosts is None:
                return self._receive(url, cap)
            return self._receive(url, cap, hosts=hosts)

    def _early_gif_frame(self, url, transfer_done):
        """Try bounded prefixes of a slow GIF without blocking its transfer or caching partial data."""
        threshold = 13
        delivered = threading.Event()
        decoding = threading.Event()
        listener = self.first_frame_ready
        key = sha256(url.encode()).hexdigest()

        def decode(data):
            nonlocal threshold
            try:
                # Normal fast responses reach the existing full decoder immediately.
                if transfer_done.wait(0.15):
                    return
                # The network thread only appends. Snapshot the available bytes
                # after the grace period so a split header cannot mask frame zero.
                while not transfer_done.is_set() and len(data) >= threshold:
                    size = min(len(data), 512 * 1024)
                    threshold = max(1024, size * 4)
                    try:
                        png = self._decode(bytes(data[:size]), "poster")
                    except Problem:
                        if size == 512 * 1024:
                            delivered.set()
                            return
                        continue
                    delivered.set()
                    listener(key, png)
                    break
            except (Problem, OSError, RuntimeError):
                # The current prefix may not contain a complete first frame yet.
                pass
            finally:
                self.early_decoders.release()
                decoding.clear()

        def progress(data, mime):
            if delivered.is_set() or mime not in ("image/gif", "application/octet-stream", "binary/octet-stream", ""):
                return True
            if len(data) < 6:
                return False
            if not data.startswith((b"GIF87a", b"GIF89a")):
                return True
            if len(data) < threshold or decoding.is_set():
                return False
            if not self.early_decoders.acquire(blocking=False):
                return False
            decoding.set()
            try:
                threading.Thread(target=decode, args=(data,),
                                 name="gif-loading-frame", daemon=True).start()
            except RuntimeError:
                self.early_decoders.release()
                return True
            return False

        return progress

    def _receive(self, url, cap, hosts=None):
        accept = "image/gif,image/webp,image/png,image/jpeg"
        if cap == 24 * MIB and self.first_frame_ready:
            transfer_done = threading.Event()
            try:
                raw, mime = fetch(url, cap, hosts=hosts, accept=accept,
                                  on_progress=self._early_gif_frame(url, transfer_done))
            finally:
                transfer_done.set()
        else:
            raw, mime = fetch(url, cap, hosts=hosts, accept=accept)
        if mime not in (
            "image/gif",
            "image/webp",
            "image/png",
            "image/jpeg",
            "application/octet-stream",
            "binary/octet-stream",
            "",
        ):
            raise Problem("The server did not return a supported raster image.")
        return raw

    def _remember_failure(self, cache, url, seconds, reason):
        with self.originals_lock:
            if len(cache) >= 256:
                cache.pop(next(iter(cache)))
            cache[url] = (time.monotonic() + seconds, reason)

    def _save_frame(self, alias, poster):
        # A full GIF already written and leased for paste remains usable on disk pressure.
        try:
            self.store.put(alias, poster, "png", lease=30)
        except (OSError, sqlite3.Error):
            pass

    def full(self, item, lease=120):
        return self._full(item, lease, background=False)

    def ready_motion(self, item, lease=120):
        """Worker-only lookup of a verified GIF. A scan never refreshes its retention."""
        if item.kind != "gif" or item.link_only:
            return None
        return self.store.lookup(item.full_alias, lease, touch=bool(lease))

    def prefetch_motion(self, item):
        """Prepare one ordinary animation without extending its disk lease."""
        return self._full(item, lease=0, background=True)

    def _full(self, item, lease, background):
        if item.kind == "reference":
            raise Problem("Use this reference image to find related GIFs.")
        if item.link_only:
            raise Problem("This source shares a link.")
        with self._lock(item.full_alias):
            path = self.store.lookup(item.full_alias, lease, touch=not background)
            frame_alias = "frame0:" + item.media
            listener = self.first_frame_ready
            frame = self.store.lookup(frame_alias, touch=not background)

            def notify(png):
                if listener is not None:
                    try:
                        listener(item.key, png)
                    except RuntimeError:
                        # A finishing worker may outlive the Qt window.
                        pass

            def publish(png):
                notify(png)
                self._save_frame(frame_alias, png)

            if frame and listener is not None:
                try:
                    if frame.stat().st_size <= MIB:
                        notify(frame.read_bytes())
                except OSError:
                    frame = None
            if path:
                if not frame and (not background or listener is not None):
                    # Populate an older installation's frame on its first use, offline.
                    try:
                        poster = self._decode_media(path.read_bytes(), "poster", item.media, requested=True)
                        publish(poster)
                    except (OSError, Problem):
                        pass
                return path
            with self.originals_lock:
                prior = self.invalid.get(item.media)
            if prior and time.monotonic() < prior[0]:
                raise Problem(prior[1])
            url, raw = self._animation_original(item, requested=not background)
            try:
                if frame:
                    out, poster = self._decode_media(raw, "gif", item.media, requested=not background), None
                else:
                    out, poster = self._decode_media(
                        raw, "gif_with_poster", item.media, requested=not background,
                        on_poster=publish if listener is not None else None,
                    )
                    if listener is not None:
                        poster = None  # Already published and cached when the first frame arrived.
            except Problem as exc:
                self._remember_failure(self.invalid, item.media, 600, str(exc))
                raise
            finally:
                with self.originals_lock:
                    self.original_bytes -= len(self.originals.pop(url, b""))
            # A speculative result has no lease. Write its small frame first so
            # pruning on the second write cannot evict the GIF being returned.
            if background and poster is not None:
                self._save_frame(frame_alias, poster)
                poster = None
            path = self.store.put(item.full_alias, out, "gif", lease=lease)
            if poster is not None:
                self._save_frame(frame_alias, poster)
            return path

    def _animation_original(self, item, *, requested):
        try:
            return item.media, self._original(item.media, requested=requested)
        except HTTPProblem as exc:
            # An indexed CDN URL can expire while the public publisher permalink still works.
            if exc.code not in (403, 404, 410) or host_of(item.page) not in PUBLIC_PERMALINKS:
                raise
            alternate = p.gif_from_publisher(item.page)
            if not alternate or alternate == item.media:
                raise
            if exc.code == 403 and host_of(alternate) == host_of(item.media):
                raise
        return alternate, self._original(alternate, media=item.media, requested=requested)

    def _original(self, url, *, media=None, requested=False):
        with self.originals_lock:
            if url in self.originals:
                self.originals.move_to_end(url)
                return self.originals[url]
            prior = self.refusals.get(url)
        if prior and time.monotonic() < prior[0]:
            raise PublisherError(url, prior[1])
        try:
            return self._download_media(url, 24 * MIB, media or url, requested=requested)
        except HTTPProblem as exc:
            # The visible tile, hover and paste can all want the same original. Honor a
            # refusal for a short time before another user action contacts the host.
            if exc.code in (401, 403, 404, 410, 429, 451):
                self._remember_failure(self.refusals, url, 60, exc.code)
            raise PublisherError(url, exc.code) from None

    def _keep_original(self, url, data):
        with self.originals_lock:
            self.original_bytes -= len(self.originals.pop(url, b""))
            self.originals[url] = data
            self.original_bytes += len(data)
            while self.original_bytes > 48 * MIB or len(self.originals) > 48:
                _, old = self.originals.popitem(last=False)
                self.original_bytes -= len(old)

    def motion(self, item, *, background=False):
        if item.kind == "reference":
            raise Problem("Use this reference image to find related GIFs.")
        if item.service_media:
            data = self._download_media(
                item.media, 24 * MIB, item.media, requested=not background, hosts=p.KLIPY_CDN
            )
            return self._decode_media(data, "gif", item.media, requested=not background)
        if background:
            return self.prefetch_motion(item)
        return self.ready_motion(item) or self.full(item)

    def _read_poster(self, item, *, frame_only=False):
        path = (
            self.store.lookup("frame0:" + item.media, 30)
            if item.media and not item.link_only
            else None
        )
        if path or frame_only:
            return path.read_bytes() if path else None
        path = self.store.lookup(item.poster_alias, 30)
        inline_alias = "poster:" + (item.media or item.page)
        if not path and item.kind != "gif" and inline_alias != item.poster_alias and not item.link_only:
            path = self.store.lookup(inline_alias, 30)
        if path:
            png = path.read_bytes()
            if self._usable_thumbnail(item, png):
                return png
        return None

    @staticmethod
    def _usable_thumbnail(item, png):
        return len(png) >= 24 and item.accepts_thumbnail(*struct.unpack_from(">II", png, 16))

    def save_browser_covers(self, frames):
        # Gallery already validated these small, Chromium-decoded RGBA buffers.
        from PIL import Image

        for key, (width, height, rgba) in frames.items():
            alias = "google-preview:" + key
            if self.store.lookup(alias, touch=False):
                continue
            with Image.frombytes("RGBA", (width, height), rgba) as image, BytesIO() as out:
                image.save(out, "PNG", compress_level=1)
                self.store.put(alias, out.getvalue(), "png")

    def cached_covers(self, items):
        covers = {}
        for item in items:
            try:
                png = self._read_poster(item)
                if png is None:
                    path = self.store.lookup("google-preview:" + item.key, 30)
                    if path:
                        png = path.read_bytes()
                        if not self._usable_thumbnail(item, png):
                            continue
                if png:
                    covers[item.key] = png
            except OSError:
                continue
        return covers

    def offer_posters(self, mapping):
        """Accept small in-document preview candidates. Qt never receives the supplied bytes."""
        accepted = set()
        with self.originals_lock:
            for key, data in mapping.items():
                if (
                    not isinstance(key, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", key)
                    or not isinstance(data, bytes)
                    or not 16 <= len(data) <= 256 * 1024
                    or not (
                        data.startswith(
                            (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")
                        )
                        or (data.startswith(b"RIFF") and data[8:12] == b"WEBP")
                    )
                ):
                    continue
                self.inline_bytes -= len(self.inline_posters.pop(key, b""))
                self.inline_posters[key] = data
                self.inline_bytes += len(data)
                accepted.add(key)
                while self.inline_bytes > 4 * MIB or len(self.inline_posters) > 128:
                    _, old = self.inline_posters.popitem(last=False)
                    self.inline_bytes -= len(old)
            return accepted.intersection(self.inline_posters)

    def has_inline_poster(self, key):
        with self.originals_lock:
            return key in self.inline_posters

    @staticmethod
    def _google_thumbnail_host(item):
        return thumbnail_host(item.thumb) if item.thumb and item.thumb != item.media and not site_icon_url(item.thumb) else ""

    def has_google_poster(self, item):
        """A Google thumbnail can load without opening the publisher's original."""
        if item.source != p.GOOGLE or item.kind not in ("gif", "reference"):
            return False
        has_source = self.has_inline_poster(item.key) or self._google_thumbnail_host(item)
        return bool(has_source) and not item.link_only

    def _decode_thumbnail(self, data, item):
        # Keep small Google thumbnails independent of large animation rebuilds.
        with self.thumbnail_decoders.take(item.media or item.page):
            out = self._decode(data, "poster")
        if self._usable_thumbnail(item, out) and min(struct.unpack_from(">II", out, 16)) >= 8:
            return out
        return None

    def google_poster(self, item):
        """Return a normalized still from cache, Google inline bytes, or its image CDN."""
        if item.source != p.GOOGLE or item.kind not in ("gif", "reference") or item.link_only:
            return None
        # A publisher may hold its normal poster lock while fetching a separate thumbnail.
        # Use the Google-specific lock so bytes arriving with a later batch can display now.
        with self._lock("google:" + item.poster_alias):
            cached = self._read_poster(item) or self._inline_poster(item)
            if cached is not None:
                return cached
            host = self._google_thumbnail_host(item)
            if not host or self.store.lookup(item.full_alias, touch=False):
                return None
            with self.originals_lock:
                miss = self.thumbnail_misses.get(item.thumb)
            if miss and miss[0] > time.monotonic():
                if miss[1] == 429 and host_of(item.media) == host:
                    raise HTTPProblem(429)
                return None
            try:
                with self.thumbnail_downloads.take(item.media or item.page):
                    raw = self._receive(item.thumb, MIB, hosts={host})
                out = self._decode_thumbnail(raw, item)
            except Problem as error:
                code = getattr(error, "code", 0)
                self._remember_failure(self.thumbnail_misses, item.thumb, 60, code)
                if code == 429 and host_of(item.media) == host:
                    raise
                return None
            if out is None:
                self._remember_failure(self.thumbnail_misses, item.thumb, 60, 0)
                return None
            self.store.put(item.poster_alias, out, "png")
            return out

    def retry_inline_poster(self, item):
        """Try the latest in-document thumbnail without contacting its publisher again."""
        if item.source != p.GOOGLE or item.link_only:
            return None
        with self._lock("google:" + item.poster_alias):
            return self._read_poster(item) or self._inline_poster(item)

    def _inline_poster(self, item):
        if item.source != p.GOOGLE or item.link_only:
            return None
        with self.originals_lock:
            data = self.inline_posters.pop(item.key, b"")
            self.inline_bytes -= len(data)
        if data:
            try:
                out = self._decode_thumbnail(data, item)
            except Problem:
                return None
            if out is not None:
                alias = item.poster_alias if item.kind == "gif" else "poster:" + (item.media or item.page)
                self.store.put(alias, out, "png")
                return out
        return None

    def _klipy_poster(self, item, urls):
        for url in dict.fromkeys(urls):
            if url and host_of(url) in p.KLIPY_CDN and (url == item.media or not site_icon_url(url)):
                try:
                    raw = self._download_media(url, 5 * MIB, hosts=p.KLIPY_CDN)
                    png = self._decode_media(raw, "poster")
                    if url == item.media or self._usable_thumbnail(item, png):
                        return png
                except HTTPProblem as exc:
                    if exc.code == 429 or url == item.media:
                        raise
                except Problem:
                    if url == item.media:
                        raise
        raise Problem("The provider has no supported preview for this item.")

    def poster(self, item):
        if item.service_media:
            return self._klipy_poster(item, (item.thumb, item.media))
        try:
            return self._thumbnail(item) or self._media_frame(item, allow_cached_thumbnail=True)
        except Problem:
            # Google can hydrate the tile while a publisher's preview request is
            # in flight. Use that candidate if the remote request just failed.
            inline = self.retry_inline_poster(item)
            if inline is not None:
                return inline
            raise

    def related_poster(self, item):
        """Authoritative still. The pane can paint a provider thumbnail while this runs."""
        if item.service_media:
            raw = self._download_media(item.media, 24 * MIB, item.media, hosts=p.KLIPY_CDN)
            return self._decode_media(raw, "poster", item.media)
        return self._media_frame(item)

    def quick_cover(self, item):
        """Read a cached frame or fetch only a thumbnail, independent of the original."""
        if item.service_media:
            if item.thumb and item.thumb != item.media:
                return self._klipy_poster(item, (item.thumb,))
            return None
        return self._thumbnail(item, independent=True)

    def _thumbnail(self, item, *, independent=False):
        with self._lock(item.poster_alias):
            google_cdn = item.source == p.GOOGLE and not item.link_only and self._google_thumbnail_host(item)
            if google_cdn or (item.source == p.GOOGLE and self.has_inline_poster(item.key)):
                cached = self.google_poster(item)
            else:
                cached = self._read_poster(item)
            if cached is not None:
                return cached
            # Prefer an existing normalized original, including when offline.
            full = self.store.lookup(item.full_alias, 120) if not item.link_only else None
            if (not full and not google_cdn and item.thumb and not site_icon_url(item.thumb)
                    and (item.link_only or item.thumb != item.media)):
                try:
                    slots = self.thumbnail_downloads if independent else self.download_slots
                    with slots.take(item.media or item.page):
                        raw = self._receive(item.thumb, 5 * MIB)
                    out = self._decode_thumbnail(raw, item)
                except HTTPProblem as exc:
                    if item.link_only or (
                        exc.code == 429 and host_of(item.thumb) == host_of(item.media)
                    ):
                        raise
                except Problem:
                    if item.link_only:
                        raise
                else:
                    if out is not None:
                        self.store.put(item.poster_alias, out, "png")
                        return out

    def _media_frame(self, item, *, allow_cached_thumbnail=False):
        if item.link_only or not item.media:
            raise Problem("No preview supplied.")
        # Serialize with animation downloads. Only the original can populate frame0.
        with self._lock(item.full_alias):
            # A main-grid thumbnail can arrive while waiting on an earlier request.
            cached = self._read_poster(item, frame_only=not allow_cached_thumbnail)
            if cached is not None:
                return cached
            full = self.store.lookup(item.full_alias, 120)
            if full:
                data = full.read_bytes()
            elif item.kind == "gif":
                url, data = self._animation_original(item, requested=False)
            else:
                data = self._original(item.media)
            out = self._decode_media(data, "poster", item.media)
            if not full and item.kind == "gif":
                self._keep_original(url, data)
            self._save_frame("frame0:" + item.media, out)
            return out

    def favorite(self, item, words):
        if item.kind == "reference":
            raise Problem("Use this reference image to find related GIFs.")
        if not item.link_only:
            self.full(item, 120)
        self.store.remember(item, words)
