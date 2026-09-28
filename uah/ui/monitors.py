"""Work-area geometry, including monitors to the left/above the primary."""
import os


def work_areas(root=None):
    if os.name == "nt":
        try:
            import ctypes as c
            from ctypes import wintypes as w
            class Info(c.Structure):
                _fields_ = [("size", w.DWORD), ("monitor", w.RECT),
                            ("work", w.RECT), ("flags", w.DWORD)]
            u = c.WinDLL("user32", use_last_error=True)
            callback_type = c.WINFUNCTYPE(w.BOOL, w.HANDLE, w.HDC, c.POINTER(w.RECT), w.LPARAM)
            u.GetMonitorInfoW.argtypes = [w.HANDLE, c.POINTER(Info)]
            u.GetMonitorInfoW.restype = w.BOOL
            u.EnumDisplayMonitors.argtypes = [w.HDC, c.POINTER(w.RECT), callback_type, w.LPARAM]
            u.EnumDisplayMonitors.restype = w.BOOL
            areas = []
            def collect(handle, dc, rect, data):
                info = Info(); info.size = c.sizeof(info)
                if u.GetMonitorInfoW(handle, c.byref(info)):
                    r = info.work
                    areas.append((r.left, r.top, r.right, r.bottom))
                return True
            u.EnumDisplayMonitors(None, None, callback_type(collect), 0)
            if areas:
                return areas
        except (OSError, AttributeError):
            pass
    return [(0, 0, root.winfo_screenwidth() if root else 1920,
             root.winfo_screenheight() if root else 1080)]


def clamp_position(x, y, width, height, areas):
    cx, cy = x + width / 2, y + height / 2
    def distance(rect):
        l, t, r, b = rect
        return max(l-cx, 0, cx-r)**2 + max(t-cy, 0, cy-b)**2
    l, t, r, b = min(areas, key=distance)
    return (max(l, min(x, max(l, r-width))), max(t, min(y, max(t, b-height))))


def tk_position(x, y):
    # Tk '-100' means offset from the RIGHT edge. '+-100' is an absolute
    # negative coordinate and is essential for left/upper secondary monitors.
    return f"+{int(x)}+{int(y)}"
