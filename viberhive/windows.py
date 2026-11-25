import ctypes as c
import os
from ctypes import wintypes as w

WINDOWS = os.name == "nt"
if WINDOWS:
    user32 = c.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = w.HWND
    user32.GetMonitorInfoW.argtypes = [w.HANDLE, c.c_void_p]
    user32.MonitorFromWindow.argtypes = [w.HWND, w.DWORD]
    user32.MonitorFromWindow.restype = w.HANDLE
    user32.GetWindowRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
    user32.SetForegroundWindow.argtypes = [w.HWND]
    user32.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
    user32.GetWindowThreadProcessId.restype = w.DWORD
    user32.AttachThreadInput.argtypes = [w.DWORD, w.DWORD, w.BOOL]
    user32.BringWindowToTop.argtypes = [w.HWND]
    user32.GetFocus.restype = w.HWND
    user32.IsChild.argtypes = [w.HWND, w.HWND]
    user32.SetFocus.argtypes = [w.HWND]
    user32.SetFocus.restype = w.HWND
    user32.SendMessageTimeoutW.argtypes = [
        w.HWND,
        w.UINT,
        w.WPARAM,
        w.LPARAM,
        w.UINT,
        w.UINT,
        c.POINTER(c.c_size_t),
    ]
    user32.SendMessageTimeoutW.restype = c.c_ssize_t
    kernel32 = c.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentThreadId.restype = w.DWORD
    user32.AllowSetForegroundWindow.argtypes = [w.DWORD]
    user32.AllowSetForegroundWindow.restype = w.BOOL
    user32.IsWindow.argtypes = [w.HWND]
    user32.GetWindowLongW.argtypes = [w.HWND, c.c_int]
    user32.GetWindowLongW.restype = w.LONG
    user32.SetWindowPos.argtypes = [w.HWND, w.HWND, c.c_int, c.c_int, c.c_int, c.c_int, w.UINT]
    user32.SetWindowPos.restype = w.BOOL


_Z_ORDER_ONLY = 0x0001 | 0x0002 | 0x0010 | 0x0200  # NOSIZE, NOMOVE, NOACTIVATE, NOOWNERZORDER
_TOPMOST_STYLE = 0x00000008


def raise_for_summon(hwnd, origin):
    if not WINDOWS or not hwnd or foreground() not in (hwnd, origin, 0, None):
        return False
    if user32.GetWindowLongW(hwnd, -20) & _TOPMOST_STYLE:
        return False  # Preserve a separately configured topmost window.
    return bool(user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, _Z_ORDER_ONLY))


def finish_summon(hwnd, *, yield_to_active=False):
    if not WINDOWS or not user32.IsWindow(hwnd):
        return
    after = -2  # HWND_NOTOPMOST
    if yield_to_active:
        active = foreground()
        if active and active != hwnd and not (user32.GetWindowLongW(active, -20) & _TOPMOST_STYLE):
            after = active  # Leave the newly selected window in front of the picker.
    user32.SetWindowPos(hwnd, after, 0, 0, 0, 0, _Z_ORDER_ONLY)


def dpi_awareness():
    if WINDOWS:
        user32.SetProcessDpiAwarenessContext(c.c_void_p(-4))


def set_app_user_model_id(value):
    if WINDOWS:
        set_id = c.WinDLL("shell32").SetCurrentProcessExplicitAppUserModelID
        set_id.argtypes = [w.LPCWSTR]
        set_id.restype = c.c_long
        set_id(value)


def session_id():
    if WINDOWS:
        session = w.DWORD()
        if kernel32.ProcessIdToSessionId(os.getpid(), c.byref(session)):
            return session.value
    return 0


def foreground():
    return user32.GetForegroundWindow() if WINDOWS else 0


def grant_foreground(pid):
    if WINDOWS and pid:
        user32.AllowSetForegroundWindow(pid)


def restore(hwnd):
    if WINDOWS and hwnd and user32.IsWindow(hwnd):
        user32.SetForegroundWindow(hwnd)


def own_window(hwnd):
    if not WINDOWS or not hwnd:
        return False
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, c.byref(pid))
    return pid.value == os.getpid()


def focus(hwnd, previous, *, keyboard=False):
    """Activate a requested window on this desktop; preserve intervening user focus."""
    if not WINDOWS:
        return True
    other = foreground()
    if other != hwnd:
        if other and other != previous:
            return False
        user32.SetForegroundWindow(hwnd)
        if other and foreground() == other:
            current = kernel32.GetCurrentThreadId()
            active = user32.GetWindowThreadProcessId(other, None)
            # Test the other queue before briefly attaching our input to it. WM_NULL,
            # SMTO_ABORTIFHUNG and a short timeout keep a frozen client out of this path.
            if (
                not active
                or active == current
                or not user32.SendMessageTimeoutW(other, 0, 0, 0, 0x0002, 80, None)
            ):
                return False
            if foreground() != other or not user32.AttachThreadInput(current, active, True):
                return False
            try:
                if foreground() == other:
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
            finally:
                user32.AttachThreadInput(current, active, False)
    if foreground() != hwnd:
        return False
    if keyboard:
        # Only the GUI thread uses this option for its own top-level picker/dialog.
        # A native child editor already holding the keyboard must retain it.
        child = user32.GetFocus()
        if child != hwnd and not (child and user32.IsChild(hwnd, child)):
            user32.SetFocus(hwnd)
            child = user32.GetFocus()
        return bool(child == hwnd or (child and user32.IsChild(hwnd, child)))
    return True


def monitor_window(hwnd):
    if not WINDOWS or not hwnd:
        return None

    class Monitor(c.Structure):
        _fields_ = [
            ("size", w.DWORD),
            ("rect", w.RECT),
            ("work", w.RECT),
            ("flags", w.DWORD),
            ("device", w.WCHAR * 32),
        ]

    m = Monitor()
    m.size = c.sizeof(m)
    box = w.RECT()
    if user32.GetMonitorInfoW(
        user32.MonitorFromWindow(hwnd, 2), c.byref(m)
    ) and user32.GetWindowRect(hwnd, c.byref(box)):
        return (m.device, (m.rect.left, m.rect.top), (box.left, box.top, box.right, box.bottom))
    return None


def dpapi(data, decrypt=False):
    class Blob(c.Structure):
        _fields_ = [("size", w.DWORD), ("data", c.c_void_p)]

    crypt = c.WinDLL("crypt32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [c.c_void_p]
    kernel.LocalFree.restype = c.c_void_p
    buf = c.create_string_buffer(data)
    inp, out = Blob(len(data), c.cast(buf, c.c_void_p)), Blob()
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [
        c.POINTER(Blob),
        c.c_void_p,
        c.c_void_p,
        c.c_void_p,
        c.c_void_p,
        w.DWORD,
        c.POINTER(Blob),
    ]
    if not fn(c.byref(inp), None, None, None, None, 1, c.byref(out)):
        raise OSError("Windows could not access the saved API key.")
    try:
        return c.string_at(out.data, out.size)
    finally:
        kernel.LocalFree(out.data)
