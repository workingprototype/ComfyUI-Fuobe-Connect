"""Fuobe Connect's middleware in front of ComfyUI's own (tests/comfy_stack.py), over real HTTP on this computer.

Run: python -m unittest discover -s tests (from the pack's folder). FUOBE_CONNECT_TEST_PORT picks the port (3360 by
default); COMFYUI_DIR=<a ComfyUI folder> reads ComfyUI's own middleware from it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import unittest
import uuid

import aiohttp
from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from comfy_stack import COMFY, Comfy, build_app  # noqa: E402
from fuobe_connect import plugin  # noqa: E402
from fuobe_connect.middleware import MARKER, Studios, comfy_held, make_middleware  # noqa: E402
from fuobe_connect.origins import allowed, normalise, parse_list, same_host, studio, trusted_origins  # noqa: E402

PORT = int(os.environ.get("FUOBE_CONNECT_TEST_PORT", "3360"))
BASE = f"http://127.0.0.1:{PORT}"
FUOBE = "https://fuobe.com"
EVIL = "https://evil.example"
NODES = frozenset({"CheckpointLoaderSimple", "CLIPTextEncode", "KSampler", "VAEDecode", "SaveImage", "LoadImage", "EmptyLatentImage", "PreviewImage"})


def browser(origin: str, site: str = "cross-site", **extra) -> dict:
    return {"Origin": origin, "Sec-Fetch-Site": site, "Sec-Fetch-Mode": "cors", **extra}


def graph(**extra) -> dict:
    g = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "model.safetensors"}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a red door", "clip": ["4", 1]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "fuobe/gen123abc", "images": ["8", 0]}},
    }
    g.update(extra)
    return g


def prompt_body(g=None, **extra) -> bytes:
    return json.dumps({"prompt": g or graph(), "prompt_id": str(uuid.uuid4()), "client_id": "fuobe-abcdefgh", **extra}).encode()


class Server:
    def __init__(self, app: web.Application):
        self.app = app

    async def __aenter__(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", PORT).start()
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc):
        await self.session.close()
        await self.runner.cleanup()

    async def req(self, method: str, path: str, headers: dict | None = None, **kw):
        async with self.session.request(method, BASE + path, headers=headers or {}, **kw) as res:
            return res.status, dict(res.headers), await res.read()

    async def json(self, method: str, path: str, origin: str = FUOBE, body=None):
        status, h, raw = await self.req(method, path, browser(origin, **({"Content-Type": "application/json"} if body is not None else {})), data=json.dumps(body).encode() if body is not None else None)
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        return status, h, data


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


def with_connect(trusted=trusted_origins(None, None), cors_flag=None, comfy=None, lan=False):
    plugin._state.clear()
    plugin._state["store"] = FakeStore()
    routes = [("GET", "/fuobe/connect", plugin.status), ("POST", "/fuobe/connect/studios", plugin.set_studios)]
    comfy = comfy or Comfy()
    # The stand-in answers like ComfyUI's PromptQueue, which the plugin hands to Studios inside ComfyUI.
    studios = Studios(lambda: NODES, comfy_held(lambda: comfy))
    app, source = build_app(cors_flag, make_middleware(lambda: trusted, cors_flag, studios, lan), routes, comfy)
    return app, source


def comfy_prompt_queue():
    """ComfyUI's own PromptQueue class, read from COMFYUI_DIR's execution.py (that class alone: nothing else of ComfyUI
    is imported). None without COMFYUI_DIR."""
    folder = os.environ.get("COMFYUI_DIR")
    if not folder:
        return None
    import ast
    import copy
    import heapq
    import threading
    import typing

    with open(os.path.join(folder, "execution.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    picked = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PromptQueue"]
    ns = {"threading": threading, "heapq": heapq, "copy": copy, "MAXIMUM_HISTORY_SIZE": 10000, **{k: getattr(typing, k) for k in typing.__all__ if hasattr(typing, k)}}
    exec(compile(ast.Module(body=picked, type_ignores=[]), "comfyui/execution.py", "exec"), ns)  # noqa: S102
    return ns["PromptQueue"]


def run(coro):
    return asyncio.run(coro)


class OriginsTest(unittest.TestCase):
    def test_normalise(self):
        self.assertEqual(normalise("https://Fuobe.com/"), "https://fuobe.com")
        self.assertEqual(normalise("https://fuobe.com:443"), "https://fuobe.com")
        self.assertEqual(normalise("http://localhost:3000"), "http://localhost:3000")
        for bad in ["fuobe.com", "https://fuobe.com/path", "javascript:alert(1)", "null", "", "https://user@fuobe.com", "ftp://fuobe.com"]:
            self.assertIsNone(normalise(bad), bad)

    def test_studios_are_https(self):
        self.assertEqual(studio("https://studio.example.com"), "https://studio.example.com")
        self.assertEqual(studio("http://localhost:3001"), "http://localhost:3001")
        self.assertEqual(studio("http://127.0.0.1:3001"), "http://127.0.0.1:3001")
        self.assertIsNone(studio("http://studio.example.com"))
        self.assertIsNone(studio("http://192.168.1.5:3000"))
        self.assertEqual(parse_list("http://studio.example.com, https://fuobe.com"), ["https://fuobe.com"])

    def test_trusted(self):
        self.assertEqual(trusted_origins(None, None), ("https://fuobe.com", "https://www.fuobe.com"))
        self.assertEqual(trusted_origins("https://studio.example.com", ["https://fuobe.com"]), ("https://studio.example.com",))
        self.assertEqual(trusted_origins(None, "https://a.example, https://b.example:8443"), ("https://a.example", "https://b.example:8443"))
        self.assertEqual(parse_list("nonsense"), [])
        # "none", or a saved empty list: no website at all.
        self.assertEqual(trusted_origins("none", None), ())
        self.assertEqual(trusted_origins(None, []), ())
        self.assertEqual(trusted_origins(None, "none"), ())
        self.assertEqual(trusted_origins("", None), ("https://fuobe.com", "https://www.fuobe.com"))

    def test_allowed_paths(self):
        uid = "0f0e0d0c-0000-4000-8000-000000000000"
        for m, p, q in [("GET", "/system_stats", ""), ("GET", "/api/system_stats", ""), ("POST", "/prompt", ""), ("POST", "/upload/image", ""), ("GET", "/ws", "clientId=fuobe-abcdefgh"), ("GET", f"/history/{uid}", ""), ("GET", "/history", "max_items=64"), ("POST", f"/api/jobs/{uid}/cancel", ""), ("GET", "/v2/customnode/installed", ""), ("POST", "/history", "")]:
            self.assertTrue(allowed(m, p, q), p)
        for m, p, q in [("GET", "/userdata/comfy.settings.json", ""), ("POST", "/v2/manager/queue/install", ""), ("POST", "/fuobe/connect/studios", ""), ("POST", "/fuobe/connect/pair", ""), ("DELETE", "/prompt", ""), ("GET", "/internal/logs", ""), ("GET", "/history", ""), ("GET", "/history", "max_items=64&offset=0"), ("GET", "/history/not-a-uuid", "")]:
            self.assertFalse(allowed(m, p, q), p)

    def test_model_lists_only(self):
        # A model folder's list, yes; ComfyUI's lists of every node pack's files and of its configs, no.
        for p in ["/models", "/models/loras", "/api/models/checkpoints", "/experiment/models", "/experiment/models/loras", "/models/custom_nodes_extra"]:
            self.assertTrue(allowed("GET", p), p)
        for p in ["/models/custom_nodes", "/api/models/custom_nodes", "/models/configs", "/experiment/models/custom_nodes", "/api/experiment/models/configs"]:
            self.assertFalse(allowed("GET", p), p)

    def test_paths_match_whole(self):
        # Python's "$" also matches before a final newline: the lists match the whole path.
        self.assertFalse(allowed("GET", "/system_stats\n"))
        self.assertFalse(allowed("GET", "/history/0f0e0d0c-0000-4000-8000-000000000000\n"))
        self.assertFalse(allowed("POST", "/prompt\n"))

    def test_view_only_results(self):
        self.assertTrue(allowed("GET", "/view", "filename=a.png&type=output&subfolder="))
        self.assertTrue(allowed("GET", "/view", "filename=a.png&type=temp&subfolder=fuobe"))
        for q in [
            "filename=a.png&type=input&subfolder=",
            # ComfyUI reads a " [input]" tag on the name before the type.
            "filename=photo.png%20%5Binput%5D&type=output&subfolder=",
            "filename=photo.png%20[input]&type=output",
            "filename=photo.png%20[output]&type=temp",
            "filename=blake3:0123abcd&type=output",
            "filename=a.png&type=output&subfolder=..",
            "filename=a.png&type=output&subfolder=fuobe/../..",
            "filename=a.png&type=output&subfolder=/etc",
            "filename=a.png&type=output&subfolder=C:/Users",
            "filename=../a.png&type=output",
            "filename=a.png&type=output&channel=a",
        ]:
            self.assertFalse(allowed("GET", "/view", q), q)

    def test_ws_only_fuobe_ids(self):
        self.assertTrue(allowed("GET", "/ws", "clientId=fuobe-abcdefgh"))
        self.assertTrue(allowed("GET", "/ws", "clientId=fuobe-live-0123456789abcdef"))
        for q in ["", "clientId=0123456789abcdef0123456789abcdef", "clientId=fuobe-abc", "clientId=fuobe-abcdefgh&x=1"]:
            self.assertFalse(allowed("GET", "/ws", q), q)

    def test_same_host_ports(self):
        self.assertTrue(same_host("http://127.0.0.1:8188", "127.0.0.1:8188"))
        # A page on port 80 isn't ComfyUI's own page on 8188.
        self.assertFalse(same_host("http://localhost", "localhost:8188"))
        self.assertFalse(same_host("http://127.0.0.1", "127.0.0.1:8188"))
        self.assertTrue(same_host("http://localhost", "localhost"))
        self.assertTrue(same_host("https://comfy.example", "comfy.example:443"))


class WithoutConnectTest(unittest.TestCase):
    """What a person sees today: ComfyUI turns the Fuobe page away."""

    def test_comfyui_refuses_the_studio(self):
        async def go():
            app, _ = build_app()
            async with Server(app) as s:
                status, _, _ = await s.req("GET", "/system_stats", browser(FUOBE))
                self.assertEqual(status, 403)

        run(go())


class DirectModeTest(unittest.TestCase):
    def test_studio_reads_and_queues(self):
        async def go():
            app, source = with_connect()
            print(f"\n  (middleware from {source})", end="")
            async with Server(app) as s:
                status, h, body = await s.req("GET", "/system_stats", browser(FUOBE))
                self.assertEqual(status, 200)
                self.assertEqual(h.get("Access-Control-Allow-Origin"), FUOBE)
                self.assertIn("Origin", h.get("Vary", ""))
                self.assertIn(b"comfyui_version", body)
                status, h, _ = await s.req("GET", "/api/system_stats", browser("https://www.fuobe.com"))
                self.assertEqual((status, h.get("Access-Control-Allow-Origin")), (200, "https://www.fuobe.com"))
                pid = str(uuid.uuid4())
                status, h, data = await s.json("POST", "/prompt", body={"prompt": graph(), "prompt_id": pid, "client_id": "fuobe-abcdefgh", "extra_data": {"preview_method": "auto"}})
                self.assertEqual((status, h.get("Access-Control-Allow-Origin")), (200, FUOBE), data)
                self.assertEqual(data["prompt_id"], pid)
                # Its own prompt's history, then its result.
                status, _, data = await s.json("GET", f"/history/{pid}")
                self.assertEqual(list(data), [pid])
                f = data[pid]["outputs"]["9"]["images"][0]
                status, _, _ = await s.req("GET", f"/view?filename={f['filename']}&subfolder={f['subfolder']}&type=output", browser(FUOBE))
                self.assertEqual(status, 200)

        run(go())

    def test_prompt_is_checked_here(self):
        """Whatever the studio's own page checked: the pack checks every prompt in Python."""

        async def go():
            app, _ = with_connect()
            async with Server(app) as s:
                refused = {
                    "a node this pack doesn't ship": graph(**{"10": {"class_type": "Image Load", "inputs": {"image_path": "C:/Users/me/secret.png"}}}),
                    "an absolute path": graph(**{"10": {"class_type": "SaveImage", "inputs": {"filename_prefix": "/etc/cron.d/x", "images": ["8", 0]}}}),
                    "a Windows path": graph(**{"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "C:\\Users\\me\\x.png", "clip": ["4", 1]}}}),
                    "a share with forward slashes": graph(**{"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "//attacker.example/share/x", "clip": ["4", 1]}}}),
                    "a device path": graph(**{"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "//?/C:/Users/Public/x", "clip": ["4", 1]}}}),
                    "the drive's root": graph(**{"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "\\Users\\Public\\x", "clip": ["4", 1]}}}),
                    "a drive with no slash": graph(**{"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "D:Users\\x", "clip": ["4", 1]}}}),
                    "another account's home": graph(**{"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "~admin/x", "clip": ["4", 1]}}}),
                    "a JSON object": graph(**{"10": {"class_type": "SaveImage", "inputs": {"filename_prefix": {"__value__": ["x"]}, "images": ["8", 0]}}}),
                    "a URL": graph(**{"6": {"class_type": "CLIPTextEncode", "inputs": {"text": "http://192.168.1.1/admin", "clip": ["4", 1]}}}),
                    "a .. step": graph(**{"10": {"class_type": "SaveImage", "inputs": {"filename_prefix": "../../startup", "images": ["8", 0]}}}),
                    "a file of yours": graph(**{"10": {"class_type": "LoadImage", "inputs": {"image": "holiday.png"}}}),
                    "your output": graph(**{"10": {"class_type": "LoadImage", "inputs": {"image": "ComfyUI_00001_.png [output]"}}}),
                    "a name from another node": graph(**{"10": {"class_type": "LoadImage", "inputs": {"image": ["6", 0]}}}),
                }
                for why, g in refused.items():
                    status, h, data = await s.json("POST", "/prompt", body={"prompt": g, "prompt_id": str(uuid.uuid4())})
                    self.assertEqual(status, 400, why)
                    self.assertEqual(h.get("Access-Control-Allow-Origin"), FUOBE)
                    self.assertEqual(data["error"]["type"], "fuobe_connect_refused", why)
                for why, body in {
                    "no prompt id": {"prompt": graph()},
                    "a prompt id that isn't a UUID": {"prompt": graph(), "prompt_id": "../manager/reboot"},
                    "another client's socket": {"prompt": graph(), "prompt_id": str(uuid.uuid4()), "client_id": "0123456789abcdef"},
                    "an unknown field": {"prompt": graph(), "prompt_id": str(uuid.uuid4()), "number": -1},
                    "extra data a run never sends": {"prompt": graph(), "prompt_id": str(uuid.uuid4()), "extra_data": {"extra_pnginfo": {}}},
                }.items():
                    status, _, _ = await s.json("POST", "/prompt", body=body)
                    self.assertEqual(status, 400, why)
                self.assertEqual(app[COMFY].posts, [])
                # Its own staged input is fine.
                status, _, _ = await s.json("POST", "/prompt", body={"prompt": graph(**{"10": {"class_type": "LoadImage", "inputs": {"image": "fuobe_abcdefgh12.png"}}}), "prompt_id": str(uuid.uuid4())})
                self.assertEqual(status, 200)

        run(go())

    def test_upload_only_run_inputs(self):
        async def go():
            app, _ = with_connect()
            async with Server(app) as s:

                async def up(name, **fields):
                    form = aiohttp.FormData()
                    form.add_field("image", b"data", filename=name, content_type="image/png")
                    for k, v in fields.items():
                        form.add_field(k, v)
                    async with s.session.post(BASE + "/upload/image", data=form, headers=browser(FUOBE)) as res:
                        return res.status

                self.assertEqual(await up("fuobe_abcdefgh12.png", type="input", overwrite="true"), 200)
                self.assertEqual(app[COMFY].uploads[-1], {"name": "fuobe_abcdefgh12.png", "type": "input", "subfolder": None, "bytes": 4})
                for name, fields in [
                    ("my_photo.png", {"type": "input", "overwrite": "true"}),
                    ("fuobe_abcdefgh12.html", {"type": "input"}),
                    ("fuobe_abcdefgh12.svg", {"type": "input"}),
                    ("fuobe_abcdefgh12.png", {"type": "output"}),
                    ("fuobe_abcdefgh12.png", {"type": "temp"}),
                    ("fuobe_abcdefgh12.png", {"type": "input", "subfolder": "../../custom_nodes"}),
                    ("fuobe_abcdefgh12.png", {"type": "input", "original_ref": "{}"}),
                ]:
                    self.assertEqual(await up(name, **fields), 403, (name, fields))
                self.assertEqual(len(app[COMFY].uploads), 1)

        run(go())

    def test_studio_sees_only_its_own_prompts(self):
        async def go():
            comfy = Comfy()
            yours = str(uuid.uuid4())
            comfy.entry(yours, {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI"}}}, [{"filename": "ComfyUI_00001_.png", "subfolder": "", "type": "output"}])
            batch = str(uuid.uuid4())
            comfy.entry(batch, {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "fuobe/gen123abc"}}}, [{"filename": "gen123abc_00002_.png", "subfolder": "fuobe", "type": "output"}])
            running = str(uuid.uuid4())
            comfy.queue_running = [[1, running, {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI"}}}, {"client_id": "my-own-tab"}, ["1"]]]
            app, _ = with_connect(comfy=comfy)
            async with Server(app) as s:
                # Your prompt's history and its file: not the studio's.
                status, _, data = await s.json("GET", f"/history/{yours}")
                self.assertEqual((status, data), (200, {}))
                status, _, _ = await s.req("GET", "/view?filename=ComfyUI_00001_.png&subfolder=&type=output", browser(FUOBE))
                self.assertEqual(status, 403)
                status, _, data = await s.json("GET", f"/api/jobs/{yours}")
                self.assertEqual(status, 404)
                # The last prompts: only Fuobe's (a batch's passes under fuobe/<id>).
                status, _, data = await s.json("GET", "/history?max_items=64")
                self.assertEqual(list(data), [batch])
                status, _, _ = await s.req("GET", "/view?filename=gen123abc_00002_.png&subfolder=fuobe&type=output", browser(FUOBE))
                self.assertEqual(status, 200)
                status, _, data = await s.json("GET", "/api/jobs")
                self.assertEqual([j["id"] for j in data["jobs"]], [batch])
                # The queue: your prompt keeps its place, with nothing else about it.
                status, _, data = await s.json("GET", "/queue")
                self.assertEqual(data["queue_running"], [[1, None, None, {}, []]])
                # The bare history list is not for studios.
                status, _, _ = await s.req("GET", "/history", browser(FUOBE))
                self.assertEqual(status, 403)
                # ComfyUI's own page still sees everything.
                status, _, raw = await s.req("GET", "/history", {"Origin": BASE, "Sec-Fetch-Site": "same-origin"})
                self.assertEqual(set(json.loads(raw)), {yours, batch})

        run(go())

    def test_studio_controls_only_its_own_prompts(self):
        async def go():
            comfy = Comfy()
            yours = str(uuid.uuid4())
            app, _ = with_connect(comfy=comfy)
            async with Server(app) as s:
                mine = str(uuid.uuid4())
                status, _, _ = await s.json("POST", "/prompt", body={"prompt": graph(), "prompt_id": mine})
                self.assertEqual(status, 200)
                for body in [{"clear": True}, {"delete": [yours]}, {"delete": [mine], "clear": True}, {"delete": []}]:
                    status, _, _ = await s.json("POST", "/queue", body=body)
                    self.assertEqual(status, 403, body)
                status, _, _ = await s.json("POST", "/queue", body={"delete": [mine]})
                self.assertEqual(status, 200)
                status, _, _ = await s.json("POST", "/history", body={"delete": [mine]})
                self.assertEqual(status, 200)
                status, _, _ = await s.json("POST", "/history", body={"clear": True})
                self.assertEqual(status, 403)
                status, _, _ = await s.json("POST", "/interrupt", body={})
                self.assertEqual(status, 403)
                status, _, _ = await s.json("POST", "/interrupt", body={"prompt_id": yours})
                self.assertEqual(status, 403)
                status, _, _ = await s.json("POST", "/interrupt", body={"prompt_id": mine})
                self.assertEqual(status, 200)
                status, _, _ = await s.json("POST", f"/api/jobs/{yours}/cancel", body={})
                self.assertEqual(status, 403)
                status, _, _ = await s.json("POST", f"/api/jobs/{mine}/cancel", body={})
                self.assertEqual(status, 200)
                self.assertEqual([p for p in comfy.posts if isinstance(p, tuple) and p[0] == "cancel"], [("cancel", mine)])

        run(go())

    def test_prompt_id_of_yours_cant_be_taken(self):
        """ComfyUI takes a new prompt under any id, one it already holds too. A studio that learned the id of a prompt of
        yours (ComfyUI sends the progress of a prompt queued without a client id to every socket) can't make it its own."""

        async def go():
            comfy = Comfy()
            done, running, waiting = (str(uuid.uuid4()) for _ in range(3))
            yours = {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI", "images": ["2", 0]}}}
            comfy.entry(done, yours, [{"filename": "ComfyUI_00042_.png", "subfolder": "", "type": "output"}])
            comfy.queue_running = [[1, running, yours, {}, ["1"]]]
            comfy.queue_pending = [[2, waiting, yours, {}, ["1"]]]
            app, _ = with_connect(comfy=comfy)
            claim = {"9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "fuobe/gen123abc", "images": ["8", 0]}}}
            async with Server(app) as s:
                for pid in (done, running, waiting):
                    status, h, data = await s.json("POST", "/prompt", body={"prompt": claim, "prompt_id": pid, "client_id": "fuobe-abcdefgh"})
                    self.assertEqual((status, h.get("Access-Control-Allow-Origin")), (400, FUOBE), pid)
                    self.assertEqual(data["error"]["type"], "fuobe_connect_refused")
                self.assertEqual(comfy.posts, [])
                status, _, data = await s.json("GET", f"/history/{done}")
                self.assertEqual((status, data), (200, {}))
                status, _, _ = await s.req("GET", "/view?filename=ComfyUI_00042_.png&subfolder=&type=output", browser(FUOBE))
                self.assertEqual(status, 403)
                status, _, _ = await s.json("GET", f"/api/jobs/{done}")
                self.assertEqual(status, 404)
                status, _, _ = await s.json("POST", "/interrupt", body={"prompt_id": running})
                self.assertEqual(status, 403)
                for path in ("/queue", "/history"):
                    status, _, _ = await s.json("POST", path, body={"delete": [waiting]})
                    self.assertEqual(status, 403, path)
                status, _, _ = await s.json("POST", f"/api/jobs/{waiting}/cancel", body={})
                self.assertEqual(status, 403)
                status, _, data = await s.json("GET", "/queue")
                self.assertEqual((data["queue_running"], data["queue_pending"]), ([[1, None, None, {}, []]], [[2, None, None, {}, []]]))

        run(go())

    def test_prompt_is_the_studios_once_comfyui_took_it(self):
        """A prompt that ComfyUI refused never becomes the studio's, so a later prompt under that id isn't either."""

        async def go():
            comfy = Comfy()
            app, _ = with_connect(comfy=comfy)
            async with Server(app) as s:
                pid = str(uuid.uuid4())
                comfy.refuse = (400, {"error": {"type": "prompt_outputs_failed_validation"}})
                status, _, _ = await s.json("POST", "/prompt", body={"prompt": graph(), "prompt_id": pid})
                self.assertEqual(status, 400)
                comfy.refuse = None
                comfy.entry(pid, {"1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI"}}}, [{"filename": "ComfyUI_00007_.png", "subfolder": "", "type": "output"}])
                status, _, data = await s.json("GET", f"/history/{pid}")
                self.assertEqual(data, {})
                status, _, _ = await s.json("POST", "/interrupt", body={"prompt_id": pid})
                self.assertEqual(status, 403)
                # The id ComfyUI took is the studio's, and it can't be sent again.
                mine = str(uuid.uuid4())
                status, _, _ = await s.json("POST", "/prompt", body={"prompt": graph(), "prompt_id": mine})
                self.assertEqual(status, 200)
                status, _, _ = await s.json("POST", "/interrupt", body={"prompt_id": mine})
                self.assertEqual(status, 200)
                status, _, _ = await s.json("POST", "/prompt", body={"prompt": graph(), "prompt_id": mine})
                self.assertEqual(status, 400)

        run(go())

    def test_prompt_id_check_that_fails_refuses(self):
        def broken(pid):
            raise RuntimeError("no queue")

        studios = Studios(lambda: NODES, broken)
        answer = studios._prompt({"prompt": graph(), "prompt_id": str(uuid.uuid4())})
        self.assertEqual(answer.status, 503)
        self.assertEqual(len(studios.prompts), 0)

    def test_held_reads_comfyuis_queue(self):
        """comfy_held against ComfyUI's own PromptQueue (with COMFYUI_DIR), else against the stand-in."""
        cls = comfy_prompt_queue()
        if cls is None:
            comfy = Comfy()
            held = comfy_held(lambda: comfy)
            comfy.queue_pending = [[0, "a", {}, {}, []]]
            self.assertTrue(held("a"))
            comfy.queue_running, comfy.queue_pending = comfy.queue_pending, []
            self.assertTrue(held("a"))
            comfy.queue_running = []
            comfy.entry("a", {})
            self.assertTrue(held("a"))
            self.assertFalse(held("b"))
        else:
            q = cls(types.SimpleNamespace(queue_updated=lambda: None))
            held = comfy_held(lambda: q)
            self.assertFalse(held("a"))
            q.put((0, "a", {}, {}, [], {}))
            self.assertTrue(held("a"))
            _, i = q.get()
            self.assertTrue(held("a"))
            q.task_done(i, {"outputs": {}, "meta": {}}, None)
            self.assertEqual(q.get_tasks_remaining(), 0)
            self.assertTrue(held("a"))
            self.assertFalse(held("b"))

        # An older ComfyUI: get_history without map_function, and no get_current_queue_volatile.
        class Older:
            def get_history(self, prompt_id=None, max_items=None, offset=-1):
                return {"c": {}} if prompt_id == "c" else {}

            def get_current_queue(self):
                return [[0, "d", {}, {}, []]], [[1, "e", {}, {}, []]]

        older = comfy_held(Older)
        self.assertEqual([older(x) for x in "cdef"], [True, True, True, False])

    def test_older_comfyui_names_the_prompt(self):
        async def go():
            comfy = Comfy(mint_ids=True)
            app, _ = with_connect(comfy=comfy)
            async with Server(app) as s:
                status, _, data = await s.json("POST", "/prompt", body={"prompt": graph(), "prompt_id": str(uuid.uuid4())})
                self.assertEqual(status, 200)
                status, _, data = await s.json("GET", f"/history/{data['prompt_id']}")
                self.assertEqual(len(data), 1)

        run(go())

    def test_input_names_stay_hidden(self):
        async def go():
            app, _ = with_connect()
            async with Server(app) as s:
                status, _, data = await s.json("GET", "/object_info")
                self.assertEqual(data["LoadImage"]["input"]["required"]["image"][0], ["fuobe_abcdefgh12.png"])
                self.assertEqual(data["LoadAudio"]["input"]["required"]["audio"][1]["options"], ["fuobe_abcdefgh12.wav"])
                self.assertEqual(data["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0], ["model.safetensors"])
                status, _, raw = await s.req("GET", "/object_info")
                self.assertIn(b"holiday.png", raw)

        run(go())

    def test_preflights(self):
        async def go():
            app, _ = with_connect()
            async with Server(app) as s:
                status, h, _ = await s.req(
                    "OPTIONS",
                    "/prompt",
                    browser(FUOBE, **{"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type", "Access-Control-Request-Private-Network": "true"}),
                )
                self.assertEqual(status, 204)
                self.assertEqual(h.get("Access-Control-Allow-Origin"), FUOBE)
                self.assertEqual(h.get("Access-Control-Allow-Private-Network"), "true")
                self.assertIn("POST", h.get("Access-Control-Allow-Methods", ""))
                self.assertEqual(h.get("Access-Control-Allow-Headers"), "content-type")
                self.assertNotIn("Access-Control-Allow-Credentials", h)
                # A simple GET that Chrome preflights only for Private Network Access.
                status, h, _ = await s.req("OPTIONS", "/system_stats", browser(FUOBE, **{"Access-Control-Request-Method": "GET", "Access-Control-Request-Private-Network": "true"}))
                self.assertEqual((status, h.get("Access-Control-Allow-Private-Network")), (204, "true"))
                # A header a run never sends: Fuobe Connect refuses the preflight.
                status, h, _ = await s.req("OPTIONS", "/prompt", browser(FUOBE, **{"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-custom"}))
                self.assertEqual(status, 403)
                self.assertNotIn("Access-Control-Allow-Origin", h)
                # Another website's preflight: refused by ComfyUI, as before.
                status, h, _ = await s.req("OPTIONS", "/prompt", browser(EVIL, **{"Access-Control-Request-Method": "POST", "Access-Control-Request-Private-Network": "true"}))
                self.assertEqual(status, 403)
                self.assertNotIn("Access-Control-Allow-Private-Network", h)

        run(go())

    def test_other_websites_stay_out(self):
        async def go():
            app, _ = with_connect()
            async with Server(app) as s:
                for origin in [EVIL, "https://fuobe.com.evil.example", "http://fuobe.com", "https://fuobe.com:444", "null"]:
                    for method, path in [("GET", "/system_stats"), ("POST", "/prompt")]:
                        status, h, _ = await s.req(method, path, browser(origin), data=b"{}" if method == "POST" else None)
                        self.assertEqual(status, 403, f"{origin} {method} {path}")
                        self.assertNotIn("Access-Control-Allow-Origin", h)
                # Without Sec-Fetch-Site (an older browser): ComfyUI's host check still refuses it.
                status, _, _ = await s.req("POST", "/prompt", {"Origin": EVIL}, data=b"{}")
                self.assertEqual(status, 403)

        run(go())

    def test_only_this_computer(self):
        """A trusted studio reaches the ComfyUI on this computer, not one at a network address (unless LAN is on)."""

        async def go():
            app, _ = with_connect()
            async with Server(app) as s:
                for host in [f"192.168.1.5:{PORT}", f"comfy.local:{PORT}"]:
                    status, h, _ = await s.req("GET", "/system_stats", browser(FUOBE, Host=host))
                    self.assertEqual(status, 403, host)
                    self.assertNotIn("Access-Control-Allow-Origin", h)
                for host in [f"localhost:{PORT}", f"127.0.0.1:{PORT}"]:
                    status, _, _ = await s.req("GET", "/system_stats", browser(FUOBE, Host=host))
                    self.assertEqual(status, 200, host)
            app, _ = with_connect(lan=True)
            async with Server(app) as s:
                status, _, _ = await s.req("GET", "/system_stats", browser(FUOBE, Host=f"192.168.1.5:{PORT}"))
                self.assertEqual(status, 200)

        run(go())

    def test_no_studio_when_none(self):
        async def go():
            app, _ = with_connect(trusted=trusted_origins(None, "none"))
            async with Server(app) as s:
                status, _, _ = await s.req("GET", "/system_stats", browser(FUOBE))
                self.assertEqual(status, 403)

        run(go())

    def test_studio_reaches_only_what_a_run_needs(self):
        async def go():
            app, _ = with_connect()
            async with Server(app) as s:
                for method, path in [("GET", "/userdata/comfy.settings.json"), ("POST", "/v2/manager/queue/install"), ("POST", "/fuobe/connect/studios"), ("GET", "/view?filename=a.png&subfolder=&type=input"), ("GET", "/view?filename=a.png%20[input]&subfolder=&type=output")]:
                    status, h, _ = await s.req(method, path, browser(FUOBE, **{"Content-Type": "application/json"}), data=b'{"studios":"https://evil.example"}' if method == "POST" else None)
                    self.assertEqual(status, 403, path)
                    self.assertNotIn("Access-Control-Allow-Origin", h)
                status, h, _ = await s.req("OPTIONS", "/v2/manager/queue/install", browser(FUOBE, **{"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}))
                self.assertEqual(status, 403)
                self.assertNotIn("Access-Control-Allow-Origin", h)

        run(go())

    def test_comfyui_page_and_programs_unchanged(self):
        async def go():
            app, _ = with_connect()
            async with Server(app) as s:
                status, h, _ = await s.req("POST", "/prompt", {"Origin": BASE, "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}, data=b'{"prompt": {"1": {"class_type": "Anything", "inputs": {"path": "C:/x"}}}}')
                self.assertEqual(status, 200)
                self.assertNotIn("Access-Control-Allow-Origin", h)
                status, _, _ = await s.req("GET", "/userdata/x")
                self.assertEqual(status, 200)

        run(go())

    def test_websocket(self):
        async def go():
            app, _ = with_connect()
            async with Server(app) as s:
                async with s.session.ws_connect(f"ws://127.0.0.1:{PORT}/ws?clientId=fuobe-abc12345", headers=browser(FUOBE, **{"Sec-Fetch-Mode": "websocket"})) as ws:
                    msg = await ws.receive(timeout=5)
                    self.assertIn("status", msg.data)
                # Your own ComfyUI tab's socket can't be taken over.
                for origin, cid in [(FUOBE, "abc12345"), (EVIL, "fuobe-abc12345")]:
                    with self.assertRaises(aiohttp.WSServerHandshakeError) as e:
                        async with s.session.ws_connect(f"ws://127.0.0.1:{PORT}/ws?clientId={cid}", headers=browser(origin, **{"Sec-Fetch-Mode": "websocket"})):
                            pass
                    self.assertEqual(e.exception.status, 403)

        run(go())

    def test_answers_the_guard_question(self):
        async def go():
            app, _ = with_connect()
            async with Server(app) as s:
                status, h, body = await s.req("GET", "/fuobe/guard", browser(FUOBE))
                self.assertEqual((status, h.get("Access-Control-Allow-Origin")), (200, FUOBE))
                self.assertIn(b'"guard": 1', body)
                status, _, _ = await s.req("GET", "/fuobe/guard", browser(EVIL))
                self.assertEqual(status, 403)

        run(go())

    def test_status_says_less_to_a_studio(self):
        async def go():
            app, _ = with_connect()
            async with Server(app) as s:
                status, h, body = await s.req("GET", "/fuobe/connect", browser(FUOBE))
                self.assertEqual((status, h.get("Access-Control-Allow-Origin")), (200, FUOBE))
                self.assertNotIn(b"studios", body)
                self.assertIn(b'"connect": 1', body)
                status, _, body = await s.req("GET", "/fuobe/connect")
                self.assertIn(b"studios", body)
                # A program can't pass itself off as a studio to get less, or more: the marker is dropped on the way in.
                status, _, body = await s.req("GET", "/fuobe/connect", {MARKER: FUOBE})
                self.assertIn(b"studios", body)
                # Only ComfyUI's own page changes the studios.
                status, _, _ = await s.req("POST", "/fuobe/connect/studios", {"Origin": BASE, "Content-Type": "application/json"}, data=b'{"studios":"https://studio.example.com"}')
                self.assertEqual(status, 200)

        run(go())


class WithCorsFlagTest(unittest.TestCase):
    """ComfyUI started with --enable-cors-header: its own check is off, so Fuobe Connect does what Fuobe's guard did."""

    def test_guard(self):
        async def go():
            app, _ = with_connect(cors_flag="https://other-studio.example")
            async with Server(app) as s:
                status, h, _ = await s.req("GET", "/system_stats", browser(FUOBE))
                self.assertEqual((status, h.get("Access-Control-Allow-Origin")), (200, FUOBE))
                status, _, _ = await s.req("POST", "/prompt", browser(EVIL), data=b"{}")
                self.assertEqual(status, 403)
                status, _, _ = await s.req("POST", "/prompt", browser("https://other-studio.example"), data=b"{}")
                self.assertEqual(status, 200)
                status, _, _ = await s.req("POST", "/v2/manager/queue/install", browser(FUOBE), data=b"{}")
                self.assertEqual(status, 403)
                status, _, _ = await s.req("POST", "/prompt", {"Sec-Fetch-Site": "cross-site"}, data=b"{}")
                self.assertEqual(status, 403)
                # A page on another port of this computer isn't ComfyUI's own page.
                status, _, _ = await s.req("POST", "/prompt", {"Origin": "http://127.0.0.1"}, data=b"{}")
                self.assertEqual(status, 403)

        run(go())

    def test_flag_naming_a_studio_gives_it_nothing_more(self):
        """--enable-cors-header <a trusted studio> switches ComfyUI's own check off for that site. The studio still gets
        only the checked paths, and only at this computer's own address (or a network address with FUOBE_CONNECT_LAN=1),
        never under a name that points at this computer."""

        def form(name, kind):
            f = aiohttp.FormData()
            f.add_field("image", b"data", filename=name, content_type="image/png")
            f.add_field("type", kind)
            return f

        async def go(flag, lan):
            comfy = Comfy()
            app, _ = with_connect(cors_flag=flag, comfy=comfy, lan=lan)
            async with Server(app) as s:
                unlisted = {"1": {"class_type": "SomeUnlistedNode", "inputs": {"path": "C:/Windows/x"}}}
                for host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"127.0.0.1.nip.io:{PORT}", f"localtest.me:{PORT}"):
                    for method, path in [
                        ("POST", "/v2/manager/queue/install"),
                        ("POST", "/api/v2/manager/queue/install"),
                        ("GET", "/userdata/comfy.settings.json"),
                        ("POST", "/settings/Comfy.Server.LaunchArgs"),
                        ("POST", "/upload/mask"),
                        ("GET", "/internal/logs"),
                    ]:
                        status, h, _ = await s.req(method, path, browser(FUOBE, Host=host, **{"Content-Type": "application/json"}), data=b"{}" if method == "POST" else None)
                        self.assertEqual(status, 403, (flag, lan, host, path))
                        self.assertNotIn("Access-Control-Allow-Origin", h)
                    status, h, _ = await s.req("OPTIONS", "/v2/manager/queue/install", browser(FUOBE, Host=host, **{"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}))
                    self.assertEqual(status, 403, (flag, lan, host))
                    self.assertNotIn("Access-Control-Allow-Origin", h)
                    # The paths a run needs: checked here, or (this computer under another name) refused.
                    checked = lan or host.startswith(("127.0.0.1:", "localhost:"))
                    status, h, _ = await s.req("POST", "/prompt", browser(FUOBE, Host=host, **{"Content-Type": "application/json"}), data=prompt_body(unlisted))
                    self.assertEqual(status, 400 if checked else 403, (flag, lan, host))
                    async with s.session.post(BASE + "/upload/image", data=form("my_photo.png", "output"), headers=browser(FUOBE, Host=host)) as res:
                        self.assertEqual(res.status, 403, (flag, lan, host))
                        self.assertEqual(res.headers.get("Access-Control-Allow-Origin"), FUOBE if checked else None)
                    status, h, _ = await s.req("GET", "/system_stats", browser(FUOBE, Host=host))
                    self.assertEqual((status, h.get("Access-Control-Allow-Origin")), (200, FUOBE) if checked else (403, None), (flag, lan, host))
                self.assertEqual((comfy.posts, comfy.uploads), ([], []))
                status, _, _ = await s.req("POST", "/prompt", browser(FUOBE, **{"Content-Type": "application/json"}), data=prompt_body())
                self.assertEqual(status, 200)

        for flag in (FUOBE, "https://www.fuobe.com", "*"):
            for lan in (False, True):
                run(go(flag, lan))
        with self.assertLogs(level="WARNING") as logs:
            with_connect(cors_flag=FUOBE)
        self.assertTrue(any("isn't needed for that studio" in line for line in logs.output), logs.output)

    def test_flag_that_isnt_an_origin(self):
        """--enable-cors-header with a value that isn't a full origin: a sandboxed page ("null") stays out."""

        async def go():
            app, _ = with_connect(cors_flag="localhost:8080")
            async with Server(app) as s:
                for origin in ["null", EVIL]:
                    status, _, _ = await s.req("POST", "/prompt", {"Origin": origin, "Content-Type": "text/plain"}, data=b"{}")
                    self.assertEqual(status, 403, origin)
                status, _, _ = await s.req("POST", "/prompt", {"Origin": BASE}, data=b"{}")
                self.assertEqual(status, 200)

        run(go())

    def test_star_adds_no_check(self):
        async def go():
            app, _ = with_connect(cors_flag="*")
            async with Server(app) as s:
                status, _, _ = await s.req("POST", "/prompt", browser(EVIL), data=b"{}")
                self.assertEqual(status, 200)

        run(go())


if __name__ == "__main__":
    unittest.main()
