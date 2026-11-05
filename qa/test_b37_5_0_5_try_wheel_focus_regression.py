"""Regression: a focused sidebar must not steal the Try-page wheel."""
from __future__ import annotations
import ast
import os
from pathlib import Path
import tkinter as tk
import unittest

ROOT=Path(__file__).resolve().parents[1]

class Contract(unittest.TestCase):
    def test_capture_and_pointer_scope(self):
        s=(ROOT/'nini_wcl_gui.py').read_text(encoding='utf-8')
        ast.parse(s)
        self.assertIn('self.bind_class("NiniTryPointerWheel", "<MouseWheel>"',s)
        self.assertIn('widget.bindtags((tag,) + tags)',s)
        self.assertIn('self.bind_all("<Map>", self._b3750_tag_mapped_widget',s)
        # RC5 (GOLD HF4): pointer hit-test plus physical-pointer geometry fallback.
        self.assertIn('px, py = int(self.winfo_pointerx()), int(self.winfo_pointery())', s)
        self.assertIn('pointer_widget = self.winfo_containing(px, py)', s)
        self.assertIn('if not inside_widget and not inside_rect:', s)

@unittest.skipUnless(os.name=='nt' or os.environ.get('DISPLAY'),'no screen')
class ActualTk(unittest.TestCase):
    def setUp(self):
        from nini_wcl_gui import App
        self.app=App(edition='raid')
        self.app.nav_state['page']='live_try'
        self.app.notebook.select(self.app.live_frame)
        # RC5.2: the build PC may auto-open a saved Try whose page is short (e.g. no engine cards), and a
        # portrait monitor opens a tall window. Make the filler taller than any window so the page always scrolls.
        self.item=tk.Label(self.app.live_frame,text='scroll regression filler',height=max(80, self.app.winfo_screenheight()//8))
        self.item.pack(fill='x')
        self.app.update_idletasks(); self.app.update()
        self.app.nav_state['page']='live_try'
        self.app.notebook.select(self.app.live_frame)
        self.app._b3750_tag_all_widgets()
        self.canvas=self.app.live_frame._canvas
        self.assertLess(self.canvas.yview()[1], 0.95)
        self.app.winfo_pointerx=lambda: 100
        self.app.winfo_pointery=lambda: 100
        self.app.winfo_containing=lambda x,y: self.item
    def tearDown(self):
        # Stop queued Tk callbacks before destroying an App in the same process.
        try:
            for handle in self.app.tk.call('after', 'info'):
                self.app.after_cancel(handle)
        except tk.TclError:
            pass
        self.app.destroy()
    def test_sidebar_focus_does_not_steal_pointer_scroll(self):
        nav=self.app.nav_canvas
        self.assertEqual(nav.bindtags()[0],'NiniTryPointerWheel')
        pos=self.canvas.yview()[0]
        nav.event_generate('<MouseWheel>',delta=-120, x=5,y=5)
        self.assertGreater(self.canvas.yview()[0],pos)
    def test_on_card_or_timeline_canvas(self):
        for widget in (self.item,self.canvas):
            pos=self.canvas.yview()[0]
            widget.event_generate('<MouseWheel>',delta=-120,x=5,y=5)
            self.assertGreater(self.canvas.yview()[0],pos)
    def test_sidebar_pointer_does_not_scroll_detail(self):
        self.app.winfo_containing=lambda x,y: self.app.nav_canvas
        pos=self.canvas.yview()[0]
        self.app.nav_canvas.event_generate('<MouseWheel>',delta=-120,x=5,y=5)
        self.assertEqual(self.canvas.yview()[0],pos)
    def test_navigational_selection_unchanged(self):
        before=self.app.nav_state.copy()
        self.item.event_generate('<MouseWheel>',delta=-120,x=5,y=5)
        self.assertEqual(self.app.nav_state,before)
