"""Fuobe Connect's own graph checks (graph.py), alone: the node list, path-like text, the loaders, and what goes back
to the studio."""

from __future__ import annotations

import enum
import glob
import json
import os
import re
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = os.path.dirname(HERE)
sys.path.insert(0, PACK)

from fuobe_connect import __version__  # noqa: E402
from fuobe_connect.graph import (  # noqa: E402
    ARG_NODES,
    FILE_LOADERS,
    LINK_INPUTS,
    LOOSE,
    LOOSE_LINKS,
    OPTIONAL_FILES,
    OWN_NODES_FILE,
    PATH_INPUTS,
    TEXT_INPUTS,
    NodeKinds,
    NodeList,
    comfy_kinds,
    dropdown_spec,
    files_in,
    graph_problem,
    has_value_key,
    input_kind,
    is_link,
    loose_type,
    may_download,
    refusal,
    run_prefix,
    safe_wanted,
    shipped_nodes,
    slim,
    staged_name_ok,
    vet_graph,
)

B = chr(92)  # a backslash


def comfy_is_link():
    """ComfyUI's own is_link: from COMFYUI_DIR's comfy_execution/graph_utils.py when set (only that function is read),
    else a copy of it."""
    folder = os.environ.get("COMFYUI_DIR")
    if folder:
        import ast

        with open(os.path.join(folder, "comfy_execution", "graph_utils.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        picked = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "is_link"]
        ns: dict = {}
        exec(compile(ast.Module(body=picked, type_ignores=[]), "comfyui/graph_utils.py", "exec"), ns)  # noqa: S102
        return ns["is_link"]

    def copy(obj):
        if not isinstance(obj, list) or len(obj) != 2 or not isinstance(obj[0], str):
            return False
        return isinstance(obj[1], int) or isinstance(obj[1], float)

    return copy
NODES = frozenset({"LoadImage", "SaveImage", "CLIPTextEncode", "Load3D", "VHS_LoadVideo", "LoadImagesFromFolderKJ", "LoadImageFromUrl", "Image Load", "Image Saver", "PrimitiveString"})
STAGED = ["fuobe_abcdefgh12.png", "fuobe_abcdefgh34.spz"]


def g(*nodes) -> dict:
    return {str(i + 1): n for i, n in enumerate(nodes)}


def node(cls, **inputs) -> dict:
    return {"class_type": cls, "inputs": inputs}


class NamesTest(unittest.TestCase):
    def test_staged_names(self):
        for ok in ["fuobe_abcdefgh12.png", "fuobe_ABC-def_1234.mp4", "fuobe_abcdefgh12.spz", "fuobe_abcdefgh12.wav"]:
            self.assertTrue(staged_name_ok(ok), ok)
        for bad in ["my_photo.png", "fuobe_short.png", "fuobe_abcdefgh12.html", "fuobe_abcdefgh12.svg", "fuobe_abcdefgh12.py", "fuobe_abcdefgh12.js", "fuobe_abcdefgh12.png [output]", "../fuobe_abcdefgh12.png", None, 3]:
            self.assertFalse(staged_name_ok(bad), bad)
        # A settings file, or a file of no known type, that a node could read as one (ffmpeg's format file is a .json).
        for bad in ["fuobe_abcdefgh12.json", "fuobe_abcdefgh12.txt", "fuobe_abcdefgh12.csv", "fuobe_abcdefgh12.bin", "fuobe_abcdefgh12.octet"]:
            self.assertFalse(staged_name_ok(bad), bad)


class VetTest(unittest.TestCase):
    def test_unknown_node(self):
        self.assertEqual(vet_graph(g(node("KJ_RunPython", code="import os")), NODES, STAGED), "unknown-node")
        self.assertEqual(vet_graph(g({"inputs": {}}), NODES, STAGED), "unknown-node")
        self.assertEqual(vet_graph({}, NODES, STAGED), "unsafe-graph")
        self.assertEqual(vet_graph("x", NODES, STAGED), "unsafe-graph")

    def test_path_like_text(self):
        for v in [
            "C:/Users/me/Pictures",
            "C:" + B + "Users" + B + "me",
            B + B + "server" + B + "share",
            "/etc/passwd",
            "/secret.png",
            "/",
            "../../secret.png",
            "fuobe/" + ".." + "/x",
            "~/.ssh/id_rsa",
            "http://192.168.1.1/admin",
            "https://example.com/x.png",
            "file:///etc/passwd",
            "blake3:0123456789abcdef",
            "ComfyUI_00001_.png [output]",
            "preview.png [temp]",
            "holiday.png [input]",
        ]:
            self.assertEqual(vet_graph(g(node("CLIPTextEncode", text=v)), NODES, STAGED), "unsafe-path", v)
        # Ordinary words, a staged input with its folder tag, a Fuobe prefix, a model in a subfolder: fine.
        for v in ["a red door", "wait.. what?", "...", "fuobe/gen123abc", "flux/model.safetensors", "1/2 cup", "fuobe_abcdefgh12.png [input]"]:
            self.assertIsNone(vet_graph(g(node("CLIPTextEncode", text=v)), NODES, STAGED), v)

    def test_every_absolute_form(self):
        """Every way Windows, Linux and macOS read a text as a place outside a node's own folder."""
        for v in [
            "//?/C:/Users/Public/x.srt",  # a Win32 device path with forward slashes
            "//attacker.example/share/x.srt",  # a network share with forward slashes
            B + B + "?" + B + "C:" + B + "x",  # \\?\C:\x
            B + B + "." + B + "PhysicalDrive0",  # \\.\PhysicalDrive0
            B + "Users" + B + "Public" + B + "x.srt",  # \Users\...: the root of ComfyUI's drive
            B + "/attacker.example/share/x.srt",  # \/host/share: a share too
            "D:Users" + B + "x.srt",  # a drive with no slash: that drive's own working folder
            "C:x.srt",
            "C:",
            "~",
            "~admin/Videos/x.mp4",  # another account's home
            "~admin",
            "$HOME/x",
            "${HOME}/x",
            "$HOME",
            "%USERPROFILE%" + B + "x",
            "%APPDATA%",
            "%HOMEDRIVE%%HOMEPATH%" + B + "x",  # two variables, one after the other
            "${HOME}$USER/x",
            # What a node that trims its text sees.
            " C:/Users/me",
            "\t/etc/passwd",
            "\u3000" + B + "Users",
            '"C:/Users/me"',
            "'//host/share'",
            ' " ~/x',
        ]:
            self.assertEqual(vet_graph(g(node("CLIPTextEncode", text=v)), NODES, STAGED), "unsafe-path", repr(v))
        # Words that start like one but aren't: a dialogue line, a mood, a price, a share of something.
        for v in ["A: hello there", "B:\tok", "~ dreamy ~", "$5 bill", "$HOME is where the heart is", "50% off", "%d frames", "SPEAKER A: Test"]:
            self.assertIsNone(vet_graph(g(node("CLIPTextEncode", text=v)), NODES, STAGED), repr(v))

    def test_a_place_inside_json(self):
        """A plan or a settings text that a node parses: no key that names a file, a folder or an address."""
        for v in [
            '{"shots": [{"id": "a", "lip_sync_source_asset": {"path": "x.wav"}}]}',
            '{"video_path": "clip.mp4"}',
            "{'output_folder': 'x'}",
            '{"model_root": "models"}',
            '{"config_file": "a.yaml"}',
            '{"url": "x"}',
            '{"src": "x"}',
            '{"FileName": "x"}',
        ]:
            self.assertEqual(vet_graph(g(node("CLIPTextEncode", text=v)), NODES, STAGED), "unsafe-path", v)
        # The JSON Fuobe's own graphs send: a lean picture prompt, boxes, a plan, touches.
        for v in [
            '{"high_level_description": "a red door", "elements": [{"type": "photo", "desc": "x", "bbox": [0, 0, 1, 1]}], "color_palette": ["#fff"]}',
            '{"coordinate_space": "pixels", "boxes": []}',
            '{"shots": [{"id": "intro", "prompt": "Describe the opening"}]}',
            '{"items": [{"op": "add", "x": 1, "y": 2, "t": 0}]}',
            "Profile: a file of notes, a folder of pictures",
        ]:
            self.assertIsNone(vet_graph(g(node("CLIPTextEncode", text=v)), NODES, STAGED), v)

    def test_nodes_that_read_or_fetch(self):
        """Nodes from common packs that read any folder, fetch a URL or write anywhere: refused by the list, and by the
        path check when the person added them to their own list."""
        risky = [
            node("LoadImagesFromFolderKJ", folder="C:/Users/me/Pictures"),
            node("LoadImageFromUrl", url="http://192.168.1.1/admin"),
            node("Image Load", image_path="C:/ProgramData/x.png"),
            node("Image Saver", path="C:/ProgramData/Microsoft/Windows/Start Menu", images=["1", 0]),
        ]
        for n in risky:
            self.assertEqual(vet_graph(g(n), frozenset({"SaveImage"}), STAGED), "unknown-node", n)
            self.assertEqual(vet_graph(g(n), NODES, STAGED), "unsafe-path", n)

    def test_loaders_read_only_this_runs_files(self):
        self.assertIsNone(vet_graph(g(node("LoadImage", image="fuobe_abcdefgh12.png")), NODES, STAGED))
        self.assertIsNone(vet_graph(g(node("LoadImage", image="fuobe_abcdefgh12.png [input]")), NODES, STAGED))
        for bad in [
            node("LoadImage", image="my_photo.png"),
            node("LoadImage", image="fuobe_zzzzzzzz99.png"),  # a Fuobe name, but not this run's
            node("LoadImage", image=["2", 0]),  # a name another node works out
            node("VHS_LoadVideo", video="clip.mp4"),
            node("Load3D", model_file="3d/scene.spz"),
            node("Load3D", model_file="fuobe_abcdefgh34.spz", image={"image": "secret.png", "camera_info": {}}),
        ]:
            self.assertEqual(vet_graph(g(bad, node("PrimitiveString", value="x")), NODES, STAGED), "unsafe-graph", bad)
        self.assertIsNone(vet_graph(g(node("Load3D", model_file="fuobe_abcdefgh34.spz", image={"image": "fuobe_abcdefgh12.png", "mask": "", "camera_info": {"position": {"x": 1, "y": 2, "z": 3}, "zoom": 1}})), NODES, STAGED))
        # Direct mode doesn't know the run: any name Fuobe gives an input.
        self.assertIsNone(graph_problem(g(node("LoadImage", image="fuobe_zzzzzzzz99.png")), None))
        self.assertIsNotNone(graph_problem(g(node("LoadImage", image="my_photo.png")), None))

    def test_studio_workflows_pass(self):
        """Every workflow Fuobe ships passes every check once its loaders name the files the studio sends (when the
        pack is inside Fuobe): only listed nodes, no path-like text, every rule above."""
        folder = os.path.join(PACK, "..", "..", "workflows", "comfyui")
        files = [f for f in glob.glob(os.path.join(folder, "*.json")) if os.path.basename(f) not in ("requirements.json", "manifest.json", "object_info.subset.json")]
        if not files:
            self.skipTest("Fuobe's workflows aren't beside this copy of the pack")
        nodes = shipped_nodes()
        checked = 0
        for f in files:
            with open(f, encoding="utf-8") as fh:
                wf = json.load(fh)
            if not isinstance(wf, dict) or not all(isinstance(n, dict) and "class_type" in n for n in wf.values()):
                continue
            checked += 1
            self.assertIsNone(vet_graph(staged_like_the_studio(wf), nodes, None), os.path.basename(f))
        self.assertGreater(checked, 50)


def staged_like_the_studio(wf: dict) -> dict:
    """The workflow as the studio sends it: each loader's placeholder file becomes a Fuobe input name, and a 2K finish
    loads its own draft's latent (src/lib/catalog/regenerate.ts latentFileFor)."""
    wf = json.loads(json.dumps(wf))
    count = [0]

    def staged(v):
        if not isinstance(v, str) or not v or staged_name_ok(v):
            return v
        count[0] += 1
        ext = v.rsplit(".", 1)[1].lower() if "." in v else "png"
        return f"fuobe_placeholder{count[0]:04d}.{ext}"

    for n in wf.values():
        inputs = n.get("inputs") or {}
        for k in FILE_LOADERS.get(n["class_type"], []) + OPTIONAL_FILES.get(n["class_type"], []):
            inputs[k] = staged(inputs.get(k))
        if n["class_type"] == "OpenH3LoadRefinedLatent":
            inputs["latent_file"] = "fuobe_h3_gen123abc_00001.safetensors"
        if n["class_type"] == "Load3D" and isinstance(inputs.get("image"), dict):
            for k, v in inputs["image"].items():
                if k != "camera_info":
                    inputs["image"][k] = staged(v)
    return wf


SHIPPED = shipped_nodes()


def vet1(cls, staged=STAGED, **inputs):
    """One node of the shipped list, with these inputs."""
    return vet_graph(g(node(cls, **inputs)), SHIPPED, staged)


class NodeRulesTest(unittest.TestCase):
    """Inputs that a listed node reads as a file or a folder take only Fuobe's own form, and never a link."""

    def test_rules_name_shipped_nodes(self):
        for table in (PATH_INPUTS, LINK_INPUTS, TEXT_INPUTS, OPTIONAL_FILES, ARG_NODES, LOOSE):
            for cls in table:
                self.assertIn(cls, SHIPPED, cls)
        self.assertIn("Hi3DLoadMesh", FILE_LOADERS)

    def test_subtitle_file(self):
        """AILab_Qwen3ASRSubtitle writes its file at output_path (an absolute path as given)."""
        for ok in ["", "fuobe/captions", "fuobe/gen123/captions"]:
            self.assertIsNone(vet1("AILab_Qwen3ASRSubtitle", output_path=ok, audio=["9", 0]), ok)
        for bad in [
            "//?/C:/Users/Public/x.srt",
            "//attacker.example/share/x.srt",
            B + "Users" + B + "Public" + B + "x.srt",
            B + "/attacker.example/share/x.srt",
            "D:Users" + B + "x.srt",
            "C:/Users/Public/x.srt",
            "captions",  # anywhere in your output folder
            "fuobe/x.bat",
            "fuobe/../x",
            "fuobe",
        ]:
            self.assertEqual(vet1("AILab_Qwen3ASRSubtitle", output_path=bad, audio=["9", 0]), "unsafe-path", repr(bad))
        # A path another node works out: never.
        self.assertEqual(vet_graph(g(node("PrimitiveStringMultiline", value="fuobe/captions"), node("AILab_Qwen3ASRSubtitle", output_path=["1", 0])), SHIPPED, STAGED), "unsafe-path")

    def test_source_timeline_reads_no_file(self):
        """MiniMaxH3SourceTimeline opens any file at video_path / audio_path: Fuobe's graphs leave them empty."""
        self.assertIsNone(vet1("MiniMaxH3SourceTimeline", video_path="", audio_path="", source_audio=["2", 0]))
        for bad in [
            B + "Users" + B + "me" + B + "Videos" + B + "private.mp4",
            "//?/D:/private/voice.wav",
            "ComfyUI/input/my_private.mp4",  # relative to ComfyUI's working folder: your own files
            "ComfyUI/output/ComfyUI_00001_.mp4",
            "~admin/Videos/x.mp4",
            "fuobe_abcdefgh12.png",
        ]:
            self.assertEqual(vet1("MiniMaxH3SourceTimeline", video_path=bad, audio_path=""), "unsafe-path", bad)
            self.assertEqual(vet1("MiniMaxH3SourceTimeline", video_path="", audio_path=bad), "unsafe-path", bad)
        self.assertEqual(vet_graph(g(node("StringConcatenate", string_a="C", string_b="x", delimiter=":"), node("MiniMaxH3SourceTimeline", video_path=["1", 0], audio_path="")), SHIPPED, STAGED), "unsafe-path")

    def test_model_paths(self):
        self.assertIsNone(vet1("AuKModelLoader", checkpoint_path="ckpts/AuK-Flash/auk_flash.safetensors", config_path="", qwen_path="ckpts/Qwen2.5-Omni-3B", device="cuda:0", dtype="bf16"))
        self.assertIsNone(vet1("AuKModelLoader", checkpoint_path="ckpts/AuK/auk_base.safetensors", config_path="", qwen_path=""))
        for bad in [
            {"checkpoint_path": "ckpts/../x.safetensors"},
            {"checkpoint_path": "ckpts/AuK/x.ckpt"},
            {"checkpoint_path": "input/fuobe_abcdefgh12.safetensors"},
            {"config_path": "ckpts/AuK/config.yaml"},
            {"qwen_path": "models"},
            {"qwen_path": "ckpts/.hidden"},
        ]:
            inputs = {"checkpoint_path": "ckpts/AuK/auk_base.safetensors", "config_path": "", "qwen_path": "", **bad}
            self.assertEqual(vet1("AuKModelLoader", **inputs), "unsafe-path", bad)
        self.assertIsNone(vet1("Hi3DModelLoader", model_root="", attention="sdpa"))
        self.assertEqual(vet1("Hi3DModelLoader", model_root="models"), "unsafe-path")
        self.assertIsNone(vet1("QWMRefineMask", sam_model="sam_vit_b_01ec64.pth", boxes_json=["2", 0]))
        for bad in ["input/fuobe_vit_b_12345678.bin", "fuobe_vit_b_12345678.bin", "sam_vit_b.bin", "x/sam_vit_b_01ec64.pth"]:
            self.assertEqual(vet1("QWMRefineMask", sam_model=bad), "unsafe-path", bad)
        self.assertIsNone(vet1("ZenImage21AdapterLoader", model_folder="", adapter_file="models/text_encoders/adapter_v12.safetensors", text_encoder="Qwen/Qwen3.5-0.8B"))
        self.assertIsNone(vet1("ZenImage21AdapterLoader", model_folder="", adapter_file="models/text_encoders/adapter_v12.safetensors", text_encoder="models/text_encoders/qwen3.5_0.8b"))
        for bad in [{"model_folder": "models"}, {"adapter_file": "input/fuobe_abcdefgh12.bin"}, {"text_encoder": "someone/other-model"}, {"text_encoder": "models/text_encoders/../x"}]:
            inputs = {"model_folder": "", "adapter_file": "models/text_encoders/adapter_v12.safetensors", "text_encoder": "Qwen/Qwen3.5-0.8B", **bad}
            self.assertEqual(vet1("ZenImage21AdapterLoader", **inputs), "unsafe-path", bad)

    def test_folders_a_node_writes(self):
        self.assertIsNone(vet1("WanVideoImageToVideoMultiTalk", output_path="", mode="infinitetalk"))
        self.assertEqual(vet1("WanVideoImageToVideoMultiTalk", output_path="frames"), "unsafe-path")
        self.assertIsNone(vet1("OpenH3LoadRefinedLatent", latent_file="fuobe_h3_gen123abc_00001.safetensors"))
        for bad in ["(newest)", "my_latent_00001.safetensors", "open_h3_regen_00001.safetensors"]:
            self.assertEqual(vet1("OpenH3LoadRefinedLatent", latent_file=bad), "unsafe-path", bad)

    def test_run_folder(self):
        """A Context Loop run's folder under output/h3_chains: a Fuobe job's own, or a swap plan's."""
        self.assertIsNone(vet1("MiniMaxH3ChainPlanModern", run_name="fuobe_abc123XYZ"))
        self.assertIsNone(vet1("MiniMaxH3ChainPlanModern", run_name="fuobe_job-x"))
        for bad in ["my_film", "fuobe_", "fuobe_a/b", "h3_chain"]:
            self.assertEqual(vet1("MiniMaxH3ChainPlanModern", run_name=bad), "unsafe-path", bad)
        plan = node("H3AdvancedSwapPlan", roles="")
        self.assertIsNone(vet_graph(g(plan, node("MiniMaxH3ChainPlanModern", run_name=["1", 3])), SHIPPED, STAGED))
        self.assertEqual(vet_graph(g(node("PrimitiveStringMultiline", value="my_film"), node("MiniMaxH3ChainPlanModern", run_name=["1", 0])), SHIPPED, STAGED), "unsafe-graph")

    def test_optional_history_file(self):
        self.assertIsNone(vet1("H3CharacterSwapShot", shot_id="character-swap"))
        self.assertIsNone(vet1("H3CharacterSwapShot", history_file=""))
        self.assertIsNone(vet1("H3CharacterSwapShot", history_file="fuobe_abcdefgh56.npz", staged=["fuobe_abcdefgh56.npz"]))
        for bad in ["history.npz", "fuobe_zzzzzzzz99.npz", ["2", 0]]:
            self.assertEqual(vet1("H3CharacterSwapShot", history_file=bad), "unsafe-graph", bad)

    def test_mesh_loader(self):
        self.assertIsNone(vet1("Hi3DLoadMesh", mesh_file="fuobe_abcdefgh34.glb", staged=["fuobe_abcdefgh34.glb"]))
        for bad in ["coarse.glb", "hi3d/mine.glb", ["2", 0]]:
            self.assertEqual(vet1("Hi3DLoadMesh", mesh_file=bad), "unsafe-graph", bad)

    def test_json_objects(self):
        """ComfyUI passes an object the graph writes to a node unchecked (a LoRA list, a model path, a {"__value__"})."""
        for bad in [
            node("WanVideoModelLoader", extra_model={"path": "models/x.safetensors"}),
            node("WanVideoModelLoader", lora={"__value__": [{"path": "loras/x.safetensors", "strength": 1}]}),
            node("WanVideoModelLoader", compile_args={"backend": "inductor"}),
            node("SaveGLB", mesh={"path": "x.glb"}),
        ]:
            self.assertEqual(vet_graph(g(bad), SHIPPED, STAGED), "unsafe-graph", bad)
        # A 3D view's entries are the one object a graph writes.
        self.assertIsNone(vet1("Save3DAdvanced", viewport_state={"image": "", "mask": "", "normal": "", "camera_info": {"position": {"x": 1, "y": 2, "z": 3}, "zoom": 1}}))
        self.assertEqual(vet1("Save3DAdvanced", viewport_state={"image": "C:/x.png"}), "unsafe-path")

    def test_svg_comes_from_the_vectorizer(self):
        vector = node("TS_ImageToSVGStringColor_Vtracer", image=["9", 0])
        self.assertIsNone(vet_graph(g(vector, node("TS_SVGStringToSVGBytesIO", SVG_String=["1", 0])), SHIPPED, STAGED))
        self.assertEqual(vet1("TS_SVGStringToSVGBytesIO", SVG_String="<svg onload='x()'/>"), "unsafe-graph")
        self.assertEqual(vet_graph(g(node("StringConcatenate", string_a="<svg", string_b="></svg>", delimiter=""), node("TS_SVGStringToSVGBytesIO", SVG_String=["1", 0])), SHIPPED, STAGED), "unsafe-graph")

    def test_format_reads_no_attribute(self):
        """Python's format reaches a module's globals through an object: "{a.__init__.__globals__[os].environ}"."""
        for ok in ["Dress {a} in image 1 in {b}. Keep the rest.", '{{"width": {a}, "height": {b}, "coords": "relative-1000"}}', "{a:.2f} and {b!r}", "{{a.b}}"]:
            self.assertIsNone(vet1("StringFormat", f_string=ok), ok)
        for bad in ["{a.__init__.__globals__[os].environ}", "{a[0]}", "x {b.real}", "{{{a.x}}}", "{a:{b.__class__}}", ["2", 0]]:
            self.assertEqual(vet1("StringFormat", f_string=bad), "unsafe-path", bad)


def chain(*nodes) -> dict:
    """A graph whose last node is fed by the ones before it (ids "1", "2", ...)."""
    return g(*nodes)


_DYNAMIC = ("COMFY_AUTOGROW_V3", "COMFY_DYNAMICCOMBO_V3")


def recorded_kinds(info_file: str) -> NodeKinds:
    """NodeKinds from a recorded /object_info. A v3 node's dynamic input ("images", "mode") stands for the inputs
    ComfyUI works out under its name ("images.image_1", "mode.yaw"); inside ComfyUI, comfy_kinds asks ComfyUI's own
    get_finalized_class_inputs (test_comfy_engine.py runs that). Start Loop and End Loop are ComfyUI 0.42's loops."""
    with open(info_file, encoding="utf-8") as f:
        info = {k: v for k, v in json.load(f).items() if isinstance(v, dict)}

    def specs(cls):
        inputs = info[cls].get("input") or {}
        return {k: s for part in ("required", "optional") for k, s in (inputs.get(part) or {}).items()}

    def dynamic(cls):
        return [k for k, s in specs(cls).items() if isinstance(s, list) and s and s[0] in _DYNAMIC]

    def lookup(cls):
        if cls not in info:
            return None
        about = {"output": info[cls].get("output_node") is True, "loop": cls in ("StartLoop", "EndLoop"), "dynamic": bool(dynamic(cls))}
        return info[cls].get("input") or {}, info[cls].get("output") or [], about

    def checked(cls, inputs):
        names = set(specs(cls))
        grows = dynamic(cls)
        return names | {k for k in inputs if "." in k and k.split(".")[0] in grows}

    return NodeKinds(lookup, checked)


class FakeNode:
    """A node class as ComfyUI's NODE_CLASS_MAPPINGS holds it."""

    def __init__(self, inputs: dict, outputs: tuple):
        self._inputs = inputs
        self.RETURN_TYPES = outputs

    def INPUT_TYPES(self):
        return self._inputs


class AnyType(str):
    """A node pack's own "any" type: a text that says it equals every other type."""

    def __ne__(self, other):
        return False


class IO(str, enum.Enum):
    """ComfyUI's own IO types (comfy/comfy_types/node_typing.py, ComfyUI 0.42): a str enum whose __ne__ treats "*" as
    every type and compares comma-separated types as sets."""

    STRING = "STRING"
    IMAGE = "IMAGE"
    CLIP = "CLIP"
    MODEL = "MODEL"
    LATENT = "LATENT"
    CONDITIONING = "CONDITIONING"
    COMBO = "COMBO"
    MATCHTYPE = "COMFY_MATCHTYPE_V3"
    NUMBER = "FLOAT,INT"
    ANY = "*"

    def __str__(self) -> str:
        return self.value

    def __ne__(self, value: object) -> bool:
        if self == "*" or value == "*":
            return False
        if not isinstance(value, str):
            return True
        a = frozenset(self.split(","))
        b = frozenset(value.split(","))
        return not (b.issubset(a) or a.issubset(b))


# What ComfyUI's node classes say about the nodes the tests below use (as /object_info shows them).
CLASSES = {
    "CreateList": FakeNode({"required": {"input1": ("*", {})}}, ("*",)),
    "GetItemFromList": FakeNode({"required": {"list": ("*", {}), "index": ("INT", {})}}, ("*",)),
    "VRAM_Debug": FakeNode({"required": {"any_input": ("*", {})}}, ("*", "INT", "INT", "INT")),
    "ComfySwitchNode": FakeNode({"required": {"switch": ("BOOLEAN", {}), "on_true": ("COMFY_MATCHTYPE_V3", {}), "on_false": ("COMFY_MATCHTYPE_V3", {})}}, ("COMFY_MATCHTYPE_V3",)),
    "PrimitiveStringMultiline": FakeNode({"required": {"value": ("STRING", {})}}, ("STRING",)),
    "StringConcatenate": FakeNode({"required": {"string_a": ("STRING", {}), "string_b": ("STRING", {}), "delimiter": ("STRING", {})}}, ("STRING",)),
    "CLIPTextEncode": FakeNode({"required": {"text": ("STRING", {}), "clip": ("CLIP",)}}, ("CONDITIONING",)),
    "LoraLoader": FakeNode({"required": {"model": ("MODEL",), "clip": ("CLIP",), "lora_name": (["mine.safetensors"],), "strength_model": ("FLOAT", {})}}, ("MODEL", "CLIP")),
    "Apply Whisper": FakeNode({"required": {"audio": ("AUDIO",), "model": (["base", "small"],)}}, ("STRING", "whisper_alignment", "whisper_alignment")),
    "GetVideoComponents": FakeNode({"required": {"video": ("VIDEO", {})}}, ("IMAGE", "AUDIO", "FLOAT", "COMBO", "COMBO")),
    "CreateVideo": FakeNode({"required": {"images": ("IMAGE", {}), "fps": ("FLOAT", {}), "bit_depth": ("COMBO", {"options": ["8bit", "10bit"]})}}, ("VIDEO",)),
    "MyAnyNode": FakeNode({"required": {"value": ("STRING", {})}}, (AnyType("ANYTHING"),)),
    "MyOptions": FakeNode({"required": {}}, (["a", "b"],)),
}
KINDS = comfy_kinds(lambda: CLASSES)
WITH_OWN = SHIPPED | {"MyAnyNode", "MyOptions"}


class LinkTest(unittest.TestCase):
    """ComfyUI doesn't type-check a link from an "any", match-typed or dropdown-typed output, and checks a dropdown's
    value against its list only when the graph writes it. Such links, and links into a dropdown, feed only what Fuobe's
    own graphs feed that way."""

    def test_shipped_list(self):
        for cls in ("CreateList", "GetItemFromList", "VRAM_Debug", "ComfySwitchNode", "StartLoop", "EndLoop"):
            self.assertIn(cls, LOOSE, cls)
        self.assertEqual(LOOSE["GetVideoComponents"], frozenset({3, 4}))
        self.assertIn(("GetVideoComponents", "CreateVideo", "bit_depth"), LOOSE_LINKS)
        self.assertIn(("StartLoop", "AudioConcat", "audio#"), LOOSE_LINKS)
        for src, dst, key in LOOSE_LINKS:
            self.assertIn(src, SHIPPED, src)
            self.assertIn(dst, SHIPPED, dst)
            self.assertEqual(input_kind(key), key)
        self.assertEqual(input_kind("images.image12"), "images.image#")
        self.assertEqual(input_kind("audio2"), "audio#")

    def test_any_link_into_a_dropdown(self):
        """CreateList or VRAM_Debug ("*") hands a dropdown any text: a model name it then downloads, or a file name."""
        evil = "attacker/evil-repo"
        for kinds in (None, KINDS):
            through_list = chain(node("CreateList", input1=evil), node("GetItemFromList", list=["1", 0], index=0), node("DownloadAndLoadWav2VecModel", model=["2", 0], base_precision="fp16", load_device="main_device"))
            self.assertIsNotNone(vet_graph(through_list, SHIPPED, STAGED, kinds))
            through_debug = chain(node("PrimitiveStringMultiline", value=evil), node("VRAM_Debug", any_input=["1", 0]), node("DownloadAndLoadWav2VecModel", model=["2", 0]))
            self.assertIsNotNone(vet_graph(through_debug, SHIPPED, STAGED, kinds))
            # A dropdown that no rule here names: the link itself is refused.
            for src in (node("GetItemFromList", list=["9", 0], index=0), node("VRAM_Debug", any_input=["9", 0]), node("ComfySwitchNode", switch=True, on_true=["9", 0], on_false=["9", 0])):
                self.assertEqual(vet_graph(chain(src, node("LoraLoader", model=["8", 0], clip=["8", 1], lora_name=["1", 0])), SHIPPED, STAGED, kinds), "unsafe-graph", src)
                self.assertEqual(vet_graph(chain(src, node("Apply Whisper", audio=["8", 0], model=["1", 0])), SHIPPED, STAGED, kinds), "unsafe-graph", src)
                # Into any input Fuobe's graphs don't feed that way: the text would reach it unchecked.
                self.assertEqual(vet_graph(chain(src, node("CLIPTextEncode", text=["1", 0], clip=["8", 0])), SHIPPED, STAGED, kinds), "unsafe-graph", src)

    def test_links_fuobe_graphs_make(self):
        for kinds in (None, KINDS):
            # Fuobe's own: a switch picks a model for the sampler; a clip's bit depth goes to the encoder.
            self.assertIsNone(vet_graph(chain(node("ComfySwitchNode", switch=True, on_true=["8", 0], on_false=["9", 0]), node("KSampler", model=["1", 0], positive=["7", 0])), SHIPPED, STAGED, kinds))
            self.assertIsNone(vet_graph(chain(node("GetVideoComponents", video=["9", 0]), node("CreateVideo", images=["1", 0], fps=["1", 2], bit_depth=["1", 3])), SHIPPED, STAGED, kinds))
            self.assertIsNone(vet_graph(chain(node("StartLoop", initial_iteration_value=["9", 0]), node("AudioConcat", audio1=["1", 4], audio2=["9", 0])), SHIPPED, STAGED, kinds))
            # A typed output (pictures, a number) feeds any input it fits; ComfyUI checks its type.
            self.assertIsNone(vet_graph(chain(node("GetVideoComponents", video=["9", 0]), node("SaveImage", images=["1", 0], filename_prefix="fuobe/x")), SHIPPED, STAGED, kinds))
            # The bit depth into another dropdown: no.
            self.assertEqual(vet_graph(chain(node("GetVideoComponents", video=["9", 0]), node("Apply Whisper", audio=["9", 0], model=["1", 3])), SHIPPED, STAGED, kinds), "unsafe-graph")

    def test_every_link_comfyui_reads(self):
        """ComfyUI reads [node, false] as output 0, [node, true] as output 1 and [node, -1] as the last output, and
        takes a number with a point as a link too. Each one is a link here, and each one is refused: a value worked out
        by a loose output would otherwise reach a dropdown or ffmpeg's arguments as if no link were there."""
        comfy = comfy_is_link()
        for v in (["1", 0], ["1", 3], ["1", False], ["1", True], ["1", 0.0], ["1", -1], ["1", 2.5], ["1", "0"], [1, 0], ["1"], ["1", 0, 0], ("1", 0), "1"):
            self.assertEqual(is_link(v), comfy(v), v)
        made = node("StringConcatenate", string_a="mine", string_b=".safetensors", delimiter="")
        switch = node("ComfySwitchNode", switch=True, on_true=["1", 0], on_false=["1", 0])
        video = {"images": ["9", 0], "format": "video/h264-mp4", "frame_rate": 24, "filename_prefix": "fuobe/x"}
        for kinds in (None, KINDS):
            # Slot 0: the link rules refuse these (the loose switch into a dropdown, a link into ffmpeg's arguments).
            self.assertEqual(vet_graph(chain(made, switch, node("CheckpointLoaderSimple", ckpt_name=["2", 0])), SHIPPED, STAGED, kinds), "unsafe-graph")
            self.assertEqual(vet_graph(chain(made, switch, node("VHS_VideoCombine", **video, pix_fmt=["2", 0])), SHIPPED, STAGED, kinds), "unsafe-graph")
            for slot in (False, True, 0.0, 1.0, -1, 1000):
                for target in (
                    node("CheckpointLoaderSimple", ckpt_name=["2", slot]),
                    node("LoraLoader", model=["8", 0], clip=["8", 1], lora_name=["2", slot]),
                    node("VHS_VideoCombine", **video, pix_fmt=["2", slot]),
                    node("VHS_VideoCombine", **{**video, "filename_prefix": ["2", slot]}),
                    node("VHS_VideoCombine", **{**video, "images": ["2", slot]}),
                    node("CLIPTextEncode", text=["2", slot], clip=["8", 0]),
                    node("KSampler", model=["2", slot], positive=["7", 0]),
                    node("LoadImage", image=["2", slot]),
                ):
                    self.assertEqual(vet_graph(chain(made, switch, target), SHIPPED, STAGED, kinds), "unsafe-graph", (slot, target))
            # Fuobe's own form still passes.
            self.assertIsNone(vet_graph(chain(made, switch, node("KSampler", model=["2", 0], positive=["7", 0])), SHIPPED, STAGED, kinds))

    def test_lists_inside_a_value_are_read(self):
        """ComfyUI reads a link only as an input's whole value. A list inside a value is never a link, so its texts are
        read like any other."""
        for inner in (["//host/share/x", 0], ["C:" + B + "Users" + B + "x", False], ["/etc/passwd", 0.0]):
            self.assertEqual(vet1("CLIPTextEncode", text="a red door", clip=[inner, 0]), "unsafe-path", inner)
            self.assertEqual(vet1("CLIPTextEncode", text=["x", [inner]], clip=["8", 0]), "unsafe-path", inner)
        # A link's own node id isn't text a node reads.
        self.assertIsNone(vet1("CLIPTextEncode", text="a red door", clip=["8", 0]))

    def test_node_kinds_inside_comfyui(self):
        """Inside ComfyUI the node classes say more: any node's untyped outputs (your own listed nodes' too), and
        every dropdown, which a typed link may not feed either when a node skips ComfyUI's type check."""
        own_any = chain(node("MyAnyNode", value="x"), node("CLIPTextEncode", text=["1", 0], clip=["8", 0]))
        self.assertIsNone(vet_graph(own_any, WITH_OWN, STAGED))  # not on the shipped list of untyped outputs
        self.assertEqual(vet_graph(own_any, WITH_OWN, STAGED, KINDS), "unsafe-graph")
        options = chain(node("MyOptions"), node("LoraLoader", model=["8", 0], clip=["8", 1], lora_name=["1", 0]))
        self.assertEqual(vet_graph(options, WITH_OWN, STAGED, KINDS), "unsafe-graph")
        typed_into_dropdown = chain(node("StringConcatenate", string_a="mine", string_b=".safetensors", delimiter=""), node("LoraLoader", model=["8", 0], clip=["8", 1], lora_name=["1", 0]))
        self.assertIsNone(vet_graph(typed_into_dropdown, SHIPPED, STAGED))  # ComfyUI itself refuses STRING into a dropdown
        self.assertEqual(vet_graph(typed_into_dropdown, SHIPPED, STAGED, KINDS), "unsafe-graph")
        # A typed text into a text input stays fine.
        self.assertIsNone(vet_graph(chain(node("StringConcatenate", string_a="a", string_b="b", delimiter=" "), node("CLIPTextEncode", text=["1", 0], clip=["8", 0])), SHIPPED, STAGED, KINDS))
        # A class that can't say what it is counts as having nothing to say; one that fails on its outputs is untyped.
        broken = NodeKinds(lambda cls: (_ for _ in ()).throw(RuntimeError("no")))
        self.assertFalse(broken.loose("X", 0))
        odd = NodeKinds(lambda cls: ({"required": {}}, 5))
        self.assertTrue(odd.loose("X", 0))

    def test_comfyuis_own_io_types(self):
        """ComfyUI's core nodes name their types with its IO enum (CLIPTextEncode: (IO.CONDITIONING,)): texts that
        compare by name, except IO.ANY ("*"), which equals every type. A link from IO.CONDITIONING is typed."""
        for t in (IO.CONDITIONING, IO.IMAGE, IO.NUMBER, "IMAGE", "IMAGE,MASK"):
            self.assertFalse(loose_type(t), t)
        for t in (IO.ANY, IO.MATCHTYPE, IO.COMBO, "*", "COMBO", "STRING,*", ["a", "b"], AnyType("ANYTHING")):
            self.assertTrue(loose_type(t), t)
        # Not text, and saying it equals nothing: ComfyUI matches it to no input.
        self.assertFalse(loose_type(None))
        self.assertTrue(dropdown_spec((IO.COMBO, {})))
        self.assertFalse(dropdown_spec((IO.STRING, {})))
        classes = {
            "CLIPTextEncode": FakeNode({"required": {"text": (IO.STRING, {}), "clip": (IO.CLIP, {})}}, (IO.CONDITIONING,)),
            "KSampler": FakeNode({"required": {"model": (IO.MODEL, {}), "positive": (IO.CONDITIONING, {}), "sampler_name": (["euler"],)}}, (IO.LATENT,)),
            "PrimitiveAny": FakeNode({"required": {"value": (IO.STRING, {})}}, (IO.ANY,)),
        }
        kinds = comfy_kinds(lambda: classes)
        encode = node("CLIPTextEncode", text="a red door", clip=["8", 0])
        self.assertIsNone(vet_graph(chain(encode, node("KSampler", model=["8", 0], positive=["1", 0], sampler_name="euler")), SHIPPED, STAGED, kinds))
        self.assertEqual(vet_graph(chain(node("PrimitiveAny", value="x"), node("KSampler", model=["8", 0], positive=["1", 0])), SHIPPED | {"PrimitiveAny"}, STAGED, kinds), "unsafe-graph")

    def test_ffmpeg_format_file(self):
        """VHS_VideoCombine opens video_formats/<format>.json with the name as given and hands what it reads to ffmpeg:
        a format worked out at run time could name a .json in the input folder."""
        for staged in (None, ["fuobe_abcdefgh12.json"]):
            for kinds in (None, KINDS):
                replaced = chain(
                    node("StringReplace", string="video/@@" + B + "@@" + B + "@@" + B + "input" + B + "fuobe_abcdefgh12", find="@", replace="."),
                    node("GetItemFromList", list=["1", 0], index=0),
                    node("VHS_VideoCombine", images=["9", 0], format=["2", 0], frame_rate=24, filename_prefix="fuobe/x"),
                )
                self.assertIsNotNone(vet_graph(replaced, SHIPPED, staged, kinds))
                switched = chain(
                    node("PrimitiveStringMultiline", value="video/C:" + B + "ComfyUI" + B + "input" + B + "fuobe_abcdefgh12"),
                    node("ComfySwitchNode", switch=True, on_true=["1", 0], on_false=["1", 0]),
                    node("VHS_VideoCombine", images=["9", 0], format=["2", 0], frame_rate=24, filename_prefix="fuobe/x"),
                )
                self.assertIsNotNone(vet_graph(switched, SHIPPED, staged, kinds))
        for ok in ["video/h264-mp4", "video/ffv1-mkv", "video/ProRes", "video/webm", "image/gif", "image/webp", "video/nvenc_h264-mp4"]:
            self.assertIsNone(vet1("VHS_VideoCombine", images=["9", 0], format=ok, frame_rate=24, filename_prefix="fuobe/x"), ok)
        for bad in ["video/.." + B + ".." + B + "input" + B + "fuobe_abcdefgh12", "video/../x", "video/a/b", "video/a" + B + "b", "video/", "video/.x", "text/plain", "image/png"]:
            self.assertEqual(vet1("VHS_VideoCombine", images=["9", 0], format=bad, frame_rate=24, filename_prefix="fuobe/x"), "unsafe-path", bad)

    def test_ffmpeg_settings_are_plain_words(self):
        """The format's settings go into ffmpeg's arguments, the GIF dither into a filter (where "movie=" reads a file)."""
        base = {"images": ["9", 0], "format": "video/h264-mp4", "frame_rate": 24, "filename_prefix": "fuobe/x"}
        self.assertIsNone(vet1("VHS_VideoCombine", **base, pix_fmt="yuv420p", crf=19, save_metadata=True, profile="hq", dither="sierra2_4a", audio=["8", 0]))
        for bad in ["none[x];movie=secret.png[y];[x][y]overlay", "sierra2_4a:diff_mode=rectangle", "yuv420p -i x", "a,b"]:
            self.assertEqual(vet1("VHS_VideoCombine", **{**base, "format": "video/ffmpeg-gif"}, dither=bad), "unsafe-path", bad)
        # A link only into the pictures, the sound, the batch, the VAE and the frame rate.
        made = chain(node("StringConcatenate", string_a="none", string_b="x", delimiter=","), node("VHS_VideoCombine", **{**base, "format": "video/ffmpeg-gif"}, dither=["1", 0]))
        self.assertEqual(vet_graph(made, SHIPPED, STAGED), "unsafe-graph")
        prefix = chain(node("StringConcatenate", string_a="fuobe", string_b="x", delimiter="_"), node("VHS_VideoCombine", **{**base, "filename_prefix": ["1", 0]}))
        self.assertEqual(vet_graph(prefix, SHIPPED, STAGED), "unsafe-graph")

    def test_download_names(self):
        """Nodes that download what their dropdown names, or load a pickle by name: only the names on their lists."""
        for ok in ["TencentGameMate/chinese-wav2vec2-base", "facebook/wav2vec2-base-960h"]:
            self.assertIsNone(vet1("DownloadAndLoadWav2VecModel", model=ok, base_precision="fp16"), ok)
        for bad in ["attacker/evil-repo", "facebook/wav2vec2-base-960h-evil", "TencentGameMate/chinese-wav2vec2-base/../x", ["2", 0]]:
            self.assertEqual(vet1("DownloadAndLoadWav2VecModel", model=bad), "unsafe-path", bad)
        for ok in ["rife47.pth", "rife49.pth", "rife417.pth", "rife426.pth", "sudo_rife4_269.662_testV1_scale1.pth"]:
            self.assertIsNone(vet1("RIFE VFI", ckpt_name=ok, frames=["9", 0]), ok)
        for bad in ["rife50.pth", "x/rife49.pth", "rife49.pth.evil", ["2", 0]]:
            self.assertEqual(vet1("RIFE VFI", ckpt_name=bad), "unsafe-path", bad)
        for ok in ["face_yolov8m.pt", "bbox" + B + "face_yolov8m.pt", "segm/person_yolov8m-seg.pt"]:
            self.assertIsNone(vet1("H3FaceTrackCrop", detector=ok), ok)
        for bad in ["face.pth", "bbox/../x.pt", "other" + B + "x.pt", ".x.pt", ["2", 0]]:
            self.assertEqual(vet1("H3FaceTrackCrop", detector=bad), "unsafe-path", bad)

    def test_studio_workflows_pass_with_node_kinds(self):
        """Every workflow Fuobe ships passes with the node classes' own kinds too (from Fuobe's recorded /object_info)."""
        folder = os.path.join(PACK, "..", "..", "workflows", "comfyui")
        info_file = os.path.join(folder, "object_info.subset.json")
        if not os.path.exists(info_file):
            self.skipTest("Fuobe's workflows aren't beside this copy of the pack")
        kinds = recorded_kinds(info_file)
        checked = 0
        for path in glob.glob(os.path.join(folder, "*.json")):
            if os.path.basename(path) in ("requirements.json", "manifest.json", "object_info.subset.json"):
                continue
            with open(path, encoding="utf-8") as fh:
                wf = json.load(fh)
            if not isinstance(wf, dict) or not all(isinstance(n, dict) and "class_type" in n for n in wf.values()):
                continue
            checked += 1
            self.assertIsNone(vet_graph(staged_like_the_studio(wf), SHIPPED, None, kinds), os.path.basename(path))
        self.assertGreater(checked, 50)


# Fuobe's own 3D view (workflows/comfyui/image-to-3d-trellis2.json, Save3DAdvanced's viewport_state).
FUOBE_CAMERA = {
    "position": {"x": 10, "y": 10, "z": 10.000000000000002},
    "target": {"x": 0, "y": 0, "z": 0},
    "zoom": 1,
    "cameraType": "perspective",
    "quaternion": {"x": -0.2798481423331213, "y": 0.3647051996310008, "z": 0.11591689595929511, "w": 0.8804762392171493},
    "fov": 35,
    "aspect": 1,
    "near": 0.01,
    "far": 10000,
    "frustum": {"left": -5, "right": 5, "top": 5, "bottom": -5},
}
FUOBE_VIEW = {"image": "", "mask": "", "normal": "", "camera_info": FUOBE_CAMERA, "recording": "", "model_3d_info": []}
# A file in your output folder whose name ComfyUI makes up the same way on every computer.
ECHOED_FILE = {"filename": "ComfyUI_00001_.png", "subfolder": "", "type": "output"}


def save3d(view, **more) -> dict:
    return node("Save3DAdvanced", model_3d=["2", 6], viewport_state=view, width=512, height=512, filename_prefix="fuobe/abcdef12", **more)


def load3d(view) -> dict:
    return node("Load3D", model_file="fuobe_abcdefgh34.spz", image=view, width=512, height=512)


class SpecNode(FakeNode):
    """A node class with ComfyUI's other attributes: an output node, or one whose optional inputs claim every name."""

    def __init__(self, inputs: dict, outputs: tuple, output_node: bool = False):
        super().__init__(inputs, outputs)
        self.OUTPUT_NODE = output_node


class AllNames(dict):
    """A node pack's optional inputs that say they hold every name (VHS_VideoCombine's): get_input_info finds any name
    there, but validate_inputs goes through only the names the dict holds."""

    def __contains__(self, key):
        return True


ENGINE = {
    "SaveImage": SpecNode({"required": {"images": ("IMAGE",), "filename_prefix": ("STRING", {})}, "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"}}, (), True),
    "CLIPLoader": SpecNode({"required": {"clip_name": (["a.safetensors"],), "type": (["sd3"],)}, "optional": {"device": (["default", "cpu"], {})}}, ("CLIP",)),
    "EmptyImage": SpecNode({"required": {"width": ("INT", {}), "height": ("INT", {}), "batch_size": ("INT", {}), "color": ("INT", {})}}, ("IMAGE",)),
    "VHS_VideoCombine": SpecNode(
        {"required": {"images": ("IMAGE",), "frame_rate": ("FLOAT", {}), "filename_prefix": ("STRING", {}), "format": (["video/h264-mp4"],)}, "optional": AllNames({"audio": ("AUDIO",)}), "hidden": {"unique_id": "UNIQUE_ID"}},
        ("VHS_FILENAMES",),
        True,
    ),
}


class FakeReplacements:
    """ComfyUI's NodeReplaceManager, with the old node types it swaps."""

    def __init__(self, old: set):
        self.old = old

    def has_replacement(self, cls):
        return cls in self.old


def spec_kinds(specs: dict) -> NodeKinds:
    """NodeKinds from (inputs, outputs, about) per class; a dynamic input's worked-out names are "<name>.<anything>"."""

    def lookup(cls):
        return specs.get(cls)

    def checked(cls, inputs):
        spec = specs[cls][0]
        names = set(spec.get("required") or {}) | set(spec.get("optional") or {})
        return names | {k for k in inputs if "." in k and k.split(".")[0] in names}

    return NodeKinds(lookup, checked)


MT = "COMFY_MATCHTYPE_V3"
LOOPS = {
    "StartLoop": ({"required": {"mode": ("COMFY_DYNAMICCOMBO_V3", {}), "cache_iterations": ("BOOLEAN", {})}, "optional": {"initial_iteration_value": (MT, {})}, "hidden": {"dynprompt": "DYNPROMPT"}}, ("INT", "BOOLEAN", "BOOLEAN", MT, MT), {"loop": True, "dynamic": True}),
    "EndLoop": ({"required": {"accumulate": ("BOOLEAN", {})}, "optional": {"output_value": (MT, {}), "next_iteration_value": (MT, {}), "terminations": ("COMFY_AUTOGROW_V3", {})}}, (MT,), {"loop": True, "dynamic": True}),
    "Save3DAdvanced": ({"required": {"model_3d": ("FILE_3D", {}), "filename_prefix": ("STRING", {}), "viewport_state": ("LOAD_3D", {}), "width": ("INT", {}), "height": ("INT", {})}}, ("FILE_3D", "LOAD3D_MODEL_INFO", "LOAD3D_CAMERA", "INT", "INT"), {"output": True, "dynamic": True}),
    "Load3D": ({"required": {"model_file": ("COMBO", {}), "image": ("LOAD_3D", {}), "width": ("INT", {}), "height": ("INT", {})}}, ("IMAGE", "MASK", "STRING", "IMAGE", "LOAD3D_CAMERA", "VIDEO", "FILE_3D", "LOAD3D_MODEL_INFO"), {"dynamic": True}),
    "PrimitiveInt": ({"required": {"value": ("INT", {})}}, ("INT",), {"dynamic": True}),
    "EmptyImage": ({"required": {"width": ("INT", {}), "height": ("INT", {}), "batch_size": ("INT", {}), "color": ("INT", {})}}, ("IMAGE",), {}),
    "RebatchImages": ({"required": {"images": ("IMAGE",), "batch_size": ("INT", {})}}, ("IMAGE",), {}),
    "SaveImage": ({"required": {"images": ("IMAGE",), "filename_prefix": ("STRING", {})}}, (), {"output": True}),
}


class ComfyOwnRulesTest(unittest.TestCase):
    """What ComfyUI's own validate and execute code does with a graph, which the per-input rules above don't see: a value
    it turns into a link, inputs it runs from but never validates, keys it reads as its own state, a node type it swaps
    after the check, nodes a loop runs unvalidated, and data it shows in the history beside the files.
    test_comfy_engine.py runs each attack against ComfyUI's own code."""

    def test_value_wrapper_becomes_a_link(self):
        """validate_inputs takes x out of {"__value__": x} in place: a link inside one runs a node it never validated."""
        attack = g(
            save3d({"__value__": ["3", 0]}),
            load3d({"image": "", "mask": "", "normal": "", "recording": ""}),
            node("CLIPLoader", clip_name="not-in-the-list/anything.safetensors", type="no_such_type", device="nowhere"),
        )
        for staged in (None, ["fuobe_abcdefgh34.spz"]):
            for kinds in (None, KINDS):
                self.assertEqual(vet_graph(attack, SHIPPED, staged, kinds), "unsafe-graph")
        # At any depth, in any input, as a key of the inputs themselves.
        for wrapped in (
            {"camera_info": {"__value__": ["3", 0]}},
            {"camera_info": {"position": {"__value__": 1}}},
            {"model_3d_info": [{"__value__": ["3", 0]}]},
            {**FUOBE_VIEW, "__value__": ""},
        ):
            self.assertEqual(vet1("Save3DAdvanced", viewport_state=wrapped), "unsafe-graph", wrapped)
            self.assertEqual(vet1("Load3D", model_file="fuobe_abcdefgh34.spz", image=wrapped), "unsafe-graph", wrapped)
        for value in ({"__value__": "a red door"}, {"__value__": ["3", 0]}, [[{"__value__": ["3", 0]}]], ["x", {"a": {"b": {"c": {"__value__": 1}}}}]):
            self.assertEqual(vet1("CLIPTextEncode", text=value, clip=["8", 0]), "unsafe-graph", value)
        self.assertEqual(vet_graph(g({"class_type": "CLIPTextEncode", "inputs": {"text": "x", "__value__": ["3", 0]}}), SHIPPED, STAGED), "unsafe-graph")
        self.assertFalse(has_value_key({"a": [{"b": "__value__"}], "__values__": 1}))

    def test_lists_and_numbers(self):
        """ComfyUI reads a list as a link, so a list a node gets as written could only come through an input ComfyUI
        never validates (VHS_VideoCombine takes any name). NaN passes every min and max."""
        for bad in (["a", "b", "c"], [], ["x"], [1, 0], [["3", 0]], ["3", 0, 0], [["3", 0], 0]):
            self.assertEqual(vet1("CLIPTextEncode", text=bad, clip=["8", 0]), "unsafe-graph", bad)
        video = {"images": ["9", 0], "format": "video/h264-mp4", "frame_rate": 24, "filename_prefix": "fuobe/x"}
        self.assertEqual(vet1("VHS_VideoCombine", **video, pix_fmt=["yuv420p", "-i", "x"]), "unsafe-graph")
        for bad in (float("nan"), float("inf"), float("-inf")):
            self.assertEqual(vet1("KSampler", model=["8", 0], cfg=bad), "unsafe-graph", bad)
        for ok in (7, 7.5, -1, True, None, "x"):
            self.assertIsNone(vet1("KSampler", model=["8", 0], cfg=ok), ok)

    def test_node_keys_comfyui_reads(self):
        """A node holds class_type, inputs and its title. "is_changed" stands in for IS_CHANGED (the cache key of a
        node's output); Start Loop reads "_loop_body" and "_loop_end"."""
        save = node("SaveImage", images=["1", 0], filename_prefix="fuobe/x")
        empty = node("EmptyImage", width=8, height=8, batch_size=1, color=0)
        for ok in ({**save, "_meta": {"title": "Save Image"}}, {**save, "_meta": {"title": "x", "cnr_id": "comfy-core", "ver": "0.3.1"}}, {**save, "_meta": {}}):
            self.assertIsNone(vet_graph(chain(empty, ok), SHIPPED, STAGED), ok)
        loop = node("StartLoop", mode="simple", cache_iterations=False)
        for bad in (
            {**save, "is_changed": "x"},
            {**save, "is_changed": [1.0]},
            {**loop, "_loop_body": ["1"], "_loop_end": "3"},
            {**save, "_meta": "Save Image"},
            {**save, "_meta": {"title": ["x"]}},
            {**save, "_meta": {"title": {"__value__": ["1", 0]}}},
            {**save, "outputs": {}},
        ):
            self.assertEqual(vet_graph(chain(empty, bad), SHIPPED, STAGED), "unsafe-graph", bad)

    def test_3d_views_have_comfyuis_own_form(self):
        """The camera and the models' placements: numbers under ComfyUI's own names, nothing else. Save3DAdvanced shows
        both in the run's history, where a camera that named a file read as one of the run's results."""
        self.assertIsNone(vet1("Save3DAdvanced", viewport_state=FUOBE_VIEW))
        self.assertIsNone(vet1("Save3DAdvanced", viewport_state={}))
        self.assertIsNone(vet1("Save3DAdvanced", viewport_state={**FUOBE_VIEW, "camera_info": None}))
        placed = [{"position": {"x": 0, "y": 1, "z": 0}, "quaternion": {"x": 0, "y": 0, "z": 0, "w": 1}, "scale": {"x": 1, "y": 1, "z": 1}}]
        self.assertIsNone(vet1("Save3DAdvanced", viewport_state={**FUOBE_VIEW, "model_3d_info": placed}))
        self.assertIsNone(vet1("Save3DAdvanced", viewport_state={**FUOBE_VIEW, "camera_info": {**FUOBE_CAMERA, "useCustomUp": True, "cameraType": "orthographic"}}))
        any_angle = {"image": "fuobe_abcdefgh12.png", "mask": "fuobe_abcdefgh12.png", "normal": "fuobe_abcdefgh12.png", "recording": "", "camera_info": None}
        self.assertIsNone(vet1("Load3D", model_file="fuobe_abcdefgh34.spz", image=any_angle))
        self.assertIsNone(vet1("Load3D", model_file="fuobe_abcdefgh34.spz", image={**any_angle, "camera_info": FUOBE_CAMERA, "model_3d_info": placed}))
        for camera in (
            ECHOED_FILE,
            {**FUOBE_CAMERA, **ECHOED_FILE},
            {**FUOBE_CAMERA, "position": {"x": "10", "y": 10, "z": 10}},
            {**FUOBE_CAMERA, "position": {"x": 1, "y": 2, "z": 3, "w": 4}},
            {**FUOBE_CAMERA, "position": ECHOED_FILE},
            {**FUOBE_CAMERA, "cameraType": "fisheye"},
            {**FUOBE_CAMERA, "zoom": True},
            {**FUOBE_CAMERA, "zoom": float("nan")},
            {**FUOBE_CAMERA, "near": [0.01]},
            {**FUOBE_CAMERA, "useCustomUp": 1},
            {**FUOBE_CAMERA, "up": {"x": 0, "y": 1, "z": 0}},
            ["3", 0],
            "perspective",
        ):
            for view in ({**FUOBE_VIEW, "camera_info": camera}, {**any_angle, "camera_info": camera}):
                self.assertEqual(vet_graph(g(save3d(view) if "mask" in view and view["mask"] == "" else load3d(view)), SHIPPED, STAGED + ["fuobe_abcdefgh34.spz"]), "unsafe-graph", camera)
        for placements in ([ECHOED_FILE], [{**placed[0], "filename": "x.png"}], [{"position": {"x": 1}, "name": "chair"}], [[0, 1, 0]], ["x"], {}, "x", [{"scale": {"x": 1, "y": 1, "z": 1, "w": 1}}], placed * 65):
            self.assertEqual(vet1("Save3DAdvanced", viewport_state={**FUOBE_VIEW, "model_3d_info": placements}), "unsafe-graph", placements)
            self.assertEqual(vet1("Load3D", model_file="fuobe_abcdefgh34.spz", image={**any_angle, "model_3d_info": placements}), "unsafe-graph", placements)
        # Save3DAdvanced reads no picture entry; Load3D's are texts (each one of this run's files, checked as such).
        for view in ({**FUOBE_VIEW, "image": "fuobe_abcdefgh12.png"}, {**FUOBE_VIEW, "recording": None}, {**FUOBE_VIEW, "files": []}, {**FUOBE_VIEW, "camera": {}}):
            self.assertEqual(vet1("Save3DAdvanced", viewport_state=view), "unsafe-graph", view)
        for view in ({**any_angle, "image": 5}, {**any_angle, "image": None}, {**any_angle, "image": {"image": "fuobe_abcdefgh12.png"}}, {**any_angle, "extra": ""}, {**any_angle, "image": "secret.png"}):
            self.assertEqual(vet1("Load3D", model_file="fuobe_abcdefgh34.spz", image=view), "unsafe-graph", view)

    def test_history_data_is_never_a_file(self):
        """What a 3D node shows beside its files (the camera, the placements) never counts as one of the run's files."""
        entry = {
            "outputs": {
                "7": {
                    "3d": [{"filename": "abcdef12_00001_.glb", "subfolder": "fuobe", "type": "output"}],
                    "camera_info": [ECHOED_FILE],
                    "model_3d_info": [{**ECHOED_FILE, "filename": "secret.png"}],
                }
            }
        }
        self.assertEqual([f["filename"] for f in files_in([entry])], ["abcdef12_00001_.glb"])
        self.assertFalse(may_download(ECHOED_FILE, [entry]))
        self.assertTrue(may_download({"filename": "abcdef12_00001_.glb", "subfolder": "fuobe", "type": "output"}, [entry]))

    def test_links_only_into_inputs_comfyui_validates(self):
        """ComfyUI's scheduler follows a link in any input; validate_inputs goes through only the required and optional
        ones. A link anywhere else runs a node that nothing validated (its dropdowns, its numbers, its own links)."""
        kinds = comfy_kinds(lambda: ENGINE)
        loader = node("CLIPLoader", clip_name="not-in-the-list.safetensors", type="no_such_type")
        empty = node("EmptyImage", width=8, height=8, batch_size=1, color=0)
        for extra in ({"zzz": ["1", 0]}, {"prompt": ["1", 0]}, {"extra_pnginfo": ["1", 0]}):
            attack = chain(loader, empty, node("SaveImage", images=["2", 0], filename_prefix="fuobe/x", **extra))
            self.assertEqual(vet_graph(attack, SHIPPED, STAGED, kinds), "unsafe-graph", extra)
            # Without the node classes this can't be seen; inside ComfyUI they are always there.
            self.assertIsNone(vet_graph(attack, SHIPPED, STAGED), extra)
        # A hidden input as written goes to the node unchecked.
        self.assertEqual(vet_graph(chain(empty, node("SaveImage", images=["1", 0], filename_prefix="fuobe/x", prompt="x")), SHIPPED, STAGED, kinds), "unsafe-graph")
        # A text in an input the node doesn't list is dropped by ComfyUI: fine. Links into listed inputs: fine.
        self.assertIsNone(vet_graph(chain(empty, node("SaveImage", images=["1", 0], filename_prefix="fuobe/x", note="hi")), SHIPPED, STAGED, kinds))
        self.assertIsNone(vet_graph(chain(node("CLIPLoader", clip_name="a.safetensors", type="sd3", device=["9", 0]), empty), SHIPPED, STAGED, kinds))
        # A pack's inputs that claim every name: validate_inputs still goes through only its own.
        video = {"images": ["1", 0], "format": "video/h264-mp4", "frame_rate": 24, "filename_prefix": "fuobe/x"}
        self.assertIsNone(vet_graph(chain(empty, node("VHS_VideoCombine", **video, audio=["1", 0])), SHIPPED, STAGED, kinds))
        self.assertEqual(vet_graph(chain(empty, node("VHS_VideoCombine", **video, vae=["1", 0])), SHIPPED, STAGED, kinds), "unsafe-graph")
        # A dynamic node: only the inputs ComfyUI works out from its values.
        dyn = spec_kinds({**LOOPS, "BatchImagesNode": ({"required": {"images": ("COMFY_AUTOGROW_V3", {})}}, ("IMAGE",), {"dynamic": True})})
        batch = lambda **i: chain(node("EmptyImage", width=8, height=8, batch_size=1, color=0), node("BatchImagesNode", **i), node("SaveImage", images=["2", 0], filename_prefix="fuobe/x"))  # noqa: E731
        self.assertIsNone(vet_graph(batch(**{"images.image0": ["1", 0], "images.image1": ["1", 0]}), SHIPPED, STAGED, dyn))
        self.assertEqual(vet_graph(batch(**{"images.image0": ["1", 0], "pictures.image1": ["1", 0]}), SHIPPED, STAGED, dyn), "unsafe-graph")
        # A class that can't say what it validates: refused (ComfyUI can't validate it either).
        broken = NodeKinds(lambda cls: (_ for _ in ()).throw(RuntimeError("no")))
        self.assertEqual(vet_graph(chain(empty), SHIPPED, STAGED, broken), "unsafe-graph")
        failing = NodeKinds(lambda cls: ({"required": {}}, ()), lambda cls, inputs: (_ for _ in ()).throw(RuntimeError("no")))
        self.assertIsNone(vet_graph(chain(empty), SHIPPED, STAGED, failing))  # not dynamic: its listed inputs
        failing_dyn = NodeKinds(lambda cls: ({"required": {}}, (), {"dynamic": True}), lambda cls, inputs: (_ for _ in ()).throw(RuntimeError("no")))
        self.assertEqual(vet_graph(chain(empty), SHIPPED, STAGED, failing_dyn), "unsafe-graph")

    def test_node_types_comfyui_swaps(self):
        """ComfyUI swaps a missing node type for another (its node replacements) after this check, with other inputs."""
        graph = chain(node("EmptyImage", width=8, height=8, batch_size=1, color=0), node("OldLoader", path="photo.png"), node("SaveImage", images=["2", 0], filename_prefix="fuobe/x"))
        allowed = SHIPPED | {"OldLoader"}
        swaps = comfy_kinds(lambda: ENGINE, lambda: FakeReplacements({"OldLoader"}))
        self.assertEqual(vet_graph(graph, allowed, STAGED, swaps), "unknown-node")
        # A missing node type ComfyUI doesn't swap: ComfyUI refuses the whole graph itself (before it validates or runs
        # any node), and says which type is missing.
        self.assertIsNone(vet_graph(graph, allowed, STAGED, comfy_kinds(lambda: ENGINE, lambda: FakeReplacements(set()))))
        self.assertIsNone(vet_graph(graph, allowed, STAGED, comfy_kinds(lambda: ENGINE, lambda: None)))
        unlisted = {**graph, "3": node("SaveImage", images=["1", 0], filename_prefix="fuobe/x", zzz=["2", 0])}
        self.assertIsNone(vet_graph(unlisted, allowed, STAGED, comfy_kinds(lambda: ENGINE, lambda: None)))
        self.assertEqual(vet_graph({k: v for k, v in unlisted.items() if k != "2"}, allowed, STAGED, comfy_kinds(lambda: ENGINE, lambda: None)), "unsafe-graph")
        # When the replacements can't be read: refused.
        self.assertEqual(vet_graph(graph, allowed, STAGED, comfy_kinds(lambda: ENGINE, lambda: (_ for _ in ()).throw(RuntimeError("no")))), "unknown-node")

    def test_loops_validate_every_node(self):
        """validate_loops pairs an End Loop downstream of an output with its Start Loop, and Start Loop's expansion runs
        the body and the End Loop: nodes validate_inputs never reached. A graph with a loop has only validated nodes."""
        kinds = spec_kinds(LOOPS)
        start = node("StartLoop", mode="simple", **{"mode.num_iterations": 2}, cache_iterations=False)
        model = load3d({"image": "", "mask": "", "normal": "", "recording": ""})
        # 1 Start Loop -> 2 Save3DAdvanced (an output, with outputs) -> 4 End Loop; 1 -> 3 (never validated) -> 4.
        attack = {
            "1": start,
            "2": node("Save3DAdvanced", model_3d=["5", 6], viewport_state={}, width=["1", 0], height=512, filename_prefix="fuobe/abcdef12"),
            "3": node("PrimitiveInt", value=["1", 0]),
            "4": node("EndLoop", accumulate=False, **{"terminations.termination0": ["2", 3]}, next_iteration_value=["3", 0]),
            "5": model,
        }
        self.assertEqual(vet_graph(attack, SHIPPED, ["fuobe_abcdefgh34.spz"], kinds), "unsafe-graph")
        # Fuobe's form: everything feeds the output.
        ok = {
            "1": start,
            "2": node("EmptyImage", width=8, height=8, batch_size=1, color=["1", 0]),
            "3": node("EndLoop", accumulate=True, output_value=["2", 0]),
            "4": node("RebatchImages", images=["3", 0], batch_size=64),
            "5": node("SaveImage", images=["4", 0], filename_prefix="fuobe/x"),
        }
        self.assertIsNone(vet_graph(ok, SHIPPED, STAGED, kinds))
        # A node no output needs: ComfyUI never runs it in a graph without a loop; with one, refused.
        spare = node("PrimitiveInt", value=5)
        self.assertIsNone(vet_graph(chain(node("EmptyImage", width=8, height=8, batch_size=1, color=0), node("SaveImage", images=["1", 0], filename_prefix="fuobe/x"), spare), SHIPPED, STAGED, kinds))
        self.assertEqual(vet_graph({**ok, "6": spare}, SHIPPED, STAGED, kinds), "unsafe-graph")


class NodeListTest(unittest.TestCase):
    def test_shipped_and_own(self):
        shipped = shipped_nodes()
        self.assertIn("LoadImage", shipped)
        self.assertIn("SaveImage", shipped)
        self.assertNotIn("LoadImagesFromFolderKJ", shipped)
        with tempfile.TemporaryDirectory() as d:
            nodes = NodeList(d)
            self.assertEqual(nodes(), shipped)
            with open(os.path.join(d, OWN_NODES_FILE), "w", encoding="utf-8") as f:
                json.dump({"nodes": ["MyOwnNode"]}, f)
            self.assertIn("MyOwnNode", nodes())
            os.remove(os.path.join(d, OWN_NODES_FILE))
            self.assertNotIn("MyOwnNode", nodes())


class ResultsTest(unittest.TestCase):
    def test_wanted(self):
        self.assertTrue(safe_wanted({"filename": "a.png", "subfolder": "fuobe", "type": "output"}))
        for w in [
            {"filename": "a.png", "subfolder": "", "type": "input"},
            {"filename": "../a.png", "subfolder": "", "type": "output"},
            {"filename": "a.png [input]", "subfolder": "", "type": "output"},
            {"filename": "blake3:abc", "subfolder": "", "type": "output"},
            {"filename": "a.png", "subfolder": "..", "type": "output"},
            {"filename": "a.png", "subfolder": "/etc", "type": "output"},
            {"filename": "a.png", "subfolder": "C:/x", "type": "output"},
        ]:
            self.assertFalse(safe_wanted(w), w)
        entries = [{"outputs": {"9": {"images": [{"filename": "a.png", "subfolder": "fuobe", "type": "output"}]}}}]
        self.assertTrue(may_download({"filename": "a.png", "subfolder": "fuobe", "type": "output"}, entries))
        self.assertFalse(may_download({"filename": "b.png", "subfolder": "fuobe", "type": "output"}, entries))

    def test_prefix(self):
        graph = g(node("SaveImage", filename_prefix="fuobe/gen123abc"))
        self.assertEqual(run_prefix("fuobe/gen123abc", graph), "fuobe/gen123abc")
        # The person's own prefix, or one this run's graph doesn't save under: none.
        self.assertIsNone(run_prefix("ComfyUI", g(node("SaveImage", filename_prefix="ComfyUI"))))
        self.assertIsNone(run_prefix("fuobe/other12345", graph))
        self.assertIsNone(run_prefix("fuobe/../x", graph))

    def test_slim_drops_traceback_and_folders(self):
        e = {
            "prompt": [1, "p", {"big": "graph"}, {}, []],
            "outputs": {"9": {"images": []}},
            "status": {"status_str": "error", "completed": False, "messages": [["execution_error", {"node_id": "4", "node_type": "X", "exception_message": "No file C:" + B + "Users" + B + "alice" + B + "ComfyUI" + B + "x.png", "traceback": ["File C:/ComfyUI/nodes.py"]}]]},
        }
        s = slim(e)
        self.assertEqual(s["prompt"], [1, "p"])
        msg = s["status"]["messages"][0][1]
        self.assertNotIn("traceback", msg)
        self.assertNotIn("alice", msg["exception_message"])
        self.assertEqual(msg["node_type"], "X")

    def test_refusal_keeps_what_the_studio_needs(self):
        answer = {
            "error": {"type": "prompt_outputs_failed_validation", "message": "Prompt outputs failed validation", "details": "x", "extra_info": {}},
            "node_errors": {
                "4": {
                    "class_type": "CheckpointLoaderSimple",
                    "dependent_outputs": ["9"],
                    "errors": [
                        {
                            "type": "value_not_in_list",
                            "message": "Value not in list",
                            "details": "ckpt_name: 'flux.safetensors' not in ['my_private_model.safetensors', 'another.ckpt']",
                            "extra_info": {"input_name": "ckpt_name", "received_value": "flux.safetensors", "input_config": [["my_private_model.safetensors"]]},
                        }
                    ],
                }
            },
        }
        r = refusal(answer, {"flux.safetensors"})
        text = json.dumps(r)
        self.assertNotIn("my_private_model", text)
        self.assertNotIn("another.ckpt", text)
        self.assertEqual(r["node_errors"]["4"]["class_type"], "CheckpointLoaderSimple")
        err = r["node_errors"]["4"]["errors"][0]
        self.assertIn("not in list", err["message"])
        self.assertEqual(err["extra_info"], {"input_name": "ckpt_name", "received_value": "flux.safetensors"})
        # A value the graph didn't send isn't echoed.
        self.assertNotIn("received_value", refusal(answer, {"other"})["node_errors"]["4"]["errors"][0]["extra_info"])
        missing = refusal({"error": {"type": "missing_node_type", "message": "Node 'Foo' not found. The custom node may not be installed.", "extra_info": {"class_type": "Foo", "node_id": "3"}}})
        self.assertEqual(missing["error"]["extra_info"], {"class_type": "Foo"})


class PackageTest(unittest.TestCase):
    def test_one_version(self):
        with open(os.path.join(PACK, "pyproject.toml"), encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(re.search(r'^version = "([^"]+)"', text, re.M).group(1), __version__)

    def test_registry_metadata(self):
        with open(os.path.join(PACK, "pyproject.toml"), encoding="utf-8") as f:
            text = f.read()
        name = re.search(r'^name = "([^"]+)"', text, re.M).group(1)
        self.assertNotIn("comfyui", name.lower())
        self.assertNotRegex(text, r'Icon = ""')
        self.assertIn("requires-python", text)
        self.assertIn("requires-comfyui", text)
        with open(os.path.join(PACK, ".comfyignore"), encoding="utf-8") as f:
            ignored = f.read().split()
        self.assertIn("tests/", ignored)
        self.assertIn("assets/", ignored)

    def test_registry_icon(self):
        """The Registry shows the icon from this repository: a square PNG of at most 400 x 400."""
        with open(os.path.join(PACK, "pyproject.toml"), encoding="utf-8") as f:
            url = re.search(r'^Icon = "([^"]+)"', f.read(), re.M).group(1)
        prefix = "https://raw.githubusercontent.com/workingprototype/ComfyUI-Fuobe-Connect/main/"
        self.assertTrue(url.startswith(prefix), url)
        with open(os.path.join(PACK, *url[len(prefix):].split("/")), "rb") as f:
            head = f.read(24)
        self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", head[16:24]), (400, 400))


if __name__ == "__main__":
    unittest.main()
