"""Each attack against ComfyUI's own validate and execute code, and Fuobe Connect's answer to it.

Needs COMFYUI_DIR (a ComfyUI folder) and this Python able to import it (torch and ComfyUI's requirements): ComfyUI's
own embedded Python, for one. comfy_engine.py imports ComfyUI's execution code from that folder in a process of its
own, on the CPU, with no custom nodes and with its input, output, temp and user folders in a scratch folder. No server
starts and nothing in the ComfyUI folder changes. Each test first shows what ComfyUI itself does with the graph (it
takes it, and runs a node it never validated, or lists a file of yours as a result), then that vet_graph refuses it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.dirname(HERE)
WORKFLOWS = os.path.normpath(os.path.join(PACK, "..", "..", "workflows", "comfyui"))


class ComfyEngineTest(unittest.TestCase):
    results: dict = {}

    @classmethod
    def setUpClass(cls):
        folder = os.environ.get("COMFYUI_DIR")
        if not folder:
            raise unittest.SkipTest("Set COMFYUI_DIR to a ComfyUI folder to run the attacks against ComfyUI's own code")
        scratch = tempfile.mkdtemp(prefix="fuobe-connect-engine-")
        try:
            done = subprocess.run(
                [sys.executable, "-I", os.path.join(HERE, "comfy_engine.py"), folder, PACK, scratch, WORKFLOWS if os.path.isdir(WORKFLOWS) else ""],
                cwd=scratch,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1200,
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if done.returncode != 0 or not done.stdout.strip():
            raise AssertionError(f"comfy_engine.py failed ({done.returncode}):\n{done.stderr[-4000:]}")
        cls.results = json.loads(done.stdout.strip().splitlines()[-1])

    def test_value_wrapper(self):
        """{"__value__": ["20", 0]} in Save3DAdvanced.viewport_state: ComfyUI unwraps it into a link and runs node 20
        (a dropdown value not in its list) unvalidated; linked normally, ComfyUI refuses node 20."""
        r = self.results["value-wrapper"]
        self.assertTrue(r["valid"])
        self.assertTrue(r["now_a_link"])
        self.assertEqual(r["unvalidated_scheduled"], ["20"])
        self.assertIn("value_not_in_list", r["control_refused"])
        self.assertEqual(r["vet"], "unsafe-graph")
        self.assertEqual(r["vet_direct"], "unsafe-graph")

    def test_link_in_an_input_the_node_doesnt_list(self):
        r = self.results["unlisted-input"]
        self.assertTrue(r["valid"])
        self.assertEqual(r["unvalidated_scheduled"], ["20"])
        self.assertEqual(r["vet"], "unsafe-graph")

    def test_link_in_a_hidden_input(self):
        r = self.results["hidden-input"]
        self.assertTrue(r["valid"])
        self.assertEqual(r["unvalidated_scheduled"], ["20"])
        self.assertEqual(r["vet"], "unsafe-graph")

    def test_dynamic_inputs_as_comfyui_works_them_out(self):
        r = self.results["dynamic-inputs"]
        self.assertTrue(r["valid_ok"])
        self.assertIsNone(r["vet_ok"])
        self.assertTrue(r["valid_bad"])
        self.assertEqual(r["unvalidated_scheduled_bad"], ["20"])
        self.assertEqual(r["vet_bad"], "unsafe-graph")

    def test_is_changed_key(self):
        r = self.results["is-changed"]
        self.assertTrue(r["valid"])
        self.assertTrue(r["comfy_takes"])
        self.assertTrue(r["own_differs"])
        self.assertEqual(r["vet"], "unsafe-graph")

    def test_loop_runs_unvalidated_nodes(self):
        """ComfyUI validates nodes 1, 2 and 5 only, yet its loop runs node 3 (the body) and node 4 (the End Loop)."""
        r = self.results["loop-body"]
        self.assertTrue(r["valid"], r["error"])
        self.assertEqual(r["loop_end"], "4")
        self.assertIn("3", r["loop_body"])
        self.assertEqual(r["body_unvalidated"], ["3", "4"])
        self.assertEqual(r["vet"], "unsafe-graph")
        self.assertEqual(self.results["loop-keys"]["vet"], "unsafe-graph")

    def test_node_replacement(self):
        r = self.results["node-replacement"]
        self.assertEqual(r["swapped_to"], "LoadImage")
        self.assertEqual(r["reads"], "my_photo.png")
        self.assertTrue(r["valid"])
        self.assertEqual(r["vet"], "unknown-node")

    def test_history_echo(self):
        """A camera naming a file of yours: Save3DAdvanced runs and shows it in the history, where the old check read it
        as one of the run's results."""
        r = self.results["history-echo"]
        self.assertTrue(r["valid"])
        self.assertTrue(r["ran"])
        self.assertIn("camera_info", r["ui_keys"])
        self.assertTrue(r["old_lists_it"])
        self.assertFalse(r["lists_it"])
        self.assertEqual(r["vet"], "unsafe-graph")

    def test_fuobe_workflows_pass(self):
        r = self.results["fuobe-workflows"]
        if not r["checked"]:
            self.skipTest("Fuobe's workflows aren't beside this copy of the pack")
        self.assertEqual(r["refused"], {})
        self.assertGreater(r["core_only"], 50)


if __name__ == "__main__":
    unittest.main()
