"""Google Images addresses for a search and for the exact selected image."""

from hashlib import sha256
from urllib.parse import urlsplit

from .net import web_url
from .providers import TOKEN, endpoint


def thumbnail_host(url):
    """Recognize public Google image CDN thumbnails for the small-preview queue."""
    if not web_url(url):
        return ""
    parsed = urlsplit(url)
    host = parsed.hostname
    if host in {"gstatic.com", "ggpht.com"} or host.endswith((".gstatic.com", ".ggpht.com")):
        return host
    if host.endswith(".googleusercontent.com") and "/proxy/" in parsed.path:
        return host
    return ""


def account_signin_url(url):
    if not web_url(url):
        return False
    parts = urlsplit(url)
    path = parts.path.lower().rstrip("/")
    return parts.hostname == "accounts.google.com" and (
        path in {"/servicelogin", "/interactivelogin", "/accountchooser", "/signin", "/v2/signin", "/v3/signin"}
        or path.startswith(("/signin/", "/v2/signin/", "/v3/signin/"))
    )


def cache_key(query, *, references=False, seed=None):
    prefix = "google-reference-v1:" if references else "google-browser-v1:"
    if seed:
        prefix += f"selected-panel-v2:{seed.panel_identity}:"
    return prefix + sha256(query.encode("utf-8")).hexdigest()


def selected_image_url(query, seed):
    """Reopen a cached image when its thumbnail is no longer mounted."""
    parts = seed.seed.split(".", 1)
    params = {"q": query}
    if len(parts) == 2 and all(TOKEN.fullmatch(x) for x in parts):
        params.update(tbnid=parts[0], docid=parts[1])
    if web_url(seed.media):
        params["imgurl"] = seed.media
    if "tbnid" not in params and "imgurl" not in params:
        return ""
    if web_url(seed.page):
        params["imgrefurl"] = seed.page
    return endpoint("https://www.google.com/imgres", **params)


def search_url(query, *, references=False, seed=None):
    if seed is not None and (url := selected_image_url(query, seed)):
        return url
    params = {"udm": 2, "safe": "active", "q": query}
    if not references:
        params["tbs"] = "itp:animated"
    return endpoint("https://www.google.com/search", **params)
