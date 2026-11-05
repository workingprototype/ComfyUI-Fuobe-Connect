"""Fuobe Connect's graph check with the node classes of a real ComfyUI (comfy_kinds reading NODE_CLASS_MAPPINGS).

Needs COMFYUI_DIR (a ComfyUI folder) and a Python that can load it (torch and ComfyUI's requirements): ComfyUI's own
embedded Python, for one. Without torch the tests are skipped. comfy_kinds_check.py loads ComfyUI's nodes from that
folder in a process of its own, on the CPU, with no custom nodes and with its input, output, temp and user folders in a
scratch folder. No server starts and nothing in the ComfyUI folder changes.
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


class ComfyKindsTest(unittest.TestCase):
    results: dict = {}

    @classmethod
    def setUpClass(cls):
        folder = os.environ.get("COMFYUI_DIR")
        if not folder:
            raise unittest.SkipTest("Set COMFYUI_DIR to a ComfyUI folder to check graphs with its own node classes")
        scratch = tempfile.mkdtemp(prefix="fuobe-connect-kinds-")
        try:
            done = subprocess.run(
                [sys.executable, "-I", "-B", os.path.join(HERE, "comfy_kinds_check.py"), folder, PACK, scratch, WORKFLOWS if os.path.isdir(WORKFLOWS) else ""],
                cwd=scratch,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1200,
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if done.returncode == 3:
            raise unittest.SkipTest("This Python can't load ComfyUI (no torch): run the tests with ComfyUI's own Python")
        if done.returncode != 0 or not done.stdout.strip():
            raise AssertionError(f"comfy_kinds_check.py failed ({done.returncode}):\n{done.stderr[-4000:]}")
        cls.results = json.loads(done.stdout.strip().splitlines()[-1])

    def test_takes_a_graph_with_listed_inputs(self):
        self.assertIsNone(self.results["listed"])

    def test_refuses_a_link_into_an_input_the_node_doesnt_list(self):
        r = self.results["unlisted-input"]
        self.assertEqual(r["vet"], "unsafe-graph")
        self.assertIsNone(r["vet_without_kinds"])

    def test_refuses_a_link_into_a_hidden_input(self):
        self.assertEqual(self.results["hidden-input"]["vet"], "unsafe-graph")

    def test_takes_only_a_dynamic_nodes_own_inputs(self):
        r = self.results["dynamic-inputs"]
        self.assertIsNone(r["vet_listed"])
        self.assertEqual(r["vet_other"], "unsafe-graph")

    def test_refuses_a_value_object(self):
        r = self.results["value-object"]
        self.assertEqual(r["vet"], "unsafe-graph")
        self.assertEqual(r["vet_direct"], "unsafe-graph")

    def test_refuses_a_node_key_other_than_type_inputs_and_title(self):
        self.assertEqual(self.results["node-key"]["vet"], "unsafe-graph")

    def test_loop_nodes_are_reached_from_an_output(self):
        r = self.results["loop"]
        self.assertIsNone(r["vet_reached"])
        self.assertEqual(r["vet_spare"], "unsafe-graph")

    def test_refuses_a_node_type_with_a_replacement(self):
        r = self.results["node-replacement"]
        self.assertEqual(r["vet_replaced"], "unknown-node")
        self.assertIsNone(r["vet_missing"])

    def test_fuobe_workflows_pass(self):
        r = self.results["fuobe-workflows"]
        if not r["checked"]:
            self.skipTest("Fuobe's workflows aren't beside this copy of the pack")
        self.assertEqual(r["refused"], {})
        self.assertGreater(r["core_only"], 50)


if __name__ == "__main__":
    unittest.main()
