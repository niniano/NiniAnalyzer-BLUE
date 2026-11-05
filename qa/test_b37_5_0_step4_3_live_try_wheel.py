from __future__ import annotations
import ast
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'nini_wcl_gui.py'


def extracted_helpers():
    tree = ast.parse(SRC.read_text(encoding='utf-8'))
    app = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'App')
    names = {
        '_b3750_widget_is_descendant',
        '_b3750_wheel_units',
        '_b3750_widget_can_scroll',
        '_b3750_live_page_scroll_target',
    }
    funcs = [n for n in app.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    cls = ast.ClassDef(name='Dummy', bases=[], keywords=[], body=funcs, decorator_list=[])
    mod = ast.Module(body=[cls], type_ignores=[])
    ast.fix_missing_locations(mod)
    ns = {}
    exec(compile(mod, '<helpers>', 'exec'), ns)
    return ns['Dummy']


class Event:
    def __init__(self, delta=0, num=None):
        self.delta = delta
        self.num = num


class Node:
    _counter = 0
    def __init__(self, master=None, y=(0.0, 1.0), klass='Frame', height=100):
        type(self)._counter += 1
        self.master = master
        self._y = y
        self._klass = klass
        self._height = height
        self._children = []
        self._id = type(self)._counter
        if master is not None:
            master._children.append(self)
    def yview(self):
        return self._y
    def winfo_class(self):
        return self._klass
    def winfo_height(self):
        return self._height
    def winfo_children(self):
        return list(self._children)
    def winfo_exists(self):
        return 1
    def __str__(self):
        return f'.fake{self._id}'


class Step43WheelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.H = extracted_helpers()
        cls.text = SRC.read_text(encoding='utf-8')

    def test_windows_wheel(self):
        self.assertEqual(self.H._b3750_wheel_units(Event(delta=120)), -3)
        self.assertEqual(self.H._b3750_wheel_units(Event(delta=-120)), 3)
        self.assertEqual(self.H._b3750_wheel_units(Event(delta=240)), -6)

    def test_high_resolution_wheel_is_not_dropped(self):
        self.assertEqual(self.H._b3750_wheel_units(Event(delta=30)), -3)
        self.assertEqual(self.H._b3750_wheel_units(Event(delta=-30)), 3)

    def test_linux_wheel(self):
        self.assertEqual(self.H._b3750_wheel_units(Event(num=4)), -3)
        self.assertEqual(self.H._b3750_wheel_units(Event(num=5)), 3)

    def test_descendant_scope(self):
        page = Node()
        child = Node(page)
        grand = Node(child)
        other = Node()
        self.assertTrue(self.H._b3750_widget_is_descendant(grand, page))
        self.assertFalse(self.H._b3750_widget_is_descendant(other, page))

    def test_nested_scroll_direction(self):
        mid = Node(y=(0.25, 0.75))
        top = Node(y=(0.0, 0.5))
        bottom = Node(y=(0.5, 1.0))
        self.assertTrue(self.H._b3750_widget_can_scroll(mid, -3))
        self.assertTrue(self.H._b3750_widget_can_scroll(mid, 3))
        self.assertFalse(self.H._b3750_widget_can_scroll(top, -3))
        self.assertFalse(self.H._b3750_widget_can_scroll(bottom, 3))

    def test_outer_page_canvas_wins_over_timeline_canvas(self):
        page = Node(klass='Frame', height=700)
        outer = Node(page, y=(0.0, 0.45), klass='Canvas', height=700)
        timeline = Node(page, y=(0.0, 0.65), klass='Canvas', height=190)
        app = types.SimpleNamespace(
            live_frame=page, b3641_try_timeline=timeline,
            b3750_skill_ref_canvas=None, nav_canvas=None, b3794_left_canvas=None,
            _b3750_live_page_scroll_widget=None,
        )
        target = self.H._b3750_live_page_scroll_target(app)
        self.assertIs(target, outer)

    def test_install_is_scoped_and_called(self):
        self.assertIn('self._install_b3750_live_try_wheel()', self.text)
        # The current router must follow the *visible* Try page and pointer,
        # even while nav_state is stale during asynchronous page transitions.
        self.assertIn('if not outer.winfo_ismapped():', self.text)
        # RC5 (GOLD HF4): pointer hit-test plus physical-pointer geometry fallback.
        self.assertIn('px, py = int(self.winfo_pointerx()), int(self.winfo_pointery())', self.text)
        self.assertIn('pointer_widget = self.winfo_containing(px, py)', self.text)
        self.assertIn('and self._b3750_widget_is_descendant(pointer_widget, outer))', self.text)
        self.assertIn('if not inside_widget and not inside_rect:', self.text)
        self.assertIn('target.yview_scroll(units, "units")', self.text)


if __name__ == '__main__':
    unittest.main()
