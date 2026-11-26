"""Private, offscreen Google Images data source for the native GIF picker."""

import base64
import binascii
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from itertools import islice
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PyQt6 import sip
from PyQt6.QtCore import QObject, QPoint, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtWebEngineCore import (
    QWebEngineLoadingInfo,
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineScript,
    QWebEngineSettings,
    QWebEngineUrlRequestInfo,
    QWebEngineUrlRequestInterceptor,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import providers
from .google_query import account_signin_url, search_url, selected_image_url
from .model import THUMB_BATCH_BYTES, THUMB_BATCH_COUNT, GoogleBatch, Item, unpack_thumbnail_pixels
from .net import host_of, web_url

INSPECTOR = Path(__file__).with_suffix(".js").read_text("utf-8")
WORLD = QWebEngineScript.ScriptWorldId.ApplicationWorld.value
SEARCH_HOSTS = {"google.com", "www.google.com", "images.google.com"}
CHECK_HOSTS = {"consent.google.com", "sorry.google.com"}
RESOURCE_HOSTS = (
    SEARCH_HOSTS
    | CHECK_HOSTS
    | {
        "ogs.google.com",
        "apis.google.com",
        "fonts.googleapis.com",
        "recaptcha.google.com",
        "www.recaptcha.net",
        "recaptcha.net",
    }
)
TYPE = QWebEngineUrlRequestInfo.ResourceType
CHECK_MESSAGE = "Google requests consent or verification. Choose Verify with Google to continue."
INITIAL_TIMEOUT = 20.0
BATCH_TIMEOUT = 9.0
EMPTY_WAIT = 4.5
SETTLE_WAIT = 1.0
POLL_MS = 350
INLINE_LIMIT = 64 * 1024
BATCH_INLINE_LIMIT = 1536 * 1024


def top_url(url):
    if url == "about:blank":
        return True
    if not web_url(url):
        return False
    p = urlsplit(url)
    if p.hostname in CHECK_HOSTS:
        return True
    return p.hostname in SEARCH_HOSTS and (
        p.path in {"/", "/search", "/imgres", "/images", "/webhp", "/sorry"}
        or p.path.startswith("/sorry/")
    )


def image_results_url(url):
    """Only Google image galleries qualify as a panel's navigation destination."""
    if not web_url(url):
        return False
    parsed = urlsplit(url)
    if parsed.hostname not in SEARCH_HOSTS:
        return False
    if parsed.path == "/images":
        return True
    params = parse_qs(parsed.query)
    return parsed.path == "/search" and (
        "2" in params.get("udm", ())
        or "isch" in params.get("tbm", ())
        or any(part.startswith("rimg:") for tbs in params.get("tbs", ()) for part in tbs.split(","))
    )


def address(url):
    return url.toString(QUrl.ComponentFormattingOption.FullyEncoded)


def same_gallery(expected, location):
    if not expected:
        return False
    if expected.partition("#")[0] == location.partition("#")[0]:
        return True
    # Google can append tracking parameters on redirect. Only its opaque visual
    # relation token establishes the same gallery across a changed query string.
    if not image_results_url(expected) or not image_results_url(location):
        return False
    before, after = urlsplit(expected), urlsplit(location)
    if before.netloc != after.netloc:
        return False

    def relation(parsed):
        return {
            part
            for value in parse_qs(parsed.query).get("tbs", ())
            for part in value.split(",")
            if part.startswith("rimg:") and len(part) > 5
        }

    return bool(relation(before) & relation(after))


def is_google_thumb(url):
    if not web_url(url):
        return False
    p = urlsplit(url)
    host = p.hostname
    return (
        host == "gstatic.com"
        or host.endswith((".gstatic.com", ".ggpht.com"))
        or host == "ggpht.com"
        or host in SEARCH_HOSTS
        and p.path.startswith(("/images", "/imgres", "/search", "/s2/favicons"))
        or host.endswith(".googleusercontent.com")
        and "/proxy/" in p.path
    )


def allowed_request(url, kind):
    if kind == TYPE.ResourceTypeMainFrame:
        return top_url(url)
    if url == "about:blank":
        return True
    if url.startswith("data:"):
        return kind in (TYPE.ResourceTypeImage, TYPE.ResourceTypeFontResource)
    if not web_url(url):
        return False
    host = urlsplit(url).hostname
    if kind == TYPE.ResourceTypeMedia:
        return host in {
            "www.google.com",
            "www.recaptcha.net",
            "recaptcha.net",
            "www.gstatic.com",
        } and urlsplit(url).path.startswith("/recaptcha/")
    if kind in (
        TYPE.ResourceTypeObject,
        TYPE.ResourceTypePluginResource,
        TYPE.ResourceTypeWebSocket,
        TYPE.ResourceTypePing,
        TYPE.ResourceTypeServiceWorker,
        TYPE.ResourceTypeCspReport,
    ):
        return False
    if host in RESOURCE_HOSTS or host == "gstatic.com" or host.endswith(".gstatic.com"):
        return True
    # Publisher originals can be untrusted. The native downloader handles those with DNS,
    # size, type and decoder checks. Only Google-hosted thumbnails render here.
    return kind in (TYPE.ResourceTypeImage, TYPE.ResourceTypeFavicon) and (
        host.endswith((".googleusercontent.com", ".ggpht.com"))
    )


def item_from_record(record, *, references=False, related=False):
    if not isinstance(record, dict):
        return None
    page = web_url(record.get("page"))
    media = web_url(record.get("media"))
    original = media if media and not is_google_thumb(media) else ""
    if not original or not providers.gif_candidate(original, related=references or related):
        media = providers.gif_from_publisher(page) if page else ""
    kind = "gif"
    if not media:
        if not references or not (original or (page and record.get("id"))):
            return None
        kind, media = "reference", original
    if is_google_thumb(media):
        return None
    token, doc = str(record.get("id") or ""), str(record.get("doc") or "")
    seed = (
        f"{token}.{doc}"
        if providers.TOKEN.fullmatch(token) and providers.TOKEN.fullmatch(doc)
        else ""
    )
    return Item(
        providers.label(record.get("title")) or host_of(media or page),
        media,
        web_url(record.get("thumb")),
        page,
        providers.GOOGLE,
        seed,
        kind=kind,
        width=providers.size(record.get("width")),
        height=providers.size(record.get("height")),
    )


def _poster_dimensions(data):
    # Read the size header only. The service performs the actual decode in an isolated worker.
    if data.startswith((b"GIF87a", b"GIF89a")):
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20]), int.from_bytes(data[20:24])
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8X":
            return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(
                data[27:30], "little"
            )
        if data[12:16] == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(
                data[28:30], "little"
            ) & 0x3FFF
        if data[12:16] == b"VP8L" and data[20:21] == b"\x2f":
            packed = int.from_bytes(data[21:25], "little")
            return (packed & 0x3FFF) + 1, ((packed >> 14) & 0x3FFF) + 1
    if data.startswith(b"\xff\xd8\xff"):
        i = 2
        while i + 9 <= len(data) and data[i] == 0xFF:
            marker = data[i + 1]
            if marker == 0xFF:  # JPEG permits padding between segments.
                i += 1
                continue
            if marker in (
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            ):
                return int.from_bytes(data[i + 7 : i + 9]), int.from_bytes(data[i + 5 : i + 7])
            length = int.from_bytes(data[i + 2 : i + 4])
            if length < 2 or marker in (0xD9, 0xDA):
                break
            i += length + 2
    return 0, 0


def inline_poster(encoded, budget):
    if not isinstance(encoded, str) or not 0 < len(encoded) <= 4 * (
        (min(INLINE_LIMIT, budget) + 2) // 3
    ):
        return b""
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return b""
    if not 24 <= len(data) <= min(INLINE_LIMIT, budget):
        return b""
    width, height = _poster_dimensions(data)
    return data if 8 <= width <= 2048 and 8 <= height <= 2048 else b""


class _Guard(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        url = info.requestUrl()
        # Avoid converting potentially very large inline thumbnails on the request thread.
        if url.scheme() == "data":
            allowed = info.resourceType() in (TYPE.ResourceTypeImage, TYPE.ResourceTypeFontResource)
        else:
            allowed = allowed_request(
                url.toString(QUrl.ComponentFormattingOption.FullyEncoded), info.resourceType()
            )
        if not allowed:
            info.block(True)


class _GooglePage(QWebEnginePage):
    accountRequired = pyqtSignal()

    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        if not is_main_frame:
            return True
        destination = address(url)
        if account_signin_url(destination):
            # An optional account link may also appear on a signed-out search.
            # Refuse the login navigation but keep the current search available.
            if navigation_type != QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
                self.accountRequired.emit()
            return False
        return top_url(destination)

    def chooseFiles(self, mode, old_files, accepted_mime_types):
        return []

    def createWindow(self, window_type):
        return None

    def javaScriptAlert(self, origin, message):
        pass

    def javaScriptConfirm(self, origin, message):
        return False

    def javaScriptPrompt(self, origin, message, default):
        return False, ""

    def javaScriptConsoleMessage(self, level, message, line, source):
        # Remote console strings may include URLs or identifiers.
        pass


@dataclass
class _Operation:
    kind: str
    deadline: float
    hard_deadline: float
    action: str = "snapshot"
    target: Item | None = None
    parent_key: str = ""
    parent_identity: str = ""
    recognized_panel: bool = False
    pending: bool = False
    started_reading: float = 0.0
    last_addition: float = 0.0
    items: dict[str, Item] = field(default_factory=dict)
    announced: dict[str, tuple[str, bool, bool]] = field(default_factory=dict)
    panel_scrolled: bool = False
    next_panel_probe: float = 0.0
    items_before_scroll: int = -1
    inline_thumbnails: dict[str, bytes] = field(default_factory=dict)
    rendered_thumbnails: dict[str, tuple[int, int, bytes]] = field(default_factory=dict)
    waiting_pixels: dict[str, str] = field(default_factory=dict)
    pixel_budget: int = THUMB_BATCH_BYTES
    can_expand: bool = False
    poster_budget: int = BATCH_INLINE_LIMIT
    panel_end: float = 0.0
    button_deadline: float = 0.0
    button_source: str = ""


class GoogleBrowser(QObject):
    """Render Google outside the picker. Only an explicit verification request opens a window."""

    resultsReady = pyqtSignal(object)
    batchFinished = pyqtSignal(object)
    thumbnailsReady = pyqtSignal(str, object)
    returnToPicker = pyqtSignal()
    verificationResumed = pyqtSignal()
    alternativeRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.profile = QWebEngineProfile(self)
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        self.profile.setHttpCacheMaximumSize(24 * 1024**2)
        self.profile.setHttpAcceptLanguage("en-US,en;q=0.8")
        self.profile.setPersistentPermissionsPolicy(
            QWebEngineProfile.PersistentPermissionsPolicy.AskEveryTime
        )
        self.guard = _Guard(self.profile)
        self.profile.setUrlRequestInterceptor(self.guard)
        self.profile.downloadRequested.connect(lambda download: download.cancel())
        settings = self.profile.settings()
        attributes = QWebEngineSettings.WebAttribute
        for flag in (
            attributes.JavascriptCanOpenWindows,
            attributes.JavascriptCanAccessClipboard,
            attributes.JavascriptCanPaste,
            attributes.LocalContentCanAccessRemoteUrls,
            attributes.LocalContentCanAccessFileUrls,
            attributes.FullScreenSupportEnabled,
            attributes.PluginsEnabled,
            attributes.AllowRunningInsecureContent,
            attributes.DnsPrefetchEnabled,
            attributes.NavigateOnDropEnabled,
            attributes.ScreenCaptureEnabled,
            attributes.AllowWindowActivationFromJavaScript,
            attributes.PdfViewerEnabled,
            attributes.HyperlinkAuditingEnabled,
        ):
            settings.setAttribute(flag, False)
        settings.setAttribute(attributes.PlaybackRequiresUserGesture, True)
        settings.setAttribute(attributes.WebRTCPublicInterfacesOnly, True)
        settings.setUnknownUrlSchemePolicy(
            QWebEngineSettings.UnknownUrlSchemePolicy.DisallowUnknownUrlSchemes
        )
        # Qt assigns a 0x0 viewport to an unshown page. This fixed render surface supplies a
        # viewport for layout, lazy images and scrolling without exposing a native window.
        # Keep one page attached to one view for its entire life, including verification.
        self.surface = QDialog()
        self.surface.setWindowTitle("Google Images verification — ViberHive")
        self.surface.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.surface.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.surface.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.surface.resize(1024, 720)
        layout = QVBoxLayout(self.surface)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.view = QWebEngineView(self.surface)
        self.view.setAcceptDrops(False)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        layout.addWidget(self.view)
        self.controls = QWidget(self.surface)
        row = QHBoxLayout(self.controls)
        self.verify_note = QLabel()
        self.verify_note.setWordWrap(True)
        row.addWidget(self.verify_note, 1)
        retry = QPushButton("Retry search")
        retry.setAutoDefault(False)
        retry.clicked.connect(self._retry_verification)
        row.addWidget(retry)
        self.alternative = QPushButton("Continue without signing in")
        self.alternative.setAutoDefault(False)
        self.alternative.setToolTip("Search KLIPY for GIFs or Clip.Cafe for quotes.")
        self.alternative.clicked.connect(self._choose_alternative)
        row.addWidget(self.alternative)
        back = QPushButton("Return to picker")
        back.setAutoDefault(False)
        back.clicked.connect(self.surface.reject)
        row.addWidget(back)
        layout.addWidget(self.controls)
        self.controls.hide()
        self.surface.finished.connect(self._dialog_closed)
        self._page_class = _GooglePage
        self.page = None
        self.verification = None
        self.verify_poll = QTimer(self, interval=650)
        self.verify_poll.timeout.connect(self._check_verification)
        self.verify_epoch = 0
        self.verify_pending = False
        self.account_check = QTimer(self, singleShot=True, interval=1000)
        self.account_check.timeout.connect(self._recheck_account_prompt)
        self.account_document = None
        self.op = None
        self.query = ""
        self.references = False
        self.branch = None
        self.source_url = self.continuation_url = ""
        self.direct_attempted = False
        self.followed = set()
        self.origins = OrderedDict()
        self.seen = set()
        self.seen_posters = set()
        self.seen_pixels = set()
        self.pixel_watch = None
        self.last_completed = -1
        self.ready = self.held = self.disposed = False
        self.signin_required = False
        self.navigation = self.serial = 0
        self.poll = QTimer(self, interval=POLL_MS)
        self.poll.timeout.connect(self._tick)
        QApplication.instance().aboutToQuit.connect(self.dispose)

    def _new_page(self):
        old = self.page
        page = self._page_class(self.profile, self)
        self.page = page
        self.view.setPage(page)
        self.surface.show()
        if old is not None and not sip.isdeleted(old):
            sip.delete(old)
        page.permissionRequested.connect(lambda permission: permission.deny())
        page.fileSystemAccessRequested.connect(lambda request: request.reject())
        page.desktopMediaRequested.connect(lambda request: request.cancel())
        page.registerProtocolHandlerRequested.connect(lambda request: request.reject())
        page.webAuthUxRequested.connect(lambda request: request.cancel())
        page.certificateError.connect(lambda certificate: certificate.rejectCertificate())
        page.loadingChanged.connect(lambda info: self._loading(info, page))
        page.accountRequired.connect(lambda: self._account_wall(page), Qt.ConnectionType.QueuedConnection)
        page.urlChanged.connect(lambda url: self._url_changed(url, page))
        page.renderProcessTerminated.connect(lambda *_: self._renderer_stopped(page))

    def dispose(self):
        if self.disposed:
            return
        self.stop_search()
        self.disposed = True
        # Chromium requires the page to be destroyed before its profile.
        sip.delete(self.surface)
        sip.delete(self.profile)

    def stop_search(self):
        if self.disposed:
            return
        self.pause()
        self.query = ""
        self.ready = self.held = False
        self.seen.clear()
        self.seen_posters.clear()
        self.seen_pixels.clear()
        if self.page is not None:
            old, self.page = self.page, None
            if not sip.isdeleted(old):
                sip.delete(old)
        self.surface.hide()

    def pause(self):
        """Cancel collection. The document, completed results and verification stay available."""
        self.poll.stop()
        self.account_check.stop()
        self.account_document = None
        self.pixel_watch = None
        self.op = None
        self._close_verification()

    def search(self, query, *, references=False, seed=None):
        # Accept a full favorite title, including a site filter added for clip searches.
        query = " ".join(str(query).split())[:512]
        if self.disposed or not query:
            return False
        origin = self.origins.get((references, query, seed.panel_identity), "") if seed else ""
        resume = bool(
            seed
            and self.branch
            and self.query == query
            and self.references == references
            and seed.panel_identity == self.branch.panel_identity
        )
        continuation = self.continuation_url if resume else ""
        direct = self.source_url if resume and self.direct_attempted else ""
        reuse = bool(
            seed
            and self.query == query
            and self.ready
            and not self.held
            and self.references == references
            and self.page
            and self._page_matches(self.page.url())
            and (resume or not origin or same_gallery(origin, address(self.page.url())))
        )
        self.pause()
        self.source_url = address(self.page.url()) if reuse else ""
        self.continuation_url = continuation
        self.followed.clear()
        if continuation:
            self.followed.add(continuation)
        self.direct_attempted = bool(direct)
        self.query = query
        self.signin_required = False
        self.references = references
        self.branch = seed
        self.seen.clear()
        self.seen_posters.clear()
        self.seen_pixels.clear()
        if not reuse:
            self.source_url = direct or origin
            self.ready = self.held = False
            self._new_page()
        self._initial()
        if not reuse:
            self.page.setUrl(QUrl(self._url()))
        return True

    def _url(self):
        return (
            self.continuation_url
            or self.source_url
            or search_url(self.query, references=self.references) + "&hl=en"
        )

    def _initial(self):
        selecting = self.branch is not None and not (self.direct_attempted or self.continuation_url)
        self._begin("initial", "seed" if selecting else "snapshot", self.branch)

    def more_results(self, item=None, *, related=True):
        if self.disposed or self.op or self.held or not self.ready or not self.query:
            return False
        if self.branch is not None:
            self._begin("page", "panel", self.branch)
        else:
            self._begin("related" if related else "page", "seed" if related else "page", item)
        return True

    def _begin(self, kind, action, target=None):
        self.serial += 1
        deadline = time.monotonic() + (INITIAL_TIMEOUT if kind == "initial" else BATCH_TIMEOUT)
        # Following a real panel link can start a second network load near the end
        # of a batch. Leave headroom inside the worker client's 24s/45s watchdog.
        self.op = _Operation(kind, deadline, deadline + 8.0, action, target)
        self.poll.start(100 if self._progressive(self.op) else POLL_MS)

    def _progressive(self, op):
        return op.kind == "initial" and op.action == "snapshot" and not self.branch and not self.references

    def _publish_visible(self, op):
        # Send first-screen metadata and artwork as the document hydrates. The final
        # batch remains authoritative for ordering, pagination and verification.
        rows, inline, rendered = [], {}, {}
        for item in islice(op.items.values(), 32):
            key = item.key
            stamp = (item.thumb, key in op.inline_thumbnails, key in op.rendered_thumbnails)
            previous = op.announced.get(key)
            if previous == stamp:
                continue
            op.announced[key] = stamp
            rows.append(item)
            if stamp[1] and not (previous and previous[1]):
                inline[key] = op.inline_thumbnails[key]
            if stamp[2] and not (previous and previous[2]):
                rendered[key] = op.rendered_thumbnails[key]
        if rows:
            self.resultsReady.emit(GoogleBatch(self.query, op.kind, "partial", rows,
                                               inline_thumbnails=inline, rendered_thumbnails=rendered))
            if rendered:
                self.poll.setInterval(POLL_MS)

    def _finish(self, status, message=""):
        op = self.op
        if op is None:
            return
        if self.continuation_url:
            for item in op.items.values():
                key = self.references, self.query, item.panel_identity
                self.origins[key] = self.continuation_url
                self.origins.move_to_end(key)
            while len(self.origins) > 512:
                self.origins.popitem(last=False)
        self.poll.stop()
        self.op = None
        self.last_completed = self.serial
        self.seen.update(op.items)
        self.seen_posters.update(op.inline_thumbnails)
        rendered = {
            k: frame for k, frame in op.rendered_thumbnails.items()
            if k in self.seen and k not in self.seen_pixels
        }
        self.seen_pixels.update(rendered)
        if status in ("ok", "exhausted") and op.waiting_pixels and self.page:
            missing = {url: k for url, k in op.waiting_pixels.items() if k in self.seen and k not in self.seen_pixels}
            if missing:
                self._watch_thumbnail_loads(missing, self._options(self.page, op, "pixels"))
        self.batchFinished.emit(
            GoogleBatch(
                self.query,
                op.kind,
                status,
                list(op.items.values()),
                message,
                op.inline_thumbnails,
                op.can_expand,
                op.parent_key,
                op.parent_identity,
                rendered,
            )
        )

    def _watch_thumbnail_loads(self, missing, options):
        # A lazy <img> can finish decoding after its result batch has settled. For
        # a short window, read only the images already returned to this picker view.
        page, navigation = self.page, self.navigation
        if self.pixel_watch:
            for url, key in self.pixel_watch.items():
                if len(missing) >= 64:
                    break
                if key not in self.seen_pixels:
                    missing.setdefault(url, key)
        self.pixel_watch = missing
        delays = iter((200, 500, 1000, 1800))
        options.update(posterBudget=0, pixelBudget=1024**2, pixelCount=24)

        def current():
            return (self.pixel_watch is missing and self.page is page and self.navigation == navigation
                    and not self.disposed and self.ready and not self.held and not page.isLoading())

        def schedule():
            delay = next(delays, None)
            if missing and delay is not None:
                QTimer.singleShot(delay, collect)
            elif self.pixel_watch is missing:
                self.pixel_watch = None

        def received(value):
            if not current() or not isinstance(value, dict) or value.get("state") != "pixels":
                return
            found = {}
            remaining = options["pixelBudget"]
            for row in value.get("items", [])[:options["pixelCount"]]:
                if not isinstance(row, dict) or row.get("key") not in missing:
                    continue
                frame = unpack_thumbnail_pixels(row.get("pixels"), remaining)
                if frame:
                    key = missing.pop(row["key"])
                    found[key] = frame
                    remaining -= len(frame[2])
            if found:
                self.seen_pixels.update(found)
                self.thumbnailsReady.emit(self.query, found)
            if current():
                schedule()

        def collect():
            if current() and self._page_matches(page.url()):
                options["wanted"] = list(missing)
                page.runJavaScript(INSPECTOR + "(" + json.dumps(options) + ")", WORLD, received)

        schedule()

    def _selected_document(self, url):
        if not self.branch or url.host().lower() not in SEARCH_HOSTS:
            return False
        parsed = urlsplit(address(url))
        token = self.branch.seed.partition(".")[0]
        if parsed.path == "/imgres":
            params = parse_qs(parsed.query)
            return bool(
                (token and token in params.get("tbnid", ()))
                or (self.branch.media and self.branch.media in params.get("imgurl", ()))
            )
        fragment = parse_qs(parsed.fragment)
        return bool(
            token
            and parsed.path == "/search"
            and (token in fragment.get("vhid", ()) or token in fragment.get("imgrc", ()))
        )

    def _page_matches(self, url):
        if url.host().lower() not in SEARCH_HOSTS:
            return False
        location = address(url)
        if self.continuation_url:
            return same_gallery(self.continuation_url, location)
        if self._selected_document(url) or same_gallery(self.source_url, location):
            return True
        if self.direct_attempted or (self.branch and self.source_url):
            return False
        current = parse_qs(urlsplit(location).query).get("q", [""])[0]
        return self.query.casefold() == " ".join(current.split()).casefold()

    def _claim_button_navigation(self, url):
        # Some controls have no href. Arm exactly the click on the panel-scoped
        # control and accept a distinct Google image-results destination for it.
        op = self.op
        location = address(url)
        if not (
            self.branch
            and op
            and time.monotonic() <= op.button_deadline
            and op.button_source.partition("#")[0] != location.partition("#")[0]
            and urlsplit(op.button_source).netloc == urlsplit(location).netloc
            and image_results_url(location)
        ):
            return
        op.button_deadline = 0.0
        op.deadline = min(op.hard_deadline, max(op.deadline, time.monotonic() + BATCH_TIMEOUT))
        self.continuation_url = location
        self.followed.add(location)
        op.action = "snapshot"
        op.recognized_panel = False
        op.panel_end = 0.0
        op.started_reading = op.last_addition = time.monotonic()

    def _go_to(self, op, url):
        page = self.page
        self.ready = False
        op.deadline = min(op.hard_deadline, max(op.deadline, time.monotonic() + BATCH_TIMEOUT))
        op.action = "snapshot"
        op.recognized_panel = False
        op.panel_end = op.next_panel_probe = 0.0
        op.started_reading = op.last_addition = 0.0
        self.navigation += 1
        # Return from runJavaScript's callback before starting another navigation.
        QTimer.singleShot(
            0, lambda: page.setUrl(QUrl(url)) if self.op is op and self.page is page else None
        )

    def _hold(self):
        self.pixel_watch = None
        if self.signin_required:
            return
        self.held = True
        self.ready = False
        self._finish("verification", CHECK_MESSAGE)

    def _account_wall(self, page):
        if (self.disposed or self.signin_required or page is not self.page or not self.query
                or (self.op is None and not self.held)):
            return
        self.signin_required = self.held = True
        self.ready = False
        if self.op is not None:
            self._finish("signin", providers.GOOGLE_SIGNIN)
        else:
            # A completed CAPTCHA can lead to an account gate after the initial batch ended.
            self.batchFinished.emit(GoogleBatch(self.query, "session", "signin", [], providers.GOOGLE_SIGNIN))
        self._close_verification(self.surface.isActiveWindow())

    def _possible_account_wall(self, page):
        # Google can replace a prompt with results in the same document. Re-read
        # it after the redirect or in-page update has had time to settle.
        document = (page, page.url())
        if self.account_document != document:
            self.account_document = document
            self.account_check.start()

    def _recheck_account_prompt(self):
        page, url = self.account_document
        self.account_document = None
        if page is self.page and not page.isLoading() and page.url() == url:
            self._inspect_access(page, url, confirm=True)

    def _inspect_access(self, page, url, *, changed_panel=False, confirm=False):
        operation = self.op

        def checked(value):
            if self.disposed or page is not self.page or page.isLoading() or page.url() != url:
                return
            if value == "signin":
                if confirm:
                    self._account_wall(page)
                else:
                    self._possible_account_wall(page)
            elif changed_panel and operation is self.op and not self.held:
                self._finish("error", "Google did not reopen this image’s related-results panel.")

        page.runJavaScript(INSPECTOR + '({"action":"access"})', WORLD, checked)

    def _url_changed(self, url, origin):
        if origin is not self.page or not self.query or url != self.page.url():
            return
        if url.host().lower() in CHECK_HOSTS or url.path().startswith("/sorry"):
            self._hold()
        else:
            self._claim_button_navigation(url)

    def _renderer_stopped(self, origin):
        if origin is self.page and self.query:
            # Defer window changes until Chromium's termination signal has unwound.
            QTimer.singleShot(0, lambda: self._report_renderer_failure(origin))

    def _report_renderer_failure(self, origin):
        if self.disposed or origin is not self.page or not self.query:
            return
        self.ready = self.held = False
        self._close_verification()
        message = "The Google browser stopped. Choose Retry to reopen this search."
        if self.op:
            self._finish("error", message)
        else:
            self.batchFinished.emit(GoogleBatch(self.query, "session", "error", [], message))

    def _loading(self, info, origin):
        if origin is not self.page or not self.query or self.signin_required or info.url().scheme() != "https":
            return
        states = QWebEngineLoadingInfo.LoadStatus
        state, url = info.status(), info.url()
        if state == states.LoadStoppedStatus:
            return
        if account_signin_url(address(url)):
            # acceptNavigationRequest handles the attempted main-frame account
            # navigation. Qt can also announce a failed load for a refused link.
            return
        if url.host().lower() in CHECK_HOSTS or url.path().startswith("/sorry"):
            self._url_changed(url, origin)
            if state in (states.LoadSucceededStatus, states.LoadFailedStatus) and url == origin.url():
                self._inspect_access(origin, url)
            return
        self._claim_button_navigation(url)
        if not self._page_matches(url):
            if self.op and url == self.page.url() and url.host().lower() in SEARCH_HOSTS:
                if state == states.LoadFailedStatus and info.errorCode() in (403, 429):
                    self._hold()
                    self._inspect_access(origin, url)
                elif state == states.LoadSucceededStatus:
                    self._inspect_access(origin, url, changed_panel=self.branch is not None)
            return
        if state == states.LoadStartedStatus:
            self.navigation += 1
            self.pixel_watch = None
            self.ready = False
            if self.op:
                # A real pagination/preview link may navigate before the click's JS callback
                # returns. Treat navigation as acknowledgment; never activate the next link twice.
                if self.op.pending and self.op.action in {"page", "seed", "panel", "activateMore"}:
                    self.op.action = "snapshot"
                self.op.pending = False
            return
        if url != self.page.url() and state != states.LoadFailedStatus:
            return
        if state == states.LoadFailedStatus:
            if info.errorCode() in (403, 429):
                self._hold()
                if url == origin.url():
                    self._inspect_access(origin, url)
            else:
                self.ready = False
                self._finish("error", "Google Images could not load on this connection.")
        elif state == states.LoadSucceededStatus:
            if self.held:
                return
            self.ready = True
            if self.op and self._progressive(self.op) and not self.op.items:
                self.op.started_reading = 0.0
            self._tick()

    def open_verification(self, center=None):
        if self.disposed or self.signin_required or not self.held or self.page is None:
            return False
        if self.verification is None:
            self.verify_epoch += 1
            self.verification = self.surface
            self.surface.hide()
            self.surface.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, False)
            self.surface.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
            self.controls.show()
            self.verify_note.setText(
                "Complete Google’s prompt here, or continue your search with another source."
            )
            point = QPoint(*center) if center is not None else None
            screen = QApplication.screenAt(point) if point is not None else None
            area = (screen or self.surface.screen()).availableGeometry()
            self.surface.resize(min(820, area.width()), min(660, area.height()))
            self.surface.move(area.center() - self.surface.rect().center())
            self.surface.show()
            self.verify_poll.start()
        self.surface.raise_()
        self.surface.activateWindow()
        self.view.setFocus()
        return True

    def _retry_verification(self):
        if self.verification is not None and self.page is not None:
            self.page.setUrl(QUrl(self._url()))

    def _choose_alternative(self):
        if self.verification is not None:
            self._close_verification(True)
            self.alternativeRequested.emit()

    def _dialog_closed(self, _result):
        epoch = self.verify_epoch
        QTimer.singleShot(
            0, lambda: self._close_verification(True) if self.verify_epoch == epoch else None
        )

    def _check_verification(self):
        page, epoch = self.page, self.verify_epoch
        if (
            self.verification is None
            or self.verify_pending
            or page is None
            or page.isLoading()
            or page.url().host().lower() not in SEARCH_HOSTS | CHECK_HOSTS
        ):
            return
        self.verify_pending = True
        document_url = page.url()
        script = (
            INSPECTOR
            + "("
            + json.dumps(
                {
                    "query": self.query,
                    "action": "verified",
                    "sourceDocument": bool(self.source_url or self.continuation_url),
                    "selectedDocument": self._selected_document(page.url()),
                }
            )
            + ")"
        )

        def apply_result(value):
            if epoch != self.verify_epoch or page is not self.page or page.url() != document_url:
                return
            if value == "signin":
                self._possible_account_wall(page)
            else:
                self._resume_verification(page, epoch)

        def checked(value):
            if self.disposed or epoch != self.verify_epoch or page is not self.page:
                return
            self.verify_pending = False
            if value in ("signin", "results"):
                QTimer.singleShot(0, lambda: apply_result(value))

        page.runJavaScript(script, WORLD, checked)

    def _resume_verification(self, page, epoch):
        if (
            epoch != self.verify_epoch
            or self.verification is None
            or page is not self.page
            or page.isLoading()
            or not self._page_matches(page.url())
        ):
            return
        self.verificationResumed.emit()
        self._close_verification(self.surface.isActiveWindow())
        self.held = False
        self.ready = True
        self._initial()

    def _close_verification(self, return_focus=False):
        if self.verification is None:
            return
        self.verify_poll.stop()
        self.verify_epoch += 1
        self.verify_pending = False
        self.verification = None
        if return_focus:
            self.returnToPicker.emit()
        self.surface.hide()
        self.surface.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.surface.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.controls.hide()
        self.surface.resize(1024, 720)
        self.surface.show()

    def _tick(self):
        op, page = self.op, self.page
        if op is None or page is None:
            return
        now = time.monotonic()
        if now >= op.deadline:
            if op.items and not self.ready:
                self._finish("error", "Google returned some GIFs but timed out loading the page. Choose Retry to continue.")
            elif op.items:
                self._finish("ok")
            elif self.branch:
                self._finish(
                    "error",
                    "Google’s related results did not finish loading. Choose Retry to reopen this image.",
                )
            elif self.ready and op.started_reading:
                self._finish(
                    "exhausted", "Google supplied no further readable GIFs for this request."
                )
            else:
                self._finish("error", "Google Images timed out. Submit the search again.")
            return
        if op.pending or self.held or not self._page_matches(page.url()):
            return
        if not self.ready and not (self._progressive(op) and page.isLoading()):
            return
        action = op.action
        options = self._options(page, op, action)
        navigation = self.navigation
        if action == "activateMore":
            op.button_source = address(page.url())
            op.button_deadline = now + 2.5
        op.pending = True

        def receive(value):
            if (
                self.disposed
                or self.op is not op
                or self.page is not page
                or self.navigation != navigation
            ):
                return
            op.pending = False
            if self._page_matches(page.url()):
                self._receive(op, value if isinstance(value, dict) else {})

        page.runJavaScript(
            INSPECTOR + "(" + json.dumps(options, ensure_ascii=True) + ")", WORLD, receive
        )

    def _options(self, page, op, action):
        options = {
            "query": self.query,
            "loadingDocument": not self.ready,
            "allowStills": self.references or self.branch is not None,
            "branch": self.branch is not None,
            "continuationDocument": bool(self.continuation_url),
            "sourceDocument": bool(self.source_url),
            "selectedDocument": self._selected_document(page.url()),
            "inPanel": self.branch is not None or op.kind == "related",
            "action": action,
            "operation": self.serial,
            "posterBudget": op.poster_budget,
            "pixelBudget": op.pixel_budget,
            "pixelCount": THUMB_BATCH_COUNT - len(op.rendered_thumbnails),
            "lastCompleted": self.last_completed,
        }
        if op.target is not None:
            options.update(media=op.target.media, page=op.target.page, seed=op.target.seed)
        return options

    def _reopen_selected(self, op):
        if self.direct_attempted:
            return False
        url = selected_image_url(self.query, self.branch)
        if not url:
            return False
        self.direct_attempted = True
        self.source_url = url
        self._go_to(op, url)
        return True

    def _branch_action(self, op, value, now):
        state = value.get("state")
        if op.action == "activateMore" and state != "expanded":
            op.button_deadline = 0.0
        if state == "continuation":
            url = value.get("url")
            if (
                image_results_url(url)
                and urlsplit(url).netloc == urlsplit(address(self.page.url())).netloc
                and url not in self.followed
            ):
                self.followed.add(url)
                self.continuation_url = url
                self._go_to(op, url)
                return True
            state = "panelEnd"
        elif state == "continuationButton":
            op.action = "activateMore"
            return True
        elif state == "expanded":
            op.panel_end = 0.0
            op.action = "snapshot"
            op.started_reading = op.last_addition = now
            op.next_panel_probe = now + SETTLE_WAIT
            return True
        elif state == "unavailable" and op.action == "seed":
            if not self._reopen_selected(op):
                self._finish(
                    "error",
                    "Google could not locate the selected image to open its related results.",
                )
            return True
        if state == "unavailable" and op.action == "panel":
            state = "panelEnd"
        if state not in {"panelEnd", "panelWaiting", "scrolled", "clicked"}:
            return False
        op.action = "snapshot"
        op.next_panel_probe = now + SETTLE_WAIT
        if not op.panel_end and (
            state == "panelEnd" or (state == "panelWaiting" and op.recognized_panel)
        ):
            op.panel_end = now
            op.deadline = min(op.hard_deadline, max(op.deadline, now + EMPTY_WAIT + SETTLE_WAIT))
        if state in {"clicked", "scrolled"}:
            op.panel_end = 0.0
            op.started_reading = op.started_reading or now
        if state == "clicked":
            op.parent_key = self.branch.key
            op.parent_identity = self.branch.panel_identity
        return True

    def _receive(self, op, value):
        now = time.monotonic()
        state = value.get("state")
        panel = op.kind == "related" or self.branch is not None
        if state == "signin":
            self._possible_account_wall(self.page)
            return
        if state == "interaction":
            self._hold()
            return
        if state == "different":
            self._finish("error", "Google changed the search. Submit the keyword again.")
            return
        if self.branch and self._branch_action(op, value, now):
            return
        if state == "unavailable":
            if op.kind == "related" and op.target is None:
                op.kind, op.action = "page", "page"
                return
            self._finish("exhausted", "Google supplied no further results for this selection.")
            return
        if state == "restoring":
            # A virtualized root disappeared. Wait for exactly that root to be restored.
            return
        if state in ("panelEnd", "panelWaiting"):
            op.action = "snapshot"
            # A container may be mounted before its first row and scrollbar exist. Only an
            # actual scroll or an already populated panel consumes the one scroll attempt.
            if state == "panelWaiting":
                op.panel_scrolled = False
                op.next_panel_probe = now + SETTLE_WAIT
            return
        if state in ("clicked", "scrolled"):
            op.action = "snapshot"
            if state == "clicked":
                root = item_from_record(value.get("root"), references=self.references)
                op.parent_key = root.key if root else ""
                op.parent_identity = root.panel_identity if root else ""
            if panel and op.panel_scrolled:
                op.items_before_scroll = len(op.items)
                op.last_addition = now
            if not op.started_reading:
                op.started_reading = now
            return
        if state not in ("ready", "unchanged"):
            return
        if not op.started_reading:
            op.started_reading = now
        if state == "ready" and panel and value.get("hasPanel") and not op.recognized_panel:
            op.recognized_panel = True
            op.items.clear()
            op.items_before_scroll = -1
        if state == "ready":
            op.can_expand = bool(value.get("canExpand"))
        for row in value.get("items", []):
            if not isinstance(row, dict):
                continue
            encoded = row.get("inline")
            budget = op.poster_budget
            if isinstance(encoded, str):
                # Charge even a rejected or duplicate raster to the cross-process transfer limit.
                op.poster_budget = max(0, budget - ((len(encoded) + 3) // 4) * 3)
            if self.branch and not value.get("hasPanel"):
                continue
            item = item_from_record(row, references=self.references, related=panel)
            if item is None or (self.branch and item.key == self.branch.key):
                continue
            key = item.key
            if row.get("posterOnly") and key not in self.seen:
                continue
            if (
                not row.get("posterOnly")
                and key not in op.items
                and (key not in self.seen or (op.kind == "related" and op.parent_key))
            ):
                op.items[key] = item
                op.panel_end = 0.0
                op.last_addition = now
            elif key in op.items and item.thumb and not op.items[key].thumb:
                # A lazy HTTPS thumbnail can arrive after the original URL in this same batch.
                op.items[key] = replace(op.items[key], thumb=item.thumb)
            if key not in self.seen_posters and key not in op.inline_thumbnails:
                data = inline_poster(encoded, budget)
                if data:
                    op.inline_thumbnails[key] = data
            if key not in self.seen_pixels and key not in op.rendered_thumbnails:
                source = row.get("media") or row.get("page")
                frame = unpack_thumbnail_pixels(row.get("pixels"), op.pixel_budget)
                if frame and len(op.rendered_thumbnails) < THUMB_BATCH_COUNT:
                    op.rendered_thumbnails[key] = frame
                    op.pixel_budget -= len(frame[2])
                    op.waiting_pixels.pop(source, None)
                elif isinstance(source, str) and len(source) <= 2048 and len(op.waiting_pixels) < 64:
                    op.waiting_pixels[source] = key
        if self._progressive(op):
            self._publish_visible(op)
            if not self.ready:
                # A slow subresource can keep the page loading after rows are visible.
                # Only a finished document can establish ordinary completion or exhaustion.
                return
        quiet = now - max(op.last_addition, op.started_reading)
        if self.branch:
            if not op.recognized_panel and not self.continuation_url and quiet >= EMPTY_WAIT:
                if not self._reopen_selected(op):
                    self._finish(
                        "error",
                        "Google did not expose the selected image’s related-results panel. Choose Retry to reopen it.",
                    )
            elif op.panel_end and now - op.panel_end >= EMPTY_WAIT and quiet >= EMPTY_WAIT:
                note = (
                    ""
                    if op.items
                    else (
                        "Google’s selected panel has no further matching images."
                        if self.references
                        else "Google’s selected panel has no further GIFs."
                    )
                )
                self._finish("exhausted", note)
            elif op.items and quiet >= SETTLE_WAIT:
                self._finish("ok")
            elif now >= op.next_panel_probe and quiet >= SETTLE_WAIT:
                op.action = "panel"
            return
        if not op.items and quiet >= EMPTY_WAIT:
            self._finish("exhausted", "Google supplied no further readable GIFs for this request.")
        elif (
            panel and not op.panel_scrolled and quiet >= SETTLE_WAIT and now >= op.next_panel_probe
        ):
            op.panel_scrolled = True
            op.action = "panel"
        elif op.items and op.panel_scrolled and len(op.items) == op.items_before_scroll:
            # The browser may need another network response after the scroll. Give that
            # response the same bounded no-progress window as the first row.
            if quiet >= EMPTY_WAIT:
                self._finish("ok")
        elif (
            op.items
            and quiet >= (EMPTY_WAIT if panel and not op.recognized_panel else SETTLE_WAIT)
            and (not panel or op.panel_scrolled)
        ):
            self._finish("ok")
