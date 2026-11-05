"""Test-only: Fuobe Connect's pairing and job runner outside ComfyUI, for the end-to-end test (tests/e2e/fuobe-connect.mjs
in the Fuobe repo), against a stand-in studio on this computer. It asks the studio for a code, prints it as one JSON
line, waits for the approval, then runs that person's jobs on the ComfyUI at --comfy until it is stopped (or unpaired).

It prints the worker token only when FUOBE_CONNECT_TEST_PRINT_TOKEN=1 (the end-to-end test needs it, with a throwaway
studio); otherwise only its last four characters. A token to start with comes from FUOBE_CONNECT_TOKEN, never from the
command line. Not part of the published pack.

    python tests/pair_and_run.py --site http://127.0.0.1:3361 --comfy http://127.0.0.1:3362
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fuobe_connect.net import Comfy, Site  # noqa: E402
from fuobe_connect.runner import Runner, Unpaired  # noqa: E402


def say(**kw):
    print(json.dumps(kw), flush=True)


async def main(site_url: str, comfy_url: str, name: str, token: str | None):
    async with aiohttp.ClientSession() as session:
        site = Site(session, site_url)
        if not token:
            a = await site.pair_start(name, {"device": "Test GPU", "vramBytes": 8 * 2**30})
            if a.status != 200:
                say(event="error", status=a.status, body=a.body)
                return 2
            say(event="code", code=a.body["code"], url=a.body["url"])
            for _ in range(300):
                await asyncio.sleep(0.5)
                p = await site.pair_poll(a.body["id"], a.body["secret"])
                if p.status == 200 and p.body.get("state") == "approved":
                    token = p.body["token"]
                    shown = token if os.environ.get("FUOBE_CONNECT_TEST_PRINT_TOKEN") == "1" else f"…{token[-4:]}"
                    say(event="paired", workerId=p.body["workerId"], token=shown, account=p.body.get("account"))
                    break
                if p.status == 404:
                    say(event="gone")
                    return 3
        runner = Runner(Site(session, site_url, token), Comfy(session, comfy_url), f"connect-{name}", int(comfy_url.rsplit(":", 1)[1]), on_state=lambda s, m=None: say(event="state", state=s, message=m))
        try:
            await runner.run_forever()
        except Unpaired:
            say(event="unpaired")
            return 4
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--comfy", required=True)
    ap.add_argument("--name", default="test-pc")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.site, args.comfy, args.name, os.environ.get("FUOBE_CONNECT_TOKEN") or None)))
