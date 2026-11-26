"""Small HTTPS client for public media. Connect to the exact address we checked."""

import base64
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import getproxies, proxy_bypass

from . import __version__


class Problem(Exception):
    """A short message suitable for the picker."""


class HTTPProblem(Problem):
    def __init__(self, code):
        self.code = code
        super().__init__(
            {
                401: "The service rejected the API key.",
                403: "The service declined this request.",
                404: "The requested item is no longer available.",
                429: "The service has temporarily limited requests.",
            }.get(code, f"The service returned HTTP {code}.")
        )


def public_ip(value):
    addr = ipaddress.ip_address(value)
    if addr.version == 6 and (
        addr.ipv4_mapped
        or addr.sixtofour
        or addr.teredo
        or addr in ipaddress.ip_network("64:ff9b::/96")
    ):
        return False
    return addr.is_global and not (
        addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        or getattr(addr, "is_site_local", False)
    )


def parts(url):
    if not isinstance(url, str) or len(url) > 8192 or re.search(r"[\x00-\x20\x7f\\]", url):
        raise Problem("Invalid web address.")
    try:
        p = urlsplit(url)
        if (
            p.scheme != "https"
            or not p.hostname
            or p.username is not None
            or p.password is not None
            or p.port not in (None, 443)
        ):
            raise ValueError
        host = p.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if not host or "%" in host or len(host) > 253:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            ):
                raise ValueError
            if host.endswith(
                (".local", ".localhost", ".internal", ".home.arpa", ".test", ".onion")
            ):
                raise ValueError
        else:
            if not public_ip(str(address)):
                raise ValueError
    except (ValueError, UnicodeError):
        raise Problem("Only public HTTPS addresses on port 443 are accepted.") from None
    return p, host


def web_url(value):
    try:
        parts(value)
        return value
    except Problem:
        return ""


def host_of(url):
    return parts(url)[1] if url else ""


TLS = ssl.create_default_context()
UA = f"ViberHive/{__version__} (personal desktop image search; Python)"

# A forward proxy resolves its own destinations. Never delegate DNS for arbitrary
# publisher URLs returned by a search, including URLs reached via a redirect.
PROXY_DESTINATIONS = frozenset(
    {
        "www.google.com",
        "serpapi.com",
        "commons.wikimedia.org",
        "upload.wikimedia.org",
        "klipy.com",
        "www.klipy.com",
        "api.klipy.com",
        "static.klipy.com",
        "static1.klipy.com",
        "static2.klipy.com",
        "tenor.com",
        "www.tenor.com",
        "media.tenor.com",
        "media1.tenor.com",
        "media.giphy.com",
        *(f"media{i}.giphy.com" for i in range(5)),
        *(f"encrypted-tbn{i}.gstatic.com" for i in range(4)),
        "clip.cafe",
        "search.brave.com",
        "imgs.search.brave.com",
        "memes.yarn.co",
        "memes.getyarn.io",
        "tv.getyarn.io",
        "trailers.getyarn.io",
        "y.yarn.co",
        "y.getyarn.io",
        "yarn.co",
        "www.yarn.co",
        "getyarn.io",
        "www.getyarn.io",
    }
)


def provider_proxy(host):
    if host not in PROXY_DESTINATIONS:
        return None
    address = getproxies().get("https")
    if not address or proxy_bypass(host) or proxy_bypass(host + ":443"):
        return None
    try:
        if re.search(r"[\x00-\x20\x7f\\]", address):
            raise ValueError
        proxy = urlsplit(address if "://" in address else "http://" + address)
        if not proxy.hostname or proxy.path not in ("", "/") or proxy.query or proxy.fragment:
            raise ValueError
        if proxy.scheme != "http":
            raise Problem(
                "The configured HTTPS proxy must support HTTP CONNECT on an http:// address."
            )
        port = proxy.port if proxy.port is not None else 80
        if not port:
            raise ValueError
        headers = {}
        if proxy.username is not None:
            credentials = unquote(proxy.username) + ":" + unquote(proxy.password or "")
            token = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
            headers["Proxy-Authorization"] = "Basic " + token
        return proxy.hostname, port, headers
    except (UnicodeError, ValueError):
        raise Problem("The configured HTTPS proxy address is invalid.") from None


def public_addresses(host):
    records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not records or any(not public_ip(record[4][0]) for record in records):
        raise Problem("The address points to a private or reserved network.")
    return list(dict.fromkeys(record[4][0] for record in records))


def fetch(url, limit, *, hosts=None, accept="*/*", deadline=16, redirects=3, on_progress=None):
    end = time.monotonic() + deadline
    for step in range(redirects + 1):
        p, host = parts(url)
        if hosts is not None and host not in hosts:
            raise Problem("The service redirected to an unexpected host.")

        def remaining():
            seconds = end - time.monotonic()
            if seconds <= 0:
                raise TimeoutError
            return min(6, seconds)

        conn = None
        try:
            proxy = provider_proxy(host)
            if proxy:
                proxy_host, port, headers = proxy
                conn = http.client.HTTPSConnection(
                    proxy_host, port, timeout=remaining(), context=TLS
                )
                conn.set_tunnel(host, 443, headers=headers)
                conn.connect()  # CONNECT first, then verify TLS against the destination hostname.
            else:
                conn = http.client.HTTPSConnection(host, timeout=remaining(), context=TLS)
                sock = None
                for ip in public_addresses(host):
                    try:
                        sock = socket.create_connection((ip, 443), timeout=remaining())
                        break
                    except OSError:
                        continue
                if sock is None:
                    raise OSError
                try:
                    sock.settimeout(remaining())
                    conn.sock = TLS.wrap_socket(sock, server_hostname=host)
                except BaseException:
                    sock.close()
                    raise
            target = urlunsplit(
                (
                    "",
                    "",
                    quote(p.path or "/", safe="/%:@!$&'()*+,;=~-._"),
                    quote(p.query, safe="%/:@!$&'()*+,;=?~-._"),
                    "",
                )
            )
            conn.sock.settimeout(remaining())
            conn.request(
                "GET",
                target,
                headers={"User-Agent": UA, "Accept": accept, "Accept-Encoding": "identity"},
            )
            # Keep the socket for deadline updates even if HTTPConnection detaches it after reading headers.
            tls_sock = conn.sock
            tls_sock.settimeout(max(0.1, end - time.monotonic()))
            with conn.getresponse() as response:
                if response.status in (301, 302, 303, 307, 308):
                    destination = response.getheader("Location")
                    if step == redirects or not destination:
                        raise Problem("The service returned an unsupported redirect.")
                    url = urljoin(url, destination)
                    continue
                if response.status != 200:
                    raise HTTPProblem(response.status)
                if response.getheader("Content-Encoding", "identity").lower() not in (
                    "",
                    "identity",
                ):
                    raise Problem("Unsupported transfer encoding.")
                length = response.getheader("Content-Length")
                if length and (not length.isdecimal() or len(length) > 12 or int(length) > limit):
                    raise Problem("The response exceeds the download limit.")
                mime = response.getheader("Content-Type", "").split(";")[0].strip().lower()
                data = bytearray()
                while len(data) <= limit and not response.isclosed():
                    tls_sock.settimeout(remaining())
                    chunk = response.read1(min(65536, limit - len(data) + 1))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if on_progress and len(data) < limit and (not length or len(data) < int(length)):
                        if on_progress(data, mime):
                            on_progress = None
                if len(data) > limit or (length and len(data) != int(length)):
                    raise Problem("The download is incomplete or too large.")
                return bytes(data), mime
        except (OSError, http.client.HTTPException):
            raise Problem(
                "Could not complete the secure connection. Check the network or try again."
            ) from None
        finally:
            if conn is not None:
                conn.close()
    raise Problem("Too many redirects.")


def read_json(url, host):
    raw, _ = fetch(
        url, 6 * 1024**2, hosts={host}, accept="application/json", deadline=35, redirects=0
    )
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError, UnicodeError):
        raise Problem("The service returned an unreadable response.") from None
    if not isinstance(value, dict):
        raise Problem("Unexpected search response.")
    return value
