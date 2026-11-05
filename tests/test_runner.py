"""The paired runner (runner.py) with a stand-in studio and a stand-in ComfyUI, in memory: what it refuses, which ids
reach an address, and what it reads back or takes off."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fuobe_connect.net import Answer, TooBig  # noqa: E402
from fuobe_connect.runner import SEND_TRIES, Runner, Stopped  # noqa: E402

NODES = frozenset({"LoadImage", "SaveImage", "CLIPTextEncode", "AILab_Qwen3ASRSubtitle", "MiniMaxH3SourceTimeline", "WanVideoModelLoader"})
DATA = b"picture"
SHA = hashlib.sha256(DATA).hexdigest()
LEASE = "a" * 64


class FakeSite:
    def __init__(self, answers=None, file=DATA):
        self.calls: list = []
        self.answers = answers or {}
        self.file = file

    async def call(self, method, path, body=None, headers=None, data=None, timeout=60):
        self.calls.append((method, path, body))
        for key, a in self.answers.items():
            if key in path and (body or {}).get("event") in (None, a.get("_event", (body or {}).get("event"))):
                return Answer(200, {k: v for k, v in a.items() if k != "_event"}, {})
        return Answer(200, {}, {})

    async def fetch_file(self, path, limit, headers=None):
        self.calls.append(("FILE", path, limit))
        if len(self.file) > limit:
            raise TooBig("big")
        return 200, io.BytesIO(self.file), len(self.file), hashlib.sha256(self.file).hexdigest(), {}

    def events(self):
        return [c[2] for c in self.calls if c[0] == "POST" and c[1].endswith("/report")]


class FakeComfy:
    def __init__(self, upload_fails=False, history=None, queue=None):
        self.calls: list = []
        self.upload_fails = upload_fails
        self.history = history or {}
        self.queue = queue or {"queue_running": [], "queue_pending": []}
        self.session = None
        self._ssl = None

    async def get_json(self, path, timeout=30):
        self.calls.append(("GET", path))
        if path == "/system_stats":
            return {"system": {}, "devices": []}
        if path == "/api/jobs":
            raise RuntimeError("no jobs api")
        if path == "/queue":
            return self.queue
        if path.startswith("/history/"):
            pid = path[9:]
            return {pid: self.history[pid]} if pid in self.history else {}
        if path.startswith("/history?"):
            return self.history
        return {}

    async def get_status(self, path):
        self.calls.append(("GET", path))
        return 404, None

    async def post_json(self, path, body, timeout=60):
        self.calls.append(("POST", path, body))
        if path == "/prompt":
            self.history[body["prompt_id"]] = {"prompt": [1, body["prompt_id"], body["prompt"]], "outputs": {"9": {"images": [{"filename": "gen_00001_.png", "subfolder": "fuobe", "type": "output"}]}}, "status": {"completed": True, "status_str": "success"}}
            return 200, {"prompt_id": body["prompt_id"], "number": 1}
        return 200, {}

    async def upload_input(self, name, data, content_type):
        self.calls.append(("UPLOAD", name))
        if self.upload_fails:
            raise ConnectionResetError("ComfyUI went away")
        return 200

    async def view(self, filename, subfolder, kind, limit=0):
        self.calls.append(("VIEW", filename, subfolder, kind))
        return io.BytesIO(b"result"), 6


async def no_sleep(_s):
    return None


def task(**patch) -> dict:
    t = {
        "id": "task_abcdefgh1234",
        "lease": LEASE,
        "promptId": str(uuid.uuid4()),
        "prefix": "fuobe/gen123abc",
        "graph": {
            "1": {"class_type": "LoadImage", "inputs": {"image": "fuobe_abcdefgh12.png"}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "fuobe/gen123abc", "images": ["1", 0]}},
        },
        "inputs": [{"n": 0, "name": "fuobe_abcdefgh12.png", "size": len(DATA), "sha256": SHA, "contentType": "image/png"}],
    }
    t.update(patch)
    return t


def runner(site=None, comfy=None, **kw) -> Runner:
    return Runner(site or FakeSite(), comfy or FakeComfy(), "connect-test", 8188, sleep=no_sleep, nodes=lambda: NODES, **kw)


def run(coro):
    return asyncio.run(coro)


class VetTest(unittest.TestCase):
    def refused(self, t, reason):
        site = FakeSite()
        comfy = FakeComfy()
        jobs = []
        run(runner(site, comfy, on_job=jobs.append).run(t))
        self.assertEqual(site.events(), [{"event": "failed", "reason": reason}], t)
        self.assertFalse([c for c in comfy.calls if c[0] in ("UPLOAD", "POST")], "nothing reached ComfyUI")
        self.assertTrue(jobs and jobs[0]["state"].startswith("refused"))

    def test_refusals(self):
        self.refused(task(graph={"1": {"class_type": "KJ_RunPython", "inputs": {"code": "x"}}}), "unknown-node")
        self.refused(task(graph={"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "C:/Users/me/x.png"}}}), "unsafe-path")
        self.refused(task(graph={"1": {"class_type": "LoadImage", "inputs": {"image": "my_photo.png"}}}), "unsafe-graph")
        # A node that writes or reads a file at a path it is given: only Fuobe's own form, in pairing mode too.
        self.refused(task(graph={"1": {"class_type": "AILab_Qwen3ASRSubtitle", "inputs": {"output_path": "//?/C:/Users/Public/x.srt", "output_format": "srt"}}}), "unsafe-path")
        self.refused(task(graph={"1": {"class_type": "MiniMaxH3SourceTimeline", "inputs": {"video_path": "ComfyUI/input/private.mp4", "audio_path": ""}}}), "unsafe-path")
        self.refused(task(graph={"1": {"class_type": "WanVideoModelLoader", "inputs": {"extra_model": {"path": "x.safetensors"}}}}), "unsafe-graph")
        # The studio's input names: only Fuobe's own, each once, with a file type a run sends.
        for name in ["my_photo.png", "fuobe_abcdefgh12.html", "ComfyUI_00001_.png [output]"]:
            t = task()
            t["inputs"][0]["name"] = name
            t["graph"]["1"]["inputs"]["image"] = name
            self.refused(t, "unsafe-graph")
        t = task()
        t["inputs"].append(dict(t["inputs"][0], n=1))
        self.refused(t, "unsafe-graph")
        self.refused(task(promptId="../manager/reboot"), "unsafe-graph")
        t = task()
        t["inputs"][0]["n"] = "0/../../x"
        self.refused(t, "unsafe-graph")

    def test_bad_task_id_is_skipped(self):
        site = FakeSite()
        run(runner(site).run(task(id="../../api/settings")))
        run(runner(site).run(task(lease="x\r\nX-Other: 1")))
        self.assertEqual(site.calls, [])


class RunTest(unittest.TestCase):
    def test_a_job_runs_and_uploads_its_own_result(self):
        site = FakeSite({"/report": {"_event": "finished", "wanted": [{"n": 0, "filename": "gen_00001_.png", "subfolder": "fuobe", "type": "output"}]}, "/outputs/0": {"received": 6, "done": True}})
        comfy = FakeComfy()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "fuobe_abcdefgh12.png"), "wb") as f:
                f.write(DATA)
            with open(os.path.join(d, "keep.png"), "wb") as f:
                f.write(DATA)
            r = runner(site, comfy, input_dir=d)
            t = task()
            run(r.run(t))
            self.assertEqual(r.jobs[t["id"]]["phase"], "done")
            self.assertIn(t["promptId"], r.own)
            # The job's own input leaves the input folder; nothing else does.
            self.assertEqual(sorted(os.listdir(d)), ["keep.png"])
        self.assertIn(("VIEW", "gen_00001_.png", "fuobe", "output"), comfy.calls)
        self.assertTrue(any(c[0] == "PUT" and c[1].startswith("/api/gpu/computer/tasks/task_abcdefgh1234/outputs/0?") for c in site.calls))

    def test_follow_only_its_own_batch(self):
        """The runner follows only its own job's prefix, never another one (ComfyUI's default 'ComfyUI')."""
        yours = str(uuid.uuid4())
        comfy = FakeComfy(history={yours: {"prompt": [1, yours, {"9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI"}}}], "outputs": {"9": {"images": [{"filename": "ComfyUI_00001_.png", "subfolder": "", "type": "output"}]}}}})
        site = FakeSite({"/report": {"_event": "finished", "follow": "ComfyUI"}})
        r = runner(site, comfy)
        t = task()
        run(r.run(t))
        self.assertIn({"event": "failed", "reason": "unsafe-graph"}, site.events())
        self.assertFalse(any(c[0] == "VIEW" for c in comfy.calls))
        self.assertFalse(any(e.get("event") == "passes" for e in site.events()))

    def test_resume_reads_only_its_own_prompts(self):
        """Collecting a prompt this runner never queued: its history isn't read, so nothing of yours is uploaded."""
        yours = str(uuid.uuid4())
        comfy = FakeComfy(history={yours: {"prompt": [1, yours, {}], "outputs": {"9": {"images": [{"filename": "mine.png", "subfolder": "", "type": "output"}]}}}})
        site = FakeSite()
        r = runner(site, comfy)
        run(r.run(task(promptId=yours, resume="collecting", wanted=[{"n": 0, "filename": "mine.png", "subfolder": "", "type": "output"}])))
        self.assertNotIn(("GET", f"/history/{yours}"), comfy.calls)
        self.assertFalse(any(c[0] == "VIEW" for c in comfy.calls))
        self.assertIn({"event": "failed", "reason": "output-missing"}, site.events())

    def test_a_prompt_id_already_in_use_isnt_followed(self):
        yours = str(uuid.uuid4())
        comfy = FakeComfy(history={yours: {"prompt": [1, yours, {}], "outputs": {}, "status": {"completed": True}}})
        site = FakeSite()
        run(runner(site, comfy).run(task(promptId=yours)))
        self.assertEqual(site.events(), [{"event": "failed", "reason": "unsafe-graph"}])

    def test_retries_end(self):
        """A ComfyUI that answers but keeps failing uploads: a few tries, then comfy-error (no endless retry)."""
        site = FakeSite()
        comfy = FakeComfy(upload_fails=True)
        r = runner(site, comfy)
        run(r.run(task()))
        self.assertEqual(len([c for c in comfy.calls if c[0] == "UPLOAD"]), SEND_TRIES)
        self.assertEqual(site.events()[-1], {"event": "failed", "reason": "comfy-error"})

    def test_input_bigger_than_said(self):
        site = FakeSite(file=DATA * 10)
        run(runner(site).run(task()))
        self.assertEqual(site.events(), [{"event": "failed", "reason": "input-changed"}])

    def test_refused_answer_is_slimmed(self):
        class Refusing(FakeComfy):
            async def post_json(self, path, body, timeout=60):
                return 400, {"error": {"type": "prompt_outputs_failed_validation", "message": "x"}, "node_errors": {"4": {"class_type": "CheckpointLoaderSimple", "errors": [{"message": "Value not in list", "details": "ckpt_name: 'a' not in ['mine.safetensors']", "extra_info": {"received_value": "a"}}]}}}

        site = FakeSite()
        run(runner(site, Refusing()).run(task()))
        ev = site.events()[-1]
        self.assertEqual(ev["event"], "refused")
        self.assertNotIn("mine.safetensors", str(ev))

    def test_paused_takes_no_job(self):
        site = FakeSite()
        r = runner(site, paused=lambda: True)
        r.runner = True
        self.assertFalse(run(r.claim_and_run()))
        self.assertFalse(any("/claim" in c[1] for c in site.calls))


class TakeOffTest(unittest.TestCase):
    def test_only_its_own_prompts(self):
        yours = str(uuid.uuid4())
        mine = str(uuid.uuid4())
        pass_id = str(uuid.uuid4())
        comfy = FakeComfy(queue={"queue_running": [[1, yours, {"9": {"inputs": {"filename_prefix": "ComfyUI"}}}]], "queue_pending": [[2, pass_id, {"9": {"inputs": {"filename_prefix": "fuobe/gen123abc"}}}]]})
        r = runner(FakeSite(), comfy)
        run(r.take_off({"task": "task_1", "promptId": yours, "prefix": "ComfyUI"}))
        self.assertFalse([c for c in comfy.calls if c[0] == "POST"])
        r.own.add(mine)
        r.jobs_api = False
        r.prefixes.add("fuobe/gen123abc")
        comfy.queue = {"queue_running": [], "queue_pending": [[2, pass_id, {"9": {"inputs": {"filename_prefix": "fuobe/gen123abc"}}}]]}

        async def go():
            async def drop(_s):
                comfy.queue = {"queue_running": [], "queue_pending": []}

            r.sleep = drop
            await r.take_off({"task": "task_2", "promptId": mine, "prefix": "fuobe/gen123abc"})

        run(go())
        posted = [c[1] for c in comfy.calls if c[0] == "POST"]
        self.assertIn("/queue", posted)  # its own prompt (no jobs API here)
        self.assertIn(f"/api/jobs/{pass_id}/cancel", posted)
        self.assertFalse(any(yours in p for p in posted))
        self.assertIn("task_2", r.stopped_done)

    def test_bad_ids(self):
        comfy = FakeComfy()
        r = runner(FakeSite(), comfy)
        run(r.take_off({"task": "../x", "promptId": str(uuid.uuid4())}))
        run(r.take_off({"task": "t1", "promptId": "../../manager/reboot"}))
        self.assertEqual(comfy.calls, [])


class StoppedTest(unittest.TestCase):
    def test_stopped_is_an_exception(self):
        self.assertTrue(issubclass(Stopped, Exception))


if __name__ == "__main__":
    unittest.main()
