"""Fuobe Connect inside ComfyUI: the middleware, its own small API for ComfyUI's page, and the pairing agent.

ComfyUI loads custom nodes after it made its web app and before it starts serving, so the middleware can still be put
in front of ComfyUI's own (once ComfyUI serves, the list is frozen and nothing can be added). Nothing in ComfyUI's own
files changes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys

from aiohttp import web

from . import __version__
from .graph import NodeList, comfy_kinds
from .middleware import MARKER, Studios, comfy_held, make_middleware
from .origins import DEFAULT_STUDIOS, NONE, host_name, is_ip, is_loopback, normalise, parse_list, same_host, says_none, studio, trusted_origins, with_twins

log = logging.getLogger("fuobe_connect")

_state: dict = {}


def trusted() -> tuple[str, ...]:
    store = _state.get("store")
    configured = store.load().get("studios") if store else None
    return trusted_origins(os.environ.get("FUOBE_CONNECT_ORIGINS"), configured)


def pairing_sites() -> list[str]:
    """The studios this ComfyUI may pair with: the trusted ones, Fuobe's own, and FUOBE_CONNECT_SITE."""
    sites = list(trusted()) + with_twins(list(DEFAULT_STUDIOS))
    env = studio(os.environ.get("FUOBE_CONNECT_SITE"))
    if env:
        sites.append(env)
    return sites


def own_hosts() -> set[str]:
    """Host names (besides this computer's own and IP addresses) that ComfyUI's own page is opened at:
    FUOBE_CONNECT_HOSTS, for a ComfyUI you reach by a name (comfy.local, or a domain in front of it)."""
    return {h.strip().lower().rstrip(".") for h in re.split(r"[\s,]+", os.environ.get("FUOBE_CONNECT_HOSTS", "")) if h.strip()}


def _own_page(request: web.Request, post: bool = False) -> bool:
    """A call from ComfyUI's own page, or from a program on this computer: never from a trusted studio or any website.

    The Host must be this computer (127.0.0.1, ::1, localhost), an IP address, or a name in FUOBE_CONNECT_HOSTS: a page
    whose own name was pointed at this computer (DNS rebinding) has neither. The Origin, when there is one, is that
    Host with the same port; the browser's Sec-Fetch-Site, when sent, says same-origin; a change is JSON."""
    if request.headers.get(MARKER):
        return False
    host = host_name(request.headers.get("Host"))
    if not host or not (is_loopback(host) or is_ip(host) or host.lower().rstrip(".") in own_hosts()):
        return False
    origin = request.headers.get("Origin")
    if origin is not None and not same_host(origin, request.headers.get("Host")):
        return False
    site = request.headers.get("Sec-Fetch-Site")
    if site is not None and site.lower() not in ("same-origin", "none" if not post else "same-origin"):
        return False
    if post and request.content_type != "application/json":
        return False
    return True


async def _json(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


async def status(request: web.Request) -> web.Response:
    """GET /fuobe/connect. A studio sees only that Fuobe Connect is here; ComfyUI's own page sees the rest."""
    base = {"connect": 1, "version": __version__, "guard": 1}
    if not _own_page(request):
        return web.json_response(base)
    store = _state.get("store")
    agent = _state.get("agent")
    from_env = bool(os.environ.get("FUOBE_CONNECT_ORIGINS"))
    saved = store.load().get("studios") if store else None
    return web.json_response(
        {
            **base,
            "studios": list(trusted()),
            "setting": NONE if says_none(saved) else ", ".join(saved) if isinstance(saved, list) else None,
            "fromEnv": from_env,
            "pairSites": pairing_sites(),
            **(agent.status() if agent else {"state": "off"}),
        }
    )


async def set_studios(request: web.Request) -> web.Response:
    """POST /fuobe/connect/studios {studios: "https://a, https://b" | "none"}: the studios that may use this ComfyUI from
    the browser ("none": no website; pairing still works)."""
    if not _own_page(request, post=True):
        return web.Response(status=403)
    raw = (await _json(request)).get("studios")
    if says_none(raw):
        _state["store"].update(studios=[])
    else:
        studios = parse_list(raw)
        if not studios:
            return web.json_response({"error": "Name a studio like https://fuobe.com, or type none for no website. A studio's address starts with https://."}, status=400)
        _state["store"].update(studios=studios)
    return web.json_response({"studios": list(trusted()), "fromEnv": bool(os.environ.get("FUOBE_CONNECT_ORIGINS"))})


async def pair(request: web.Request) -> web.Response:
    """POST /fuobe/connect/pair {site?, name?}: a code to approve on the studio. Only a studio this ComfyUI may pair with."""
    if not _own_page(request, post=True):
        return web.Response(status=403)
    agent = _state.get("agent")
    body = await _json(request)
    sites = pairing_sites()
    site = studio(body.get("site")) if body.get("site") is not None else (sites[0] if sites else None)
    if not site or site not in sites:
        return web.json_response({"error": "Pair only with a studio you trust: add it in Settings → Fuobe Connect first."}, status=400)
    try:
        fut = agent.call(agent.start_pairing(site, body.get("name") if isinstance(body.get("name"), str) else None))
        return web.json_response(await asyncio.wrap_future(fut))
    except Exception as e:
        return web.json_response({"error": str(e)[:200]}, status=502)


async def unpair(request: web.Request) -> web.Response:
    if not _own_page(request, post=True):
        return web.Response(status=403)
    agent = _state["agent"]
    agent.cancel_pairing()
    return web.json_response(await asyncio.wrap_future(agent.call(agent.unpair())))


async def set_jobs(request: web.Request) -> web.Response:
    """POST /fuobe/connect/jobs {paused: bool}: pause or resume Fuobe jobs on this computer."""
    if not _own_page(request, post=True):
        return web.Response(status=403)
    paused = (await _json(request)).get("paused")
    if not isinstance(paused, bool):
        return web.json_response({"error": "Send paused: true or false"}, status=400)
    return web.json_response(_state["agent"].set_paused(paused))


def install():
    """Called once when ComfyUI loads this custom node."""
    import folder_paths
    import server
    from comfy.cli_args import args

    from .agent import Agent
    from .net import loopback_base
    from .store import Store

    instance = getattr(server.PromptServer, "instance", None)
    if instance is None:
        log.warning("[Fuobe Connect] ComfyUI's server isn't there, so Fuobe Connect is off.")
        return
    app = instance.app
    if any(getattr(m, "__name__", "") == "fuobe_connect" for m in app.middlewares):
        return
    user_dir = folder_paths.get_user_directory()
    store = Store(user_dir)
    _state["store"] = store
    # ComfyUI's node classes, read when a graph first uses each type (node packs that load after this one count too).
    nodes = NodeList(user_dir, comfy_kinds(lambda: getattr(sys.modules.get("nodes"), "NODE_CLASS_MAPPINGS", None) or {}))
    # A studio's prompt id is checked against ComfyUI's own queue and history (read when a prompt comes in).
    studios = Studios(nodes, comfy_held(lambda: instance.prompt_queue))
    _state["studios"] = studios
    lan = os.environ.get("FUOBE_CONNECT_LAN") == "1"
    try:
        app.middlewares.insert(0, make_middleware(trusted, getattr(args, "enable_cors_header", None), studios, lan))
    except RuntimeError:
        log.warning("[Fuobe Connect] ComfyUI had already started, so Fuobe Connect is off. Restart ComfyUI.")
        return
    instance.routes.get("/fuobe/connect")(status)
    instance.routes.post("/fuobe/connect/studios")(set_studios)
    instance.routes.post("/fuobe/connect/pair")(pair)
    instance.routes.post("/fuobe/connect/unpair")(unpair)
    instance.routes.post("/fuobe/connect/jobs")(set_jobs)

    for source in (os.environ.get("FUOBE_CONNECT_ORIGINS"), store.load().get("studios")):
        items = source if isinstance(source, list) else re.split(r"[\s,]+", source or "")
        for item in items:
            if isinstance(item, str) and item.strip() and not says_none(item) and normalise(item) and not studio(item):
                log.warning("[Fuobe Connect] %s isn't trusted: a studio's address starts with https:// (http only on this computer).", item[:120])

    port = int(getattr(args, "port", 8188) or 8188)
    try:
        input_dir = folder_paths.get_input_directory()
    except Exception:
        input_dir = None
    sites = pairing_sites()
    agent = Agent(store, loopback_base(getattr(args, "listen", None), port, bool(getattr(args, "tls_keyfile", None))), port, sites[0], nodes, input_dir)
    _state["agent"] = agent
    agent.start()
    if trusted():
        log.info("[Fuobe Connect] %s can use this ComfyUI from the browser.", ", ".join(trusted()))
    else:
        log.info("[Fuobe Connect] No website may use this ComfyUI from the browser (studios: none).")
    if os.environ.get("FUOBE_CONNECT_PAIR") == "1" and not store.load().get("token"):
        # A PC with no screen: the code and the address go to ComfyUI's console.
        agent.call(agent.start_pairing(studio(os.environ.get("FUOBE_CONNECT_SITE")) or sites[0]))
    elif not store.load().get("token"):
        # ASCII only: on Windows a log that goes to a file or a pipe takes only the system's code page.
        log.info("[Fuobe Connect] To run Fuobe jobs here with no browser tab open: ComfyUI menu > Extensions > Connect to Fuobe.")
