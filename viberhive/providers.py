"""Read search metadata only. Sites, result HTML and scripts never execute here."""

import json
import re
from collections import deque
from dataclasses import dataclass, replace
from html import unescape
from html.parser import HTMLParser
from itertools import islice
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit

from . import clipcafe, klipy_web, yarn_index
from .model import Item, Page, site_icon_url
from .net import HTTPProblem, Problem, fetch, host_of, read_json, web_url

GOOGLE = "Google Images"
GOOGLE_SIGNIN = "Google requires sign-in to show more results for this search."
COMMONS = "Wikimedia Commons"
KLIPY = "KLIPY"
CLIPS = "Auto"
YARN = yarn_index.YARN
CLIP_CAFE = clipcafe.NAME
YARN_HOSTS = yarn_index.YARN_HOSTS
KLIPY_CDN = {"static.klipy.com", "static1.klipy.com", "static2.klipy.com"}
TOKEN = re.compile(r"[\w:=-]{2,512}\Z", re.ASCII)
TITLE_LIMIT = 240


class GoogleSignInRequired(Problem):
    def __init__(self):
        super().__init__(GOOGLE_SIGNIN)


def endpoint(base, **params):
    return base + "?" + urlencode(params)


def website(source, query):
    if source == GOOGLE:
        return endpoint(
            "https://www.google.com/search", udm=2, tbs="itp:animated", safe="active", q=query
        )
    if source == KLIPY:
        return klipy_web.search_url(query)
    if source in (CLIPS, YARN, CLIP_CAFE):
        return clips_website(query, source)
    return endpoint(
        "https://commons.wikimedia.org/w/index.php",
        search=query + ' filemime:"image/gif"',
        title="Special:MediaSearch",
        type="image",
    )


def size(value):
    return min(value, 50000) if type(value) is int and value > 0 else 0


def label(value):
    return " ".join(str(value or "").split())[:TITLE_LIMIT]


def gif_candidate(url, related=False, tag=""):
    if not web_url(url):
        return False
    path = urlsplit(url).path.lower()
    if re.search(r"\.(?:jpe?g|png|bmp|svg|ico|avif|html?|mp[34]|webm|exe|zip)$", path):
        return False
    # Google's related panel can contain photographs. Require positive format evidence there.
    if related:
        if str(tag).casefold() in {"gif", "animated gif", "animated"}:
            return True
        return bool(re.search(r"\.(gif|webp)(?:$|/)", path)) or any(
            v.lower() in ("gif", "webp")
            for k, vs in parse_qs(urlsplit(url).query).items()
            if k.lower() in ("format", "fm", "type", "ext")
            for v in vs
        )
    return True


IMAGE_FILTER = re.compile(r"rimg:[A-Za-z0-9_-]{1,2048}={0,2}")
RELATED_ID = re.compile(r"[A-Za-z0-9_+./:=-]{2,1024}")


def valid_panel_filter(value):
    parts = value.split(",")
    if "itp:animated" in parts:
        parts.remove("itp:animated")
    return len(parts) == 1 and IMAGE_FILTER.fullmatch(parts[0]) is not None


def valid_panel_query(value):
    return 0 < len(value) <= 512 and bool(value.strip()) and value.isprintable()


def panel_continuation(response, query):
    """Keep the panel's image filter and any revised search text from its More link."""
    for name in ("serpapi_see_more_link", "see_more_link"):
        link = web_url(response.get(name))
        if not link:
            continue
        parsed = urlsplit(link)
        params = parse_qs(parsed.query, keep_blank_values=True)
        if name == "serpapi_see_more_link":
            if (
                host_of(link) != "serpapi.com"
                or parsed.path not in {"/search", "/search.json"}
                or params.get("engine") != ["google_images"]
            ):
                continue
        elif (
            host_of(link) not in {"google.com", "www.google.com"}
            or parsed.path != "/search"
            or not (params.get("udm") == ["2"] or params.get("tbm") == ["isch"])
        ):
            continue
        filters = params.get("tbs", [])
        queries = params.get("q", [query])
        if len(filters) != 1 or not valid_panel_filter(filters[0]):
            continue
        if len(queries) == 1 and valid_panel_query(queries[0]):
            suffix = "" if queries[0] == query else "|" + queries[0]
            return "0:" + filters[0] + suffix
    return None


def google_api(query, page, seed, key):
    panel = bool(seed) and page == 0
    image_filter = ""
    search_query = query
    index = page
    if seed and not (isinstance(seed, str) and RELATED_ID.fullmatch(seed)):
        raise Problem("This image has no usable related-content ID.")
    if panel:
        parameters = {
            "engine": "google_images_related_content",
            "related_content_id": seed,
        }
    else:
        if seed:
            # Subsequent pages are Google Images searches with this SAME rimg filter.
            # Never send the API key to a URL supplied by a search result.
            number, _, gallery = (
                page.partition(":") if isinstance(page, str) else ("", "", "")
            )
            image_filter, separator, gallery_query = gallery.partition("|")
            if (
                not re.fullmatch(r"[0-9]{1,2}", number)
                or not valid_panel_filter(image_filter)
                or (separator and not valid_panel_query(gallery_query))
            ):
                raise Problem("The related-image continuation is unavailable.")
            if separator:
                search_query = gallery_query
            index = int(number)
        elif type(index) is not int or not 0 <= index <= 99:
            raise Problem("The Google Images page number is invalid.")
        parameters = {
            "engine": "google_images",
            "google_domain": "google.com",
            "ijn": index,
            "safe": "active",
        }
        if image_filter:
            parameters["tbs"] = image_filter
        else:
            parameters["image_type"] = "animated"
    data = read_json(
        endpoint(
            "https://serpapi.com/search.json",
            q=search_query,
            hl="en",
            gl="us",
            api_key=key,
            **parameters,
        ),
        "serpapi.com",
    )
    if data.get("error"):
        if any(
            x in str(data["error"]).lower()
            for x in (
                "hasn't returned any results",
                "has not returned any results",
                "no results",
            )
        ):
            return Page()
        raise Problem(
            "SerpApi could not complete the search. Check your key, quota and connection."
        )
    rows = data.get("related_content" if panel else "images_results", [])
    items = []
    for row in rows[:110]:
        if not isinstance(row, dict):
            continue
        media = row.get("original")
        if not gif_candidate(media, bool(seed), row.get("tag", "")) or row.get(
            "unsafe"
        ):
            continue
        related = row.get("related_content_id")
        items.append(
            Item(
                label(row.get("title")) or host_of(media),
                media,
                web_url(row.get("thumbnail")),
                web_url(row.get("link")),
                GOOGLE,
                related
                if isinstance(related, str) and RELATED_ID.fullmatch(related)
                else "",
                width=size(row.get("original_width")),
                height=size(row.get("original_height")),
            )
        )
    more = None
    if panel:
        more = panel_continuation(data, query)
    elif index < 99 and data.get("serpapi_pagination", {}).get("next"):
        more = f"{index + 1}:{gallery}" if seed else index + 1
    return Page(items, more)


def gif_from_publisher(url):
    """Resolve a known image permalink locally. The media downloader validates the result."""
    if not web_url(url):
        return ""
    parsed = urlsplit(url)
    host = host_of(url)
    path = parsed.path.rstrip("/")
    if host in {"tenor.com", "www.tenor.com"}:
        if re.fullmatch(
            r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?view/(?:[\w%-]+-)?\d{4,22}(?:\.gif)?",
            path,
            re.ASCII | re.IGNORECASE,
        ):
            return f"https://tenor.com{path.removesuffix('.gif')}.gif"
        return ""
    if host in {"giphy.com", "www.giphy.com"}:
        match = re.fullmatch(r"/(?:gifs|stickers)/(?:[\w%-]+-)?([a-zA-Z0-9]{6,64})", path, re.ASCII)
        if match:
            return f"https://media.giphy.com/media/{match[1]}/giphy.gif"
        return ""
    if host == "commons.wikimedia.org":
        file_page = re.fullmatch(
            r"/wiki/File:([^/\\]+\.(?:gif|webp))", unquote(path), re.IGNORECASE
        )
        if file_page:
            return "https://commons.wikimedia.org/wiki/Special:FilePath/" + quote(
                file_page[1], safe=""
            )
        return ""
    return url if gif_candidate(url, related=True) else ""


@dataclass
class _GoogleImage:
    media: str
    source: str = ""
    title: str = ""
    thumb: str = ""
    thumb_id: str = ""
    doc_id: str = ""
    width: int = 0
    height: int = 0


def _valid_id(value):
    return isinstance(value, str) and TOKEN.fullmatch(value) is not None


def _number(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,5}", value):
        value = int(value)
    return size(value)


def _image_link(value):
    if isinstance(value, str) and value.startswith("//"):
        value = "https:" + value
    return web_url(value)


def _thumbnail_link(value):
    if isinstance(value, str) and value.startswith("/images?"):
        value = urljoin("https://www.google.com/", value)
    url = _image_link(value)
    return url if url and not site_icon_url(url) else ""


class GoogleHTML(HTMLParser):
    """Extract the result card's source, its nested thumbnail and its per-image IDs together."""

    _void_tags = frozenset("area base br col embed hr img input link meta param source track wbr".split())

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ref_for_image = {}
        self.ref_for_page = {}
        self.links = []
        self.permalinks = {}
        self.next_page = False
        self.anchor = None
        self._next_anchor_page = ""
        self._card_stack = []
        self._legacy_meta = False
        self.account_link = False
        self.page_text = []
        self._text_left = 4096
        self._ignored_access_tags = []

    def _permalink(self, page, thumb_id="", doc_id=""):
        if page not in self.permalinks:
            media = gif_from_publisher(page)
            if not media:
                return None
            self.permalinks[page] = _GoogleImage(media, source=page)
        record = self.permalinks[page]
        if _valid_id(thumb_id) and _valid_id(doc_id):
            record.thumb_id, record.doc_id = thumb_id, doc_id
        return record

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        # The account prompt must belong to the page itself. Header sign-in links,
        # quoted result text and inactive templates cannot establish a requirement.
        if tag not in self._void_tags:
            if self._ignored_access_tags:
                if tag == self._ignored_access_tags[-1]:
                    self._ignored_access_tags.append(tag)
            elif (
                tag in {"script", "style", "template", "nav", "header"}
                or a.get("role") in {"navigation", "banner"}
                or "hidden" in a or a.get("aria-hidden") == "true"
                or any(key in a for key in ("data-docid", "data-tbnid", "data-iurl", "data-ou", "data-lpage"))
            ):
                self._ignored_access_tags.append(tag)
        # Google names the thumbnail's own ID data-docid and the publisher's ID data-ref-docid.
        thumb_id = a.get("data-tbnid") or a.get("data-docid") or a.get("data-id") or ""
        ref_id = (
            a.get("data-ref-docid") or (a.get("data-docid") if a.get("data-tbnid") else "") or ""
        )
        source = _image_link(a.get("data-lpage"))
        if _valid_id(ref_id):
            if _valid_id(thumb_id):
                self.ref_for_image[thumb_id] = ref_id
            if source:
                self.ref_for_page[source] = ref_id
        card = self._permalink(source, thumb_id, ref_id) if source else None
        if tag == "div":
            self._card_stack.append(
                card if source or not self._card_stack else self._card_stack[-1]
            )
        if tag == "div" and "rg_meta" in (a.get("class") or "").split():
            self._legacy_meta = True
        if a.get("id") == "pnnext" or a.get("rel") == "next":
            self.next_page = True
        if tag == "a":
            self.anchor = None
            self._next_anchor_page = ""
            href = a.get("href") or ""
            try:
                parsed = urlsplit(urljoin("https://www.google.com/", href))
                params = parse_qs(parsed.query, max_num_fields=150)
            except ValueError:
                return
            if parsed.scheme == "https" and parsed.hostname == "accounts.google.com":
                self.account_link = True

            def first(name):
                return params.get(name, [""])[0]

            if (
                parsed.hostname in {"google.com", "www.google.com"}
                and parsed.path == "/imgres"
                and first("imgurl")
            ):
                self.anchor = _GoogleImage(
                    _image_link(first("imgurl")),
                    _image_link(first("imgrefurl")),
                    a.get("aria-label") or a.get("title") or "",
                    thumb=_thumbnail_link(first("tbnurl")),
                    thumb_id=first("tbnid") or thumb_id,
                    doc_id=first("docid") or ref_id,
                    width=_number(first("w")),
                    height=_number(first("h")),
                )
                self.links.append(self.anchor)
            elif parsed.hostname in {"google.com", "www.google.com"} and parsed.path == "/url":
                self._next_anchor_page = _image_link(first("q") or first("url"))
            else:
                self._next_anchor_page = _image_link(href)
        if a.get("data-iurl") or a.get("data-ou"):
            record = _GoogleImage(
                _image_link(a.get("data-iurl") or a.get("data-ou")), source, a.get("alt") or "",
                _thumbnail_link(a.get("data-tu") or a.get("data-thumb") or a.get("data-src")),
                thumb_id, ref_id, _number(a.get("data-ow")), _number(a.get("data-oh")),
            )
            self.links.append(record)
            if tag == "div":
                self._card_stack[-1] = record
        if tag in ("img", "source", "video"):
            record = self.anchor or (
                self._permalink(self._next_anchor_page) if self._next_anchor_page else None
            )
            if record is None and self._card_stack:
                record = self._card_stack[-1]
            alt = a.get("alt") or ""
            if record and not re.fullmatch(r"favicon|(?:site|website|publisher|source) (?:icon|logo)", alt.strip(), re.I):
                record.title = record.title or alt
                sources = [a.get(attr) for attr in ("data-src","data-thumb","data-deferred-src","poster")]
                if tag == "img":
                    sources.append(a.get("src"))
                for attr in ("data-srcset", "srcset"):
                    for entry in (a.get(attr) or "")[:8192].split(",")[:6]:
                        if candidate := entry.split():
                            sources.append(candidate[0])
                for source_url in sources:
                    thumb = _thumbnail_link(source_url)
                    if thumb and thumb != record.media:
                        record.thumb = record.thumb or thumb
                        break

    def handle_data(self, data):
        if self._text_left > 0 and not self._ignored_access_tags and not self._legacy_meta:
            words = data.strip()[:self._text_left]
            if words:
                self.page_text.append(words)
                self._text_left -= len(words) + 1
        if self._legacy_meta:
            try:
                meta = json.loads(data)
            except (ValueError, RecursionError):
                return
            if isinstance(meta, dict) and _image_link(meta.get("ou")):
                self.links.append(
                    _GoogleImage(
                        meta["ou"],
                        _image_link(meta.get("ru")),
                        meta.get("pt") or "",
                        _image_link(meta.get("tu")),
                        meta.get("id") or "",
                        meta.get("docid") or "",
                        _number(meta.get("ow")),
                        _number(meta.get("oh")),
                    )
                )

    def handle_endtag(self, tag):
        if self._ignored_access_tags and self._ignored_access_tags[-1] == tag:
            self._ignored_access_tags.pop()
        if tag == "a":
            self.anchor = None
            self._next_anchor_page = ""
        elif tag == "div":
            if self._card_stack:
                self._card_stack.pop()
            self._legacy_meta = False


# Embedded image records can use a data URI, a protocol-relative URL, or null for the thumbnail.
RECORD = re.compile(
    r'\[\s*(?:null|\d+)\s*,\s*"[\w:=-]{2,256}"\s*,\s*(?:\[\s*"(?:https?:|//|data:image/|/images\?)|null\s*,\s*\[\s*"https?:)',
    re.ASCII,
)
JSON_DECODER = json.JSONDecoder()
ESCAPE = re.compile(r"""\\(?:x([\da-f]{2})|u00([\da-f]{2})|(["'\\/]))""", re.IGNORECASE)


def _document_texts(markup):
    yield markup
    if "&quot;" in markup and '"2003"' not in markup:
        markup = unescape(markup)
        yield markup
    if any(delimiter in markup for delimiter in (r"\"", r"\x22", r"\u0022")):
        # Unwrap one JavaScript-string/JSON-string layer without evaluating script.
        yield ESCAPE.sub(lambda m: m[3] or chr(int(m[1] or m[2], 16)), markup)


def _embedded_images(markup, parser):
    for text in _document_texts(markup):
        for match in islice(RECORD.finditer(text), 300):
            try:
                record, _ = JSON_DECODER.raw_decode(text, match.start())
                meta = next(
                    (
                        v["2003"]
                        for v in record[4:]
                        if isinstance(v, dict) and isinstance(v.get("2003"), list)
                    ),
                    [],
                )
                original, h, w = record[3][:3]
                thumb = record[2][0] if isinstance(record[2], list) and record[2] else ""
                image_id = record[1]
                source = _image_link(meta[2]) if len(meta) > 2 else ""
                title = meta[3] if len(meta) > 3 else ""
                ref_id = parser.ref_for_image.get(image_id)
                if not ref_id and len(meta) > 1 and _valid_id(meta[1]):
                    ref_id = meta[1]
                yield _GoogleImage(
                    _image_link(original),
                    source,
                    title,
                    _thumbnail_link(thumb),
                    image_id,
                    ref_id or parser.ref_for_page.get(source, ""),
                    _number(w),
                    _number(h),
                )
            except (ValueError, TypeError, KeyError, IndexError, RecursionError):
                continue


def parse_google_public(body, query, page=0, related=False):
    markup = body.decode("utf-8", "replace")
    lower = markup.lower()
    parser = GoogleHTML()
    parser.feed(markup)
    parser.close()
    if parser.account_link:
        text = " ".join(" ".join(parser.page_text).casefold().replace("’", "'").split())
        if "sign in to continue" in text and re.search(r"sign in to (?:verify|confirm|prove) (?:that )?you(?:'re| are) (?:a )?human\b", text):
            raise GoogleSignInRequired()
    if ("consent.google.com" in lower and "before you continue" in lower) or any(
        marker in lower for marker in ("/sorry/index", "detected unusual traffic", "g-recaptcha")
    ):
        raise Problem("Google requested verification or consent in the browser.")
    items = {}
    represented_pages = set()
    parsed_count = 0

    def add(rec):
        nonlocal parsed_count
        if rec.media:
            parsed_count += 1
        if not gif_candidate(rec.media, related):
            return
        seed = (
            f"{rec.thumb_id}.{rec.doc_id}"
            if _valid_id(rec.thumb_id) and _valid_id(rec.doc_id)
            else ""
        )
        current = items.get(rec.media)
        thumb = _thumbnail_link(rec.thumb) if rec.thumb != rec.media else ""
        if current is not None and (not seed or current.seed):
            if thumb and not current.thumb:
                items[rec.media] = replace(current, thumb=thumb)
            return
        fallback = unquote(urlsplit(rec.source or rec.media).path).rstrip("/").rsplit("/", 1)[-1]
        title = (
            label(rec.title)
            or label(fallback.replace("-", " ").replace("_", " "))
            or host_of(rec.media)
        )
        items[rec.media] = Item(
            title,
            rec.media,
            thumb or (current.thumb if current else ""),
            rec.source,
            GOOGLE,
            seed,
            width=rec.width,
            height=rec.height,
        )
        if rec.source:
            represented_pages.add(rec.source)

    for record in _embedded_images(markup, parser):
        add(record)
    for record in parser.links:
        add(record)
    for source, record in parser.permalinks.items():
        if source not in represented_pages:
            add(record)
    explicit_empty = any(
        s in lower for s in ("did not match any", "no results found", "couldn't find any results")
    )
    if not items and not explicit_empty and (not related or not parsed_count):
        if "/httpservice/retry/enablejs" in lower or "enable javascript" in lower:
            raise Problem("Google requires a browser to render this search.")
        raise Problem("Google's public search page did not include usable animation links.")
    return Page(
        list(items.values()),
        page + 1 if not related and page < 9 and (items or parser.next_page) else None,
    )


def google_public(query, page, seed):
    if seed:
        fields = seed.split(".", 1)
        if len(fields) != 2 or not all(TOKEN.fullmatch(x) for x in fields):
            raise Problem("This image has no public related panel.")
        url = endpoint(
            "https://www.google.com/async/imgv",
            udm=2,
            tbnid=fields[0],
            docid=fields[1],
            q=query,
            hl="en",
            gl="us",
            **{"async": "_fmt:prog"},
        )
    else:
        url = (
            website(GOOGLE, query) + "&hl=en" + (f"&ijn={page}&start={page * 100}" if page else "")
        )
    try:
        data, _ = fetch(url, 5 * 1024**2, hosts={"www.google.com"}, accept="text/html,*/*")
    except HTTPProblem as e:
        if e.code in (403, 429):
            raise Problem(
                "Google limited the direct search request. A browser session may require verification."
            ) from None
        raise
    return parse_google_public(data, query, page, bool(seed))


def commons(query, offset):
    url = endpoint(
        "https://commons.wikimedia.org/w/api.php",
        action="query",
        format="json",
        formatversion=2,
        generator="search",
        gsrnamespace=6,
        gsrlimit=24,
        gsroffset=offset,
        gsrsearch=query + ' filemime:"image/gif"',
        prop="imageinfo",
        iiprop="url|mime|size",
        iiurlwidth=320,
    )
    data = read_json(url, "commons.wikimedia.org")
    if "error" in data:
        raise Problem("Wikimedia could not complete the search.")
    rows = sorted(data.get("query", {}).get("pages", []), key=lambda row: row.get("index", 0))
    items = []
    for row in rows:
        info = (row.get("imageinfo") or [{}])[0]
        media = web_url(info.get("url"))
        if info.get("mime") == "image/gif" and media:
            title = row.get("title", "").removeprefix("File:")
            items.append(
                Item(
                    label(title.rsplit(".", 1)[0].replace("_", " ")),
                    media,
                    web_url(info.get("thumburl")),
                    web_url(info.get("descriptionurl")),
                    COMMONS,
                    width=size(info.get("width")),
                    height=size(info.get("height")),
                )
            )
    nxt = data.get("continue", {}).get("gsroffset")
    return Page(items, nxt if isinstance(nxt, int) and nxt > offset else None)


def klipy_rendition(variants, sizes, formats):
    for scale in sizes:
        renditions = variants.get(scale)
        if not isinstance(renditions, dict):
            continue
        for fmt in formats:
            v = renditions.get(fmt)
            if isinstance(v, str):
                v = {"url": v}
            if not isinstance(v, dict):
                continue
            url = web_url(v.get("url"))
            if url and host_of(url) in KLIPY_CDN:
                return url, size(v.get("width")), size(v.get("height"))
    return "", 0, 0


def klipy_items(rows):
    items = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("file"), dict):
            continue
        variants = row["file"]
        media, w, h = klipy_rendition(variants, ("hd", "md", "sm", "xs"), ("gif", "webp"))
        thumb, _, _ = klipy_rendition(variants, ("sm", "xs", "md", "hd"), ("jpg", "webp", "gif"))
        slug = row.get("slug")
        if not media or media in seen or not isinstance(slug, str) or slug.strip() in ("", ".", ".."):
            continue
        seen.add(media)
        items.append(
            Item(
                label(row.get("title")) or "KLIPY GIF", media, thumb,
                "https://klipy.com/gifs/" + quote(slug, safe=""), KLIPY, width=w, height=h,
            )
        )
    return items


def klipy(query, page, key, customer):
    if not key:
        if page != 0:
            return Page()
        rows, has_more = klipy_web.search(query)
        return Page(klipy_items(rows), klipy_web.WEBSITE_MORE if has_more else None)
    url = endpoint(
        "https://api.klipy.com/api/v1/" + quote(key, safe="") + "/gifs/search",
        q=query,
        page=page + 1,
        per_page=24,
        customer_id=customer,
        locale="us",
        content_filter="medium",
    )
    reply = read_json(url, "api.klipy.com")
    if not reply.get("result"):
        raise Problem("KLIPY could not complete the search. Check your app key.")
    data = reply.get("data", {})
    return Page(klipy_items(data.get("data", [])), page + 1 if data.get("has_next") else None)


def clips_website(query, source=YARN):
    if source == CLIP_CAFE:
        return clipcafe.website(query)
    return endpoint("https://yarn.co/yarn-find", text=query, p=0)


def clip_link(url):
    return yarn_link(url) or clipcafe.clip_url(url)


def yarn_link(url):
    return yarn_index.clip_link(url)


class YarnHTML(HTMLParser):
    def __init__(self, query="", page=0):
        super().__init__()
        self.query = query
        self.page = page
        self.current = None
        self.items = {}
        self.has_next = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a":
            self.handle_endtag("a")
            try:
                url = urljoin("https://yarn.co/yarn-find", a.get("href") or "")
            except ValueError:
                return
            if yarn_link(url):
                self.current = [url.split("?")[0].split("#")[0], [a.get("title") or ""], ""]
            if web_url(url) and host_of(url) in YARN_HOSTS and urlsplit(url).path == "/yarn-find":
                params = parse_qs(urlsplit(url).query)
                next_link = (
                    params.get("p") == [str(self.page + 1)]
                    or "next" in (a.get("rel") or "").casefold().split()
                )
                if next_link and params.get("text", [self.query]) == [self.query]:
                    self.has_next = True
        if self.current and tag in ("img", "video"):
            self.current[1].append(a.get("alt") or "")
            src = a.get("poster") if tag == "video" else a.get("data-src") or a.get("src")
            if src:
                try:
                    self.current[2] = web_url(urljoin("https://yarn.co/", src))
                except ValueError:
                    pass

    def handle_data(self, text):
        if self.current:
            self.current[1].append(text)

    def handle_endtag(self, tag):
        if tag == "a" and self.current:
            url, words, pic = self.current
            self.current = None
            old = self.items.get(url)
            title = label(" ".join(words)) or (old.title if old else "Yarn clip")
            self.items[url] = Item(
                title, "", pic or (old.thumb if old else ""), url, YARN, kind="clip"
            )


def _yarn_page(query, page, serp_key):
    if serp_key:
        data = read_json(
            endpoint(
                "https://serpapi.com/search.json",
                engine="google",
                hl="en",
                safe="active",
                q=f"(site:yarn.co/yarn-clip/ OR site:getyarn.io/yarn-clip/) {query}",
                start=page * 10,
                api_key=serp_key,
            ),
            "serpapi.com",
        )
        if data.get("error") and "results" not in str(data["error"]).lower():
            raise Problem("SerpApi could not search Yarn. Check the key or open Yarn directly.")
        items = [
            Item(
                label(row.get("title")) or "Yarn clip",
                "",
                web_url(row.get("thumbnail")),
                row["link"],
                YARN,
                kind="clip",
            )
            for row in data.get("organic_results", [])
            if isinstance(row, dict) and yarn_link(row.get("link"))
        ]
        return Page(items, page + 1 if data.get("serpapi_pagination", {}).get("next") else None)
    try:
        raw, _ = fetch(
            endpoint("https://yarn.co/yarn-find", text=query, p=page),
            2 * 1024**2,
            hosts=YARN_HOSTS,
            accept="text/html",
        )
    except HTTPProblem as e:
        if e.code in (403, 429):
            raise Problem(
                "Yarn requires a browser on this connection. Choose Open search on website or add a SerpApi key to find indexed clips."
            ) from None
        raise
    parser = YarnHTML(query, page)
    parser.feed(raw.decode("utf-8", "replace"))
    parser.close()
    if not parser.items:
        raise Problem("Yarn returned no readable clip links. Choose Open search on website to look up this quote.")
    return Page(list(parser.items.values())[:48], page + 1 if parser.has_next else None)


def yarn(query, page, serp_key):
    if page < 0:
        return clipcafe.search(query, -page)
    try:
        primary = _yarn_page(query, page, serp_key)
        if primary.items or page > 0:
            return primary
    except Problem:
        if page > 0:
            raise
    # A challenge, a JavaScript-only page, or no indexed clips can all leave Yarn
    # without shareable results. Fetch the same quote from an independent catalogue.
    try:
        return clipcafe.search(query, 1)
    except Problem as exc:
        raise Problem(
            f"Yarn supplied no clips. Clip.Cafe could not finish the search. {exc}"
        ) from exc


def search_clips(source, query, page, key):
    if source == CLIPS:
        return yarn(query, page, key)
    if source == CLIP_CAFE:
        return clipcafe.search(query, -page if page < 0 else page + 1)
    if source != YARN:
        raise Problem("Unknown clip source.")
    if page < 0:
        return yarn_index.search(query, -page)
    try:
        direct = _yarn_page(query, page, key)
        if direct.items or page:
            return direct
    except Problem as error:
        if page:
            raise
        reason = str(error)
    else:
        reason = "Yarn returned no clip links."
    try:
        return yarn_index.search(query)
    except Problem as error:
        raise Problem(
            f"{reason} The public index could not finish this search. "
            "Choose Open search on website to search Yarn, or select Clip.Cafe."
        ) from error


class Browse:
    """One original seed per expansion. Alternate related panels and ordinary result pages."""

    def __init__(self):
        self.next_page = 0
        self.seeds = deque()
        self.used = set()
        self.known = set()
        self.seen_media = set()
        self.panels_since_page = 0
        self.branch_seed = ""

    def next_request(self, google):
        if self.branch_seed:
            return (self.next_page, self.branch_seed) if self.next_page is not None else None
        if google and self.seeds and (self.next_page is None or self.panels_since_page < 3):
            return (0, self.seeds[0])
        return (self.next_page, "") if self.next_page is not None else None

    def accept(self, page, seed=""):
        if self.branch_seed:
            if seed != self.branch_seed:
                return []
            self.next_page = page.more
        elif seed:
            self.used.add(seed)
            if seed in self.seeds:
                self.seeds.remove(seed)
            self.panels_since_page += 1
        else:
            self.next_page = page.more
            self.panels_since_page = 0
            for item in page.items:
                if item.seed and item.seed not in self.known and item.seed not in self.used:
                    self.seeds.append(item.seed)
                    self.known.add(item.seed)
        fresh = []
        for item in page.items:
            if item.key not in self.seen_media:
                fresh.append(item)
                self.seen_media.add(item.key)
        return fresh
