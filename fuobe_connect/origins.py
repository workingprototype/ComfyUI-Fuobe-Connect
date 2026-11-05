"""Which websites Fuobe Connect trusts, and the paths they may use.

Pure (no ComfyUI imports), so the tests load it on its own.

A trusted site is a Fuobe studio: https://fuobe.com (and https://www.fuobe.com) unless you name others, or none at all.
Only an exact origin counts (scheme, host and port), and only https, except a studio on this computer (http://localhost
or http://127.0.0.1, for development). A trusted site may use only the paths a Fuobe run needs (`allowed`); the
middleware then checks what each request asks for (middleware.py). It never reaches the Manager's install calls, your
user files or your settings: the middleware turns away every other request of a trusted site itself, even when
ComfyUI's own check is off (--enable-cors-header).
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qs, urlsplit

DEFAULT_STUDIOS = ("https://fuobe.com",)
# What the person types to trust no website (pairing still works).
NONE = "none"

# A site that is also reached under "www.".
_WWW_TWINS = {"fuobe.com"}

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalise(origin: str | None) -> str | None:
    """'https://Fuobe.com/' -> 'https://fuobe.com'. None for anything that isn't an http(s) origin."""
    if not origin or not isinstance(origin, str):
        return None
    text = origin.strip()
    if len(text) > 300:
        return None
    try:
        u = urlsplit(text)
        port = u.port
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    if u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
        return None
    host = u.hostname.lower().rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port != _DEFAULT_PORTS[u.scheme]:
        return f"{u.scheme}://{host}:{port}"
    return f"{u.scheme}://{host}"


def is_loopback(host: str | None) -> bool:
    """127.0.0.1, ::1 or localhost (a host name, without a port)."""
    if not host:
        return False
    h = host.strip().lower().strip("[]").rstrip(".")
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def is_ip(host: str | None) -> bool:
    try:
        ipaddress.ip_address((host or "").strip().strip("[]"))
        return True
    except ValueError:
        return False


def host_name(host_header: str | None) -> str | None:
    """The host of a Host header ('127.0.0.1:8188' -> '127.0.0.1', '[::1]:8188' -> '::1')."""
    try:
        return urlsplit("//" + (host_header or "").strip()).hostname
    except ValueError:
        return None


def studio(origin: str | None) -> str | None:
    """A studio's origin: https, or http only on this computer (a studio in development). None for anything else."""
    o = normalise(origin)
    if not o:
        return None
    u = urlsplit(o)
    if u.scheme == "http" and not is_loopback(u.hostname):
        return None
    return o


def parse_list(text: str | list | tuple | None) -> list[str]:
    """A comma- or space-separated list (or a list) of studios: the valid ones, in order, without repeats."""
    if text is None:
        return []
    items = text if isinstance(text, (list, tuple)) else re.split(r"[\s,]+", str(text))
    out: list[str] = []
    for item in items:
        o = studio(item) if isinstance(item, str) else None
        if o and o not in out:
            out.append(o)
    return out


def says_none(text) -> bool:
    """The person's 'none': trust no website."""
    if isinstance(text, (list, tuple)):
        return len(text) == 0 or (len(text) == 1 and says_none(text[0]))
    return isinstance(text, str) and text.strip().lower() == NONE


def with_twins(origins: list[str]) -> list[str]:
    out = list(origins)
    for o in origins:
        u = urlsplit(o)
        if u.hostname in _WWW_TWINS:
            twin = f"{u.scheme}://www.{u.hostname}" + (f":{u.port}" if u.port else "")
            if twin not in out:
                out.append(twin)
    return out


def trusted_origins(env_value: str | None, configured: str | list | None) -> tuple[str, ...]:
    """FUOBE_CONNECT_ORIGINS when set, else the configured studios, else https://fuobe.com. 'none' (or an empty saved
    list) in either: no website."""
    for source in (env_value, configured):
        if source is None or (isinstance(source, str) and not source.strip()):
            continue
        if says_none(source):
            return ()
        found = parse_list(source)
        if found:
            return tuple(with_twins(found))
    return tuple(with_twins(list(DEFAULT_STUDIOS)))


# ---------------------------------------------------------------------------
# What a trusted site may use (ComfyUI answers each path with and without "/api" in front). The middleware checks the
# body or the answer of several of these (middleware.py).

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# A model folder's list. ComfyUI also answers for folders that hold no models: custom_nodes (every file of every node
# pack) and configs.
_MODELS = r"models(?:/(?!(?:custom_nodes|configs)\Z)[A-Za-z0-9_.-]{1,64})?"
_GET = re.compile(
    r"/(?:"
    rf"system_stats|object_info(?:/[^/]{{1,128}})?|{_MODELS}|embeddings|queue|features"
    rf"|history(?:/{_UUID})?|jobs(?:/{_UUID})?|view|ws"
    rf"|experiment/{_MODELS}"
    r"|(?:v2/)?manager/(?:version|queue/status)|(?:v2/)?customnode/installed"
    r"|fuobe/connect|fuobe/guard|fuobe/live"
    r")"
)
_POST = re.compile(
    rf"/(?:upload/image|prompt|queue|history|interrupt|jobs/{_UUID}/cancel|(?:v2/)?customnode/import_fail_info(?:_bulk)?)"
)
_TAG = re.compile(r" \[[a-z]+\]\Z")


def _bare(path: str) -> str:
    return path[4:] if path.startswith("/api/") else path


def view_ok(query: str) -> bool:
    """A /view of a result: output or temp, a plain file name (no folder tag, no asset name), no way out of the folder."""
    q = parse_qs(query, keep_blank_values=True)
    if set(q) - {"filename", "subfolder", "type"} or any(len(v) != 1 for v in q.values()):
        return False
    name = q.get("filename", [""])[0]
    sub = q.get("subfolder", [""])[0]
    if q.get("type", ["output"])[0] not in ("output", "temp"):
        return False
    if not name or name in (".", "..") or re.search(r"[\\/\x00]", name) or _TAG.search(name) or name.lower().startswith("blake3:"):
        return False
    if "\x00" in sub or sub.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", sub) or any(p == ".." for p in re.split(r"[\\/]", sub)):
        return False
    return True


def ws_ok(query: str) -> bool:
    """The live socket, under a Fuobe client id only (never the id of your own ComfyUI tab)."""
    q = parse_qs(query, keep_blank_values=True)
    ids = q.get("clientId", [])
    return set(q) == {"clientId"} and len(ids) == 1 and bool(re.fullmatch(r"fuobe-[A-Za-z0-9_-]{4,72}", ids[0]))


def history_list_ok(query: str) -> bool:
    """/history only as the last few prompts (max_items), the way a run follows a batch."""
    q = parse_qs(query, keep_blank_values=True)
    return set(q) == {"max_items"} and len(q["max_items"]) == 1 and bool(re.fullmatch(r"[0-9]{1,3}", q["max_items"][0]))


def allowed(method: str, path: str, query: str = "") -> bool:
    """May a trusted site send this? (`query`: the raw query string.)"""
    p = _bare(path)
    m = method.upper()
    if m in ("GET", "HEAD"):
        if not _GET.fullmatch(p):
            return False
        if p == "/view":
            return view_ok(query)
        if p == "/ws":
            return ws_ok(query)
        if p == "/history":
            return history_list_ok(query)
        return True
    if m == "POST":
        return bool(_POST.fullmatch(p))
    return False


def _port(scheme: str, port: int | None) -> int | None:
    return port if port is not None else _DEFAULT_PORTS.get(scheme)


def same_host(origin: str, host: str | None) -> bool:
    """The page is ComfyUI's own: the origin's host and port are the Host header's (a missing port is the scheme's
    usual one, so http://localhost is port 80, not ComfyUI's 8188)."""
    try:
        o = urlsplit(origin)
        h = urlsplit("//" + (host or "").strip().lower())
        if o.scheme not in _DEFAULT_PORTS or not o.hostname or not h.hostname or o.hostname.lower() != h.hostname:
            return False
        return _port(o.scheme, o.port) == _port(o.scheme, h.port)
    except ValueError:
        return False
