"""Each attack on ComfyUI's own code: what ComfyUI does with the graph, and what Fuobe Connect's check says.

Run by test_comfy_engine.py in a process of its own (python -I comfy_engine.py <ComfyUI folder> <pack folder>
<scratch folder> [<Fuobe's workflows folder>]). It imports ComfyUI's execution code from that folder as main.py would
with --cpu --disable-all-custom-nodes --disable-api-nodes, with its input, output, temp and user folders in the scratch
folder: no server starts, no port opens, and nothing in the ComfyUI folder changes. It prints one JSON object.

For each attack, "comfy" is what ComfyUI itself does: whether validate_prompt takes the graph, which nodes
validate_inputs validated, and which nodes run anyway (the scheduler's, a loop's, a node replacement's, the history's).
"vet" is vet_graph's answer with the node classes' own kinds, as inside ComfyUI.
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
import execution  # noqa: E402
from comfy_execution.graph import DynamicPrompt, TopologicalSort  # noqa: E402
from comfy_execution.graph_utils import is_link  # noqa: E402

asyncio.run(nodes.init_extra_nodes(init_custom_nodes=False, init_api_nodes=False))

from fuobe_connect.graph import comfy_kinds, files_in, shipped_nodes, vet_graph  # noqa: E402

MODEL = "fuobe_abcdefgh12.glb"
PICTURE = "fuobe_abcdefgh12.png"
with open(os.path.join(scratch, "input", MODEL), "wb") as f:
    f.write(b"glTF")
from PIL import Image  # noqa: E402

Image.new("RGB", (8, 8), (200, 10, 10)).save(os.path.join(scratch, "input", PICTURE))
# A file of yours in the output folder, under a name ComfyUI makes up the same way on every computer.
Image.new("RGB", (8, 8), (10, 200, 10)).save(os.path.join(scratch, "output", "ComfyUI_00001_.png"))
STAGED = [MODEL, PICTURE]

from app.node_replace_manager import NodeReplaceManager  # noqa: E402
from comfy_api.latest._io import NodeReplace  # noqa: E402

replacements = NodeReplaceManager()
replacements.register(NodeReplace(new_node_id="LoadImage", old_node_id="FuobeOldLoader", input_mapping=[{"new_id": "image", "old_id": "path"}]))
KINDS = comfy_kinds(lambda: nodes.NODE_CLASS_MAPPINGS, lambda: replacements)
ALLOWED = shipped_nodes()

validated_ids: set = set()
_validate_inputs = execution.validate_inputs


async def recording_validate_inputs(prompt_id, prompt, item, validated, visiting=None):
    validated_ids.add(item)
    return await _validate_inputs(prompt_id, prompt, item, validated, visiting)


# validate_inputs calls itself through the module's name, so every node it validates is seen here.
execution.validate_inputs = recording_validate_inputs


def comfy_check(graph: dict) -> dict:
    """What ComfyUI does: validate_prompt, then the nodes its scheduler would run from the outputs it kept."""
    prompt = copy.deepcopy(graph)
    validated_ids.clear()
    valid, error, outputs, node_errors = asyncio.run(execution.validate_prompt("p", prompt, None))
    scheduled: set = set()
    if valid:
        sort = TopologicalSort(DynamicPrompt(prompt))
        for o in outputs:
            sort.add_node(o)
        scheduled = set(sort.pendingNodes)
    return {
        "valid": bool(valid),
        "validated": sorted(validated_ids),
        "scheduled": sorted(scheduled),
        "unvalidated_scheduled": sorted(scheduled - validated_ids),
        "errors": {k: [e["type"] for e in v["errors"]] for k, v in node_errors.items()},
        "error": error.get("type") if isinstance(error, dict) else None,
        "prompt": prompt,
    }


def vet(graph: dict, staged=STAGED, kinds=KINDS):
    return vet_graph(copy.deepcopy(graph), ALLOWED, staged, kinds)


def save3d(view, model=("30", 6), **more) -> dict:
    return {"class_type": "Save3DAdvanced", "inputs": {"model_3d": list(model), "viewport_state": view, "width": 512, "height": 512, "filename_prefix": "fuobe/abcdef12", **more}}


LOAD3D = {"class_type": "Load3D", "inputs": {"model_file": MODEL, "image": {"image": PICTURE, "mask": PICTURE, "normal": PICTURE, "recording": "", "camera_info": {}}, "width": 512, "height": 512}}
# A node ComfyUI refuses when it validates it: a dropdown value not in its list, an option that isn't one.
UNCHECKED = {"class_type": "CLIPLoader", "inputs": {"clip_name": "not-in-the-list/anything.safetensors", "type": "no_such_type", "device": "nowhere"}}

results: dict = {}

# 1. {"__value__": [node, slot]} in Save3DAdvanced.viewport_state: validate_inputs unwraps it into a link in place.
graph = {"10": save3d({"__value__": ["20", 0]}), "30": LOAD3D, "20": UNCHECKED}
c = comfy_check(graph)
control = comfy_check({**graph, "10": save3d({}), "11": {"class_type": "PreviewAny", "inputs": {"source": ["20", 0]}}})
results["value-wrapper"] = {
    "vet": vet(graph),
    "vet_direct": vet(graph, None),
    "valid": c["valid"],
    "unvalidated_scheduled": c["unvalidated_scheduled"],
    "now_a_link": is_link(c["prompt"]["10"]["inputs"]["viewport_state"]),
    "control_refused": control["errors"].get("20", []),
}

# 2. A link in an input the node doesn't list: the scheduler runs node 20, validate_inputs never looks at it.
graph = {"10": {"class_type": "SaveImage", "inputs": {"images": ["40", 0], "filename_prefix": "fuobe/abcdef12", "zzz": ["20", 0]}}, "40": {"class_type": "EmptyImage", "inputs": {"width": 8, "height": 8, "batch_size": 1, "color": 0}}, "20": UNCHECKED}
c = comfy_check(graph)
results["unlisted-input"] = {"vet": vet(graph), "vet_without_kinds": vet(graph, kinds=None), "valid": c["valid"], "unvalidated_scheduled": c["unvalidated_scheduled"]}

# 3. The same through a hidden input's name (SaveImage's "prompt").
graph = {**graph, "10": {"class_type": "SaveImage", "inputs": {"images": ["40", 0], "filename_prefix": "fuobe/abcdef12", "prompt": ["20", 0]}}}
c = comfy_check(graph)
results["hidden-input"] = {"vet": vet(graph), "valid": c["valid"], "unvalidated_scheduled": c["unvalidated_scheduled"]}

# 4. A dynamic node's inputs: ComfyUI works out which ones it validates from the node's own values.
graph = {
    "10": {"class_type": "SaveImage", "inputs": {"images": ["41", 0], "filename_prefix": "fuobe/abcdef12"}},
    "40": {"class_type": "EmptyImage", "inputs": {"width": 8, "height": 8, "batch_size": 1, "color": 0}},
    "41": {"class_type": "BatchImagesNode", "inputs": {"images.image0": ["40", 0], "images.image1": ["40", 0]}},
}
c = comfy_check(graph)
bad = copy.deepcopy(graph)
bad["41"]["inputs"]["pictures.image9"] = ["20", 0]
bad["20"] = UNCHECKED
cb = comfy_check(bad)
results["dynamic-inputs"] = {"vet_ok": vet(graph), "valid_ok": c["valid"], "vet_bad": vet(bad), "valid_bad": cb["valid"], "unvalidated_scheduled_bad": cb["unvalidated_scheduled"]}

# 5. "is_changed" on a node: ComfyUI takes it in place of the node's own IS_CHANGED (the key of its cached output).
graph = {"10": {"class_type": "SaveImage", "inputs": {"images": ["42", 0], "filename_prefix": "fuobe/abcdef12"}}, "42": {"class_type": "LoadImage", "inputs": {"image": PICTURE}, "is_changed": ["chosen by the studio"]}}
c = comfy_check(graph)
own = asyncio.run(execution.IsChangedCache("p", DynamicPrompt(copy.deepcopy({**graph, "42": {k: v for k, v in graph["42"].items() if k != "is_changed"}})), None).get("42"))
taken = asyncio.run(execution.IsChangedCache("p", DynamicPrompt(copy.deepcopy(graph)), None).get("42"))
results["is-changed"] = {"vet": vet(graph), "valid": c["valid"], "comfy_takes": taken == ["chosen by the studio"], "own_differs": own != taken}

# 6. A loop: validate_loops pairs an End Loop downstream of an output with its Start Loop, and Start Loop's expansion
# runs the body and the End Loop. Node 3 (any node: here an INT far past its max) is never validated.
graph = {
    "1": {"class_type": "StartLoop", "inputs": {"mode": "simple", "mode.num_iterations": 2, "cache_iterations": False}},
    "2": save3d({}, model=("5", 6), **{}),
    "3": {"class_type": "CreateCameraInfo", "inputs": {"mode": "orbit", "mode.yaw": ["1", 0], "mode.pitch": 10, "mode.distance": 5, "target_x": 0, "target_y": 0, "target_z": 0, "roll": 0, "fov": 99999, "zoom": 1}},
    "4": {"class_type": "EndLoop", "inputs": {"accumulate": False, "terminations.termination0": ["2", 3], "next_iteration_value": ["3", 0]}},
    "5": LOAD3D,
}
graph["2"]["inputs"]["width"] = ["1", 0]
c = comfy_check(graph)
body = c["prompt"]["1"].get("_loop_body", [])
results["loop-body"] = {
    "vet": vet(graph),
    "valid": c["valid"],
    "error": c["error"],
    "validated": c["validated"],
    "loop_body": body,
    "loop_end": c["prompt"]["1"].get("_loop_end"),
    "body_unvalidated": sorted(set(body + [c["prompt"]["1"].get("_loop_end")]) - set(c["validated"]) - {None}),
}
# 6b. "_loop_body" written by the studio: refused before ComfyUI writes its own.
forged = copy.deepcopy(graph)
forged["1"]["_loop_body"] = ["3"]
forged["1"]["_loop_end"] = "4"
results["loop-keys"] = {"vet": vet(forged)}

# 7. A node type ComfyUI swaps before it validates: "FuobeOldLoader" (missing) becomes LoadImage, its "path" the image,
# so it reads a picture of yours from the input folder.
Image.new("RGB", (8, 8), (10, 10, 200)).save(os.path.join(scratch, "input", "my_photo.png"))
graph = {"10": {"class_type": "SaveImage", "inputs": {"images": ["43", 0], "filename_prefix": "fuobe/abcdef12"}}, "43": {"class_type": "FuobeOldLoader", "inputs": {"path": "my_photo.png"}}}
swapped = copy.deepcopy(graph)
replacements.apply_replacements(swapped)
c = comfy_check(swapped)
results["node-replacement"] = {
    "vet": vet_graph(copy.deepcopy(graph), ALLOWED | {"FuobeOldLoader"}, STAGED, KINDS),
    "swapped_to": swapped["43"]["class_type"],
    "reads": swapped["43"]["inputs"].get("image"),
    "valid": c["valid"],
}

# 8. A camera that names a file: Save3DAdvanced shows it in the run's history, where it read as one of the run's files.
echo = {"filename": "ComfyUI_00001_.png", "subfolder": "", "type": "output"}
graph = {"10": save3d({"image": "", "mask": "", "normal": "", "recording": "", "camera_info": echo, "model_3d_info": [echo]}), "30": LOAD3D}
c = comfy_check(graph)
import types  # noqa: E402

server = types.SimpleNamespace(client_id=None, last_node_id=None, send_sync=lambda *a, **k: None, queue_updated=lambda *a, **k: None)
ex = execution.PromptExecutor(server, cache_type=False, cache_args={"lru": 0, "ram": 0, "ram_inactive": 0})
ran = ex.execute_async(copy.deepcopy(c["prompt"]), "p8", {}, ["10"] if c["valid"] else [])
if asyncio.iscoroutine(ran):
    asyncio.run(ran)
ui = (getattr(ex, "history_result", None) or {}).get("outputs", {})
entry = {"outputs": ui}


def old_files_in(entries):
    """files_in before this change: every dict in every list of the history's outputs."""
    found = []
    for e in entries:
        for u in (e.get("outputs") or {}).values():
            for v in u.values() if isinstance(u, dict) else []:
                if isinstance(v, list):
                    found += [f for f in v if isinstance(f, dict) and isinstance(f.get("filename"), str) and isinstance(f.get("type"), str)]
    return found


results["history-echo"] = {
    "vet": vet(graph),
    "valid": c["valid"],
    "ran": bool(ex.success),
    "ui_keys": sorted({k for u in ui.values() if isinstance(u, dict) for k in u}),
    "old_lists_it": any(f.get("filename") == "ComfyUI_00001_.png" for f in old_files_in([entry])),
    "lists_it": any(f.get("filename") == "ComfyUI_00001_.png" for f in files_in([entry])),
    "results": [f.get("filename") for f in files_in([entry])],
}

# 9. Fuobe's own workflows pass with ComfyUI's own kinds (node types this ComfyUI doesn't have are left to ComfyUI).
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
