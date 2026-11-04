"""已选启阙图形、主题资源和发布图标使用同一品牌资产。"""

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

from scripts.build_windows_app import _make_icon
from scripts.create_brand_assets import ICO_SIZES, INK, WHITE, ORANGE, mark_image


ASSETS = Path(__file__).resolve().parents[1] / "assets"


class BrandAssetTests(unittest.TestCase):
    def test_theme_logos_keep_geometry_and_orange_cursor(self):
        for name, color, expected in (("light", INK, (32, 35, 40)), ("dark", WHITE, (240, 241, 244))):
            with self.subTest(theme=name), Image.open(ASSETS / f"changan-logo-{name}.png") as image:
                self.assertEqual(image.mode, "RGBA")
                self.assertEqual(image.size, (512, 512))
                self.assertEqual(image.tobytes(), mark_image(color).tobytes())
                self.assertEqual(image.getpixel((112, 256))[:3], expected)
                self.assertEqual(image.getpixel((420, 82))[:3], (230, 103, 54))
                self.assertEqual(image.getpixel((0, 0))[3], 0)

    def test_svg_and_multisize_windows_icon_are_valid(self):
        for name in ("changan-logo.svg", "changan-app.svg"):
            root = ET.parse(ASSETS / name).getroot()
            self.assertEqual(root.attrib["viewBox"], "0 0 100 100")
            self.assertIn(ORANGE, (ASSETS / name).read_text(encoding="utf-8"))
        with Image.open(ASSETS / "changan.ico") as icon:
            self.assertEqual(icon.ico.sizes(), {(n, n) for n in ICO_SIZES})
            for size in ICO_SIZES:
                self.assertIsNotNone(icon.ico.getimage((size, size)).getbbox())

    def test_build_copies_the_approved_icon_without_recreating_old_brand(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "app.ico"
            _make_icon(target)
            self.assertEqual(target.read_bytes(), (ASSETS / "changan.ico").read_bytes())
