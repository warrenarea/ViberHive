"""A title search with its own pagination and Google browser session for the preview pane."""

from PyQt6.QtCore import QObject, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication

from . import klipy_web, providers as p
from .google_query import cache_key
from .google_results import CACHE_TTL
from .google_worker import GoogleClient
from .model import Page


def related_query(picker, item):
    words = item.title.strip() or picker.store.favorite_words(item.key)
    if not words and picker.tab == "GIFs":
        words = picker.query
    return " ".join(words[:p.TITLE_LIMIT].split())


class PreviewSearch(QObject):
    cleared = pyqtSignal()
    changed = pyqtSignal()
    itemsAdded = pyqtSignal(object)
    sourcesReady = pyqtSignal(object)
    thumbnailsReady = pyqtSignal(object)
    inlinePostersReady = pyqtSignal(object)

    def __init__(self, picker):
        super().__init__(picker)
        self.picker = picker
        self.browser = None
        self.job = None
        self.generation = 0
        self.reset()

    def reset(self):
        self.generation += 1
        self.active = self.busy = self.streamed_fresh = False
        self.missing_sources = set()
        if self.job is not None:
            self.job.cancel()
            self.job = None
        if self.browser is not None and self.browser.query:
            self.browser.stop_search()
        self.query = self.provider = self.message = self.blocked = ""
        self.web_google = self.web_ready = self.exhausted = self.manual_more = False
        self.recover_covers = False
        self.browse = p.Browse()
        self.cleared.emit()
        self.changed.emit()

    def shutdown(self):
        self.reset()
        if self.browser is not None:
            self.browser.dispose()
            self.browser.deleteLater()
            self.browser = None

    def start(self, provider, query):
        self.reset()
        self.query = " ".join(query[:p.TITLE_LIMIT].split())
        if not self.query:
            return
        self.provider = provider
        self.active = True
        self.web_google = provider == p.GOOGLE and not self.picker.settings.serp
        if self.web_google:
            cached, age = self.picker.store.read_page(cache_key(self.query))
            if cached and cached.items:
                self._append(cached)
                if age < CACHE_TTL:
                    self.recover_covers = True
                    self.exhausted = cached.more == 0
                    self.message = "Cached Google results."
                    self.changed.emit()
                    return
        self._request()

    def _api_page(self):
        related = self.provider == p.GOOGLE and self.picker.settings.values.get("related", True)
        return self.browse.next_request(related)

    @property
    def action_text(self):
        if not self.active or self.busy:
            return ""
        if self.blocked:
            return {
                "error": "Retry",
                "verification": "Verify with Google…",
                "signin": "Open in browser ↗",
                "website": "More on KLIPY ↗",
            }[self.blocked]
        return "" if self.exhausted else "More results"

    @property
    def may_load_more(self):
        return self.active and not (self.busy or self.exhausted or self.blocked or self.manual_more)

    def load_more(self):
        if self.may_load_more:
            self._request()

    def more(self):
        if not self.action_text:
            return
        if self.blocked in ("website", "signin"):
            if not QDesktopServices.openUrl(QUrl(p.website(self.provider, self.query))):
                self.message = "The search could not be opened in your browser."
                self.changed.emit()
            return
        if self.blocked == "verification":
            if self.browser and self.browser.open_verification():
                self.message = "Complete Google’s prompt in the verification window."
                self.changed.emit()
            else:
                self._problem("error", "The verification window could not be opened.")
            return
        self.blocked = ""
        self._request()

    def refresh_missing_covers(self):
        if self.active and self.recover_covers and not self.busy and not self.blocked:
            self._request()

    def _request(self):
        self.recover_covers = False
        self.busy = True
        self.streamed_fresh = False
        self.manual_more = False
        self.message = "Searching…" if not self.browse.seen_media else "Loading more GIFs…"
        self.changed.emit()
        if self.web_google:
            self._request_google()
            return
        token = self.generation
        provider, query = self.provider, self.query
        page, seed = self._api_page()

        def completed(answer, error):
            if not self.active or token != self.generation:
                return
            self.job = None
            if error:
                self._problem("signin" if error == p.GOOGLE_SIGNIN else "error", error)
                return
            result, note = answer
            website_more = provider == p.KLIPY and result.more == klipy_web.WEBSITE_MORE
            fresh = self._append(Page(result.items, None if website_more else result.more), seed)
            if note == p.GOOGLE_SIGNIN:
                self._problem("signin", note)
                return
            self.exhausted = self._api_page() is None
            self.manual_more = not fresh
            if website_more:
                self.blocked = "website"
            self.busy = False
            self.message = note or ("" if self.browse.seen_media else "No matching GIFs.")
            self.changed.emit()

        self.job = self.picker.jobs.run(
            "search", lambda: self.picker.svc.search(provider, query, page, seed), completed
        )

    def _append(self, page, seed=""):
        gifs = [item for item in page.items if item.kind == "gif"]
        fresh = self.browse.accept(Page(gifs, page.more), seed)
        if self.web_google:
            updates = {it.key: it for it in gifs if it.thumb and it.key in self.missing_sources}
            if updates:
                self.missing_sources.difference_update(updates)
                self.sourcesReady.emit(updates)
            self.missing_sources.update(it.key for it in fresh if not it.thumb)
        if fresh:
            self.itemsAdded.emit(fresh)
        return bool(fresh)

    def _request_google(self):
        if self.browser is None:
            self.browser = GoogleClient(self.picker)
            if hasattr(self.browser, "resultsReady"):
                self.browser.resultsReady.connect(self._google_batch)
            self.browser.batchFinished.connect(self._google_batch)
            self.browser.thumbnailsReady.connect(self._google_pixels)
            self.browser.verificationResumed.connect(self._verification_resumed)
            self.browser.alternativeRequested.connect(self._switch_to_klipy)
            self.browser.returnToPicker.connect(self._return_to_picker)
        if self.web_ready:
            started = self.browser.more_results(related=self.picker.settings.values.get("related", True))
        else:
            started = self.browser.search(self.query)
        if not started:
            self._problem("error", "Google Images could not start the request.")

    def _google_batch(self, batch):
        if not self.active or not self.web_google or batch.query != self.query:
            return
        if batch.kind != "session" and not self.busy:
            return
        if batch.status == "partial" and (self.blocked or batch.kind != "initial"):
            return
        if batch.inline_thumbnails:
            accepted = self.picker.svc.offer_posters(batch.inline_thumbnails)
            if accepted:
                self.inlinePostersReady.emit(accepted)
        fresh = self._append(Page(batch.items))
        if batch.rendered_thumbnails:
            self._google_pixels(batch.query, batch.rendered_thumbnails)
        if batch.status == "partial":
            self.streamed_fresh |= fresh
            return
        fresh = fresh or self.streamed_fresh
        self.streamed_fresh = False
        if batch.status in ("verification", "signin", "error"):
            self._problem(batch.status, batch.message)
            return
        if batch.kind == "initial" and batch.items:
            # Preserve the first successful middle-click page for both views. A main
            # search may already have cached more pages while this request was running.
            key = cache_key(self.query)
            existing, age = self.picker.store.read_page(key)
            if not existing or age >= CACHE_TTL:
                self.picker.store.save_page(key, Page(batch.items[:512], 1))
        self.web_ready = True
        self.blocked = ""
        can_expand = batch.can_expand and self.picker.settings.values.get("related", True)
        if batch.status == "exhausted":
            self.exhausted = batch.kind == "page" or (
                batch.kind == "initial" and not self.browse.seen_media and not can_expand
            )
        self.manual_more = not fresh
        self.busy = False
        self.message = batch.message or ("" if fresh else "No new GIFs in this batch.")
        self.changed.emit()

    def _google_pixels(self, query, frames):
        if self.active and self.web_google and query == self.query:
            known = {key: frame for key, frame in frames.items() if key in self.browse.seen_media}
            if known:
                self.thumbnailsReady.emit(known)

    def _verification_resumed(self):
        if self.active and self.blocked == "verification":
            self.busy = True
            self.blocked = ""
            self.message = "Verification complete. Loading Google results…"
            self.changed.emit()

    def _switch_to_klipy(self):
        if self.active and self.blocked in ("signin", "verification"):
            self.start(p.KLIPY, self.query)

    def _return_to_picker(self):
        if self.active and self.web_google and self.picker.isVisible() and not QApplication.activeModalWidget():
            self.picker.raise_()
            self.picker.activateWindow()

    def _problem(self, status, message):
        self.busy = self.web_ready = False
        self.blocked = status
        self.message = p.GOOGLE_SIGNIN if status == "signin" else message
        self.changed.emit()
