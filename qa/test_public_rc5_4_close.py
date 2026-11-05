"""rc5.4 (GitHub): 창을 닫을 때 Windows에서만 나던 Tk 오류 재현 → 고침.

1) 자식 위젯이 걸어 둔 after 를 루트가 취소하면 Tcl 명령이 지워지는데, 자식은 그 이름을 그대로 들고 있어
   destroy 때 "can't delete Tcl command" (QA 휠 테스트 tearDown 이 바로 이 순서)
2) 닫는 도중 <<NotebookTabChanged>> 가 불려 이미 없어진 위젯을 건드림
"""
from __future__ import annotations

import os
import tkinter as tk
import unittest


@unittest.skipUnless(os.name == "nt" or os.environ.get("DISPLAY"), "no screen")
class Close(unittest.TestCase):
    def test_child_after_cancelled_by_root_then_destroy(self):
        from nini_wcl_gui import App
        app = App(edition="raid")
        try:
            app.update_idletasks()
            child = app.live_frame
            child.after(100000, lambda: None)          # 자식 위젯 타이머 (닫을 때 남아 있음)
            for handle in app.tk.call("after", "info"):  # QA tearDown 과 같은 취소
                app.after_cancel(handle)
        finally:
            app.destroy()                                 # 예전: TclError can't delete Tcl command

    def test_closing_flag_and_tab_event_ignored(self):
        from nini_wcl_gui import App
        app = App(edition="raid")
        app.destroy()
        self.assertTrue(getattr(app, "_closing", False))
        app._on_main_frame_changed()                     # 닫힌 뒤 불려도 오류 없음


if __name__ == "__main__":
    unittest.main()
