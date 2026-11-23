"""BLUE 1.0.20: 구글 캘린더 토큰 DPAPI 암호화 XML (Export-Clixml 형식). DPAPI 는 가짜로 바꿔 끼움."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live import dpapi_secret as D
from live import wow_calendar_sync as S


def fake_protect(b: bytes) -> bytes:
    return b"\x01\x00\x00\x00FAKE" + bytes(x ^ 0x5A for x in b)


def fake_unprotect(b: bytes) -> bytes:
    if not b.startswith(b"\x01\x00\x00\x00FAKE"):
        raise D.SecretError("다른 PC")
    return bytes(x ^ 0x5A for x in b[8:])


class FakeDpapi(unittest.TestCase):
    def setUp(self):
        self._p = [mock.patch.object(D, "_protect", fake_protect), mock.patch.object(D, "_unprotect", fake_unprotect),
                   mock.patch.object(D, "_available", True)]
        for p in self._p:
            p.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        for p in self._p:
            p.stop()
        self.tmp.cleanup()


class Clixml(FakeDpapi):
    def test_roundtrip_and_format(self):
        p = self.dir / "s" / "secret.xml"
        self.assertTrue(D.write_verified(p, {"ID": "id-가", "SECRET": "s<&>"}))
        text = p.read_text(encoding="utf-8")
        self.assertIn('xmlns="http://schemas.microsoft.com/powershell/2004/04"', text)
        self.assertIn("<T>System.Collections.Hashtable</T>", text)
        self.assertIn('<SS N="Value">01000000', text)        # SecureString = DPAPI 덩어리 16진수
        self.assertNotIn("id-가", text)
        self.assertNotIn("s<&>", text)
        self.assertEqual(D.read(p), {"ID": "id-가", "SECRET": "s<&>"})

    def test_utf16_file_from_powershell(self):
        p = self.dir / "ps.xml"
        D.write(p, {"k": "v"})
        p.write_bytes(p.read_text(encoding="utf-8").encode("utf-16"))   # Export-Clixml 기본 = UTF-16 BOM
        self.assertEqual(D.read(p), {"k": "v"})

    def test_other_pc_raises(self):
        p = self.dir / "x.xml"
        D.write(p, {"k": "v"})
        with mock.patch.object(D, "_unprotect", side_effect=D.SecretError("다른 PC")):
            with self.assertRaises(D.SecretError):
                D.read(p)

    def test_not_windows(self):
        with mock.patch.object(D, "_available", False):
            self.assertIsNone(D.read(self.dir / "없음.xml"))
            with self.assertRaises(D.SecretError):
                D.write(self.dir / "a.xml", {"k": "v"})


class Calendar(FakeDpapi):
    def test_plain_json_migrates(self):
        (self.dir / "google_token.json").write_text(json.dumps({"refresh_token": "rt"}), encoding="utf-8")
        self.assertEqual(S.load_secret_json(self.dir, "google_token")["refresh_token"], "rt")
        self.assertFalse((self.dir / "google_token.json").exists())
        self.assertTrue((self.dir / "google_token.xml").exists())
        self.assertNotIn("rt", (self.dir / "google_token.xml").read_text(encoding="utf-8"))
        self.assertEqual(S.load_secret_json(self.dir, "google_token")["refresh_token"], "rt")

    def test_failed_verify_keeps_plain(self):
        (self.dir / "google_token.json").write_text(json.dumps({"refresh_token": "rt"}), encoding="utf-8")
        with mock.patch.object(D, "write_verified", return_value=False):
            S.load_secret_json(self.dir, "google_token")
        self.assertTrue((self.dir / "google_token.json").exists())

    def test_save_and_expire(self):
        S.save_secret_json(self.dir, "google_token", {"refresh_token": "rt"})
        self.assertTrue(S.has_secret(self.dir, "google_token"))
        S.mark_expired(self.dir)
        self.assertTrue((self.dir / "google_token.expired.xml").exists())
        self.assertTrue(S.was_expired(self.dir))

    def test_client_install(self):
        src = self.dir / "c.json"
        src.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}), encoding="utf-8")
        S.install_client_file(self.dir, src)
        self.assertFalse((self.dir / "google_client.json").exists())
        self.assertEqual(S.load_client(self.dir)["client_secret"], "cs")


if __name__ == "__main__":
    unittest.main()
