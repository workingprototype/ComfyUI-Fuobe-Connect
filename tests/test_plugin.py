"""Fuobe Connect as ComfyUI loads it: __init__.py run against a stand-in of ComfyUI's server module (its PromptServer
with ComfyUI's own middleware, tests/comfy_stack.py), then pairing through ComfyUI's page with a stand-in studio. And
the checks that keep its own small API to ComfyUI's own page.

Ports: FUOBE_CONNECT_TEST_PORT + 4 for the page checks, + 5 for the ComfyUI stand-in, + 6 for the studio stand-in
(3364, 3365 and 3366 by default).
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import logging
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import aiohttp
from aiohttp import web

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, PACK)

from comfy_stack import COMFY, build_app  # noqa: E402
from fuobe_connect import plugin  # noqa: E402
from fuobe_connect.store import access_list  # noqa: E402

BASE_PORT = int(os.environ.get("FUOBE_CONNECT_TEST_PORT", "3360"))
PAGE_PORT = BASE_PORT + 4
COMFY_PORT = BASE_PORT + 5
SITE_PORT = BASE_PORT + 6
SITE = f"http://127.0.0.1:{SITE_PORT}"
PAGE = f"http://127.0.0.1:{PAGE_PORT}"


class FakeStore:
    def __init__(self, data=None):
        self.data = data or {}

    def load(self):
        return dict(self.data)

    def update(self, **c):
        for k, v in c.items():
            if v is None:
                self.data.pop(k, None)
            else:
                self.data[k] = v
        return self.data


class FakeAgent:
    def __init__(self):
        self.paired_with = []
        self.paused = None

    def call(self, coro):
        import concurrent.futures

        fut = concurrent.futures.Future()
        try:
            coro.send(None)
        except StopIteration as e:
            fut.set_result(e.value)
        return fut

    async def start_pairing(self, site, name=None):
        self.paired_with.append((site, name))
        return {"state": "pairing"}

    def set_paused(self, paused):
        self.paused = paused
        return {"paused": paused}

    def cancel_pairing(self):
        pass

    def status(self):
        return {"state": "unpaired"}


class OwnPageTest(unittest.TestCase):
    """Only ComfyUI's own page (or a program here) changes the studios, pairs, or pauses: never a page under another
    host name, a page on another port, or another website."""

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for k in ("FUOBE_CONNECT_ORIGINS", "FUOBE_CONNECT_HOSTS", "FUOBE_CONNECT_SITE"):
            os.environ.pop(k, None)
        self.addCleanup(self.env.stop)
        plugin._state.clear()
        plugin._state["store"] = FakeStore()
        self.agent = plugin._state["agent"] = FakeAgent()
        self.addCleanup(plugin._state.clear)

    def serve(self, test):
        async def go():
            app = web.Application()
            app.router.add_get("/fuobe/connect", plugin.status)
            app.router.add_post("/fuobe/connect/studios", plugin.set_studios)
            app.router.add_post("/fuobe/connect/pair", plugin.pair)
            app.router.add_post("/fuobe/connect/jobs", plugin.set_jobs)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", PAGE_PORT).start()
            try:
                async with aiohttp.ClientSession() as s:

                    async def req(method, path, headers=None, body=None, raw=None):
                        h = dict(headers or {})
                        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
                        if body is not None and "Content-Type" not in h:
                            h["Content-Type"] = "application/json"
                        async with s.request(method, PAGE + path, headers=h, data=data) as res:
                            return res.status, await res.json(content_type=None) if res.status != 403 else None

                    await test(req)
            finally:
                await runner.cleanup()

        asyncio.run(go())

    def test_refuses_requests_from_other_pages(self):
        async def test(req):
            own = {"Origin": PAGE, "Sec-Fetch-Site": "same-origin"}
            status, body = await req("GET", "/fuobe/connect", own)
            self.assertIn("studios", body)
            # A page at other.example on ComfyUI's port: its Origin and Host agree, but the name isn't one of ComfyUI's own.
            other_name = {"Host": f"other.example:{PAGE_PORT}", "Origin": f"http://other.example:{PAGE_PORT}", "Sec-Fetch-Site": "same-origin"}
            status, body = await req("GET", "/fuobe/connect", other_name)
            self.assertNotIn("studios", body)
            for path, b in [("/fuobe/connect/studios", {"studios": "https://other.example"}), ("/fuobe/connect/pair", {"site": "https://other.example"}), ("/fuobe/connect/jobs", {"paused": False})]:
                status, _ = await req("POST", path, other_name, b)
                self.assertEqual(status, 403, path)
            # A page on port 80 of this computer, another website, a form post: not ComfyUI's page either.
            for headers in [
                {"Origin": "http://127.0.0.1", "Host": f"127.0.0.1:{PAGE_PORT}"},
                {"Origin": "https://other.example", "Sec-Fetch-Site": "cross-site"},
                {"Origin": PAGE, "Sec-Fetch-Site": "cross-site"},
            ]:
                status, _ = await req("POST", "/fuobe/connect/studios", headers, {"studios": "https://other.example"})
                self.assertEqual(status, 403, headers)
            status, _ = await req("POST", "/fuobe/connect/studios", {"Origin": PAGE, "Content-Type": "text/plain"}, raw=b'{"studios":"https://other.example"}')
            self.assertEqual(status, 403)
            self.assertNotIn("studios", plugin._state["store"].data)
            # A name the person lists is ComfyUI's own page.
            os.environ["FUOBE_CONNECT_HOSTS"] = "comfy.local"
            status, body = await req("GET", "/fuobe/connect", {"Host": f"comfy.local:{PAGE_PORT}", "Origin": f"http://comfy.local:{PAGE_PORT}"})
            self.assertIn("studios", body)

        self.serve(test)

    def test_studios_setting(self):
        async def test(req):
            own = {"Origin": PAGE, "Sec-Fetch-Site": "same-origin"}
            status, body = await req("POST", "/fuobe/connect/studios", own, {"studios": "http://studio.example.com"})
            self.assertEqual(status, 400)
            status, body = await req("POST", "/fuobe/connect/studios", own, {"studios": "https://studio.example.com, http://localhost:3001"})
            self.assertEqual(body["studios"], ["https://studio.example.com", "http://localhost:3001"])
            status, body = await req("POST", "/fuobe/connect/studios", own, {"studios": "none"})
            self.assertEqual((status, body["studios"]), (200, []))
            self.assertEqual(plugin.trusted(), ())
            status, body = await req("GET", "/fuobe/connect", own)
            self.assertEqual((body["studios"], body["setting"]), ([], "none"))
            # Pairing still works with no trusted website: with Fuobe's own studio.
            self.assertEqual(body["pairSites"][0], "https://fuobe.com")

        self.serve(test)

    def test_pairs_only_with_a_trusted_studio(self):
        async def test(req):
            own = {"Origin": PAGE, "Sec-Fetch-Site": "same-origin"}
            for site in ["https://other.example", "http://fuobe.com", "javascript:alert(1)"]:
                status, _ = await req("POST", "/fuobe/connect/pair", own, {"site": site})
                self.assertEqual(status, 400, site)
            status, _ = await req("POST", "/fuobe/connect/pair", own, {"name": "Studio PC"})
            self.assertEqual(status, 200)
            self.assertEqual(self.agent.paired_with, [("https://fuobe.com", "Studio PC")])
            status, body = await req("POST", "/fuobe/connect/jobs", own, {"paused": True})
            self.assertEqual((status, self.agent.paused), (200, True))

        self.serve(test)


class FakeStudio:
    """Answers the pairing and worker calls the way a Fuobe studio does, with fixed values."""

    def __init__(self):
        self.polls = 0
        self.calls: list[str] = []
        self.url = None
        self.unpair_status = 200
        self.app = web.Application()
        self.app.router.add_post("/api/gpu/pair/start", self.start)
        self.app.router.add_post("/api/gpu/pair/poll", self.poll)
        self.app.router.add_post("/api/gpu/computer/beat", self.beat)
        self.app.router.add_post("/api/gpu/computer/claim", self.claim)
        self.app.router.add_post("/api/gpu/computer/unpair", self.unpair)

    async def start(self, request):
        self.calls.append("start")
        body = await request.json()
        self.name = body.get("name")
        return web.json_response({"id": "pair-1", "secret": "s" * 64, "code": "ABCD-EFGH", "url": self.url or f"{SITE}/connect?code=ABCD-EFGH", "expiresAt": 0, "pollMs": 1000})

    async def poll(self, request):
        self.polls += 1
        if self.polls < 2:
            return web.json_response({"state": "waiting"})
        return web.json_response({"state": "approved", "token": "fuobe_wkr_" + "T" * 43, "workerId": "w1", "name": self.name, "account": "a\u2022\u2022\u2022@example.com"})

    async def beat(self, request):
        self.calls.append(f"beat:{request.headers.get('Authorization', '')[:20]}")
        return web.json_response({"runner": False, "heldElsewhere": True, "waiting": 0, "stops": []})

    async def claim(self, request):
        return web.Response(status=204)

    async def unpair(self, request):
        self.calls.append(f"unpair:{self.unpair_status}")
        return web.json_response({"ok": self.unpair_status == 200}, status=self.unpair_status)


def load_pack(user_dir: str):
    """ComfyUI's view of the world, as much of it as Fuobe Connect reads, then the pack itself."""
    app, _ = build_app()
    # The stand-in's queue and history answer like ComfyUI's PromptQueue.
    instance = types.SimpleNamespace(app=app, routes=web.RouteTableDef(), prompt_queue=app[COMFY])
    server = types.ModuleType("server")
    server.PromptServer = types.SimpleNamespace(instance=instance)
    folder_paths = types.ModuleType("folder_paths")
    folder_paths.get_user_directory = lambda: user_dir
    folder_paths.get_input_directory = lambda: os.path.join(user_dir, "input")
    cli_args = types.ModuleType("comfy.cli_args")
    cli_args.args = types.SimpleNamespace(port=COMFY_PORT, listen="127.0.0.1", enable_cors_header=None, tls_keyfile=None)
    comfy = types.ModuleType("comfy")
    comfy.cli_args = cli_args
    sys.modules.update({"server": server, "folder_paths": folder_paths, "comfy": comfy, "comfy.cli_args": cli_args})
    os.environ["FUOBE_CONNECT_ORIGINS"] = SITE
    spec = importlib.util.spec_from_file_location("fuobe_connect_pack", os.path.join(PACK, "__init__.py"), submodule_search_locations=[PACK])
    module = importlib.util.module_from_spec(spec)
    sys.modules["fuobe_connect_pack"] = module
    spec.loader.exec_module(module)
    return module, instance


class PluginTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="fuobe-connect-user-")
        self.addCleanup(folder.cleanup)
        self.user_dir = folder.name
        for patcher in (mock.patch.dict(os.environ), mock.patch.dict(sys.modules)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_starts_when_the_log_goes_to_a_file(self):
        # On Windows, ComfyUI's log written to a file or a pipe takes only the system's code page (cp1252 here).
        out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", write_through=True)
        handler = logging.StreamHandler(out)
        root = logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        self.addCleanup(root.setLevel, level)
        self.addCleanup(root.removeHandler, handler)
        with mock.patch.object(sys, "stderr", out):
            load_pack(self.user_dir)
        text = out.buffer.getvalue().decode("cp1252")
        self.assertNotIn("Couldn't start", text)
        self.assertNotIn("Logging error", text)
        self.assertIn("Extensions > Connect to Fuobe", text)

    def test_loads_into_comfyui_and_pairs(self):
        user_dir = self.user_dir
        # On Japanese Windows, ComfyUI's log written to a file or a pipe takes only cp932.
        log_out = io.TextIOWrapper(io.BytesIO(), encoding="cp932", errors="strict", write_through=True)
        handler = logging.StreamHandler(log_out)
        failed = []
        handler.handleError = lambda record: failed.append(sys.exc_info()[1])
        root = logging.getLogger()
        level = root.level
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        self.addCleanup(root.setLevel, level)
        self.addCleanup(root.removeHandler, handler)
        module, instance = load_pack(user_dir)
        agent_module = sys.modules["fuobe_connect_pack.fuobe_connect.agent"]
        agent_module.REVOKE_RETRY_S = 0.3
        self.assertEqual(module.NODE_CLASS_MAPPINGS, {})
        self.assertEqual(module.WEB_DIRECTORY, "./web")
        self.assertEqual(getattr(instance.app.middlewares[0], "__name__", ""), "fuobe_connect")
        paths = {(r.method, r.path) for r in instance.routes}
        for p in [("GET", "/fuobe/connect"), ("POST", "/fuobe/connect/pair"), ("POST", "/fuobe/connect/jobs")]:
            self.assertIn(p, paths)
        # A studio's prompt id is checked against ComfyUI's own queue and history.
        studios = sys.modules["fuobe_connect_pack.fuobe_connect.plugin"]._state["studios"]
        instance.prompt_queue.entry("0f0e0d0c-0000-4000-8000-000000000001", {})
        instance.prompt_queue.queue_pending = [[1, "0f0e0d0c-0000-4000-8000-000000000002", {}, {}, []]]
        self.assertEqual([studios.held(f"0f0e0d0c-0000-4000-8000-00000000000{i}") for i in (1, 2, 3)], [True, True, False])
        instance.prompt_queue.queue_pending = []
        instance.app.add_routes(instance.routes)
        studio = FakeStudio()
        file = os.path.join(user_dir, "fuobe-connect.json")

        async def go():
            runners = []
            for app, port in ((instance.app, COMFY_PORT), (studio.app, SITE_PORT)):
                r = web.AppRunner(app)
                await r.setup()
                await web.TCPSite(r, "127.0.0.1", port).start()
                runners.append(r)
            try:
                async with aiohttp.ClientSession() as s:
                    base = f"http://127.0.0.1:{COMFY_PORT}"
                    page = {"Origin": base, "Sec-Fetch-Site": "same-origin"}

                    async def status():
                        async with s.get(f"{base}/fuobe/connect") as res:
                            return await res.json()

                    st = await status()
                    self.assertEqual(st["studios"], [SITE])
                    self.assertEqual(st["state"], "unpaired")
                    # The studio itself can't start a pairing, or change the studios.
                    async with s.post(f"{base}/fuobe/connect/pair", json={}, headers={"Origin": SITE, "Sec-Fetch-Site": "cross-site"}) as res:
                        self.assertEqual(res.status, 403)
                    # An approval link that isn't on the studio's own address is refused.
                    studio.url = "javascript:alert(1)"
                    async with s.post(f"{base}/fuobe/connect/pair", json={}, headers=page) as res:
                        self.assertEqual(res.status, 502)
                        self.assertIn("approval link", (await res.json())["error"])
                    studio.url = None
                    # ComfyUI's own page can.
                    async with s.post(f"{base}/fuobe/connect/pair", json={"name": "Studio PC"}, headers=page) as res:
                        body = await res.json()
                    self.assertEqual(res.status, 200, body)
                    self.assertEqual(body["pairing"]["code"], "ABCD-EFGH")
                    self.assertEqual(studio.name, "Studio PC")
                    deadline = time.time() + 15
                    while time.time() < deadline:
                        st = await status()
                        if st.get("paired") and any(c.startswith("beat:Bearer fuobe_wkr_") for c in studio.calls):
                            break
                        await asyncio.sleep(0.3)
                    self.assertEqual(st["paired"]["site"], SITE)
                    # The account that approved it, as the studio names it; the log writes it in ASCII.
                    self.assertEqual(st["paired"]["account"], "a\u2022\u2022\u2022@example.com")
                    self.assertEqual(failed, [])
                    self.assertIn("as a***@example.com.", log_out.buffer.getvalue().decode("cp932"))
                    self.assertTrue(any(c.startswith("beat:Bearer fuobe_wkr_") for c in studio.calls), studio.calls)
                    with open(file, encoding="utf-8") as f:
                        saved = json.load(f)
                    self.assertTrue(saved["token"].startswith("fuobe_wkr_"))
                    if os.name == "nt":
                        acl = access_list(file)
                        self.assertRegex(acl, r"^D:P\(A;;FA;;;S-1-[0-9-]+\)$")
                    else:
                        self.assertEqual(os.stat(file).st_mode & 0o777, 0o600)
                    # Pause and resume.
                    async with s.post(f"{base}/fuobe/connect/jobs", json={"paused": True}, headers=page) as res:
                        self.assertTrue((await res.json())["paused"])
                    async with s.post(f"{base}/fuobe/connect/jobs", json={"paused": False}, headers=page) as res:
                        self.assertFalse((await res.json())["paused"])
                    # Disconnect while the studio can't answer: the token stays (unused) until the studio has revoked it.
                    studio.unpair_status = 503
                    async with s.post(f"{base}/fuobe/connect/unpair", json={}, headers=page) as res:
                        st = await res.json()
                    self.assertNotIn("paired", st)
                    self.assertEqual(st["revokePending"], SITE)
                    with open(file, encoding="utf-8") as f:
                        self.assertIn("token", json.load(f))
                    studio.unpair_status = 200
                    deadline = time.time() + 10
                    while time.time() < deadline and "revokePending" in await status():
                        await asyncio.sleep(0.2)
                    self.assertIn("unpair:503", studio.calls)
                    self.assertIn("unpair:200", studio.calls)
                    with open(file, encoding="utf-8") as f:
                        self.assertNotIn("token", json.load(f))
            finally:
                for r in runners:
                    await r.cleanup()

        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
