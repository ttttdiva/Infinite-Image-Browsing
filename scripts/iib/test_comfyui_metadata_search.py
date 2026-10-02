"""Focused metadata-search tests for the private ComfyUI parser extensions.

Run from the repository root:

    python -m unittest scripts.iib.test_comfyui_metadata_search
"""

import io
import json
import os
import sys
import tempfile
import threading
import unittest

from PIL import Image
from PIL.PngImagePlugin import PngInfo

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from scripts.iib.db.datamodel import DataBase
from scripts.iib.db.update_image_data import update_image_data
from scripts.iib.parsers.comfyui import (
    _extract_comfyui_loras,
    _resolve_model_name,
    get_comfyui_exif_data,
)


def _graph():
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "sdxl-base.safetensors"},
        },
        "2": {
            "class_type": "LoraLoader",
            "inputs": {
                "model": ["1", 0],
                "clip": ["1", 1],
                "lora_name": "characters\\hero_style.safetensors",
                "strength_model": 0.8,
                "strength_clip": 0.7,
            },
        },
        "3": {
            "class_type": "Power Lora Loader (rgthree)",
            "inputs": {
                "model": ["2", 0],
                "clip": ["2", 1],
                "lora_1": {
                    "on": True,
                    "lora": "styles/lighting.safetensors",
                    "strength": 0.55,
                },
                "lora_2": {
                    "on": False,
                    "lora": "disabled.safetensors",
                    "strength": 1.0,
                },
            },
        },
        "4": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "1girl, pink hair, outdoors"},
        },
        "5": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "low quality"},
        },
        "6": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": 1024, "height": 1024},
        },
        "7": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["3", 0],
                "positive": ["4", 0],
                "negative": ["5", 0],
                "latent_image": ["6", 0],
                "seed": 123,
                "steps": 28,
                "cfg": 5.0,
                "sampler_name": "euler_ancestral",
                "scheduler": "normal",
                "denoise": 1.0,
            },
        },
    }


class ComfyMetadataSearchTest(unittest.TestCase):
    def test_model_resolution_walks_through_lora_chain(self):
        data = _graph()
        self.assertEqual(
            _resolve_model_name(data, data["7"]["inputs"]["model"]),
            "sdxl-base.safetensors",
        )

    def test_lora_extraction_supports_core_and_nested_custom_loader(self):
        loras = _extract_comfyui_loras(_graph())
        by_name = {item["name"]: item["value"] for item in loras}
        self.assertEqual(
            set(by_name),
            {
                "characters/hero_style.safetensors",
                "styles/lighting.safetensors",
            },
        )
        self.assertAlmostEqual(by_name["characters/hero_style.safetensors"], 0.8)
        self.assertAlmostEqual(by_name["styles/lighting.safetensors"], 0.55)
        self.assertNotIn("disabled.safetensors", by_name)

    def test_generation_info_exposes_model_prompt_and_loras_for_indexing(self):
        graph = _graph()
        pnginfo = PngInfo()
        pnginfo.add_text("prompt", json.dumps(graph))
        pnginfo.add_text("workflow", json.dumps({"nodes": []}))
        payload = io.BytesIO()
        Image.new("RGB", (64, 64)).save(payload, format="PNG", pnginfo=pnginfo)
        payload.seek(0)
        with Image.open(payload) as image:
            parsed = get_comfyui_exif_data(image)

        self.assertEqual(parsed["meta"]["Source Identifier"], "ComfyUI")
        self.assertEqual(parsed["meta"]["Model"], "sdxl-base.safetensors")
        self.assertIn("1girl", parsed["pos_prompt"])
        self.assertIn("pink hair", parsed["pos_prompt"])
        self.assertEqual(
            {item["name"] for item in parsed["lora"]},
            {
                "characters/hero_style.safetensors",
                "styles/lighting.safetensors",
            },
        )


class MetadataSearchIndexIntegrationTest(unittest.TestCase):
    def test_index_creates_source_model_lora_and_prompt_tags(self):
        previous_path = DataBase.path
        previous_local = DataBase.local
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.abspath(tmp)
            db_path = os.path.join(root, "test-iib.db")
            image_path = os.path.join(root, "sample.png")
            graph = _graph()
            pnginfo = PngInfo()
            pnginfo.add_text("prompt", json.dumps(graph))
            pnginfo.add_text("workflow", json.dumps({"nodes": []}))
            Image.new("RGB", (64, 64)).save(image_path, format="PNG", pnginfo=pnginfo)

            try:
                DataBase.path = db_path
                DataBase.local = threading.local()
                update_image_data([root])
                conn = DataBase.get_conn()
                tags = {
                    (str(row[0]), str(row[1]))
                    for row in conn.execute("SELECT type, name FROM tag").fetchall()
                }
            finally:
                try:
                    if hasattr(DataBase.local, "conn"):
                        DataBase.local.conn.close()
                except Exception:
                    pass
                DataBase.path = previous_path
                DataBase.local = previous_local

        self.assertIn(("Source Identifier", "ComfyUI"), tags)
        self.assertIn(("Model", "sdxl-base.safetensors"), tags)
        self.assertIn(("lora", "characters/hero_style.safetensors"), tags)
        self.assertIn(("lora", "styles/lighting.safetensors"), tags)
        self.assertIn(("pos", "1girl"), tags)
        self.assertIn(("pos", "pink hair"), tags)


if __name__ == "__main__":
    unittest.main()
