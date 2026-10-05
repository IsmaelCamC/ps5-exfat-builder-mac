"""
ui/shared/scroll.py — Robust cross-platform mousewheel and trackpad scroll implementation.

Handles:
- macOS (Darwin): raw, unscaled high-resolution deltas from trackpads and wheel mice.
- Windows: standard +/-120 delta per wheel notch.
- X11: Button-4 (up) and Button-5 (down) events.
- Child widget hover: ensures scrolling works when the mouse pointer is over child
  cards, frames, buttons, labels, and entry fields inside the scrollable canvas.
"""

import sys
import tkinter as tk

_SCROLL_SPEED = 3
_SCROLLABLE_CANVASES = set()
_GLOBAL_BOUND = False


def _get_scroll_step(event, speed=_SCROLL_SPEED):
    """Normalize scroll delta across macOS, Windows, and X11."""
    if getattr(event, 'delta', 0):
        if sys.platform == 'darwin':
            # macOS Cocoa Tk delivers raw unscaled deltas
            if abs(event.delta) >= 120:
                return int(-1 * (event.delta / 120) * speed)
            step = -int(event.delta)
            if step == 0 and event.delta != 0:
                step = -1 if event.delta > 0 else 1
            return step
        else:
            # Windows delivers +/-120 per wheel notch
            return int(-1 * (event.delta / 120) * speed)
    elif getattr(event, 'num', None) == 4:
        # X11 scroll up
        return -speed
    elif getattr(event, 'num', None) == 5:
        # X11 scroll down
        return speed
    return 0


def _find_target_canvas(event):
    """Find the visible scrollable canvas under the mouse pointer."""
    # 1. Check widget hierarchy under pointer
    try:
        w = event.widget
        under_pointer = w.winfo_containing(event.x_root, event.y_root)
        if under_pointer is not None:
            # If the user is hovering over a Text or Listbox that has its own active scrollbar,
            # allow that widget to consume the event.
            if isinstance(under_pointer, (tk.Text, tk.Listbox)):
                try:
                    top, bottom = under_pointer.yview()
                    if bottom - top < 1.0:
                        return None
                except Exception:
                    pass

            curr = under_pointer
            while curr is not None:
                if curr in _SCROLLABLE_CANVASES:
                    try:
                        if curr.winfo_viewable():
                            return curr
                    except Exception:
                        pass
                parent_name = curr.winfo_parent()
                if not parent_name:
                    break
                curr = curr._nametowidget(parent_name)
    except Exception:
        pass

    # 2. Bounding box fallback for visible canvases
    for c in list(_SCROLLABLE_CANVASES):
        try:
            if not c.winfo_viewable():
                continue
            rx = c.winfo_rootx()
            ry = c.winfo_rooty()
            rw = c.winfo_width()
            rh = c.winfo_height()
            if rx <= event.x_root <= rx + rw and ry <= event.y_root <= ry + rh:
                return c
        except Exception:
            continue

    return None


def _global_wheel(event, speed=_SCROLL_SPEED):
    target = _find_target_canvas(event)
    if target is not None:
        step = _get_scroll_step(event, speed)
        if step != 0:
            try:
                target.yview_scroll(step, 'units')
            except Exception:
                pass


def _init_global_bindings(widget):
    global _GLOBAL_BOUND
    if _GLOBAL_BOUND:
        return
    try:
        toplevel = widget.winfo_toplevel()
        toplevel.bind_all('<MouseWheel>', _global_wheel, add='+')
        toplevel.bind_all('<Button-4>', _global_wheel, add='+')
        toplevel.bind_all('<Button-5>', _global_wheel, add='+')
        _GLOBAL_BOUND = True
    except Exception:
        pass


def attach_scroll(canvas, speed=_SCROLL_SPEED):
    """Make `canvas` scroll on mousewheel/trackpad anywhere the pointer is inside
    it, even when hovering over child cards, buttons, labels, and frames."""
    if canvas is None:
        return canvas

    _SCROLLABLE_CANVASES.add(canvas)

    def _cleanup(_e=None, c=canvas):
        _SCROLLABLE_CANVASES.discard(c)

    try:
        canvas.bind('<Destroy>', _cleanup, add='+')
    except Exception:
        pass

    try:
        _init_global_bindings(canvas)
    except Exception:
        pass

    # Direct fallback on canvas itself
    def _local_wheel(e, c=canvas, s=speed):
        step = _get_scroll_step(e, s)
        if step != 0:
            try:
                c.yview_scroll(step, 'units')
            except Exception:
                pass

    try:
        canvas.bind('<MouseWheel>', _local_wheel, add='+')
        canvas.bind('<Button-4>', _local_wheel, add='+')
        canvas.bind('<Button-5>', _local_wheel, add='+')
    except Exception:
        pass

    return canvas
