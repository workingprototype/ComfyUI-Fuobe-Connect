"""Fuobe Connect's graph check with ComfyUI's own node classes, for test_comfy_kinds.py.

Run as: python -I -B comfy_kinds_check.py <ComfyUI folder> <pack folder> <scratch folder> [<Fuobe's workflows folder>].
It loads ComfyUI's nodes from that folder as main.py would with --cpu --disable-all-custom-nodes --disable-api-nodes,
with its input, output, temp and user folders in the scratch folder: no server starts, no port opens, and nothing in the
ComfyUI folder changes. It prints one JSON object: vet_graph's answer for each graph below, with comfy_kinds reading
ComfyUI's own NODE_CLASS_MAPPINGS and node replacements. Exit code 3: this Python can't load ComfyUI (no torch).
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys

root, pack, scratch = sys.argv[1], sys.argv[2], sys.argv[3]
workflows = sys.argv[4] if len(sys.argv) > 4 else ""
out = sys.stdout
sys.stdout = sys.stderr  # ComfyUI's own messages stay out of the answer
try:
    import torch  # noqa: F401
except ImportError:
    sys.exit(3)
sys.argv = ["main.py", "--cpu", "--disable-all-custom-nodes", "--disable-api-nodes"]
sys.path.insert(0, root)
sys.path.insert(0, pack)
sys.path.insert(0, os.path.join(pack, "tests"))
os.chdir(scratch)

import folder_paths  # noqa: E402

for sub in ("input", "output", "temp", "user"):
    os.makedirs(os.path.join(scratch, sub), exist_ok=True)
folder_paths.set_input_directory(os.path.join(scratch, "input"))
folder_paths.set_output_directory(os.path.join(scratch, "output"))
folder_paths.set_temp_directory(os.path.join(scratch, "temp"))
folder_paths.set_user_directory(os.path.join(scratch, "user"))

import nodes  # noqa: E402

asyncio.run(nodes.init_extra_nodes(init_custom_nodes=False, init_api_nodes=False))

from app.node_replace_manager import NodeReplaceManager  # noqa: E402
from comfy_api.latest._io import NodeReplace  # noqa: E402

from fuobe_connect.graph import comfy_kinds, shipped_nodes, vet_graph  # noqa: E402

MODEL = "fuobe_abcdefgh12.glb"
PICTURE = "fuobe_abcdefgh12.png"
STAGED = [MODEL, PICTURE]

replacements = NodeReplaceManager()
replacements.register(NodeReplace(new_node_id="LoadImage", old_node_id="FuobeOldLoader", input_mapping=[{"new_id": "image", "old_id": "path"}]))
KINDS = comfy_kinds(lambda: nodes.NODE_CLASS_MAPPINGS, lambda: replacements)
ALLOWED = shipped_nodes()


def vet(graph: dict, staged=STAGED, kinds=KINDS, allowed=ALLOWED):
    return vet_graph(copy.deepcopy(graph), allowed, staged, kinds)


def save(images, **more) -> dict:
    return {"class_type": "SaveImage", "inputs": {"images": images, "filename_prefix": "fuobe/abcdef12", **more}}


def save3d(view, model=("30", 6)) -> dict:
    return {"class_type": "Save3DAdvanced", "inputs": {"model_3d": list(model), "viewport_state": view, "width": 512, "height": 512, "filename_prefix": "fuobe/abcdef12"}}


EMPTY = {"class_type": "EmptyImage", "inputs": {"width": 8, "height": 8, "batch_size": 1, "color": 0}}
CLIP = {"class_type": "CLIPLoader", "inputs": {"clip_name": "a.safetensors", "type": "sd3", "device": "default"}}
LOAD3D = {"class_type": "Load3D", "inputs": {"model_file": MODEL, "image": {"image": PICTURE, "mask": PICTURE, "normal": PICTURE, "recording": "", "camera_info": {}}, "width": 512, "height": 512}}

results: dict = {"listed": vet({"10": save(["40", 0]), "40": EMPTY})}

# A link into an input SaveImage doesn't list, and into one of its hidden inputs.
for name, extra in (("unlisted-input", {"zzz": ["20", 0]}), ("hidden-input", {"prompt": ["20", 0]})):
    graph = {"10": save(["40", 0], **extra), "40": EMPTY, "20": CLIP}
    results[name] = {"vet": vet(graph), "vet_without_kinds": vet(graph, kinds=None)}

# A dynamic node's inputs: the ones ComfyUI works out from the node's own values, and one it doesn't list.
graph = {"10": save(["41", 0]), "40": EMPTY, "41": {"class_type": "BatchImagesNode", "inputs": {"images.image0": ["40", 0], "images.image1": ["40", 0]}}}
other = copy.deepcopy(graph)
other["41"]["inputs"]["pictures.image9"] = ["40", 0]
results["dynamic-inputs"] = {"vet_listed": vet(graph), "vet_other": vet(other)}

# A {"__value__": ...} object in a 3D view.
graph = {"10": save3d({"__value__": ["20", 0]}), "30": LOAD3D, "20": CLIP}
results["value-object"] = {"vet": vet(graph), "vet_direct": vet(graph, None)}

# A node key other than its type, inputs and title.
results["node-key"] = {"vet": vet({"10": save(["42", 0]), "42": {"class_type": "LoadImage", "inputs": {"image": PICTURE}, "is_changed": ["x"]}})}

# A loop: every node reached from an output node, and one more node (3) that no output needs.
start = {"class_type": "StartLoop", "inputs": {"mode": "simple", "mode.num_iterations": 2, "cache_iterations": False}}
reached = {
    "1": start,
    "2": {"class_type": "EmptyImage", "inputs": {"width": 8, "height": 8, "batch_size": 1, "color": ["1", 0]}},
    "3": {"class_type": "EndLoop", "inputs": {"accumulate": True, "output_value": ["2", 0]}},
    "4": {"class_type": "RebatchImages", "inputs": {"images": ["3", 0], "batch_size": 64}},
    "5": save(["4", 0]),
}
spare = {
    "1": start,
    "2": {**save3d({}, model=("5", 6))},
    "3": {"class_type": "CreateCameraInfo", "inputs": {"mode": "orbit", "mode.yaw": ["1", 0], "mode.pitch": 10, "mode.distance": 5, "target_x": 0, "target_y": 0, "target_z": 0, "roll": 0, "fov": 35, "zoom": 1}},
    "4": {"class_type": "EndLoop", "inputs": {"accumulate": False, "terminations.termination0": ["2", 3], "next_iteration_value": ["3", 0]}},
    "5": LOAD3D,
}
spare["2"]["inputs"] = {**spare["2"]["inputs"], "width": ["1", 0]}
results["loop"] = {"vet_reached": vet(reached), "vet_spare": vet(spare)}

# A missing node type that ComfyUI has a node replacement for, and one it hasn't.
replaced = {"10": save(["43", 0]), "43": {"class_type": "FuobeOldLoader", "inputs": {"path": PICTURE}}}
missing = {"10": save(["43", 0]), "43": {"class_type": "FuobeOtherLoader", "inputs": {"path": PICTURE}}}
results["node-replacement"] = {
    "vet_replaced": vet(replaced, allowed=ALLOWED | {"FuobeOldLoader"}),
    "vet_missing": vet(missing, allowed=ALLOWED | {"FuobeOtherLoader"}),
}

# Fuobe's own workflows (node types this ComfyUI doesn't have are left to ComfyUI).
checked, refused, core_only = 0, {}, 0
if workflows and os.path.isdir(workflows):
    from test_graph import staged_like_the_studio  # noqa: E402

    for name in sorted(os.listdir(workflows)):
        if not name.endswith(".json") or name in ("requirements.json", "manifest.json", "object_info.subset.json"):
            continue
        with open(os.path.join(workflows, name), encoding="utf-8") as f:
            wf = json.load(f)
        if not isinstance(wf, dict) or not wf or not all(isinstance(n, dict) and "class_type" in n for n in wf.values()):
            continue
        checked += 1
        core_only += all(n["class_type"] in nodes.NODE_CLASS_MAPPINGS for n in wf.values())
        why = vet_graph(staged_like_the_studio(wf), ALLOWED, None, KINDS)
        if why:
            refused[name] = why
results["fuobe-workflows"] = {"checked": checked, "core_only": core_only, "refused": refused}

json.dump(results, out, default=str)
out.write("\n")
out.flush()
