"""Public manual file import and wheel interaction regression tests."""
from __future__ import annotations
import os
import tempfile
import tkinter as tk
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from live.public_paths import read_public_settings

@unittest.skipUnless(os.environ.get('DISPLAY') or os.name=='nt', 'GUI display required')
class Public221GuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.old_home=os.environ.get('NINI_PUBLIC_HOME')
        os.environ['NINI_PUBLIC_HOME']=str(Path(self.tmp.name)/'home')
        from nini_wcl_gui import App
        self.app=App(edition='raid')
        self.app._public_migration_running=False
        # Let deferred startup refreshes settle *before* switching notebook
        # tabs in wheel tests. Otherwise an initial session refresh can run
        # after the test selects Try and silently switch the visible page.
        self.app.update()
        self.app.after(300)
        self.app.update()

    def tearDown(self):
        try:self.app.destroy()
        except Exception:pass
        if self.old_home is None:os.environ.pop('NINI_PUBLIC_HOME',None)
        else:os.environ['NINI_PUBLIC_HOME']=self.old_home
        self.tmp.cleanup()

    def test_manual_import_from_downloads_does_not_change_monitor_path(self):
        root=Path(self.tmp.name)
        log=root/'Downloads'/'WoWCombatLog-shared.txt'
        log.parent.mkdir()
        log.write_text('',encoding='utf-8')
        old_log_dir=self.app.live_monitor.log_dir
        with (patch('nini_wcl_gui.threading.Thread') as fake_thread,
              patch.object(self.app,'_progress_begin'),patch.object(self.app,'_progress_set')):
            self.app._public_import_start(log)
        self.assertTrue(fake_thread.called)
        self.assertEqual(self.app.live_monitor.log_dir,old_log_dir)
        self.assertEqual(read_public_settings(self.app._public_home),{})
        self.app._public_import_running=False

    def test_native_mouse_wheel_on_dynamic_try_child_scrolls_page(self):
        # A genuine Tk <MouseWheel> event (not direct Python handler invocation).
        app=self.app
        app.notebook.unbind('<<NotebookTabChanged>>')
        app.notebook.select(app.live_frame)
        # Large enough to exceed the viewport even when prior GUI tests
        # changed Tcl font/scaling defaults.
        filler=tk.Label(app.live_frame,text='Large Try details',height=240)
        filler.pack(fill='x')
        app.nav_state['page']='live_try'
        app.update_idletasks()
        app.update()
        app.live_frame._sync()
        app.update_idletasks()
        canvas=app.live_frame._canvas
        canvas.yview_moveto(0)
        child=app.live_frame._canvas
        # Native Tk class bindings with deterministic hovered widget. Virtual
        # displays sometimes refuse to move the physical cursor (warp=True),
        # so stub only pointer location, not event delivery or scroll behavior.
        with patch.object(app, 'winfo_containing', return_value=child):
            child.event_generate('<MouseWheel>',delta=-120,x=28,y=24)
            app.update()
        self.assertGreater(canvas.yview()[0],0.0)

    def test_visible_try_wheel_scrolls_even_with_stale_nav_state(self):
        # Model real session-refresh timing: Try tab is visible, shared page
        # selection has not yet advanced to live_try. Old strict guard lost wheel.
        app=self.app
        app.notebook.unbind('<<NotebookTabChanged>>')
        app.notebook.select(app.live_frame)
        filler=tk.Label(app.live_frame,text='Test try body',height=240)
        filler.pack(fill='x')
        app.update_idletasks()
        app.update()
        app.live_frame._sync()
        app.update_idletasks()
        canvas=app.live_frame._canvas
        self.assertTrue(app.live_frame._outer.winfo_ismapped())
        self.assertLess(canvas.yview()[1],1.0)
        app.nav_state['page']='session'
        child=app.live_detail_title_var  # stable Try header visual control
        target=next(c for c in app.live_frame.winfo_children() if isinstance(c,tk.Widget))
        class E:
            widget=target
            delta=-120
            num=None
        # Real pointer location over a Try child, regardless of event focus.
        # RC5 (GOLD HF4): the physical pointer position is checked too, so put it on the Try pane.
        outer=app.live_frame._outer
        inside=(outer.winfo_rootx()+outer.winfo_width()//2, outer.winfo_rooty()+outer.winfo_height()//2)
        with patch.object(app,'winfo_containing',return_value=target), \
             patch.object(app,'winfo_pointerx',return_value=inside[0]), \
             patch.object(app,'winfo_pointery',return_value=inside[1]):
            result=app._b3750_live_try_mousewheel(E())
        self.assertEqual(result,'break')
        self.assertGreater(canvas.yview()[0],0.0)
        # Wheel elsewhere must not alter the visible Try.
        y=canvas.yview()
        # Pointer physically outside the Try pane (left of it, over the sidebar).
        outside=(max(0,outer.winfo_rootx()-5), outer.winfo_rooty()+5)
        with patch.object(app,'winfo_containing',return_value=app.nav_canvas), \
             patch.object(app,'winfo_pointerx',return_value=outside[0]), \
             patch.object(app,'winfo_pointery',return_value=outside[1]):
            self.assertIsNone(app._b3750_live_try_mousewheel(E()))
        self.assertEqual(y,canvas.yview())

if __name__=='__main__':unittest.main()
