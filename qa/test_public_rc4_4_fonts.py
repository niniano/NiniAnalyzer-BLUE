"""RC4.4: never register a bundled font privately when the user installed it.

Installed + FR_PRIVATE copy of the same family made GDI measure and draw Hangul
titles with different faces (clipped / overlapping / wide gaps).
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import ui_theme  # noqa: E402

PY = "PyeojinGothic-Medium.ttf"
PL = "Paperlogy-6SemiBold.ttf"


class _FakeAdd:
    def __init__(self):
        self.calls = []
        self.argtypes = None
        self.restype = None

    def __call__(self, path, flags, _res):
        self.calls.append((Path(path).name, flags))
        return 1


def _run(entries):
    add = _FakeAdd()
    gdi = types.SimpleNamespace(AddFontResourceExW=add)
    fake_ctypes = types.SimpleNamespace(WinDLL=lambda *a, **k: gdi, c_wchar_p=object,
                                        c_uint=object, c_void_p=object, c_int=object)
    with mock.patch.object(ui_theme, "_registered", False), \
         mock.patch.object(ui_theme, "_fonts_loaded", False), \
         mock.patch.object(ui_theme.sys, "platform", "win32"), \
         mock.patch.object(ui_theme, "_installed_font_entries", lambda: list(entries)), \
         mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}):
        ok = ui_theme.register_bundled_fonts()
        loaded = ui_theme._fonts_loaded
    return ok, loaded, add.calls


class InstalledCopyTests(unittest.TestCase):
    def test_registry_name_and_path_forms(self):
        f = ui_theme._installed_copy
        self.assertTrue(f(PL, [("페이퍼로지 6 SemiBold (TrueType)", "Paperlogy-6SemiBold.ttf")]))
        self.assertTrue(f(PL, [("Paperlogy 6 Semi Bold (TrueType)", r"C:\Users\a\AppData\Local\Microsoft\Windows\Fonts\x.ttf")]))
        self.assertTrue(f(PY, [("Whatever (TrueType)", r"C:\Users\a\AppData\Local\Microsoft\Windows\Fonts\PyeojinGothic-Medium.ttf")]))
        self.assertTrue(f(PY, [("펴진고딕 Medium (TrueType)", "abc.ttf")]))
        self.assertTrue(f(PY, [("Pyeojin Gothic Medium (TrueType)", "C:/Fonts/pyeojingothic-medium.TTF")]))

    def test_other_weight_or_family_is_not_a_copy(self):
        f = ui_theme._installed_copy
        self.assertFalse(f(PL, [("Paperlogy 4 Regular (TrueType)", "Paperlogy-4Regular.ttf")]))
        self.assertFalse(f(PY, [("Pyeojin Gothic Bold (TrueType)", "PyeojinGothic-Bold.ttf")]))
        self.assertFalse(f(PL, [("맑은 고딕 (TrueType)", "malgun.ttf")]))
        self.assertFalse(f(PY, []))


class RegisterTests(unittest.TestCase):
    def test_nothing_installed_registers_both_privately(self):
        ok, loaded, calls = _run([("Arial (TrueType)", "arial.ttf")])
        self.assertTrue(ok and loaded)
        self.assertEqual(sorted(calls), sorted([(PY, 0x10), (PL, 0x10)]))

    def test_both_installed_registers_nothing(self):
        ok, loaded, calls = _run([("Pyeojin Gothic Medium (TrueType)", "PyeojinGothic-Medium.ttf"),
                                  ("페이퍼로지 6 SemiBold (TrueType)", "Paperlogy-6SemiBold.ttf")])
        self.assertTrue(ok and loaded)
        self.assertEqual(calls, [])

    def test_only_missing_one_is_registered(self):
        ok, loaded, calls = _run([("페이퍼로지 6 SemiBold (TrueType)", "Paperlogy-6SemiBold.ttf")])
        self.assertTrue(ok and loaded)
        self.assertEqual(calls, [(PY, 0x10)])

    def test_registry_read_failure_falls_back_to_private_load(self):
        def boom():
            raise OSError("no registry")
        add = _FakeAdd()
        gdi = types.SimpleNamespace(AddFontResourceExW=add)
        fake_ctypes = types.SimpleNamespace(WinDLL=lambda *a, **k: gdi, c_wchar_p=object,
                                            c_uint=object, c_void_p=object, c_int=object)
        with mock.patch.object(ui_theme, "_registered", False), \
             mock.patch.object(ui_theme, "_fonts_loaded", False), \
             mock.patch.object(ui_theme.sys, "platform", "win32"), \
             mock.patch.object(ui_theme, "_installed_font_entries", boom), \
             mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            self.assertTrue(ui_theme.register_bundled_fonts())
        self.assertEqual(len(add.calls), 2)

    def test_non_windows_is_untouched(self):
        with mock.patch.object(ui_theme, "_registered", False), \
             mock.patch.object(ui_theme.sys, "platform", "linux"), \
             mock.patch.object(ui_theme, "_installed_font_entries", side_effect=AssertionError):
            self.assertTrue(ui_theme.register_bundled_fonts())


if __name__ == "__main__":
    unittest.main()
