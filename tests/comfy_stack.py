"""A stand-in ComfyUI web app for the tests, with ComfyUI's own middleware.

With COMFYUI_DIR set to a ComfyUI folder, the middleware functions are read from that ComfyUI's own server.py and
middleware/cache_middleware.py (only those functions: nothing else of ComfyUI is imported, so no models, no GPU and no
torch are needed). Without it, a copy of ComfyUI 0.42's origin check is used. Either way the app is built the way
ComfyUI's PromptServer builds it: [cache_control, deprecation_warning] then the CORS middleware when started with
--enable-cors-header, else the origin check. Fuobe Connect then goes in front, as in ComfyUI.

The routes answer like ComfyUI's, from a small in-memory queue and history (Comfy): enough to see who gets in and what
they see.
"""

from __future__ import annotations

import ast
import ipaddress
import json
import logging
import os
import socket
import urllib
import urllib.parse
import uuid

from aiohttp import web

WANTED = {"create_cors_middleware", "is_loopback", "create_origin_only_middleware", "deprecation_warning"}


def _from_comfyui(folder: str) -> dict:
    ns: dict = {"web": web, "urllib": urllib, "ipaddress": ipaddress, "socket": socket, "logging": logging, "_deprecated_paths_warned": set()}
    with open(os.path.join(folder, "server.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    picked = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in WANTED]
    missing = WANTED - {n.name for n in picked}
    if missing:
        raise RuntimeError(f"ComfyUI's server.py has no {', '.join(sorted(missing))}")
    exec(compile(ast.Module(body=picked, type_ignores=[]), "comfyui/server.py", "exec"), ns)  # noqa: S102
    with open(os.path.join(folder, "middleware", "cache_middleware.py"), encoding="utf-8") as f:
        exec(compile(f.read(), "comfyui/middleware/cache_middleware.py", "exec"), ns)  # noqa: S102
    ns["source"] = f"ComfyUI at {folder}"
    return ns


def _copy() -> dict:
    """ComfyUI 0.42's origin check and CORS middleware, as they are in its server.py."""

    def create_cors_middleware(allowed_origin):
        @web.middleware
        async def cors_middleware(request, handler):
            response = web.Response() if request.method == "OPTIONS" else await handler(request)
            response.headers["Access-Control-Allow-Origin"] = allowed_origin
            response.headers["Access-Control-Allow-Methods"] = "POST, GET, DELETE, PUT, OPTIONS, PATCH"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
            response.headers["Access-Control-Allow-Credentials"] = "true"
            return response

        return cors_middleware

    def is_loopback(host):
        if host is None:
            return False
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        return host == "localhost"

    def create_origin_only_middleware():
        @web.middleware
        async def origin_only_middleware(request, handler):
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                return web.Response(status=403)
            if "Host" in request.headers and "Origin" in request.headers:
                host_domain = request.headers["Host"].lower()
                parsed = urllib.parse.urlparse(request.headers["Origin"])
                origin_domain = parsed.netloc.lower()
                host_parsed = urllib.parse.urlsplit("//" + host_domain)
                loopback = is_loopback(host_parsed.hostname)
                if parsed.port is None:
                    host_domain = host_parsed.hostname
                if host_parsed.port is None:
                    origin_domain = parsed.hostname
                if loopback and host_domain and origin_domain and host_domain != origin_domain:
                    return web.Response(status=403)
            return web.Response() if request.method == "OPTIONS" else await handler(request)

        return origin_only_middleware

    @web.middleware
    async def deprecation_warning(request, handler):
        return await handler(request)

    @web.middleware
    async def cache_control(request, handler):
        return await handler(request)

    return {
        "create_cors_middleware": create_cors_middleware,
        "create_origin_only_middleware": create_origin_only_middleware,
        "deprecation_warning": deprecation_warning,
        "cache_control": cache_control,
        "source": "a copy of ComfyUI 0.42's checks",
    }


def comfy_middleware() -> dict:
    folder = os.environ.get("COMFYUI_DIR")
    return _from_comfyui(folder) if folder else _copy()


class Comfy:
    """What the stand-in ComfyUI keeps: its queue, its history, and what it was sent. It also answers like ComfyUI's
    PromptQueue (get_history, get_current_queue_volatile), the way Fuobe Connect reads it inside ComfyUI."""

    def __init__(self, mint_ids: bool = False):
        self.mint_ids = mint_ids
        self.history: dict = {}
        self.queue_running: list = []
        self.queue_pending: list = []
        self.posts: list = []
        self.uploads: list = []
        self.number = 0
        # A /prompt answer of ComfyUI's own (status, body) instead of taking the prompt, for example a 400.
        self.refuse: tuple | None = None

    def get_history(self, prompt_id=None, max_items=None, offset=-1, map_function=None):
        if prompt_id is None:
            return dict(self.history)
        if prompt_id not in self.history:
            return {}
        e = self.history[prompt_id]
        return {prompt_id: map_function(e) if map_function else json.loads(json.dumps(e))}

    def get_current_queue_volatile(self):
        return list(self.queue_running), list(self.queue_pending)

    def entry(self, pid: str, prompt: dict, files: list | None = None) -> dict:
        self.number += 1
        e = {
            "prompt": [self.number, pid, prompt, {"client_id": "x"}, ["9"]],
            "outputs": {"9": {"images": files if files is not None else [{"filename": f"{pid[:8]}_00001_.png", "subfolder": "fuobe", "type": "output"}]}},
            "status": {"status_str": "success", "completed": True, "messages": []},
        }
        self.history[pid] = e
        return e


# Where build_app keeps the stand-in's Comfy on the app.
COMFY = web.AppKey("comfy", Comfy) if hasattr(web, "AppKey") else "comfy"


def build_app(cors_flag: str | None = None, fuobe_connect=None, extra_routes=None, comfy: Comfy | None = None) -> tuple[web.Application, str]:
    ns = comfy_middleware()
    middlewares = [ns["cache_control"], ns["deprecation_warning"]]
    middlewares.append(ns["create_cors_middleware"](cors_flag) if cors_flag else ns["create_origin_only_middleware"]())
    app = web.Application(middlewares=middlewares)
    c = comfy or Comfy()
    app[COMFY] = c
    routes = web.RouteTableDef()

    @routes.get("/system_stats")
    async def stats(request):
        return web.json_response({"system": {"os": "nt", "comfyui_version": "0.42.0", "ram_total": 1}, "devices": [{"name": "cuda:0 Test GPU", "vram_total": 2}]})

    @routes.post("/prompt")
    async def prompt(request):
        body = await request.json()
        if c.refuse is not None:
            return web.json_response(c.refuse[1], status=c.refuse[0])
        pid = str(uuid.uuid4()) if c.mint_ids or not body.get("prompt_id") else body.get("prompt_id")
        c.posts.append(body)
        if isinstance(body.get("prompt"), dict):
            c.entry(pid, body["prompt"])
        return web.json_response({"prompt_id": pid, "number": 1})

    @routes.get("/history")
    async def history(request):
        return web.json_response(c.history)

    @routes.get("/history/{pid}")
    async def history_one(request):
        pid = request.match_info["pid"]
        return web.json_response({pid: c.history[pid]} if pid in c.history else {})

    @routes.post("/history")
    async def history_post(request):
        c.posts.append(("history", await request.json()))
        return web.json_response({})

    @routes.get("/queue")
    async def queue(request):
        return web.json_response({"queue_running": c.queue_running, "queue_pending": c.queue_pending})

    @routes.post("/queue")
    async def queue_post(request):
        c.posts.append(("queue", await request.json()))
        return web.json_response({})

    @routes.post("/interrupt")
    async def interrupt(request):
        c.posts.append(("interrupt", await request.read()))
        return web.json_response({})

    @routes.get("/api/jobs")
    async def jobs(request):
        return web.json_response({"jobs": [{"id": pid, "status": "completed"} for pid in c.history], "has_more": False})

    @routes.get("/api/jobs/{pid}")
    async def job(request):
        pid = request.match_info["pid"]
        return web.json_response({"id": pid, "status": "completed"}) if pid in c.history else web.json_response({"error": "not found"}, status=404)

    @routes.post("/api/jobs/{pid}/cancel")
    async def cancel(request):
        c.posts.append(("cancel", request.match_info["pid"]))
        return web.json_response({"cancelled": True})

    @routes.post("/upload/image")
    async def upload(request):
        post = await request.post()
        image = post.get("image")
        data = image.file.read() if hasattr(image, "file") else b""
        c.uploads.append({"name": getattr(image, "filename", None), "type": post.get("type"), "subfolder": post.get("subfolder"), "bytes": len(data)})
        return web.json_response({"name": getattr(image, "filename", None), "subfolder": "", "type": post.get("type", "input")})

    @routes.get("/object_info")
    async def object_info(request):
        return web.json_response(
            {
                "LoadImage": {"input": {"required": {"image": [["holiday.png", "fuobe_abcdefgh12.png", "sub/me.jpg"], {"image_upload": True}]}}},
                "LoadAudio": {"input": {"required": {"audio": ["COMBO", {"options": ["voice memo.wav", "fuobe_abcdefgh12.wav"], "audio_upload": True}]}}},
                "CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["model.safetensors"], {}]}}},
            }
        )

    @routes.get("/view")
    async def view(request):
        return web.Response(body=bytes([137]) + b"PNG fake", content_type="image/png")

    @routes.get("/userdata/{file}")
    async def userdata(request):
        return web.json_response({"secret": "settings"})

    @routes.post("/v2/manager/queue/install")
    async def install(request):
        return web.json_response({"installed": True})

    @routes.get("/ws")
    async def ws(request):
        sock = web.WebSocketResponse()
        await sock.prepare(request)
        await sock.send_str(json.dumps({"type": "status", "data": {"sid": "x"}}))
        await sock.close()
        return sock

    for method, path, handler in extra_routes or []:
        routes.route(method, path)(handler)

    # ComfyUI answers every route under /api too.
    api = web.RouteTableDef()
    for r in routes:
        if not r.path.startswith("/api/"):
            api.route(r.method, "/api" + r.path)(r.handler)
    app.add_routes(api)
    app.add_routes(routes)
    if fuobe_connect is not None:
        app.middlewares.insert(0, fuobe_connect)
    return app, ns["source"]
