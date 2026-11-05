"""The two places Fuobe Connect talks to: the paired Fuobe studio (over HTTPS, with its token) and this ComfyUI itself
(on this computer, as a program: no browser headers, so ComfyUI's own checks treat it as it treats any local tool).

Every answer from the studio is read with a size limit, and files go through a temporary file, so ComfyUI's memory use
stays small. Only aiohttp, which ComfyUI already has.
"""

from __future__ import annotations

import hashlib
import io
import json
import ssl
import tempfile
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit

import aiohttp

from . import __version__
from .origins import is_loopback

USER_AGENT = f"FuobeConnect/{__version__}"
# The most a studio's JSON answer may be.
MAX_JSON = 4 * 2**20
# A file is kept in memory up to this size, then on disk.
SPOOL = 16 * 2**20
CHUNK = 2**20


class SiteError(Exception):
    def __init__(self, status: int, message: str = "", body: dict | None = None):
        super().__init__(message or f"HTTP {status}")
        self.status = status
        self.body = body or {}


class TooBig(Exception):
    """An answer larger than it may be."""


@dataclass
class Answer:
    status: int
    body: dict
    headers: dict


def site_base(site: str) -> str:
    """The studio's origin. https only, except a studio on this computer (http://localhost, for development)."""
    try:
        u = urlsplit((site or "").strip())
        u.port  # noqa: B018 (a bad port raises here)
    except ValueError:
        raise ValueError("The studio's address must start with https://")
    if not u.hostname or u.username or u.password or u.scheme not in ("https", "http") or (u.scheme == "http" and not is_loopback(u.hostname)):
        raise ValueError("The studio's address must start with https://")
    return f"{u.scheme}://{u.netloc.lower()}"


def seg(value) -> str:
    """One path segment, quoted: an id can never add a step or a query."""
    return quote(str(value), safe="")


async def read_capped(res: aiohttp.ClientResponse, limit: int) -> bytes:
    length = res.headers.get("Content-Length")
    if length and length.isdigit() and int(length) > limit:
        raise TooBig(f"The answer is {length} bytes, more than {limit}")
    out = bytearray()
    while True:
        chunk = await res.content.read(CHUNK)
        if not chunk:
            return bytes(out)
        out += chunk
        if len(out) > limit:
            raise TooBig(f"The answer is more than {limit} bytes")


async def spool(res: aiohttp.ClientResponse, limit: int):
    """The answer's body in memory, or in a temporary file once it is large, with its size and SHA-256. Raises TooBig
    past `limit`."""
    length = res.headers.get("Content-Length")
    if length and length.isdigit() and int(length) > limit:
        raise TooBig(f"The file is {length} bytes, more than {limit}")
    f = io.BytesIO()
    h = hashlib.sha256()
    size = 0
    try:
        while True:
            chunk = await res.content.read(CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise TooBig(f"The file is more than {limit} bytes")
            h.update(chunk)
            if isinstance(f, io.BytesIO) and size > SPOOL:
                disk = tempfile.TemporaryFile()
                disk.write(f.getvalue())
                f = disk
            f.write(chunk)
    except BaseException:
        f.close()
        raise
    f.seek(0)
    return f, size, h.hexdigest()


class Site:
    """The Fuobe studio. Every call says who is calling (FuobeConnect/<version>); job calls carry the worker token."""

    def __init__(self, session: aiohttp.ClientSession, site: str, token: str | None = None):
        self.session = session
        self.base = site_base(site)
        self.token = token

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"User-Agent": USER_AGENT}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if extra:
            h.update(extra)
        return h

    async def call(self, method: str, path: str, body=None, headers: dict | None = None, data=None, timeout: float = 60) -> Answer:
        kwargs = {"headers": self._headers(headers), "timeout": aiohttp.ClientTimeout(total=timeout), "allow_redirects": False}
        if body is not None:
            kwargs["data"] = json.dumps(body)
            kwargs["headers"]["Content-Type"] = "application/json"
        elif data is not None:
            kwargs["data"] = data
        async with self.session.request(method, self.base + path, **kwargs) as res:
            try:
                text = (await read_capped(res, MAX_JSON)).decode("utf-8", "replace")
                parsed = json.loads(text) if text else {}
            except (ValueError, TooBig):
                parsed = {}
            return Answer(res.status, parsed if isinstance(parsed, dict) else {}, dict(res.headers))

    async def fetch_file(self, path: str, limit: int, headers: dict | None = None):
        """(status, temporary file or None, size, sha256, headers). The file is at most `limit` bytes."""
        async with self.session.get(self.base + path, headers=self._headers(headers), timeout=aiohttp.ClientTimeout(total=600), allow_redirects=False) as res:
            if res.status != 200:
                return res.status, None, 0, "", dict(res.headers)
            f, size, sha = await spool(res, limit)
            return res.status, f, size, sha, dict(res.headers)

    # Pairing (no token yet).
    async def pair_start(self, name: str, facts: dict) -> Answer:
        return await self.call("POST", "/api/gpu/pair/start", {"name": name, "facts": facts}, timeout=20)

    async def pair_poll(self, pair_id: str, secret: str) -> Answer:
        return await self.call("POST", "/api/gpu/pair/poll", {"id": pair_id, "secret": secret}, timeout=20)


class Comfy:
    """This ComfyUI, from inside it: only the calls a run needs, to its own address."""

    def __init__(self, session: aiohttp.ClientSession, base: str):
        self.session = session
        self.base = base.rstrip("/")
        self._ssl = False if base.startswith("https://") else None

    def _kw(self, timeout: float = 30) -> dict:
        kw = {"timeout": aiohttp.ClientTimeout(total=timeout)}
        if self._ssl is False:
            kw["ssl"] = False
        return kw

    async def get_json(self, path: str, timeout: float = 30):
        async with self.session.get(self.base + path, **self._kw(timeout)) as res:
            if res.status != 200:
                raise SiteError(res.status, f"ComfyUI {path.split('?')[0]} answered HTTP {res.status}")
            return await res.json(content_type=None)

    async def get_status(self, path: str) -> tuple[int, object]:
        async with self.session.get(self.base + path, **self._kw()) as res:
            body = await res.json(content_type=None) if res.status == 200 else None
            return res.status, body

    async def post_json(self, path: str, body: dict, timeout: float = 60) -> tuple[int, dict]:
        async with self.session.post(self.base + path, data=json.dumps(body), headers={"Content-Type": "application/json"}, **self._kw(timeout)) as res:
            text = await res.text()
            try:
                parsed = json.loads(text) if text else {}
            except ValueError:
                parsed = {}
            return res.status, parsed if isinstance(parsed, dict) else {}

    async def upload_input(self, name: str, data, content_type: str) -> int:
        """`data`: bytes or an open file. Always into the input folder, under the run's own name."""
        form = aiohttp.FormData()
        form.add_field("image", data, filename=name, content_type=content_type or "application/octet-stream")
        form.add_field("type", "input")
        form.add_field("overwrite", "true")
        async with self.session.post(self.base + "/upload/image", data=form, **self._kw(600)) as res:
            await res.read()
            return res.status

    async def view(self, filename: str, subfolder: str, kind: str, limit: int = 64 * 2**30):
        """A result in a temporary file (memory, then disk) and its size, or (None, 0)."""
        q = urlencode({"filename": filename, "subfolder": subfolder, "type": kind})
        async with self.session.get(f"{self.base}/view?{q}", **self._kw(600)) as res:
            if res.status != 200:
                return None, 0
            f, size, _ = await spool(res, limit)
            return f, size

    def ws_url(self, client_id: str) -> str:
        return self.base.replace("http", "ws", 1) + "/ws?" + urlencode({"clientId": client_id})


def loopback_base(listen: str | None, port: int, tls: bool) -> str:
    """Where this ComfyUI answers on this computer, from its --listen and --port."""
    hosts = [h.strip() for h in (listen or "127.0.0.1").split(",") if h.strip()]
    pick = "127.0.0.1"
    for h in hosts:
        if h in ("127.0.0.1", "localhost"):
            pick = "127.0.0.1"
            break
        if h == "::1":
            pick = "[::1]"
            break
    else:
        if hosts and hosts[0] not in ("0.0.0.0", "::", ""):
            pick = f"[{hosts[0]}]" if ":" in hosts[0] else hosts[0]
    return f"{'https' if tls else 'http'}://{pick}:{int(port)}"


def ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()
