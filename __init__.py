"""Fuobe Connect, a ComfyUI custom node pack.

It adds no nodes. It lets a Fuobe studio (https://fuobe.com, or your own) use this ComfyUI from your browser, with no
start flag and no extra file, and it keeps ComfyUI's own check against every other website. For browsers that can't
reach this computer (Safari, phones) and PCs with no screen, it can pair this ComfyUI with your Fuobe account instead.
See README.md.
"""

import logging

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = "./web"

try:
    from .fuobe_connect.plugin import install

    install()
except Exception as e:  # never stop ComfyUI from starting
    logging.warning("[Fuobe Connect] Couldn't start: %s", e)

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
