"""Read the GIF listings published in KLIPY's public search and topic pages."""

import json
import re
from html.parser import HTMLParser
from urllib.parse import quote

from .net import HTTPProblem, Problem, fetch

WEB_HOSTS = {"klipy.com", "www.klipy.com"}
WEBSITE_MORE = "klipy:website"
PUSH = "self.__next_f.push("
ITEMS = re.compile(r'(?<!\\)"initialItems"\s*:\s*')
MORE = re.compile(r'\s*,\s*"initialHasNext"\s*:\s*true\b')


class ListingParser(HTMLParser):
    """Collect only the page's serialized text chunks, without running JavaScript."""

    def __init__(self):
        super().__init__()
        self.in_script = False
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = not any(name == "src" for name, value in attrs)

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if not self.in_script:
            return
        script = data.strip().removesuffix(";").rstrip()
        if not script.startswith(PUSH) or not script.endswith(")"):
            return
        try:
            chunk = json.loads(script[len(PUSH):-1])
        except (ValueError, RecursionError):
            return
        if isinstance(chunk, list) and len(chunk) == 2 and chunk[0] == 1 and isinstance(chunk[1], str):
            self.chunks.append(chunk[1])

    def listing(self):
        data = "".join(self.chunks)
        for match in ITEMS.finditer(data):
            try:
                rows, end = json.JSONDecoder().raw_decode(data, match.end())
            except (ValueError, RecursionError):
                continue
            if isinstance(rows, list):
                return rows, bool(MORE.match(data, end))
        return None


def search_url(query):
    return "https://klipy.com/search/" + quote(query, safe="")


def search(query):
    # Some search pages populate results in a browser. The site's GIF-only
    # topic pages can also publish those same search results in their HTML.
    topic = "-".join(query.lower().split())
    urls = [search_url(query), "https://klipy.com/explore/" + quote(topic, safe="")]
    for url in urls:
        try:
            raw, mime = fetch(url, 4 * 1024**2, hosts=WEB_HOSTS, accept="text/html", deadline=18)
        except HTTPProblem as exc:
            if exc.code == 404:
                continue
            raise
        if mime not in ("text/html", "application/xhtml+xml"):
            continue
        parser = ListingParser()
        parser.feed(raw.decode("utf-8", "replace"))
        parser.close()
        listing = parser.listing()
        if listing is not None:
            rows, has_more = listing
            gifs = [row for row in rows if isinstance(row, dict) and row.get("type") == "gif"]
            if gifs or not rows or url == urls[-1]:
                return gifs, has_more
    raise Problem("KLIPY did not publish a readable GIF list for this phrase. Try Open search on website.")
