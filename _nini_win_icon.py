"""Windows native NiniAnalyzer taskbar/window icon helper.

Python/pythonw launches may expose the Python executable icon in the taskbar.
Tk's iconphoto/iconbitmap can also run before the decorated HWND is mapped.
This module applies 16/32/48 px icons AFTER Windows creates the outer HWND.
Both PERSONAL and PUBLIC use their own ICO path and separate process AppID.
"""
from __future__ import annotations

import os
from pathlib import Path
import tkinter as tk


def install_icon(root: tk.Tk, resource_dir: Path) -> None:
    """Set Tk icon immediately and native HWND icons once the window is mapped."""
    resource_dir = Path(resource_dir)
    ico = resource_dir / "nini_analyzer.ico"
    png = resource_dir / "nini_analyzer.png"
    if png.is_file():
        try:
            root._nini_shell_icon_photo = tk.PhotoImage(file=str(png))
            root.iconphoto(True, root._nini_shell_icon_photo)
        except (OSError, tk.TclError):
            pass
    if os.name != "nt" or not ico.is_file():
        return
    try:
        root.iconbitmap(default=str(ico))
    except (OSError, tk.TclError):
        pass

    # Tk may create the visible Win32 frame only after returning to mainloop.
    # Re-apply after mapping (including delayed Windows taskbar grouping).
    def apply_native():
        try:
            if root.winfo_exists():
                _set_windows_hwnd_icons(root, ico)
        except (OSError, tk.TclError, AttributeError, ValueError):
            pass

    root.after(120, apply_native)
    root.after(800, apply_native)


def _set_windows_hwnd_icons(root: tk.Tk, icon_path: Path) -> bool:
    """Apply both WM_SETICON and class icons to the decorated Win32 Tk HWND.

    Do not destroy the loaded HICON handles while the application is running:
    Windows retains them for class/window icons. The process releases them on
    exit. Fail softly on non-Windows and unsupported Python/Tk combinations.
    """
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    try:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        load = u32.LoadImageW
        load.argtypes = (wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                         ctypes.c_int, ctypes.c_int, wintypes.UINT)
        load.restype = wintypes.HANDLE
        # IMAGE_ICON=1, LR_LOADFROMFILE=0x10. Request native size variants.
        icons = [load(None, str(icon_path), 1, size, size, 0x10)
                 for size in (16, 32, 48)]
        if not all(icons):
            for icon in icons:
                if icon:
                    u32.DestroyIcon(wintypes.HANDLE(icon))
            return False
        getancestor = u32.GetAncestor
        getancestor.argtypes = (wintypes.HWND, wintypes.UINT)
        getancestor.restype = wintypes.HWND
        getparent = u32.GetParent
        getparent.argtypes = (wintypes.HWND,)
        getparent.restype = wintypes.HWND
        send = u32.SendMessageW
        # LPARAM/WPARAM are POINTER-SIZED (not c_long on Win64).
        send.argtypes = (wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)
        send.restype = ctypes.c_ssize_t
        # HWND from winfo_id may be the inner Tk client or the decorated HWND.
        inner = int(root.winfo_id())
        decorated = int(getancestor(wintypes.HWND(inner), 2) or 0)  # GA_ROOT=2
        parent = int(getparent(wintypes.HWND(inner)) or 0)
        hwnds = tuple(dict.fromkeys(v for v in (decorated, parent, inner) if v))
        for hwnd in hwnds:
            send(hwnd, 0x0080, 0, icons[0])  # WM_SETICON / ICON_SMALL
            send(hwnd, 0x0080, 1, icons[2])  # WM_SETICON / ICON_BIG
            if hwnd == (decorated or parent or inner):
                setter = getattr(u32, 'SetClassLongPtrW', None)
                if setter is not None:
                    setter.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_void_p)
                    setter.restype = ctypes.c_void_p
                    setter(hwnd, -34, icons[0])  # GCLP_HICONSM
                    setter(hwnd, -14, icons[2])  # GCLP_HICON
        # Leave handles alive and avoid leaking them if called again.
        prior = getattr(root, '_nini_win32_handles', ())
        root._nini_win32_handles = (*prior, *[int(i) for i in icons])
        return True
    except (OSError, ValueError, AttributeError, TypeError):
        return False
