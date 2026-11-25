"""Read the server-rendered quote cards from Clip.Cafe's public search page."""

import re
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

from .model import Item, Page
from .net import Problem, fetch, host_of, web_url

NAME = "Clip.Cafe"
HOST = "clip.cafe"
ROOT = "https://clip.cafe/"


def website(query, page=1):
    params = {"s": query, "ss": "1"}
    if page > 1:
        params["p"] = str(page)
    return ROOT + "?" + urlencode(params)


def clip_url(url):
    return bool(
        web_url(url)
        and host_of(url) == HOST
        and re.fullmatch(r"/[\w-]+/[\w-]+/?", urlsplit(url).path, re.ASCII)
    )


class ClipCards(HTMLParser):
    """Keep each show, transcript and poster attached to its own search result."""

    def __init__(self):
        super().__init__()
        self.items = {}
        self.empty = self.has_next = False
        self.depth = self.card_depth = self.movie_depth = self.transcript_depth = 0
        self.card = None
        self.in_picture = False
        self.first_paragraph = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "div":
            self.depth += 1
            if "searchResultClip" in classes:
                self.card_depth = self.depth
                self.card = {
                    "url": "",
                    "thumb": "",
                    "show": [],
                    "quote": [],
                    "width": 0,
                    "height": 0,
                }
            elif self.card and "clipMovie" in classes:
                self.movie_depth = self.depth
            elif self.card and "clipTrans" in classes:
                self.transcript_depth = self.depth
                self.first_paragraph = True
        if tag == "a" and "pager-next" in classes and a.get("href"):
            self.has_next = True
        if self.card is None:
            return
        if tag == "a" and "smallClipContainer" in classes:
            url = urljoin(ROOT, a.get("href") or "")
            if clip_url(url):
                self.card["url"] = url
        elif tag == "picture" and "clipThumb" in classes:
            self.in_picture = True
        elif tag == "img" and self.in_picture:
            thumb = web_url(urljoin(ROOT, a.get("data-src") or a.get("src") or ""))
            if thumb and host_of(thumb) == HOST:
                self.card["thumb"] = thumb
            for key in ("width", "height"):
                value = a.get(key) or ""
                if re.fullmatch(r"[0-9]{1,4}", value):
                    self.card[key] = int(value)
        elif tag == "br" and self.first_paragraph:
            self.card["quote"].append(" ")

    def handle_data(self, data):
        if data.strip().casefold() == "no clips found":
            self.empty = True
        if self.card and self.movie_depth:
            self.card["show"].append(data)
        elif self.card and self.transcript_depth and self.first_paragraph:
            self.card["quote"].append(data)

    def handle_endtag(self, tag):
        if tag == "picture":
            self.in_picture = False
        elif tag == "p" and self.transcript_depth:
            self.first_paragraph = False
        elif tag == "div":
            if self.depth == self.movie_depth:
                self.movie_depth = 0
            if self.depth == self.transcript_depth:
                self.transcript_depth = 0
                self.first_paragraph = False
            if self.card is not None and self.depth == self.card_depth:
                card = self.card
                url = card["url"]
                if url:
                    show = " ".join("".join(card["show"]).split())
                    quote = (
                        " ".join("".join(card["quote"]).split()).removeprefix("Transcript:").strip()
                    )
                    fallback = urlsplit(url).path.strip("/").rsplit("/", 1)[-1].replace("-", " ")
                    title = " — ".join(part for part in (show, quote or fallback) if part)
                    self.items[url] = Item(
                        title[:240],
                        "",
                        card["thumb"],
                        url,
                        NAME,
                        kind="clip",
                        width=card["width"],
                        height=card["height"],
                    )
                self.card = None
                self.card_depth = 0
            self.depth = max(0, self.depth - 1)


def search(query, page):
    raw, _ = fetch(website(query, page), 3 * 1024**2, hosts={HOST}, accept="text/html", deadline=12)
    cards = ClipCards()
    cards.feed(raw.decode("utf-8", "replace"))
    cards.close()
    if not cards.items and not cards.empty:
        raise Problem("Clip.Cafe did not return a readable quote results page.")
    # Negative continuations keep this query on Clip.Cafe even if Yarn recovers mid-scroll.
    return Page(list(cards.items.values()), -(page + 1) if cards.items and cards.has_next else None)
