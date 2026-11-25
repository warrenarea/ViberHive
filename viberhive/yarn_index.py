"""Read authentic Yarn permalinks from public web and image search results."""

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .model import Item, Page
from .net import Problem, fetch, host_of, web_url

HOST = "search.brave.com"
THUMBNAILS = "imgs.search.brave.com"
URL = "https://" + HOST + "/search"
YARN = "Yarn"
YARN_HOSTS = {
    "yarn.co",
    "www.yarn.co",
    "getyarn.io",
    "www.getyarn.io",
    "memes.yarn.co",
    "memes.getyarn.io",
    "tv.getyarn.io",
    "trailers.getyarn.io",
}
CLIP = re.compile(r"/yarn-clip/[a-z0-9-]{8,64}/?", re.IGNORECASE)


def clip_link(url):
    return bool(web_url(url) and host_of(url) in YARN_HOSTS and CLIP.fullmatch(urlsplit(url).path))


def share_url(url):
    """An indexed GIF or embed page can refer to the same shareable Yarn clip."""
    if not web_url(url) or host_of(url) not in YARN_HOSTS:
        return ""
    parsed = urlsplit(url)
    path = re.sub(r"/(?:embed|gif)/?$", "", parsed.path).rstrip("/")
    link = urlunsplit(("https", parsed.netloc, path, "", ""))
    return link if clip_link(link) else ""


def from_image_result(row):
    page = share_url(row.page)
    if not page:
        return None
    thumb = web_url(row.thumb)
    if not thumb and row.kind == "reference":
        thumb = web_url(row.media)
    return Item(
        clean_title(row.title) or "Yarn clip",
        "",
        thumb,
        page,
        YARN,
        kind="clip",
        width=row.width,
        height=row.height,
    )


def clean_title(title):
    fields = [part.strip() for part in " ".join(title.split()).split("|")]
    if fields and fields[0].casefold() == "yarn":
        fields = fields[1:]
    output = []
    for field in fields:
        if field.casefold().startswith(("video clips by quote", "video gifs by quote")):
            break
        if field:
            output.append(field)
    return " — ".join(output)[:240]


UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)


def cited_clip(text):
    # Some result anchors are search-engine redirects. Only accept a fully printed
    # publisher host, clip path and intact UUID from the same card's visible citation.
    parts = [part.strip() for part in text.split("›")]
    if not 3 <= len(parts) <= 4 or parts[1] != "yarn-clip" or not UUID.fullmatch(parts[2]):
        return ""
    if len(parts) == 4 and parts[3] not in ("gif", "embed"):
        return ""
    host = parts[0].removeprefix("https://")
    return f"https://{host}/yarn-clip/{parts[2]}" if host in YARN_HOSTS else ""


class Results(HTMLParser):
    def __init__(self, query, page):
        super().__init__()
        self.query, self.page = query, page
        self.depth = self.card = 0
        self.items = {}
        self.empty = self.has_next = False
        self.allow_cite = self.citing = self.in_thumb = False
        self.cite = []
        self.url = self.title = self.thumb = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if "no-results" in classes or a.get("id") == "no-results":
            self.empty = True
        if tag == "div":
            self.depth += 1
            if a.get("data-type") == "web" and "snippet" in classes:
                self.card = self.depth
                self.url = self.title = self.thumb = ""
                self.cite = []
                self.allow_cite = self.citing = self.in_thumb = False
            elif self.card and "search-snippet-title" in classes:
                self.title = clean_title(a.get("title") or "")
        elif tag == "a":
            href = a.get("href") or ""
            if href.startswith("/search?"):
                params = parse_qs(urlsplit(href).query)
                if params.get("q") == [self.query] and params.get("offset") == [str(self.page + 1)]:
                    self.has_next = True
            if self.card:
                self.in_thumb = "thumbnail" in classes
                if "l1" in classes:
                    self.url = share_url(href)
                    self.allow_cite = href.startswith("/go/v1/")
                elif self.in_thumb and self.allow_cite and not self.url:
                    self.url = share_url(href)
        elif self.card and tag == "cite" and "snippet-url" in classes:
            self.citing = True
        elif self.card and self.in_thumb and tag == "img":
            thumb = web_url(a.get("src"))
            if thumb and host_of(thumb) == THUMBNAILS:
                self.thumb = thumb

    def handle_data(self, data):
        if self.citing:
            self.cite.append(data)
        if data.strip().lower().startswith("no results found for"):
            self.empty = True

    def handle_endtag(self, tag):
        if tag == "cite":
            self.citing = False
        elif tag == "a":
            self.in_thumb = False
        elif tag == "div":
            if self.card and self.depth == self.card:
                url = self.url
                if not url and self.allow_cite:
                    url = cited_clip("".join(self.cite).strip())
                if url and self.title and len(self.items) < 60:
                    self.items.setdefault(
                        url, Item(self.title, "", self.thumb, url, YARN, kind="clip")
                    )
                self.card = 0
            self.depth = max(0, self.depth - 1)


def search(query, page=0):
    query_text = "site:yarn.co/yarn-clip/ " + query
    params = {"q": query_text}
    if page:
        params["offset"] = page
    raw, _ = fetch(
        URL + "?" + urlencode(params),
        2 * 1024**2,
        hosts={HOST},
        accept="text/html",
        deadline=12,
    )
    parser = Results(query_text, page)
    parser.feed(raw.decode("utf-8", "replace"))
    parser.close()
    if not parser.items and not parser.empty:
        raise Problem("The web index did not return a readable list of Yarn clips.")
    # A negative page stays with the public index when the user asks for more.
    more = -(page + 1) if parser.items and parser.has_next and page < 9 else None
    note = (
        "Yarn clips found through Brave Search. Playback depends on Yarn."
        if parser.items
        else "The web index found no matching Yarn clips."
    )
    return Page(list(parser.items.values()), more, note)
