"""tools/build_site.py の GTM 埋め込みのテスト（bpy 不要）。"""

from __future__ import annotations

import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "build_site", Path(__file__).resolve().parent.parent / "tools" / "build_site.py"
)
build_site = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build_site)

PAGE = """<head>
<meta charset="utf-8">
{{GTM_HEAD}}
</head>
<body>
{{GTM_BODY}}
<p>{{VERSION}}</p>
<footer>
  <!-- gtm-only --><p id="privacy">cookie notice</p><!-- /gtm-only -->
  <p>always</p>
</footer>
</body>
"""


class ResolveGtmIdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.env_file = self.dir / ".env"

    def tearDown(self):
        self.tmp.cleanup()

    def test_absent_everywhere(self):
        self.assertEqual(build_site.resolve_gtm_id({}, self.env_file), "")

    def test_from_environment(self):
        self.assertEqual(build_site.resolve_gtm_id({"GTM_ID": "GTM-AB12CD3"}, self.env_file), "GTM-AB12CD3")

    def test_from_env_file_with_quotes_and_comments(self):
        self.env_file.write_text('# local settings\nexport GTM_ID="GTM-XYZ9876"\nOTHER=1\n', "utf-8")
        self.assertEqual(build_site.resolve_gtm_id({}, self.env_file), "GTM-XYZ9876")

    def test_environment_wins_over_env_file(self):
        self.env_file.write_text("GTM_ID=GTM-FILE0001\n", "utf-8")
        self.assertEqual(build_site.resolve_gtm_id({"GTM_ID": "GTM-ENV00001"}, self.env_file), "GTM-ENV00001")

    def test_empty_environment_falls_back_to_env_file(self):
        # GitHub Actions は未設定の vars を空文字で渡す
        self.env_file.write_text("GTM_ID=GTM-FILE0001\n", "utf-8")
        self.assertEqual(build_site.resolve_gtm_id({"GTM_ID": ""}, self.env_file), "GTM-FILE0001")

    def test_invalid_format_is_ignored(self):
        for bad in ("UA-12345", "GTM-abc1234", "GTM-ABCD'); alert(1)//", "GTM-"):
            with self.subTest(bad=bad), redirect_stderr(io.StringIO()) as err:
                self.assertEqual(build_site.resolve_gtm_id({"GTM_ID": bad}, self.env_file), "")
                self.assertIn("ignoring GTM_ID", err.getvalue())


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.web = root / "web"
        self.site = root / "site"
        self.web.mkdir()
        (self.web / "index.html").write_text(PAGE, "utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _built(self, gtm_id: str) -> str:
        with redirect_stderr(io.StringIO()) as err:
            build_site.build(self.site, "https://example.test/repo", self.web, gtm_id)
        self.assertNotIn("unreplaced", err.getvalue())
        return (self.site / "index.html").read_text("utf-8")

    def test_without_gtm_placeholders_become_empty(self):
        html = self._built("")
        self.assertNotIn("googletagmanager", html)
        self.assertNotIn("{{", html)

    def test_without_gtm_notice_block_is_removed(self):
        html = self._built("")
        self.assertNotIn("cookie notice", html)
        self.assertNotIn("gtm-only", html)
        self.assertIn("<footer>\n  <p>always</p>", html)

    def test_with_gtm_head_and_body_are_embedded(self):
        html = self._built("GTM-AB12CD3")
        head, body = html.split("<body>", 1)
        self.assertIn("gtm.js?id='+i+dl", head)
        self.assertIn("'GTM-AB12CD3'", head)
        self.assertIn("ns.html?id=GTM-AB12CD3", body)
        self.assertNotIn("{{", html)

    def test_with_gtm_notice_is_kept_without_markers(self):
        html = self._built("GTM-AB12CD3")
        self.assertIn('<p id="privacy">cookie notice</p>', html)
        self.assertNotIn("gtm-only", html)


if __name__ == "__main__":
    unittest.main()
