"""Running a person's Fuobe jobs on this ComfyUI, after pairing.

The same protocol the Fuobe tab speaks (src/lib/computer-runner.ts in the Fuobe repo): beat to say this computer is
here, claim the person's next job, send its inputs and its exact graph to this ComfyUI, follow it, report back, and
upload the results the studio asks for. It decides nothing about a result. Only that person's jobs reach it: the studio
picks them by the token, which belongs to one person.

Before a job runs, Fuobe Connect checks it here, whatever the studio says (graph.py): every node type is on the list
this pack ships (plus your own fuobe-connect-nodes.json), no text names a path or an address, a node's file or folder
setting holds only Fuobe's own form, the inputs have the names Fuobe gives its files, and every loader reads one of
them. Every id the studio sends is checked before it goes
into an address. Only the prompts this runner queued are followed, taken off or read back, and a result is uploaded only
when this job's own prompt (or its batch's passes, under the job's own fuobe/<id> prefix) saved it. One job at a time.
Pausing (ComfyUI's Connect to Fuobe window) stops it taking new jobs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid

import aiohttp

from .graph import (
    PREFIX,
    TASK_ID,
    UUID,
    NodeList,
    graph_strings,
    has_prefix,
    may_download,
    refusal,
    run_prefix,
    safe_wanted,
    slim,
    staged_name_ok,
    vet_graph,
)
from .net import Comfy, Site, TooBig, seg

log = logging.getLogger("fuobe_connect")

BEAT_S = 10
BUSY_BEAT_S = 5
POLL_S = 1.0
PROGRESS_EVERY_S = 0.5
PART_BYTES = 16 * 2**20
SAVE_WAIT_S = 60 * 60
# The most one input may be when the studio doesn't say its size.
MAX_INPUT = 4 * 2**30
SEND_TRIES = 3
MAX_INPUTS = 64

SHA256 = re.compile(r"^[0-9a-f]{64}\Z")
LEASE = re.compile(r"^[A-Za-z0-9_-]{16,128}\Z")


class Stopped(Exception):
    """The job ended on the studio, or another computer has it now: let go of it."""


class Unpaired(Exception):
    """The studio no longer knows this token."""


def _retry_after(headers: dict) -> float:
    try:
        return min(30.0, max(1.0, float(headers.get("Retry-After") or headers.get("retry-after") or 2)))
    except ValueError:
        return 2.0


def _small_int(n) -> bool:
    return isinstance(n, int) and not isinstance(n, bool) and 0 <= n < 1000


class Runner:
    def __init__(
        self,
        site: Site,
        comfy: Comfy,
        device: str,
        port: int,
        on_state=None,
        sleep=asyncio.sleep,
        clock=time.monotonic,
        nodes=None,
        own: set | None = None,
        prefixes: set | None = None,
        input_dir: str | None = None,
        paused=None,
        on_job=None,
        loras: bool = True,
    ):
        self.site = site
        self.comfy = comfy
        self.device = device
        self.port = port
        self.client_id = uuid.uuid4().hex
        self.on_state = on_state or (lambda state, message=None: None)
        self.sleep = sleep
        self.clock = clock
        self.alive = True
        self.jobs: dict[str, dict] = {}
        self.events: dict[str, dict] = {}
        self.stopped_done: set[str] = set()
        self.stopping: set[str] = set()
        self.jobs_api: bool | None = None
        self.facts_at = -1e9
        self.loras_at = -1e9
        self.runner = False
        self.ws_task: asyncio.Task | None = None
        self.ws_open = False
        self.comfy_ok = False
        # What this ComfyUI may run (the shipped list and the person's own file).
        self.nodes = nodes or NodeList()
        # The prompts Fuobe Connect queued on this ComfyUI (kept by the agent across runners), and the jobs' prefixes.
        self.own = own if own is not None else set()
        self.prefixes = prefixes if prefixes is not None else set()
        self.input_dir = input_dir
        self.paused = paused or (lambda: False)
        self.on_job = on_job or (lambda record: None)
        self.share_loras = loras

    def _own(self, pid: str):
        if len(self.own) > 4096:
            self.own.clear()
        self.own.add(pid)

    # ------------------------------------------------------------------
    # This ComfyUI

    async def look(self) -> dict | None:
        try:
            stats = await self.comfy.get_json("/system_stats", timeout=10)
            self.comfy_ok = True
            return stats
        except Exception:
            self.comfy_ok = False
            return None

    async def facts(self, loras: bool) -> dict | None:
        s = await self.look()
        if not s:
            return None
        system = s.get("system") or {}
        dev = (s.get("devices") or [{}])[0] or {}
        f = {"guard": True}
        if isinstance(dev.get("name"), str):
            f["device"] = dev["name"][:120]
        if isinstance(dev.get("vram_total"), (int, float)):
            f["vramBytes"] = dev["vram_total"]
        if isinstance(system.get("ram_total"), (int, float)):
            f["ramBytes"] = system["ram_total"]
        if system.get("comfyui_version"):
            f["version"] = str(system["comfyui_version"])[:40]
        if system.get("os"):
            f["os"] = str(system["os"])[:40]
        if isinstance(system.get("embedded_python"), bool):
            f["portable"] = system["embedded_python"]
        if loras and self.share_loras:
            try:
                names = await self.comfy.get_json("/models/loras")
                if isinstance(names, list):
                    f["loras"] = [n for n in names if isinstance(n, str)][:20000]
            except Exception:
                pass
        return f

    def _socket(self):
        if self.ws_task and not self.ws_task.done():
            return
        self.ws_task = asyncio.ensure_future(self._listen())

    async def _listen(self):
        try:
            async with self.comfy.session.ws_connect(self.comfy.ws_url(self.client_id), heartbeat=30, ssl=self.comfy._ssl) as ws:
                self.ws_open = True
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        m = json.loads(msg.data)
                    except ValueError:
                        continue
                    d = m.get("data") if isinstance(m, dict) and isinstance(m.get("data"), dict) else {}
                    pid = d.get("prompt_id")
                    if not isinstance(pid, str) or pid not in self.own:
                        continue
                    cur = self.events.setdefault(pid, {"nodesDone": 0})
                    t = m.get("type")
                    if t == "progress" and isinstance(d.get("value"), (int, float)) and isinstance(d.get("max"), (int, float)):
                        cur.update(value=d["value"], max=d["max"], node=d.get("node"))
                    elif t == "executing" and d.get("node"):
                        cur.update(node=d["node"], nodesDone=cur["nodesDone"] + 1, value=None, max=None)
        except Exception:
            pass
        finally:
            self.ws_open = False

    async def where(self, prompt_id: str, prefix: str | None = None) -> dict:
        if not UUID.match(prompt_id):
            return {"s": "absent"}
        pid = seg(prompt_id)
        if self.jobs_api is None:
            try:
                await self.comfy.get_json("/api/jobs")
                self.jobs_api = True
            except Exception:
                self.jobs_api = False
        if self.jobs_api:
            status, j = await self.comfy.get_status(f"/api/jobs/{pid}")
            if status == 404:
                return await self.where_by_queue(prompt_id) if prefix else {"s": "absent"}
            if status != 200:
                raise RuntimeError(f"ComfyUI answered HTTP {status}")
            st = (j or {}).get("status")
            if st == "in_progress":
                return {"s": "running"}
            if st == "pending":
                return await self.where_by_queue(prompt_id)
            entry = (await self.comfy.get_json(f"/history/{pid}")).get(prompt_id)
            return {"s": "done", "entry": entry} if entry else {"s": "absent"}
        entry = (await self.comfy.get_json(f"/history/{pid}")).get(prompt_id)
        if entry and ((entry.get("status") or {}).get("completed") or (entry.get("status") or {}).get("status_str")):
            return {"s": "done", "entry": entry}
        return await self.where_by_queue(prompt_id)

    async def where_by_queue(self, prompt_id: str) -> dict:
        q = await self.comfy.get_json("/queue")
        running = q.get("queue_running") or []
        if any(len(r) > 1 and r[1] == prompt_id for r in running):
            return {"s": "running"}
        pending = sorted(q.get("queue_pending") or [], key=lambda r: float(r[0]) if r else 0)
        for i, r in enumerate(pending):
            if len(r) > 1 and r[1] == prompt_id:
                return {"s": "pending", "ahead": i + len(running)}
        entry = (await self.comfy.get_json(f"/history/{seg(prompt_id)}")).get(prompt_id)
        return {"s": "done", "entry": entry} if entry else {"s": "absent"}

    # ------------------------------------------------------------------
    # The studio

    async def report(self, t: dict, event: dict) -> dict:
        until = self.clock() + 120
        tries = 0
        while True:
            a = await self.site.call("POST", f"/api/gpu/computer/tasks/{seg(t['id'])}/report", event, headers={"X-Fuobe-Lease": t["lease"]})
            if a.status == 200:
                return a.body
            if a.status == 401:
                raise Unpaired()
            if a.status in (404, 409):
                raise Stopped()
            if a.status == 429 and self.clock() < until:
                await self.sleep(_retry_after(a.headers))
                continue
            tries += 1
            if tries > 4:
                raise RuntimeError(f"The studio answered HTTP {a.status}")
            await self.sleep(tries)

    async def beat(self) -> dict | None:
        now = self.clock()
        due = now - self.facts_at > 60
        loras = due and now - self.loras_at > 300
        read = await self.facts(loras) if due else (await self.look() and {})
        paused = bool(self.paused())
        body: dict = {"device": self.device, "port": self.port, "ready": read is not None and not paused}
        if due and read:
            self.facts_at = now
            if loras:
                self.loras_at = now
            body["facts"] = read
        running = []
        for task_id, j in self.jobs.items():
            if j["phase"] in ("done", "failed"):
                continue
            item = {"task": task_id}
            if j.get("position") is not None:
                item["position"] = j["position"]
            ev = self.events.get(j.get("promptId") or "")
            if j["phase"] == "running" and ev:
                item.update({k: ev[k] for k in ("value", "max", "nodesDone") if ev.get(k) is not None})
            running.append(item)
        body["running"] = running
        stopped = list(self.stopped_done)
        if stopped:
            body["stopped"] = stopped
        a = await self.site.call("POST", "/api/gpu/computer/beat", body)
        if a.status == 401:
            raise Unpaired()
        if a.status != 200:
            self.runner = False
            self.on_state("waiting", a.body.get("error") if isinstance(a.body.get("error"), str) else f"The studio answered HTTP {a.status}")
            return None
        for s in stopped:
            self.stopped_done.discard(s)
        self.runner = bool(a.body.get("runner"))
        if paused:
            self.on_state("paused", "Fuobe jobs are paused on this computer.")
        elif not self.comfy_ok:
            self.on_state("comfy-down", "ComfyUI isn't answering here.")
        elif self.runner:
            self.on_state("connected", None)
        else:
            self.on_state("elsewhere", "Another computer runs your Fuobe jobs now.")
        for s in (a.body.get("stops") or [])[:50]:
            if isinstance(s, dict):
                asyncio.ensure_future(self.take_off(s))
        return a.body

    async def _cancel(self, pid: str):
        if self.jobs_api is not False:
            status, _ = await self.comfy.post_json(f"/api/jobs/{seg(pid)}/cancel", {})
            if status == 404:
                self.jobs_api = False
        if self.jobs_api is False:
            await self.comfy.post_json("/queue", {"delete": [pid]})
            q = await self.comfy.get_json("/queue")
            if any(len(r) > 1 and r[1] == pid for r in q.get("queue_running") or []):
                await self.comfy.post_json("/interrupt", {"prompt_id": pid})

    async def take_off(self, s: dict):
        """Take a job's prompt off this ComfyUI (only one this runner queued) and its batch's later passes (only under a
        prefix of a job this runner ran), then tell the studio."""
        task_id = s.get("task")
        pid = s.get("promptId")
        if not isinstance(task_id, str) or not TASK_ID.match(task_id) or task_id in self.stopping or not isinstance(pid, str) or not UUID.match(pid):
            return
        self.stopping.add(task_id)
        try:
            if pid in self.own:
                await self._cancel(pid)
            prefix = s.get("prefix")
            if isinstance(prefix, str) and PREFIX.match(prefix) and prefix in self.prefixes:
                for _ in range(5):
                    q = await self.comfy.get_json("/queue")
                    ids = [str(r[1]) for r in (q.get("queue_running") or []) + (q.get("queue_pending") or []) if len(r) > 2 and has_prefix(r[2], prefix) and isinstance(r[1], str) and UUID.match(r[1])]
                    if not ids:
                        break
                    for i in ids:
                        await self.comfy.post_json(f"/api/jobs/{seg(i)}/cancel", {})
                    await self.sleep(1)
            self.stopped_done.add(task_id)
        except Exception as e:
            log.warning("[Fuobe Connect] Couldn't stop a job: %s", e)
        finally:
            self.stopping.discard(task_id)
            j = self.jobs.get(task_id)
            if j and j["phase"] != "done":
                j["phase"] = "failed"

    async def claim_and_run(self) -> bool:
        if not self.alive or not self.runner or self.paused() or not await self.look():
            return False
        running = [k for k, j in self.jobs.items() if j["phase"] not in ("done", "failed")]
        a = await self.site.call("POST", "/api/gpu/computer/claim", {"device": self.device, "running": running})
        if a.status == 401:
            raise Unpaired()
        if a.status != 200:
            return False
        t = a.body.get("task")
        if not isinstance(t, dict):
            return False
        try:
            await self.run(t)
        except (Stopped, asyncio.CancelledError):
            raise
        except Unpaired:
            raise
        except Exception as e:
            log.warning("[Fuobe Connect] A job stopped here: %s", e)
            if isinstance(t.get("id"), str):
                self.jobs.pop(t["id"], None)
        return True

    # ------------------------------------------------------------------
    # One job

    def _job(self, t: dict, **patch):
        j = self.jobs.setdefault(t["id"], {"phase": "sending", "promptId": t.get("promptId")})
        j.update(patch)
        done = [k for k, v in self.jobs.items() if v["phase"] in ("done", "failed")]
        for k in done[:-6]:
            self.jobs.pop(k, None)

    def vet(self, t: dict) -> str | None:
        """Why this job may not run here (the worker program's reasons), or None."""
        if not isinstance(t.get("promptId"), str) or not UUID.match(t["promptId"]):
            return "unsafe-graph"
        inputs = t.get("inputs") if t.get("inputs") is not None else []
        if not isinstance(inputs, list) or len(inputs) > MAX_INPUTS:
            return "unsafe-graph"
        names: list[str] = []
        for i in inputs:
            if not isinstance(i, dict) or not _small_int(i.get("n")) or not staged_name_ok(i.get("name")) or i["name"] in names:
                return "unsafe-graph"
            if not isinstance(i.get("sha256"), str) or not SHA256.match(i["sha256"]):
                return "unsafe-graph"
            size = i.get("size")
            if size is not None and not (isinstance(size, int) and 0 <= size <= MAX_INPUT):
                return "input-too-big"
            names.append(i["name"])
        for w in t.get("wanted") or []:
            if not isinstance(w, dict) or not _small_int(w.get("n")):
                return "unsafe-graph"
        return vet_graph(t.get("graph"), frozenset(self.nodes()), names, getattr(self.nodes, "kinds", None))

    def _record(self, t: dict, why: str | None) -> dict:
        graph = t.get("graph") if isinstance(t.get("graph"), dict) else {}
        kinds = sorted({n["class_type"] for n in graph.values() if isinstance(n, dict) and isinstance(n.get("class_type"), str)})
        rec = {"at": int(time.time() * 1000), "nodes": [k[:64] for k in kinds[:40]], "more": max(0, len(kinds) - 40), "state": f"refused ({why})" if why else "running"}
        try:
            self.on_job(rec)
        except Exception:
            pass
        return rec

    async def run(self, t: dict):
        if not isinstance(t.get("id"), str) or not TASK_ID.match(t["id"]) or not isinstance(t.get("lease"), str) or not LEASE.match(t["lease"]):
            log.warning("[Fuobe Connect] The studio sent a job with a bad id; skipped it.")
            return
        resume = t.get("resume") if t.get("resume") in ("collecting", "running", "queued") else None
        self._job(t, phase={"collecting": "saving", "running": "running", "queued": "queued"}.get(resume, "sending"), promptId=t.get("promptId"))
        why = self.vet(t)
        rec = self._record(t, why)
        try:
            if why:
                await self.report(t, {"event": "failed", "reason": why})
                return self._job(t, phase="failed")
            names = [i["name"] for i in t.get("inputs") or []]
            t = {**t, "prefix": run_prefix(t.get("prefix"), t["graph"])}
            if t["prefix"]:
                if len(self.prefixes) > 4096:
                    self.prefixes.clear()
                self.prefixes.add(t["prefix"])
            # A prompt this ComfyUI's Fuobe Connect didn't queue (it restarted): start the job again.
            if resume in ("running", "queued") and t["promptId"] not in self.own:
                resume = None
            entries = None
            wanted = t.get("wanted")
            phase = resume or "sending"
            for _ in range(3):
                if phase == "collecting":
                    break
                if phase == "sending" and not await self.send_job(t):
                    return
                self._socket()
                done = await self.follow(t)
                if done == "refused":
                    return self._job(t, phase="failed")
                if done == "gone":
                    a = await self.report(t, {"event": "gone"})
                    if a.get("state") != "sending":
                        return self._job(t, phase="failed")
                    phase = "sending"
                    continue
                entries = done["entries"]
                if not done["answer"].get("wanted"):
                    return self._job(t, phase="done" if done["answer"].get("state") == "done" else "failed")
                wanted = done["answer"]["wanted"]
                phase = "collecting"
            if phase != "collecting" or not wanted:
                return
            if entries is None:
                entries = await self.own_entries(t)
            await self.collect(t, wanted, entries, t.get("received") or [])
            if self.jobs.get(t["id"], {}).get("phase") == "done":
                self.clean_inputs(names)
        except Stopped:
            self._job(t, phase="failed")
        finally:
            rec["state"] = rec["state"] if why else self.jobs.get(t["id"], {}).get("phase", "stopped")

    def clean_inputs(self, names: list[str]):
        """A finished job's inputs leave this computer's input folder (only Fuobe's own fuobe_<id> files)."""
        if not self.input_dir:
            return
        for name in names:
            if staged_name_ok(name):
                try:
                    os.remove(os.path.join(self.input_dir, name))
                except OSError:
                    pass

    async def own_entries(self, t: dict) -> list:
        own = None
        if t["promptId"] in self.own:
            try:
                own = (await self.comfy.get_json(f"/history/{seg(t['promptId'])}")).get(t["promptId"])
            except Exception:
                own = None
        passes = []
        if t.get("prefix"):
            try:
                recent = await self.comfy.get_json("/history?max_items=64")
                passes = [e for e in recent.values() if has_prefix((e.get("prompt") or [None, None, None])[2], t["prefix"])]
            except Exception:
                pass
        return ([own] if own else []) + passes

    async def wait_for_comfy(self, t: dict):
        told = False
        while True:
            if not self.alive:
                raise Stopped()
            if await self.look():
                return
            if not told:
                a = await self.report(t, {"event": "comfy-down"})
                if a.get("stop"):
                    raise Stopped()
                told = True
            await self.sleep(3)

    async def send_job(self, t: dict) -> bool:
        """Send the job's inputs and prompt, a few times when ComfyUI stops answering partway. False: it failed (and the
        studio was told)."""
        for attempt in range(SEND_TRIES):
            self._job(t, phase="sending")
            try:
                there = await self.where(t["promptId"])
            except Exception:
                there = None
            if there and there["s"] != "absent":
                if t["promptId"] not in self.own:
                    # A prompt with this id that Fuobe Connect didn't queue here: never someone else's.
                    await self.report(t, {"event": "failed", "reason": "unsafe-graph"})
                    self._job(t, phase="failed")
                    return False
                await self.report(t, {"event": "sent", "promptId": t["promptId"]})
                return True
            if there is None:
                await self.wait_for_comfy(t)
            sent = await self._send_once(t)
            if sent is not None:
                return sent
            await self.wait_for_comfy(t)
            await self.sleep(2 * (attempt + 1))
        await self.report(t, {"event": "failed", "reason": "comfy-error"})
        self._job(t, phase="failed")
        return False

    async def _fail(self, t: dict, event: dict) -> bool:
        await self.report(t, event)
        self._job(t, phase="failed")
        return False

    async def _send_once(self, t: dict) -> bool | None:
        """True: sent. False: failed (the studio was told). None: ComfyUI stopped answering; try again."""
        for inp in t.get("inputs") or []:
            size = inp.get("size")
            try:
                status, f, got, sha, _ = await self.site.fetch_file(
                    f"/api/gpu/computer/tasks/{seg(t['id'])}/inputs/{int(inp['n'])}",
                    size if isinstance(size, int) else MAX_INPUT,
                    headers={"X-Fuobe-Lease": t["lease"]},
                )
            except TooBig:
                return await self._fail(t, {"event": "failed", "reason": "input-changed"})
            if status == 401:
                raise Unpaired()
            if status in (404, 409):
                raise Stopped()
            if status != 200 or f is None:
                raise RuntimeError(f"The studio answered HTTP {status} for a file")
            try:
                if sha != inp.get("sha256"):
                    return await self._fail(t, {"event": "failed", "reason": "input-changed"})
                try:
                    up = await self.comfy.upload_input(inp["name"], f, inp.get("contentType") if isinstance(inp.get("contentType"), str) else "application/octet-stream")
                except Exception:
                    return None
            finally:
                f.close()
            if up != 200:
                return await self._fail(t, {"event": "failed", "reason": "input-too-big" if up == 413 else "comfy-error", "status": up})
        body = {"prompt": t["graph"], "prompt_id": t["promptId"], "client_id": self.client_id}
        if t.get("preview"):
            body["extra_data"] = {"preview_method": "auto"}
        self._own(t["promptId"])
        try:
            status, answer = await self.comfy.post_json("/prompt", body)
        except Exception:
            return None
        if status == 400:
            return await self._fail(t, {"event": "refused", "status": 400, "body": refusal(answer, graph_strings(t["graph"]))})
        pid = answer.get("prompt_id")
        if status != 200 or not isinstance(pid, str) or not UUID.match(pid):
            return await self._fail(t, {"event": "failed", "reason": "comfy-error", "status": status})
        self._own(pid)
        if pid != t["promptId"]:
            t["promptId"] = pid
            self._job(t, promptId=pid)
        ev = {"event": "sent", "promptId": pid}
        if isinstance(answer.get("number"), (int, float)):
            ev["number"] = answer["number"]
        await self.report(t, ev)
        return True

    async def follow(self, t: dict):
        last_sent = -1e9
        last_key = ""
        following = None
        while True:
            if not self.alive:
                raise Stopped()
            if following:
                try:
                    q = await self.comfy.get_json("/queue")
                except Exception:
                    await self.wait_for_comfy(t)
                    continue
                running = any(len(r) > 2 and has_prefix(r[2], following) for r in (q.get("queue_running") or []) + (q.get("queue_pending") or []))
                try:
                    recent = await self.comfy.get_json("/history?max_items=64")
                except Exception:
                    recent = {}
                passes = [e for e in recent.values() if has_prefix((e.get("prompt") or [None, None, None])[2], following)]
                a = await self.report(t, {"event": "passes", "running": running, "entries": [] if running else [slim(e) for e in passes]})
                if a.get("stop"):
                    raise Stopped()
                if not running:
                    return {"entries": passes, "answer": a}
                self._job(t, phase="running")
                await self.sleep(POLL_S * 2)
                continue
            try:
                w = await self.where(t["promptId"])
            except Exception:
                await self.wait_for_comfy(t)
                continue
            if w["s"] == "absent":
                return "gone"
            if w["s"] == "done":
                a = await self.report(t, {"event": "finished", "entry": slim(w["entry"])})
                if a.get("stop"):
                    raise Stopped()
                if a.get("follow"):
                    # Only this job's own batch: the prefix its own graph saves under.
                    if not t.get("prefix") or a["follow"] != t["prefix"]:
                        await self.report(t, {"event": "failed", "reason": "unsafe-graph"})
                        return "refused"
                    following = t["prefix"]
                    continue
                return {"entries": [w["entry"]], "answer": a}
            ev = self.events.get(t["promptId"]) or {}
            if w["s"] == "pending":
                rep = {"event": "queued", "position": w["ahead"]}
            else:
                rep = {"event": "running", **{k: ev[k] for k in ("value", "max", "node", "nodesDone") if ev.get(k) is not None}}
            key = json.dumps(rep, sort_keys=True)
            if key != last_key and self.clock() - last_sent >= PROGRESS_EVERY_S:
                a = await self.report(t, rep)
                if a.get("stop"):
                    raise Stopped()
                last_key = key
                last_sent = self.clock()
            self._job(t, phase="queued" if w["s"] == "pending" else "running", position=w.get("ahead"))
            await self.sleep(PROGRESS_EVERY_S if self.ws_open else POLL_S)

    async def collect(self, t: dict, wanted: list, entries: list, received: list):
        self._job(t, phase="saving")
        for w in wanted if isinstance(wanted, list) else []:
            if not isinstance(w, dict) or not _small_int(w.get("n")):
                await self.report(t, {"event": "failed", "reason": "output-missing"})
                return self._job(t, phase="failed")
            got = next((r for r in received if isinstance(r, dict) and r.get("n") == w.get("n")), None)
            if got and got.get("done"):
                continue
            if not safe_wanted(w) or not may_download(w, entries):
                await self.report(t, {"event": "failed", "reason": "output-missing"})
                return self._job(t, phase="failed")
            f, size = await self.comfy.view(w["filename"], w.get("subfolder") or "", w["type"])
            if f is None:
                await self.report(t, {"event": "failed", "reason": "output-missing"})
                return self._job(t, phase="failed")
            try:
                offset = (got or {}).get("bytes") or 0
                if offset > 0:
                    s = await self.site.call("GET", f"/api/gpu/computer/tasks/{seg(t['id'])}/outputs/{int(w['n'])}", headers={"X-Fuobe-Lease": t["lease"]})
                    offset = s.body.get("received", 0) if s.status == 200 else 0
                if not await self.upload(t, w, f, size, int(offset) if isinstance(offset, int) and 0 <= offset <= size else 0):
                    return self._job(t, phase="failed")
            finally:
                f.close()
        self._job(t, phase="done")

    async def upload(self, t: dict, w: dict, f, total: int, offset: int) -> bool:
        """Send a result to the studio in parts, from the temporary file `f` (`total` bytes), starting at `offset`."""
        fails = 0
        until = self.clock() + SAVE_WAIT_S
        path = f"/api/gpu/computer/tasks/{seg(t['id'])}/outputs/{int(w['n'])}"
        while offset < total or total == 0:
            if not self.alive:
                raise Stopped()
            f.seek(offset)
            part = f.read(PART_BYTES)
            try:
                a = await self.site.call(
                    "PUT",
                    f"{path}?offset={offset}&total={total}",
                    data=part,
                    headers={"X-Fuobe-Lease": t["lease"], "Content-Type": "application/octet-stream"},
                    timeout=600,
                )
            except Exception:
                a = None
            if a and a.status == 200:
                got = a.body.get("received", offset)
                offset = got if isinstance(got, int) and 0 <= got <= total else offset
                fails = 0
                if a.body.get("done"):
                    return True
                continue
            if a and a.status == 429 and self.clock() < until:
                await self.sleep(_retry_after(a.headers))
                continue
            if a and a.status in (404, 413, 415):
                return False
            if a and a.status == 401:
                raise Unpaired()
            if a and a.status == 409:
                s = await self.site.call("GET", path, headers={"X-Fuobe-Lease": t["lease"]})
                if s.status != 200:
                    return False
                if s.body.get("done"):
                    return True
                got = s.body.get("received", 0)
                offset = got if isinstance(got, int) and 0 <= got <= total else 0
            fails += 1
            if fails >= 3:
                try:
                    await self.report(t, {"event": "failed", "reason": "save-failed"})
                except Exception:
                    pass
                return False
            await self.sleep(1.5 * fails)
        return True

    # ------------------------------------------------------------------
    # The loops

    async def beat_loop(self):
        while self.alive:
            busy = any(j["phase"] not in ("done", "failed") for j in self.jobs.values())
            try:
                a = await self.beat()
                wait = BUSY_BEAT_S if busy or (a and a.get("waiting")) else BEAT_S
                if a is None:
                    wait = 30
            except Unpaired:
                raise
            except Exception as e:
                self.on_state("offline", f"Can't reach the studio: {e}")
                wait = 30
            await self.sleep(wait)

    async def work_loop(self):
        while self.alive:
            try:
                did = await self.claim_and_run()
            except Stopped:
                did = True
            except Unpaired:
                raise
            except Exception as e:
                log.warning("[Fuobe Connect] %s", e)
                did = False
            if not did:
                await self.sleep(2)

    async def run_forever(self):
        """Beat and work until stopped. Raises Unpaired when the studio forgot this token."""
        beats = asyncio.ensure_future(self.beat_loop())
        work = asyncio.ensure_future(self.work_loop())
        try:
            done, _ = await asyncio.wait({beats, work}, return_when=asyncio.FIRST_EXCEPTION)
            for d in done:
                if d.exception():
                    raise d.exception()
        finally:
            self.alive = False
            for task in (beats, work, self.ws_task):
                if task and not task.done():
                    task.cancel()
