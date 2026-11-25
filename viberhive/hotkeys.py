"""Summon the picker from Windows keyboard events, even between GUI event-loop iterations."""

import ctypes as c
import sys
import threading
import time

from PyQt6.QtCore import (
    QAbstractNativeEventFilter,
    QCoreApplication,
    QObject,
    Qt,
    QThread,
    pyqtSignal,
)
from PyQt6.QtGui import QWindow

F12 = 0x7B
G = 0x47
LEFT_CTRL, RIGHT_CTRL = 0xA2, 0xA3
LEFT_SHIFT, RIGHT_SHIFT = 0xA0, 0xA1
LEFT_ALT, RIGHT_ALT = 0xA4, 0xA5
LEFT_WIN, RIGHT_WIN = 0x5B, 0x5C
OTHER_MODIFIERS = frozenset((LEFT_SHIFT, RIGHT_SHIFT, LEFT_ALT, RIGHT_ALT, LEFT_WIN, RIGHT_WIN))
TRACKED = OTHER_MODIFIERS | {LEFT_CTRL, RIGHT_CTRL, F12}

WM_INPUT = 0x00FF
WM_HOTKEY = 0x0312
MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 2, 4, 0x4000
RIDEV_INPUTSINK, RIDEV_REMOVE = 0x100, 1
RID_INPUT = 0x10000003
RI_KEY_BREAK, RI_KEY_E0 = 1, 2
UINT_ERROR = 0xFFFFFFFF


# Fixed Windows ABI widths also permit verification on development hosts using LP64.
class _Message(c.Structure):
    _fields_ = [
        ("hwnd", c.c_void_p),
        ("message", c.c_uint32),
        ("wParam", c.c_size_t),
        ("lParam", c.c_ssize_t),
        ("time", c.c_uint32),
        ("x", c.c_int32),
        ("y", c.c_int32),
        ("lPrivate", c.c_uint32),
    ]


class _Device(c.Structure):
    _fields_ = [
        ("page", c.c_uint16),
        ("usage", c.c_uint16),
        ("flags", c.c_uint32),
        ("target", c.c_void_p),
    ]


class _Header(c.Structure):
    _fields_ = [
        ("type", c.c_uint32),
        ("size", c.c_uint32),
        ("device", c.c_void_p),
        ("wParam", c.c_size_t),
    ]


class _Keyboard(c.Structure):
    _fields_ = [
        ("header", _Header),
        ("scan", c.c_uint16),
        ("flags", c.c_uint16),
        ("reserved", c.c_uint16),
        ("vk", c.c_uint16),
        ("message", c.c_uint32),
        ("extra", c.c_uint32),
    ]


class _F12Chord:
    def __init__(self, initial=()):
        self.held = TRACKED.intersection(initial)

    def read(self, vk, scan, flags):
        if vk == 0x11:
            vk = RIGHT_CTRL if flags & RI_KEY_E0 else LEFT_CTRL
        elif vk == 0x10:
            vk = RIGHT_SHIFT if scan == 0x36 else LEFT_SHIFT
        elif vk == 0x12:
            vk = RIGHT_ALT if flags & RI_KEY_E0 else LEFT_ALT
        if vk not in TRACKED:
            return False
        if flags & RI_KEY_BREAK:
            self.held = self.held - {vk}
            return False
        if vk in self.held:
            return False
        self.held = self.held | {vk}
        return (
            vk == F12
            and bool(self.held & {LEFT_CTRL, RIGHT_CTRL})
            and self.held.isdisjoint(OTHER_MODIFIERS)
        )


class _Windows:
    def __init__(self):
        user = c.WinDLL("user32", use_last_error=True)
        self.user = user
        user.RegisterRawInputDevices.argtypes = [c.POINTER(_Device), c.c_uint32, c.c_uint32]
        user.RegisterRawInputDevices.restype = c.c_int32
        user.GetRegisteredRawInputDevices.argtypes = [
            c.POINTER(_Device),
            c.POINTER(c.c_uint32),
            c.c_uint32,
        ]
        user.GetRegisteredRawInputDevices.restype = c.c_uint32
        user.GetRawInputData.argtypes = [
            c.c_void_p,
            c.c_uint32,
            c.c_void_p,
            c.POINTER(c.c_uint32),
            c.c_uint32,
        ]
        user.GetRawInputData.restype = c.c_uint32
        user.RegisterHotKey.argtypes = [c.c_void_p, c.c_int32, c.c_uint32, c.c_uint32]
        user.RegisterHotKey.restype = c.c_int32
        user.UnregisterHotKey.argtypes = [c.c_void_p, c.c_int32]
        user.UnregisterHotKey.restype = c.c_int32
        user.GetSystemMetrics.argtypes = [c.c_int32]
        user.GetSystemMetrics.restype = c.c_int32
        user.GetAsyncKeyState.argtypes = [c.c_int32]
        user.GetAsyncKeyState.restype = c.c_int16

    def keyboard_owner(self):
        """Raw keyboard registration is per process. Preserve any existing Qt/browser owner."""
        count = c.c_uint32()
        fn = self.user.GetRegisteredRawInputDevices
        if fn(None, c.byref(count), c.sizeof(_Device)) == UINT_ERROR:
            raise OSError(c.get_last_error(), "Could not inspect Windows keyboard registration.")
        if not count.value:
            return None
        devices = (_Device * count.value)()
        n = fn(devices, c.byref(count), c.sizeof(_Device))
        if n == UINT_ERROR:
            raise OSError(c.get_last_error(), "Could not inspect Windows keyboard registration.")
        for device in devices[:n]:
            if device.page == 1 and (
                device.usage == 6 or (not device.usage and device.flags & 0x20)
            ):
                return device.target or 0
        return None

    def start_raw(self, hwnd):
        # RDP does not expose the remote keyboard as a raw input device.
        if self.user.GetSystemMetrics(0x1000) or self.keyboard_owner() is not None:
            return False
        device = _Device(1, 6, RIDEV_INPUTSINK, hwnd)
        return bool(self.user.RegisterRawInputDevices(c.byref(device), 1, c.sizeof(device)))

    def stop_raw(self, hwnd):
        try:
            owned = self.keyboard_owner() == hwnd
        except OSError:
            owned = False
        if owned:
            device = _Device(1, 6, RIDEV_REMOVE, None)
            self.user.RegisterRawInputDevices(c.byref(device), 1, c.sizeof(device))

    def read_raw(self, handle):
        # Some input stacks pad keyboard records to the size of the RAWINPUT union.
        # The buffer is DWORD aligned even when the Python host uses 64-bit pointers.
        buffer = (c.c_uint64 * 16)()
        size = c.c_uint32(c.sizeof(buffer))
        n = self.user.GetRawInputData(
            handle, RID_INPUT, c.byref(buffer), c.byref(size), c.sizeof(_Header)
        )
        if not c.sizeof(_Keyboard) <= n <= c.sizeof(buffer):
            return None
        packet = _Keyboard.from_buffer(buffer)
        if packet.header.type == 1 and c.sizeof(_Keyboard) <= packet.header.size <= n:
            return packet.vk, packet.scan, packet.flags
        return None

    def start_registered(self, hwnd, identifier):
        if self.user.RegisterHotKey(hwnd, identifier, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, G):
            return 0
        return c.get_last_error() or -1

    def stop_registered(self, hwnd, identifier):
        self.user.UnregisterHotKey(hwnd, identifier)

    def down(self, vk):
        return bool(self.user.GetAsyncKeyState(vk) & 0x8000)


class _EventFilter(QAbstractNativeEventFilter):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager

    def nativeEventFilter(self, event_type, pointer):
        if not pointer or bytes(event_type) not in (
            b"windows_generic_MSG",
            b"windows_dispatcher_MSG",
        ):
            return False, 0
        msg = _Message.from_address(int(pointer))
        if msg.hwnd and msg.hwnd == self.manager.hwnd:
            return self.manager._message(msg), 0
        return False, 0


class GlobalHotkey(QObject):
    """Configure on startup or preference changes. An activation always means 'summon'."""

    activated = pyqtSignal()
    statusChanged = pyqtSignal(str)
    _change = pyqtSignal(str)
    _pulse = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.choice = ""
        self.mode = ""
        self.status = "Global shortcut inactive."
        self.available = False
        self.api = None
        self.window = None
        self.hwnd = 0
        self.generation = 0
        self.identifier = 0
        self.chord = _F12Chord()
        self.filter = _EventFilter(self)
        self.thread_stop = None
        self.worker = None
        self._change.connect(self._apply, Qt.ConnectionType.QueuedConnection)
        self._pulse.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def configure(self, choice):
        if QThread.currentThread() == self.thread():
            self._apply(choice)
        else:
            self._change.emit(choice)

    def stop(self):
        self.configure("")

    def _report(self, mode, text):
        self.mode = mode
        self.available = bool(mode)
        self.status = text
        self.statusChanged.emit(text)

    def _apply(self, choice):
        if self.choice == choice and self.available:
            return
        self.generation += 1
        self._release()
        self.choice = choice
        if not choice:
            self._report("", "Global shortcut inactive.")
            return
        if sys.platform != "win32":
            self._report("", "Global shortcuts require Windows.")
            return
        if choice not in ("Ctrl+F12", "Ctrl+Shift+G"):
            self._report("", "Unknown global shortcut. Choose one in Settings.")
            return
        try:
            self.api = _Windows()
            self.window = QWindow()
            self.window.setFlags(Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
            self.hwnd = int(self.window.winId())
            if not self.hwnd:
                raise OSError("A Windows input receiver could not be created.")
            QCoreApplication.instance().installNativeEventFilter(self.filter)
            self.identifier = 1 + self.generation % 0xBFFE
            if choice == "Ctrl+Shift+G":
                error = self.api.start_registered(self.hwnd, self.identifier)
                if error:
                    self._release()
                    detail = (
                        "already assigned"
                        if error == 1409
                        else f"unavailable (Windows error {error})"
                    )
                    self._report(
                        "",
                        f"{choice} is {detail}. Choose Ctrl+F12 in Settings or use the tray icon.",
                    )
                else:
                    self._report("registered", f"{choice} is registered with Windows.")
                return
            self.chord = _F12Chord(v for v in TRACKED if self.api.down(v))
            try:
                raw = self.api.start_raw(self.hwnd)
            except OSError:
                raw = False
            if raw:
                self._report("raw", f"{choice} is active through Windows keyboard events.")
            else:
                self._start_sampling()
        except (OSError, RuntimeError) as exc:
            self._release()
            self._report("", f"{choice} could not start. Open ViberHive from its tray icon. {exc}")

    def _release(self):
        if self.thread_stop is not None:
            self.thread_stop.set()
            if self.worker and self.worker.is_alive():
                self.worker.join(0.3)
            self.thread_stop = self.worker = None
        if self.hwnd:
            if self.mode == "registered":
                self.api.stop_registered(self.hwnd, self.identifier)
            elif self.mode == "raw":
                self.api.stop_raw(self.hwnd)
            QCoreApplication.instance().removeNativeEventFilter(self.filter)
        self.mode = ""
        self.available = False
        self.hwnd = 0
        if self.window:
            self.window.destroy()
            self.window.deleteLater()
            self.window = None

    def _start_sampling(self):
        # Python 3.12+ on Windows uses a high-resolution waitable timer for time.sleep.
        # Only sample the chosen trigger until its edge; inspect modifiers only on that edge.
        stop = threading.Event()
        generation = self.generation
        down = self.api.down
        initially_down = down(F12)

        def watch():
            held = initially_down
            while not stop.is_set():
                pressed = down(F12)
                if (
                    pressed
                    and not held
                    and (down(LEFT_CTRL) or down(RIGHT_CTRL))
                    and not any(down(k) for k in OTHER_MODIFIERS)
                ):
                    self._pulse.emit(generation)
                held = pressed
                time.sleep(0.004)

        self.thread_stop = stop
        self.worker = threading.Thread(target=watch, name="ViberHive shortcut", daemon=True)
        self.worker.start()
        self._report("sampled", "Ctrl+F12 is active through the fallback key-state monitor.")

    def _deliver(self, generation):
        if self.available and generation == self.generation:
            self.activated.emit()

    def _message(self, msg):
        if self.mode == "registered" and msg.message == WM_HOTKEY:
            if (
                msg.wParam == self.identifier
                and (msg.lParam >> 16) & 0xFFFF == G
                and (msg.lParam & 0x000F) == (MOD_CONTROL | MOD_SHIFT)
            ):
                self._pulse.emit(self.generation)
                return True
        elif self.mode == "raw" and msg.message == WM_INPUT:
            values = self.api.read_raw(msg.lParam)
            if values and self.chord.read(*values):
                self._pulse.emit(self.generation)
            # Windows' default window procedure still needs to clean up foreground WM_INPUT.
        return False
