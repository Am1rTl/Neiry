"""Keep the offline preview's scripts in the order declared by the live page."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.build_preview import build


class BuildPreviewTests(unittest.TestCase):
    def test_embeds_local_assets_in_template_order(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            static = root / "static"
            (static / "vendor").mkdir(parents=True)
            (root / "reports").mkdir()
            (root / "reports/session_example.json").write_text(
                '{"session":{"id":"session_example"}}', encoding="utf-8"
            )
            assets = {
                "lab.html": (
                    '<head><link rel="stylesheet" href="/static/extra.css">'
                    '<script defer src="/static/vendor/example.js"></script>'
                    '<script defer src="/static/extra.js"></script></head><body></body>'
                ),
                "knowledge.json": "{}",
                "extra.css": ".example { display: block; }",
                "vendor/example.js": 'window.example = "</script>";',
                "extra.js": "window.usesExample = window.example;",
            }
            for name, content in assets.items():
                (static / name).write_text(content, encoding="utf-8")

            page = build(root).read_text(encoding="utf-8")
            self.assertNotIn("/static/", page)
            self.assertIn("<style>.example { display: block; }</style>", page)
            self.assertLess(page.index("window.NEUROSCOPE_PREVIEW"), page.index("window.example ="))
            self.assertLess(page.index("window.example ="), page.index("window.usesExample"))
            self.assertIn(r'window.example = "<\/script>";', page)
