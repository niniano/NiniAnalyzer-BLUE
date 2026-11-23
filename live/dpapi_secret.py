"""비밀값 DPAPI 저장 (1.0.20) — 사용자 규칙: 비밀키는 DPAPI 로 암호화해 XML 로 저장하고 그걸 읽는다.

형식은 PowerShell `Export-Clixml` 과 같다 (해시테이블 · 값은 SecureString = DPAPI 현재 사용자 · UTF-16LE).
그래서 PowerShell 에서도 `Import-Clixml 파일.xml` 로 열 수 있고, 이 PC · 이 Windows 계정에서만 풀린다.

    <Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">
      <Obj RefId="0"><TN RefId="0"><T>System.Collections.Hashtable</T><T>System.Object</T></TN>
        <DCT><En><S N="Key">이름</S><SS N="Value">01000000d08c9ddf…(DPAPI 덩어리 16진수)</SS></En></DCT>
      </Obj>
    </Objs>

Windows 가 아니면 available() 이 False → 부르는 쪽이 예전 방식(평문 파일)으로 둔다.
"""
from __future__ import annotations

import binascii
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

NS = "http://schemas.microsoft.com/powershell/2004/04"
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class SecretError(RuntimeError):
    pass


def _win_crypt(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    src = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        raise SecretError(f"DPAPI {'암호화' if protect else '복호화'} 실패 (다른 PC·다른 Windows 계정에서 만든 파일일 수 있음)")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


# 테스트에서 바꿔 끼울 수 있게 함수로 둔다
_protect = lambda data: _win_crypt(data, True)       # noqa: E731
_unprotect = lambda blob: _win_crypt(blob, False)    # noqa: E731
_available = sys.platform == "win32"


def available() -> bool:
    return bool(_available)


def write(path, values: dict[str, str]) -> Path:
    """{이름: 비밀값} → Export-Clixml 형식 XML (값은 DPAPI). 원자적으로 쓴다."""
    if not available():
        raise SecretError("DPAPI 는 Windows 에서만 됩니다")
    rows = []
    for k, v in values.items():
        blob = _protect(str(v).encode("utf-16-le"))
        rows.append(f'      <En>\n        <S N="Key">{escape(str(k))}</S>\n'
                    f'        <SS N="Value">{binascii.hexlify(blob).decode()}</SS>\n      </En>')
    text = ('<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">\n'
            '  <Obj RefId="0">\n    <TN RefId="0">\n      <T>System.Collections.Hashtable</T>\n      <T>System.Object</T>\n'
            '    </TN>\n    <DCT>\n' + "\n".join(rows) + '\n    </DCT>\n  </Obj>\n</Objs>\n')
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    return p


def read(path) -> dict[str, str] | None:
    """XML → {이름: 비밀값}. 파일이 없거나 Windows 가 아니면 None. 깨졌거나 못 풀면 SecretError."""
    p = Path(path)
    if not p.is_file() or not available():
        return None
    raw = p.read_bytes()
    try:
        text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
        root = ET.fromstring(text)
    except (UnicodeDecodeError, ET.ParseError) as e:
        raise SecretError(f"{p.name} 을 읽지 못했습니다") from e
    out = {}
    for en in root.iter(f"{{{NS}}}En"):
        key = val = None
        for child in en:
            tag = child.tag.split("}", 1)[-1]
            if child.get("N") == "Key":
                key = child.text or ""
            elif child.get("N") == "Value":
                if tag == "SS":
                    val = _unprotect(binascii.unhexlify((child.text or "").strip())).decode("utf-16-le")
                elif tag == "S":
                    val = child.text or ""
        if key is not None and val is not None:
            out[key] = val
    return out


def write_verified(path, values: dict[str, str]) -> bool:
    """쓰고 다시 풀어서 같은지 확인. 다르면 False (부르는 쪽은 평문 원본을 지우지 말 것)."""
    write(path, values)
    try:
        return read(path) == {str(k): str(v) for k, v in values.items()}
    except SecretError:
        return False
