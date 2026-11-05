"""Pairing mode: Fuobe Connect runs your Fuobe jobs on this ComfyUI with no browser tab, after one approval.

For browsers that can't reach this computer from a website (Safari, phones, and other browsers in some setups) and for
PCs with no screen. It runs in its own thread with its own event loop, so ComfyUI's own work is never held up by it.

1. "Connect to Fuobe" (ComfyUI's menu, or FUOBE_CONNECT_PAIR=1 on a PC with no screen) asks the studio for a short code.
   The studio is one you trust (Settings → Fuobe Connect) or https://fuobe.com.
2. You open the studio's /connect page, signed in, and approve that code.
3. This ComfyUI gets a token for you alone, kept in fuobe-connect.json, and runs your jobs (runner.py). The window shows
   the account that approved it (when the studio says) and the last jobs, and can pause them.

"Disconnect" in ComfyUI tells the studio first; the token is forgotten here once the studio has it revoked. Disconnect in
the studio's settings ends it too.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import threading
import time
from collections import deque
from urllib.parse import urlsplit

import aiohttp

from .graph import NodeList
from .net import Comfy, Site, site_base
from .runner import Runner, Unpaired
from .store import Store

log = logging.getLogger("fuobe_connect")

CODE = re.compile(r"^[A-Z0-9]{2,8}(?:-[A-Z0-9]{2,8})?\Z")
NAME = re.compile(r"^[^\x00-\x1f\x7f]{1,48}\Z")
REVOKE_RETRY_S = 60


def computer_name(name=None) -> str:
    """The name this computer has on Fuobe: the one the person typed, else this computer's own name."""
    if isinstance(name, str) and NAME.match(name.strip() or "x") and name.strip():
        return name.strip()
    own = (socket.gethostname() or "")[:48]
    return own if NAME.match(own or "x") and own else "ComfyUI"


def pairing_link(url, base: str) -> str:
    """The studio's approval page: on the studio's own address, https (http only on this computer)."""
    if not isinstance(url, str) or len(url) > 300:
        raise RuntimeError("The studio's answer had no approval link")
    try:
        u = urlsplit(url)
        same = site_base(f"{u.scheme}://{u.netloc}") == base
    except ValueError:
        same = False
    if not same or u.username or u.password:
        raise RuntimeError("The studio's approval link isn't on the studio's own address")
    return url


class Agent:
    def __init__(self, store: Store, comfy_base: str, port: int, default_site: str, nodes: NodeList | None = None, input_dir: str | None = None):
        self.store = store
        self.comfy_base = comfy_base
        self.port = port
        self.default_site = default_site
        self.nodes = nodes or NodeList()
        self.input_dir = input_dir
        self.loop = asyncio.new_event_loop()
        self.state = "unpaired"
        self.message: str | None = None
        self.pairing: dict | None = None
        # The prompts Fuobe Connect queued on this ComfyUI, its jobs' prefixes, and the last jobs (shown in ComfyUI's window).
        self.own: set[str] = set()
        self.prefixes: set[str] = set()
        self.recent: deque = deque(maxlen=20)
        self._runner_task: asyncio.Future | None = None
        self._pair_task: asyncio.Future | None = None
        self._revoke_task: asyncio.Future | None = None
        self._session: aiohttp.ClientSession | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Thread

    def start(self):
        self._thread = threading.Thread(target=self._main, name="fuobe-connect", daemon=True)
        self._thread.start()

    def _main(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._boot())
        self.loop.run_forever()

    async def _boot(self):
        self._session = aiohttp.ClientSession()
        data = self.store.load()
        if data.get("revokePending"):
            self._start_revoke()
        elif data.get("token"):
            self._start_runner()

    def call(self, coro):
        """Run a coroutine on the agent's loop from any thread; returns a concurrent future."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ------------------------------------------------------------------
    # State

    def paused(self) -> bool:
        return bool(self.store.load().get("paused"))

    def status(self) -> dict:
        data = self.store.load()
        out = {"state": self.state, "message": self.message, "paused": bool(data.get("paused")), "jobs": list(self.recent)[::-1]}
        if data.get("token") and not data.get("revokePending"):
            out["paired"] = {"site": data.get("site"), "name": data.get("name"), "account": data.get("account"), "since": data.get("pairedAt")}
        if data.get("revokePending"):
            out["revokePending"] = data.get("site")
        if self.pairing and self.state == "pairing":
            out["pairing"] = {k: self.pairing[k] for k in ("code", "url", "expiresAt", "site")}
        return out

    def _set(self, state: str, message: str | None = None):
        if (state, message) != (self.state, self.message):
            self.state, self.message = state, message
            if message:
                log.info("[Fuobe Connect] %s", message)

    def set_paused(self, paused: bool) -> dict:
        self.store.update(paused=True if paused else None)
        if self.store.load().get("token"):
            self._set("paused" if paused else "connected", "Fuobe jobs are paused on this computer." if paused else "Your Fuobe jobs run on this ComfyUI again.")
        return self.status()

    # ------------------------------------------------------------------
    # Pairing

    async def start_pairing(self, site: str | None = None, name: str | None = None) -> dict:
        base = site_base(site or self.default_site)
        if self.store.load().get("revokePending"):
            raise RuntimeError("This ComfyUI is still telling the studio it disconnected. Try again in a minute.")
        if self._pair_task and not self._pair_task.done():
            self._pair_task.cancel()
        comfy = Comfy(self._session, self.comfy_base)
        facts = {}
        try:
            stats = await comfy.get_json("/system_stats", timeout=10)
            dev = (stats.get("devices") or [{}])[0] or {}
            facts = {k: v for k, v in {"device": dev.get("name"), "vramBytes": dev.get("vram_total"), "version": (stats.get("system") or {}).get("comfyui_version")}.items() if v}
        except Exception:
            pass
        a = await Site(self._session, base).pair_start(computer_name(name), facts)
        if a.status != 200 or not a.body.get("code"):
            raise RuntimeError(str(a.body.get("error") or f"The studio answered HTTP {a.status}")[:200])
        code, pair_id, secret = a.body.get("code"), a.body.get("id"), a.body.get("secret")
        if not (isinstance(code, str) and CODE.match(code) and isinstance(pair_id, str) and len(pair_id) <= 128 and isinstance(secret, str) and len(secret) <= 256):
            raise RuntimeError("The studio's answer wasn't a pairing code")
        url = pairing_link(a.body.get("url"), base)
        poll_ms = a.body.get("pollMs") if isinstance(a.body.get("pollMs"), int) else 2000
        expires = a.body.get("expiresAt") if isinstance(a.body.get("expiresAt"), int) else None
        self.pairing = {"code": code, "url": url, "expiresAt": expires, "site": base}
        self._set("pairing", f"To connect this ComfyUI, open {url} and approve the code {code}.")
        self._pair_task = asyncio.ensure_future(self._poll(base, pair_id, secret, min(max(poll_ms, 1000), 30000), expires))
        return self.status()

    async def _poll(self, base: str, pair_id: str, secret: str, poll_ms: int, expires_at: int | None):
        site = Site(self._session, base)
        until = time.time() + 11 * 60
        while time.time() < until:
            await asyncio.sleep(poll_ms / 1000)
            try:
                a = await site.pair_poll(pair_id, secret)
            except Exception:
                continue
            token = a.body.get("token")
            if a.status == 200 and a.body.get("state") == "approved" and isinstance(token, str) and 16 <= len(token) <= 256:
                account = a.body.get("account") if isinstance(a.body.get("account"), str) else None
                name = a.body.get("name") if isinstance(a.body.get("name"), str) else None
                worker = a.body.get("workerId") if isinstance(a.body.get("workerId"), str) else None
                self.store.update(site=base, token=token, name=(name or "")[:48] or None, account=(account or "")[:80] or None, workerId=(worker or "")[:64] or None, pairedAt=int(time.time() * 1000), paused=None)
                self.pairing = None
                who = f" as {account[:80]}" if account else ""
                self._set("connected", f"Connected to {base}{who}. Your Fuobe jobs now run on this ComfyUI.")
                self._start_runner()
                return
            if a.status == 404 or a.body.get("state") == "gone":
                break
        self.pairing = None
        self._set("unpaired", "The code wasn't approved. Press Connect to Fuobe again for a new one.")

    def cancel_pairing(self):
        if self._pair_task and not self._pair_task.done():
            self._pair_task.cancel()
        self.pairing = None
        if self.state == "pairing":
            self._set("unpaired", None)

    async def _revoke(self, site: str, token: str) -> bool:
        """Tell the studio to revoke the token. True when it is gone there (revoked now, or already unknown)."""
        try:
            a = await Site(self._session, site, token).call("POST", "/api/gpu/computer/unpair", {}, timeout=10)
        except Exception:
            return False
        return a.status in (200, 401, 404)

    async def unpair(self) -> dict:
        """Stop running jobs, tell the studio to revoke the token, and forget it here once it did. If the studio can't be
        reached, the token is kept (unused) and Fuobe Connect tries again every minute."""
        data = self.store.load()
        if self._runner_task and not self._runner_task.done():
            self._runner_task.cancel()
        token, site = data.get("token"), data.get("site")
        if not token or not site:
            self.store.update(token=None, workerId=None, pairedAt=None, account=None, revokePending=None)
            self._set("unpaired", None)
            return self.status()
        if await self._revoke(site, token):
            self.store.update(token=None, workerId=None, pairedAt=None, account=None, revokePending=None)
            self._set("unpaired", "Disconnected from Fuobe.")
        else:
            self.store.update(revokePending=True)
            self._set("unpaired", "Stopped here. Fuobe couldn't be reached to forget this computer: Fuobe Connect keeps trying. You can also remove it in Fuobe's settings.")
            self._start_revoke()
        return self.status()

    def _start_revoke(self):
        if self._revoke_task and not self._revoke_task.done():
            return
        self._revoke_task = asyncio.ensure_future(self._revoke_loop())

    async def _revoke_loop(self):
        while True:
            data = self.store.load()
            if not data.get("revokePending") or not data.get("token") or not data.get("site"):
                self.store.update(revokePending=None)
                return
            if await self._revoke(data["site"], data["token"]):
                self.store.update(token=None, workerId=None, pairedAt=None, account=None, revokePending=None)
                self._set("unpaired", "Disconnected from Fuobe.")
                return
            await asyncio.sleep(REVOKE_RETRY_S)

    # ------------------------------------------------------------------
    # Running jobs

    def _start_runner(self):
        if self._runner_task and not self._runner_task.done():
            return
        self._runner_task = asyncio.ensure_future(self._run())

    async def _run(self):
        backoff = 5
        while True:
            data = self.store.load()
            token, site = data.get("token"), data.get("site")
            if not token or not site or data.get("revokePending"):
                return
            device = f"connect-{(data.get('workerId') or 'here')}"[:64]
            runner = Runner(
                Site(self._session, site, token),
                Comfy(self._session, self.comfy_base),
                device,
                self.port,
                on_state=self._set,
                nodes=self.nodes,
                own=self.own,
                prefixes=self.prefixes,
                input_dir=self.input_dir,
                paused=self.paused,
                on_job=self.recent.append,
                loras=os.environ.get("FUOBE_CONNECT_LORAS", "1") != "0",
            )
            try:
                await runner.run_forever()
                backoff = 5
            except Unpaired:
                self.store.update(token=None, workerId=None, pairedAt=None, account=None)
                self._set("unpaired", "This ComfyUI was disconnected from Fuobe. Press Connect to Fuobe to pair it again.")
                return
            except asyncio.CancelledError:
                runner.alive = False
                raise
            except Exception as e:
                self._set("offline", f"Fuobe Connect stopped for a moment: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)
