"""Keep Google results in the native gallery while a private renderer collects more."""

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtWidgets import QApplication

from . import yarn_index
from .google_query import cache_key as cache_key, search_url as search_url
from .model import Page
from .providers import CLIP_CAFE, GOOGLE, GOOGLE_SIGNIN, KLIPY

CACHE_TTL = 24 * 3600
CACHE_LIMIT = 512
INITIAL_PREFETCH = 2
AUTO_ROUNDS = 5
EMPTY_ROUNDS = 3
NO_REQUEST = object()


def yarn_cache_key(query):
    return "yarn-clips:" + cache_key("site:yarn.co/yarn-clip/ " + query, references=True)


class GoogleResults(QObject):
    def __init__(self, picker):
        super().__init__(picker)
        self.picker = picker
        self.browser = None
        self.active = False
        self.keep_page = False
        self.write_cache = QTimer(self, singleShot=True, interval=350)
        self.write_cache.timeout.connect(self.flush)
        self.prefetch = QTimer(self, singleShot=True, interval=120)
        self.prefetch.timeout.connect(self.pump)

    def flush(self):
        self.write_cache.stop()
        if self.active and self.items:
            combined = self.items | self.retained
            entries = list(combined.values())
            complete = self.exhausted and len(entries) <= CACHE_LIMIT
            self.picker.store.save_page(
                self.cache, Page(entries[:CACHE_LIMIT], 0 if complete else 1, self.note)
            )

    def keep_page_for_branch(self):
        self.keep_page = self.active and self.browser is not None

    def reset(self):
        self.prefetch.stop()
        if self.write_cache.isActive():
            self.flush()
        was_active, self.active = self.active, False
        if was_active and self.browser is not None:
            if self.keep_page:
                self.browser.pause()
            else:
                self.browser.stop_search()
        self.keep_page = False

    def shutdown(self):
        self.prefetch.stop()
        if self.write_cache.isActive():
            self.flush()
        self.active = False
        if self.browser is not None:
            self.browser.dispose()
            self.browser = None

    def make_browser(self):
        from .google_worker import GoogleClient

        return GoogleClient(self.picker)

    def ensure_browser(self):
        if self.browser is None:
            try:
                self.browser = self.make_browser()
            except (ImportError, OSError):
                return False
            self.browser.batchFinished.connect(self.received)
            if hasattr(self.browser, "thumbnailsReady"):
                self.browser.thumbnailsReady.connect(self.rendered_ready)
            self.browser.verificationResumed.connect(self.verification_resumed)
            self.browser.returnToPicker.connect(self.return_to_picker)
            self.browser.alternativeRequested.connect(self.search_alternative)
        return True

    def verification_resumed(self):
        if self.active and not self.paused and self.blocked == "verification":
            self.blocked = ""
            self.inflight = "initial"
            self.picker.status.setText("Verification complete. Loading Google results…")
            self.sync()

    def return_to_picker(self):
        if self.active and self.picker.isVisible() and QApplication.activeModalWidget() is None:
            if self.blocked == "verification":
                self.picker.status.setText(
                    "Google verification is paused. Choose Verify with Google to reopen the prompt."
                )
            self.picker.raise_()
            self.picker.activateWindow()

    def restore_recent_yarn(self, query):
        page, age = self.picker.store.read_page(yarn_cache_key(query))
        if page and page.items and age < CACHE_TTL:
            return self.start(query, yarn_clips=True, snapshot=page)
        return False

    def start(self, query, *, references=False, seed=None, snapshot=None, yarn_clips=False, signin_required=False):
        self.yarn_clips = yarn_clips
        self.cache = (
            yarn_cache_key(query)
            if yarn_clips
            else cache_key(query, references=references, seed=seed)
        )
        if yarn_clips:
            query = "site:yarn.co/yarn-clip/ " + query
            references = True
        self.query = query
        self.references = references
        self.branch = seed
        self.retained = {}
        self.active = True
        self.paused = not self.picker.isVisible()
        self.ready = self.exhausted = False
        self.blocked = self.inflight = ""
        self.items = {}
        self.late_posters = set()
        self.requested = NO_REQUEST
        self.current_target = None
        self.warm_left = INITIAL_PREFETCH
        self.auto_left = AUTO_ROUNDS
        self.empty_rounds = self.last_scroll = 0
        saved, age = self.picker.store.read_page(self.cache)
        self.note = saved.note if saved else ""
        restored = snapshot is not None and bool(snapshot.items)
        if restored:
            self.append(snapshot.items)
            if saved:
                self.retained = {it.key: it for it in saved.items if it.key not in self.items}
            self.exhausted = snapshot.more == 0
            self.warm_left = 0
            self.auto_left = (
                0  # History restores the viewport without immediately sending requests.
            )
            self.picker.status.setText(self.note or "Restored Google results.")
        elif saved and saved.items:
            self.append(saved.items)
            self.exhausted = age < CACHE_TTL and saved.more == 0
            self.picker.status.setText(self.note or "Cached Google results.")
        if signin_required:
            self.problem("signin", GOOGLE_SIGNIN)
            return True
        if restored:
            self.sync()
            return True
        if saved and saved.items and age < CACHE_TTL:
            # A fresh grid with a full runway needs no network until the user browses it.
            self.warm_left = int(self.branch is not None and not self.exhausted)
            self.prefetch.start()
            self.sync()
            return True
        if not self.ensure_browser():
            if self.items:
                self.problem(
                    "error",
                    "Live Google search needs the browser component from Setup.cmd.",
                )
                return True
            self.reset()
            return False
        if not self.paused:
            self.begin_search()
        self.sync()
        return True

    def begin_search(self):
        if not self.ensure_browser():
            if not (self.items or self.yarn_clips or self.branch or self.references):
                self.reset()
                self.picker.get_page(0, "")
                return
            self.problem(
                "error",
                "Live Google search needs the browser component from Setup.cmd.",
            )
            return
        self.inflight = "initial"
        self.ready = False
        self.picker.status.setText("Searching Google Images…")
        if self.yarn_clips:
            self.picker.status.setText("Finding Yarn clips in Google Images…")
        elif self.branch:
            self.picker.status.setText(f"Opening Google’s image panel for “{self.branch.title}”…")
        elif self.references:
            self.picker.status.setText("Searching Google for reference images…")
        options = (
            {"references": self.references, "seed": self.branch}
            if self.references or self.branch
            else {}
        )
        if not self.browser.search(self.query, **options):
            self.problem("error", "Google Images could not start the search.")
        self.sync()

    def append(self, items):
        fresh = []
        for item in items:
            if self.branch and item.key == self.branch.key:
                continue
            if item.key not in self.items:
                self.items[item.key] = item
                self.retained.pop(item.key, None)
                fresh.append(item)
        if fresh:
            self.picker.consider_search_cover(fresh)
            self.picker.board.extend(fresh)
            self.picker.paint_timer.start()
        return len(fresh)

    def near_end(self):
        scroll = self.picker.scroll
        bar = scroll.verticalScrollBar()
        return bar.maximum() - bar.value() < 2 * max(1, scroll.viewport().height())

    def on_scroll(self, value):
        if not self.active or self.paused or self.exhausted or self.blocked:
            return
        # Grant another finite round only after meaningful forward progress near the end.
        if (
            value - self.last_scroll >= max(1, self.picker.scroll.viewport().height() // 2)
            and self.near_end()
        ):
            self.last_scroll = value
            self.auto_left = AUTO_ROUNDS
            self.empty_rounds = 0
            if not self.prefetch.isActive():
                self.prefetch.start()

    def pump(self):
        if not self.active or self.paused or self.inflight or self.blocked:
            return
        if self.requested is not NO_REQUEST:
            if not self.ready:
                self.begin_search()
                return
            target, self.requested = self.requested, NO_REQUEST
            self.expand(target)
            return
        if (
            self.exhausted
            or not self.auto_left
            or self.empty_rounds >= EMPTY_ROUNDS
            or self.picker.has_draft()
            or not (self.warm_left or self.near_end())
        ):
            return
        if not self.ready:
            self.begin_search()
            return
        self.auto_left -= 1
        self.warm_left = max(0, self.warm_left - 1)
        self.expand(None)

    def expand(self, target):
        related = target is not None or (
            not self.references
            and self.branch is None
            and self.picker.settings.values.get("related", True)
        )
        self.inflight = "more"
        self.current_target = target
        self.picker.status.setText(
            "Finding more Yarn clips…"
            if self.yarn_clips
            else "Loading more from this image’s Google results…"
            if self.branch
            else "Finding related GIFs…"
            if related
            else "Finding more GIFs…"
        )
        if not self.browser.more_results(target, related=related):
            self.problem("error", "Google Images could not continue this search.")
        self.sync()

    def received(self, batch):
        if not self.active or batch.query != self.query:
            return
        if batch.kind == "session":
            self.ready = False
            if batch.status == "signin":
                self.problem("signin", GOOGLE_SIGNIN)
            elif self.blocked != "signin":
                self.problem("error", batch.message)
            return
        if self.paused:
            return
        resuming = self.blocked == "verification" and batch.kind == "initial"
        expected = "initial" if batch.kind == "initial" else "more"
        if self.inflight != expected and not resuming:
            return
        self.inflight = self.blocked = ""
        if self.yarn_clips:
            self.receive_yarn(batch)
            return
        if thumbnails := getattr(batch, "inline_thumbnails", None):
            self.offer_posters(batch.items, thumbnails)
        fresh = self.append(batch.items)
        if rendered := getattr(batch, "rendered_thumbnails", None):
            self.rendered_ready(batch.query, rendered)
        if self.branch:
            self.receive_branch(batch, fresh)
            return
        parent = self.items.get(getattr(batch, "parent_key", ""))
        if (
            batch.kind == "related" and parent and batch.items
            and getattr(batch, "parent_identity", "") == parent.panel_identity
        ):
            # Retain automatic panel matches for a later explicit visual search,
            # including after a restart. This is a partial branch, open for further results.
            key = cache_key(self.query, references=self.references, seed=parent)
            prior, _ = self.picker.store.read_page(key)
            matches = {it.key: it for it in (prior.items if prior else [])}
            matches.update((it.key, it) for it in batch.items if it.key != parent.key)
            if matches and (not prior or prior.more != 0 or len(matches) > len(prior.items)):
                self.picker.store.save_page(key, Page(list(matches.values())[:CACHE_LIMIT], 1))
        if fresh:
            self.write_cache.start()
        if batch.status in ("verification", "signin", "error"):
            self.problem(batch.status, batch.message)
            return
        target, self.current_target = self.current_target, None
        if batch.kind == "initial":
            self.ready = True
            can_expand = getattr(batch, "can_expand", False) and self.picker.settings.values.get(
                "related", True
            )
            if batch.status == "exhausted" and not self.items and not can_expand:
                self.problem(
                    "error",
                    "Google supplied no readable GIF addresses on this results page. "
                    "Retry, open the search on Google, or try another phrase.",
                )
                return
        elif target is None:
            self.empty_rounds = 0 if fresh else self.empty_rounds + 1
            if batch.kind == "page" and batch.status == "exhausted":
                self.exhausted = True
        if self.exhausted and self.items:
            self.write_cache.start()
        if not self.items:
            self.picker.board.empty = (
                "Looking for GIFs in related results…"
                if batch.kind == "initial"
                else "No readable GIFs in this batch",
                self.query,
            )
            self.picker.board.update()
        if self.exhausted:
            note = (
                "End of Google results."
                if self.items
                else "Google supplied no readable GIF addresses."
            )
        elif not self.items and batch.kind == "initial":
            note = "Finding GIFs in related Google results…"
        elif not fresh and batch.kind != "initial":
            note = "No new GIFs in this batch."
        else:
            note = "Hover to play. Click to paste into Discord. Right-click for Copy GIF."
        if not self.exhausted and self.references and not self.branch:
            note = "Select a reference image to find related GIFs."
        self.picker.status.setText(self.note or batch.message or note)
        self.sync()
        self.prefetch.start()

    def receive_branch(self, batch, fresh):
        """The selected image owns this grid and every subsequent page."""
        if fresh:
            self.write_cache.start()
        if batch.status in ("verification", "signin", "error"):
            self.problem(batch.status, batch.message)
            return
        self.current_target = None
        self.ready = True
        self.exhausted = batch.status == "exhausted"
        self.empty_rounds = 0 if fresh else self.empty_rounds + 1
        self.note = ""
        if self.exhausted and self.items:
            self.write_cache.start()
        noun = "images" if self.references else "GIFs"
        if self.items:
            message = (
                f"End of the similar {noun} Google returned for this image."
                if self.exhausted else "Showing results from the selected Google image."
            )
        else:
            message = batch.message or (
                f"Google returned no similar {noun} for this image."
                if self.exhausted else "Checking this image’s additional Google results…"
            )
            self.picker.board.empty = (
                f"No similar {noun}" if self.exhausted else f"Looking for similar {noun}…",
                message,
            )
            self.picker.board.update()
        self.picker.status.setText(message)
        self.sync()
        if not self.exhausted:
            self.prefetch.start()

    def receive_yarn(self, batch):
        fresh = self.append(
            clip for item in batch.items if (clip := yarn_index.from_image_result(item))
        )
        if fresh:
            self.write_cache.start()
        if batch.status in {"verification", "signin", "error"}:
            self.problem(batch.status, batch.message)
            return
        self.ready = True
        self.current_target = None
        self.exhausted = batch.status == "exhausted"
        self.empty_rounds = 0 if fresh else self.empty_rounds + 1
        if self.items:
            self.note = "Yarn clips found through Google Images. Playback depends on Yarn."
            if self.exhausted:
                self.write_cache.start()
        else:
            self.note = (
                "Google Images found no matching Yarn clip pages."
                if self.exhausted
                else "Looking for Yarn clip pages in Google Images…"
            )
            self.picker.board.empty = ("Yarn clips", self.note)
            self.picker.board.update()
        self.picker.status.setText(self.note)
        self.sync()
        self.prefetch.start()

    def rendered_ready(self, query, frames):
        if not self.active or self.paused or self.yarn_clips or query != self.query:
            return
        accepted = {
            key: frame for key, frame in frames.items()
            if (item := self.items.get(key)) is not None and item.source == GOOGLE and not item.link_only
        }
        if self.picker.board.offer_google_pixels(accepted):
            self.picker.refresh_preview()

    def offer_posters(self, new_items, thumbnails):
        incoming = {item.key: item for item in new_items}
        offered = {}
        for key, data in thumbnails.items():
            item = self.items.get(key) or incoming.get(key)
            if item is not None and item.source == GOOGLE and not item.link_only:
                offered[key] = data
        if offered:
            accepted = self.picker.svc.offer_posters(offered)
            self.late_posters.update(key for key in accepted if key in self.items)
            if accepted:
                self.picker.google_stills_tried.difference_update(accepted)
                self.picker.schedule_posters()

    def take_poster_retry(self, item):
        if not self.active or item.key not in self.late_posters:
            return False
        self.late_posters.remove(item.key)
        return self.picker.svc.has_inline_poster(item.key)

    def problem(self, status, message):
        self.inflight = ""
        self.blocked = status
        self.prefetch.stop()
        self.requested = NO_REQUEST
        self.current_target = None
        if status == "signin":
            self.ready = False
            message = GOOGLE_SIGNIN
        message = message or "Google Images could not finish this search."
        self.picker.status.setText(message)
        if not self.items:
            if status == "signin":
                self.picker.board.empty = (
                    "Google requires sign-in",
                    f"Open this search in your browser or continue on {self.alternative_provider}.",
                )
            else:
                title = "Google verification" if status == "verification" else "Search unavailable"
                self.picker.board.empty = (title, message)
            self.picker.board.update()
        self.sync()

    @property
    def alternative_provider(self):
        return CLIP_CAFE if self.yarn_clips else KLIPY

    def search_alternative(self):
        if not self.active or self.blocked not in ("signin", "verification"):
            return
        ui = self.picker
        # A visual panel has a useful title. A Yarn index query includes an internal
        # site filter, so use only the quote displayed in the picker's search box.
        phrase = ui.edit.text().strip() or ui.query
        if self.branch and phrase == ui.query:
            phrase = self.branch.title.strip() or phrase
        ui.set_search_text(phrase)
        if self.yarn_clips:
            ui.clip_source.setCurrentIndex(ui.clip_source.findData(CLIP_CAFE))
        else:
            ui.source.setCurrentText(KLIPY)

    def more(self, item=None):
        if not self.active or self.paused:
            return
        if self.blocked == "verification":
            if self.browser.open_verification():
                self.picker.status.setText("Complete Google's prompt in the verification window.")
            else:
                self.problem(
                    "error", "The Google prompt is unavailable. Choose Retry to reopen the search."
                )
            return
        if self.blocked == "signin" and self.items:
            self.search_alternative()
            return
        if self.blocked in ("error", "signin"):
            self.blocked = ""
            self.ready = self.exhausted = False
            if item is not None:
                self.requested = item
            self.begin_search()
            return
        if self.exhausted and item is None:
            return
        self.requested = item
        self.pump()

    def has_more(self):
        return self.active and (bool(self.blocked) or not self.exhausted)

    def sync(self):
        if not self.active:
            return
        more = self.picker.more
        more.setVisible(self.has_more())
        more.setEnabled(not self.inflight)
        if self.blocked == "verification":
            more.setText("Verify with Google…")
        elif self.blocked == "signin":
            more.setText(f"Search {self.alternative_provider}" if self.items else "Retry Google")
        elif self.blocked == "error":
            more.setText("Retry")
        else:
            more.setText(
                "Searching…" if self.inflight
                else "More results" if self.branch
                else "Load more"
            )
        self.picker.update_result_count()

    def visibility_changed(self, visible):
        if not self.active:
            return
        self.paused = not visible
        if not visible:
            self.prefetch.stop()
            if self.current_target is not None:
                self.requested = self.current_target
            if self.browser is not None:
                self.browser.pause()
            self.inflight = ""
            self.current_target = None
        else:
            self.prefetch.start()
        self.sync()
