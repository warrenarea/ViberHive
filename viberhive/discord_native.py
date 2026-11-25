"""Verify one Discord composer and paste from a disposable Windows worker process."""

import ctypes as c
import json
import ntpath
import os
import queue
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes as w
from functools import partial
from pathlib import Path

if __name__ == "__main__" and not __package__:
    # The isolated interpreter receives exactly this bundled module path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from viberhive.discord_access import Automation, AutomationError

CLIENTS = ("Discord", "DiscordPTB", "DiscordCanary")
VERSION = re.compile(r"app-(\d+(?:\.\d+){1,4})", re.IGNORECASE)
NO_CHAT = "Open the intended chat in Discord, then paste the copied item with Ctrl+V."
CHANGED = "The foreground window changed. The copied item is ready for manual paste."
CLIP_CHANGED = "The clipboard changed before Discord was ready. Automatic paste was cancelled."


class Stopped(Exception):
    pass


def known_client(path, local, programs):
    name = ntpath.basename(path).lower()
    app = next((app for app in CLIENTS if name == app.lower() + ".exe"), None)
    if not app:
        return False
    directory = ntpath.dirname(path)
    root = ntpath.dirname(directory)
    if (
        local
        and VERSION.fullmatch(ntpath.basename(directory))
        and ntpath.normcase(root) == ntpath.normcase(ntpath.join(local, app))
    ):
        return True
    for base in programs:
        if not base:
            continue
        installed = ntpath.normcase(ntpath.join(base, app))
        if ntpath.normcase(directory) == installed or (
            ntpath.normcase(root) == installed and VERSION.fullmatch(ntpath.basename(directory))
        ):
            return True
    return False


def installed_exe():
    local = os.environ.get("LOCALAPPDATA")
    programs = (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"))
    for app in CLIENTS:
        for base in (local, *programs):
            if not base:
                continue
            root = Path(base) / app
            versions = []
            for entry in root.glob("app-*"):
                match = VERSION.fullmatch(entry.name)
                exe = entry / (app + ".exe")
                if match and exe.is_file() and exe.resolve().is_relative_to(root.resolve()):
                    versions.append((tuple(map(int, match[1].split("."))), exe))
            if versions:
                return max(versions, key=lambda pair: pair[0])[1]
            plain = root / (app + ".exe")
            if base in programs and plain.is_file() and plain.resolve().parent == root.resolve():
                return plain
    return None


# The Windows ABI uses fixed-width LONG/DWORD even on x64. Define the whole union,
# including MOUSEINPUT, so sizeof(INPUT) is 40 bytes on the supported Windows x64 build.
class Keyboard(c.Structure):
    _fields_ = [
        ("vk", c.c_uint16),
        ("scan", c.c_uint16),
        ("flags", c.c_uint32),
        ("time", c.c_uint32),
        ("extra", c.c_size_t),
    ]


class Mouse(c.Structure):
    _fields_ = [
        ("x", c.c_int32),
        ("y", c.c_int32),
        ("data", c.c_uint32),
        ("flags", c.c_uint32),
        ("time", c.c_uint32),
        ("extra", c.c_size_t),
    ]


class InputData(c.Union):
    _fields_ = [("keyboard", Keyboard), ("mouse", Mouse)]


class Input(c.Structure):
    _fields_ = [("type", c.c_uint32), ("data", InputData)]


class GuiThread(c.Structure):
    _fields_ = [
        ("size", w.DWORD),
        ("flags", w.DWORD),
        ("active", w.HWND),
        ("focus", w.HWND),
        ("capture", w.HWND),
        ("menu", w.HWND),
        ("move", w.HWND),
        ("caret", w.HWND),
        ("rc", w.RECT),
    ]


def keyboard_input(vk, up=False):
    event = Input()
    event.type = 1
    event.data.keyboard.vk = vk
    event.data.keyboard.flags = (2 if up else 0) | (1 if vk == 0x23 else 0)
    return event


class Native:
    cancelled = False

    def __init__(self):
        self.u = c.WinDLL("user32", use_last_error=True)
        self.k = c.WinDLL("kernel32", use_last_error=True)
        self.callback = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

        def bind(lib, name, result, *args):
            fn = getattr(lib, name)
            fn.restype = result
            fn.argtypes = list(args)

        bind(self.k, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
        bind(
            self.k,
            "QueryFullProcessImageNameW",
            w.BOOL,
            w.HANDLE,
            w.DWORD,
            w.LPWSTR,
            c.POINTER(w.DWORD),
        )
        bind(self.k, "CloseHandle", w.BOOL, w.HANDLE)
        bind(self.u, "GetForegroundWindow", w.HWND)
        bind(self.u, "GetClipboardSequenceNumber", w.DWORD)
        bind(self.u, "GetWindowThreadProcessId", w.DWORD, w.HWND, c.POINTER(w.DWORD))
        bind(self.u, "GetClassNameW", c.c_int, w.HWND, w.LPWSTR, c.c_int)
        bind(self.u, "GetWindowTextW", c.c_int, w.HWND, w.LPWSTR, c.c_int)
        bind(self.u, "GetAncestor", w.HWND, w.HWND, w.UINT)
        bind(self.u, "WindowFromPoint", w.HWND, w.POINT)
        bind(self.u, "GetCursorPos", w.BOOL, c.POINTER(w.POINT))
        bind(self.u, "SetCursorPos", w.BOOL, c.c_int, c.c_int)
        bind(self.u, "GetWindow", w.HWND, w.HWND, w.UINT)
        bind(self.u, "GetWindowRect", w.BOOL, w.HWND, c.POINTER(w.RECT))
        bind(self.u, "GetClientRect", w.BOOL, w.HWND, c.POINTER(w.RECT))
        bind(self.u, "GetDpiForWindow", w.UINT, w.HWND)
        bind(self.u, "ClientToScreen", w.BOOL, w.HWND, c.POINTER(w.POINT))
        bind(self.u, "GetGUIThreadInfo", w.BOOL, w.DWORD, c.POINTER(GuiThread))
        bind(self.u, "ShowWindowAsync", w.BOOL, w.HWND, c.c_int)
        for name in (
            "IsWindowVisible",
            "IsWindowEnabled",
            "IsIconic",
            "SetForegroundWindow",
        ):
            bind(self.u, name, w.BOOL, w.HWND)
        bind(self.u, "EnumWindows", w.BOOL, self.callback, w.LPARAM)
        bind(self.u, "GetAsyncKeyState", c.c_short, c.c_int)
        bind(self.u, "SendInput", w.UINT, w.UINT, c.POINTER(Input), c.c_int)
        bind(self.u, "SetProcessDpiAwarenessContext", w.BOOL, c.c_void_p)
        self.u.SetProcessDpiAwarenessContext(c.c_void_p(-4))

    def process(self, hwnd):
        pid = w.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, c.byref(pid))
        return pid.value

    def executable(self, hwnd):
        proc = self.k.OpenProcess(0x1000, False, self.process(hwnd)) if hwnd else None
        if not proc:
            return ""
        try:
            path = c.create_unicode_buffer(32768)
            size = w.DWORD(len(path))
            return (
                path.value
                if self.k.QueryFullProcessImageNameW(proc, 0, path, c.byref(size))
                else ""
            )
        finally:
            self.k.CloseHandle(proc)

    @staticmethod
    def client_path(path):
        return known_client(
            path,
            os.getenv("LOCALAPPDATA"),
            (os.getenv("ProgramFiles"), os.getenv("ProgramFiles(x86)")),
        )

    def launch_window(self, hwnd):
        # A Discord splash or Squirrel updater may briefly take focus on a cold start.
        # It is permitted only while waiting. It can never receive a click or a paste.
        path = self.executable(hwnd)
        if self.client_path(path):
            return True
        local = os.getenv("LOCALAPPDATA")
        return bool(
            local
            and any(
                ntpath.normcase(path) == ntpath.normcase(ntpath.join(local, app, "Update.exe"))
                for app in CLIENTS
            )
        )

    def discord(self, hwnd):
        if not hwnd or self.u.GetAncestor(hwnd, 2) != hwnd:
            return False
        buf = c.create_unicode_buffer(120)
        return bool(
            self.u.GetClassNameW(hwnd, buf, len(buf))
            and buf.value == "Chrome_WidgetWin_1"
            and self.client_path(self.executable(hwnd))
        )

    def main_window(self, hwnd):
        if not self.discord(hwnd) or self.u.GetWindow(hwnd, 4):  # GW_OWNER excludes tool windows.
            return False
        title = c.create_unicode_buffer(512)
        self.u.GetWindowTextW(hwnd, title, len(title))
        box = w.RECT()
        if not title.value.strip() or not self.u.GetWindowRect(hwnd, c.byref(box)):
            return False
        # Electron startup/splash windows can use exactly the main window class and title.
        scale = (self.u.GetDpiForWindow(hwnd) or 96) / 96
        return bool(
            self.u.IsIconic(hwnd)
            or (box.right - box.left >= 400 * scale and box.bottom - box.top >= 280 * scale)
        )

    def target(self, previous, foreground):
        if self.main_window(foreground):
            return foreground
        if self.main_window(previous):
            return previous
        candidates = []

        def visit(hwnd, _):
            if self.main_window(hwnd):
                candidates.append((hwnd, bool(self.u.IsWindowVisible(hwnd))))
            return True

        self.u.EnumWindows(self.callback(visit), 0)
        visible = [hwnd for hwnd, shown in candidates if shown]
        targets = visible or [hwnd for hwnd, _ in candidates]
        if len(targets) > 1:
            raise Stopped(
                "More than one Discord window is open. Open ViberHive from the intended Discord chat."
            )
        return targets[0] if targets else None

    def foreground(self):
        return self.u.GetForegroundWindow() or 0

    def activate(self, hwnd, origin):
        from viberhive import windows

        windows.focus(hwnd, origin)

    def seq(self):
        return self.u.GetClipboardSequenceNumber()

    def composer_image(self, hwnd):
        from PIL import ImageGrab

        from viberhive.anchor import composer_point, locate

        if self.foreground() != hwnd:
            return None, None
        area = w.RECT()
        origin = w.POINT(0, 0)
        if not self.u.GetClientRect(hwnd, c.byref(area)) or not self.u.ClientToScreen(
            hwnd, c.byref(origin)
        ):
            return None, None
        width, height = area.right, area.bottom
        start = round(width * 0.42)
        top = origin.y + round(height * 0.52)
        if width < 200 or height < 150:
            return None, None
        scale = max(0.5, (self.u.GetDpiForWindow(hwnd) or 96) / 96)
        try:
            pixels = ImageGrab.grab(
                bbox=(origin.x, top, origin.x + width, origin.y + height),
                all_screens=True,
            ).convert("RGB")
            hit = locate(pixels.crop((start, 0, width, pixels.height)), scale)
            if hit and self.foreground() == hwnd:
                arrow = (start + hit[0], hit[1], hit[2])
                point = composer_point(pixels, arrow)
                return (
                    (origin.x + arrow[0], top + arrow[1], arrow[2]),
                    (origin.x + point[0], top + point[1]) if point else None,
                )
        except (OSError, ValueError):
            pass
        return None, None

    def window_at(self, hwnd, point):
        hit = self.u.WindowFromPoint(w.POINT(*point))
        return bool(hit and self.u.GetAncestor(hit, 2) == hwnd)

    def click_field(self, hwnd, point, seq):
        guard(self, seq, {hwnd})
        if self.modifiers_down() or not self.window_at(hwnd, point):
            raise Stopped(NO_CHAT)
        previous = w.POINT()
        if not self.u.GetCursorPos(c.byref(previous)) or not self.u.SetCursorPos(*point):
            raise Stopped("Windows could not point to the Discord message field.")
        current = w.POINT()
        try:
            guard(self, seq, {hwnd})
            if (
                not self.u.GetCursorPos(c.byref(current))
                or (current.x, current.y) != point
                or self.modifiers_down()
                or not self.window_at(hwnd, point)
            ):
                raise Stopped(NO_CHAT)
            click = (Input * 2)()
            click[0].data.mouse.flags = 0x0002  # MOUSEEVENTF_LEFTDOWN
            click[1].data.mouse.flags = 0x0004  # MOUSEEVENTF_LEFTUP
            count = self.u.SendInput(2, click, c.sizeof(Input))
            if count != 2:
                if count == 1:
                    self.u.SendInput(1, c.byref(click[1]), c.sizeof(Input))
                raise Stopped("Windows did not complete the click in the Discord message field.")
            # Let the renderer dispatch the click before restoring the pointer.
            time.sleep(0.09)
        finally:
            if self.u.GetCursorPos(c.byref(current)) and (current.x, current.y) == point:
                self.u.SetCursorPos(previous.x, previous.y)

    def keyboard_ready(self, hwnd):
        tid = self.u.GetWindowThreadProcessId(hwnd, None)
        info = GuiThread()
        info.size = c.sizeof(info)
        return (
            tid
            and self.u.IsWindowEnabled(hwnd)
            and self.u.GetGUIThreadInfo(tid, c.byref(info))
            and not info.flags & 0x1E
            and info.focus
            and self.u.GetAncestor(info.focus, 2) == hwnd
        )

    def modifiers_down(self):
        return any(
            self.u.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 0x10, 0x11, 0x12, 0x5B, 0x5C)
        )

    def paste(self):
        # Ctrl+End collapses any selection at the end of the current draft. A paste
        # adds content there without replacing selected text. No Enter is generated.
        keys = [
            (0x11, False),
            (0x23, False),
            (0x23, True),
            (0x56, False),
            (0x56, True),
            (0x11, True),
        ]
        batch = (Input * len(keys))(*(keyboard_input(vk, up) for vk, up in keys))
        sent = self.u.SendInput(len(batch), batch, c.sizeof(Input))
        if sent != len(keys):
            held = set()
            for vk, up in keys[:sent]:
                if up:
                    held.discard(vk)
                else:
                    held.add(vk)
            if held:
                release = (Input * len(held))(*(keyboard_input(key, True) for key in held))
                self.u.SendInput(len(release), release, c.sizeof(Input))
            raise Stopped(
                "Windows did not complete the paste shortcut. Check the draft before retrying."
            )


def guard(native, seq, allowed, *, current=None):
    if native.cancelled is True:
        raise Stopped("The draft transfer was cancelled.")
    if seq is not None and (not seq or native.seq() != seq):
        raise Stopped(CLIP_CHANGED)
    if (native.foreground() if current is None else current) not in allowed:
        raise Stopped(CHANGED)


def select_and_paste(native, hwnd, pid, seq, deadline):
    while time.monotonic() < deadline:
        guard(native, seq, {hwnd})
        if native.modifiers_down():
            time.sleep(0.04)
            continue
        arrow, point = native.composer_image(hwnd)
        if point:
            guard(native, seq, {hwnd})
            native.click_field(hwnd, point, seq)
            deliver(native, hwnd, pid, seq, partial(native.window_at, hwnd, point))
            return
        try:
            with Automation() as automation:
                editors = automation.editors(hwnd)
                element = automation.choose(editors, arrow)
                if element:
                    rect = next(rect for candidate, _, rect in editors if candidate is element)
                    if not automation.exposed(element, rect):
                        raise Stopped(NO_CHAT)
                    if not automation.focused(element):
                        left, top, right, bottom = rect
                        guard(native, seq, {hwnd})
                        native.click_field(hwnd, ((left + right) // 2, (top + bottom) // 2), seq)
                        if not automation.focused(element):
                            guard(native, seq, {hwnd})
                            automation.set_focus(element)
                    deliver(native, hwnd, pid, seq, partial(automation.focused, element))
                    return
        except (AutomationError, OSError):
            # Electron accessibility can be unavailable while Discord is starting.
            # The next pass still tries the independent screenshot target.
            pass
        time.sleep(0.24)
    raise Stopped(NO_CHAT)


def deliver(native, hwnd, pid, seq, field_ready):
    until = time.monotonic() + 1.15
    while time.monotonic() < until:
        guard(native, seq, {hwnd})
        if field_ready() and not native.modifiers_down():
            break
        time.sleep(0.04)
    if not native.keyboard_ready(hwnd):
        raise Stopped(NO_CHAT)
    if native.modifiers_down() or native.process(hwnd) != pid or not native.discord(hwnd):
        raise Stopped(
            "Discord or the keyboard state changed. The copied item is ready for manual paste."
        )
    if not field_ready():
        raise Stopped(NO_CHAT)
    guard(native, seq, {hwnd})
    native.paste()


def present(native, origin, previous, owner, seq=None):
    """Start or restore the desktop client and wait until its main window is on screen."""
    guard(native, seq, {origin})
    target = native.target(previous, origin)
    if origin != owner and target != origin:
        raise Stopped(CHANGED)
    launched = target is None
    if launched:
        exe = installed_exe()
        if not exe:
            raise Stopped(
                "Discord Desktop could not be found. Open Discord to paste the copied item."
            )
        guard(native, seq, {origin})
        subprocess.Popen(
            [str(exe)],
            cwd=exe.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    deadline = time.monotonic() + (40 if launched else 5)
    next_activation = 0
    while time.monotonic() < deadline:
        current = native.foreground()
        allowed = {origin, target, 0}
        if launched and current not in allowed and native.launch_window(current):
            allowed.add(current)
        guard(native, seq, allowed, current=current)
        if launched:
            candidate = native.target(previous, current)
            if candidate != target:
                target = candidate
                next_activation = 0
        if target:
            iconic = native.u.IsIconic(target)
            visible = native.u.IsWindowVisible(target)
            if current == target and visible and not iconic:
                return target, native.process(target), launched
            if time.monotonic() >= next_activation:
                if iconic or not visible:
                    native.u.ShowWindowAsync(target, 9 if iconic else 5)  # SW_RESTORE / SW_SHOW
                elif current:
                    native.activate(target, current)
                elif not native.foreground():
                    native.u.SetForegroundWindow(target)
                next_activation = time.monotonic() + 0.35
        time.sleep(0.04 if target else 0.12)
    if not target:
        raise Stopped("Discord started, but its desktop chat window did not appear in time.")
    raise Stopped(
        "Windows did not bring the Discord chat to the front. Select its window to paste manually."
    )


def finish_draft(native, window, seq):
    hwnd, pid, launched = window
    guard(native, seq, {hwnd})
    if native.process(hwnd) != pid or not native.discord(hwnd):
        raise Stopped(
            "Discord or the keyboard state changed. The copied item is ready for manual paste."
        )
    select_and_paste(native, hwnd, pid, seq, time.monotonic() + (25 if launched else 7))


def dispatch(native, origin, previous, owner, seq):
    window = present(native, origin, previous, owner, seq)
    finish_draft(native, window, seq)


def receive_clipboard(native, mailbox):
    """EOF cancels an in-progress opening or a transfer even through a venv redirector."""
    try:
        line = b""
        while len(line) < 48 and not line.endswith(b"\n"):
            chunk = os.read(0, 1)
            if not chunk:
                return
            line += chunk
        mailbox.put(line.strip())
        os.read(0, 1)
    except OSError:
        pass
    finally:
        native.cancelled = True
        if mailbox.empty():
            mailbox.put_nowait(b"")


if __name__ == "__main__":
    # A Windows venv redirector can have a different PID from the actual worker.
    print(os.getpid(), flush=True)
    if sys.stdin.buffer.readline(16).strip() != b"open":
        sys.exit(0)
    try:
        native = Native()
        mailbox = queue.Queue(maxsize=1)
        threading.Thread(target=receive_clipboard, args=(native, mailbox), daemon=True).start()
        window = present(native, *(int(value) for value in sys.argv[1:4]))
        print("ready", flush=True)
        command = mailbox.get(timeout=180)
        match = re.fullmatch(rb"paste ([1-9][0-9]{0,9})", command)
        if not match:
            raise Stopped("The draft transfer was cancelled.")
        finish_draft(native, window, int(match[1]))
        answer = [
            True,
            "Paste shortcut delivered to Discord. Review the draft and press Enter to send.",
        ]
    except (Stopped, AutomationError) as e:
        answer = [False, str(e)]
    except (OSError, ValueError, queue.Empty):
        answer = [
            False,
            "Windows could not prepare the Discord chat. Use Ctrl+V to paste the copied item.",
        ]
    print(json.dumps(answer), flush=True)
