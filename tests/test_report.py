import unittest

from tests import _paths  # noqa: F401  (sys.path 設定)
from unitypackage_loader.core.report import ImportReport, MaterialReport, describe_exception, sanitize_display


class SanitizeTests(unittest.TestCase):
    def test_control_characters_become_visible(self):
        self.assertEqual(sanitize_display("a\x1b[31mred\x1b[0m"), "a\\x1b[31mred\\x1b[0m")
        self.assertEqual(sanitize_display("x\x00y\x7fz"), "x\\x00y\\x7fz")
        self.assertEqual(sanitize_display("line\nbreak\ttab\r"), "line\\x0abreak\\x09tab\\x0d")
        # C1 制御文字（8 ビットの CSI など）も対象
        self.assertEqual(sanitize_display("a\x9b31mb"), "a\\x9b31mb")

    def test_plain_text_is_unchanged(self):
        for text in ("Assets/Models/Body.fbx", "日本語 名前", "emoji 🙂", "", "quote's \"double\" · dot"):
            self.assertEqual(sanitize_display(text), text)


class AsTextTests(unittest.TestCase):
    def test_as_text_has_no_control_characters(self):
        report = ImportReport(package="evil\x1b[2J.unitypackage")
        report.models.append("Assets/\x1b]0;title\x07Model.fbx")
        report.objects.append("Obj\nNewline")
        report.materials.append(
            MaterialReport(blender_name="Mat\x1b[1m", fbx_name="fbx\x00", guid=None, method="none",
                           textures=["tex\x1b"], warnings=["warn\x9b"])
        )
        report.images.append("img\x7f")
        report.warn("warning \x1b[31m")
        report.errors.append("error \x1b")
        text = report.as_text()
        for c in text:
            self.assertFalse((ord(c) < 0x20 and c != "\n") or 0x7F <= ord(c) <= 0x9F, repr(text))
        self.assertIn("evil\\x1b[2J.unitypackage", text)
        self.assertIn("Obj\\x0aNewline", text)
        # 名前に含まれる改行で行が増えない: header, extract root, models(1+1), objects(1+1),
        # materials(1+1+tex+warn), images(1+1), warnings(1+1), errors(1+1)
        self.assertEqual(len(text.splitlines()), 2 + 2 + 2 + 4 + 2 + 2 + 2)



class DescribeExceptionTests(unittest.TestCase):
    def test_os_error_includes_filename(self):
        exc = FileNotFoundError(2, "The filename or extension is too long", "C:\\cache\\Assets\\Base.png")
        self.assertEqual(
            describe_exception(exc),
            "FileNotFoundError: The filename or extension is too long: C:\\cache\\Assets\\Base.png",
        )

    def test_other_exceptions_use_repr(self):
        self.assertEqual(describe_exception(ValueError("bad")), "ValueError('bad')")
        self.assertEqual(describe_exception(OSError("no file name")), "OSError('no file name')")


if __name__ == "__main__":
    unittest.main()
