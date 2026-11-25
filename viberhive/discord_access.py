"""Small Windows UI Automation client for the Discord composer (desktop worker only).

The method slots are from Microsoft's UIAutomationClient.h. All interfaces live and
are released in the same worker apartment. No message contents are requested.
"""

import ctypes as c
import re
import uuid
from ctypes import wintypes as w

PTR = c.c_void_p
INT = c.c_int32
EDIT = 50004
DOCUMENT = 50030


class AutomationError(Exception):
    pass


class VariantData(c.Union):
    # VARIANT's largest member is a record with two pointers on Win64.
    _fields_ = [("integer", INT), ("record", PTR * 2)]


class Variant(c.Structure):
    _fields_ = [("vt", c.c_uint16), ("reserved", c.c_uint16 * 3), ("data", VariantData)]


def invoke(obj, slot, types, *args):
    table = c.cast(obj, c.POINTER(c.POINTER(PTR))).contents
    fn = c.WINFUNCTYPE(INT, PTR, *types)(table[slot])
    hr = fn(obj, *args)
    if hr < 0:
        raise AutomationError("Windows accessibility could not read the Discord composer.")
    return hr


def composer_name(name):
    return bool(
        re.match(r"^(?:(?:send a )?message\s+(?:to\s+)?[#@]\S)", name.strip(), re.IGNORECASE)
    )


def channel_editor(role, attributes, name, help_text):
    """Unlabelled and generic multiline editors need a matched send arrow as well."""
    labels = f"{name} {help_text}"
    if re.search(r"\b(?:edit|editing|search|rename)\b", labels, re.IGNORECASE):
        return False
    if role != "textbox" or "multiline=true" not in attributes:
        return False
    if re.search(r"(?:^|\s)[#@]\S", labels):
        return True
    generic = {"", "message", "message input", "message field", "type a message"}
    return name.strip().casefold() in generic and help_text.strip().casefold() in generic


def arrow_row(rect, arrow):
    if not arrow:
        return False
    left, top, right, bottom = rect
    x, y, scale = arrow
    return (
        right - left >= 64 * scale
        and left + 24 * scale < x
        and -8 * scale <= x - right <= 360 * scale
        and top - 7 * scale <= y <= bottom + 7 * scale
    )


class Automation:
    def __enter__(self):
        self.refs = []
        self.ole = c.WinDLL("ole32")
        self.strings = c.WinDLL("oleaut32")
        self.ole.CoUninitialize.argtypes = []
        self.ole.CoUninitialize.restype = None
        self.strings.SysFreeString.restype = None
        self.strings.SysFreeString.argtypes = [PTR]
        self.strings.SysStringLen.argtypes = [PTR]
        self.strings.SysStringLen.restype = c.c_uint
        self.ole.CoInitializeEx.argtypes = [PTR, c.c_uint32]
        self.ole.CoInitializeEx.restype = INT
        if self.ole.CoInitializeEx(None, 0) < 0:  # MTA, in a fresh worker process.
            raise AutomationError("Windows accessibility is unavailable.")
        self.ole.CoCreateInstance.argtypes = [PTR, PTR, c.c_uint32, PTR, c.POINTER(PTR)]
        self.ole.CoCreateInstance.restype = INT
        cls = c.create_string_buffer(uuid.UUID("ff48dba4-60ef-4201-aa87-54103eef594e").bytes_le)
        iid = c.create_string_buffer(uuid.UUID("30cbe57d-d9d0-452a-ab13-7ac5ac4825ee").bytes_le)
        self.client = PTR()
        if self.ole.CoCreateInstance(cls, None, 1, iid, c.byref(self.client)) < 0:
            self.ole.CoUninitialize()
            raise AutomationError("Windows accessibility is unavailable.") from None
        self.refs.append(self.client)
        return self

    def __exit__(self, *_):
        for ref in reversed(self.refs):
            invoke(ref, 2, [])  # IUnknown::Release
        self.ole.CoUninitialize()

    def ptr(self, obj, slot, types=(), *args):
        out = PTR()
        invoke(obj, slot, [*types, c.POINTER(PTR)], *args, c.byref(out))
        if out:
            self.refs.append(out)
        return out

    @staticmethod
    def integer(obj, slot):
        out = INT()
        invoke(obj, slot, [c.POINTER(INT)], c.byref(out))
        return out.value

    def text(self, element, slot):
        bstr = PTR()
        invoke(element, slot, [c.POINTER(PTR)], c.byref(bstr))
        if not bstr:
            return ""
        try:
            return c.wstring_at(bstr, min(512, self.strings.SysStringLen(bstr)))
        finally:
            self.strings.SysFreeString(bstr)

    def equal(self, a, b):
        if not a or not b:
            return False
        out = INT()
        invoke(self.client, 3, [PTR, PTR, c.POINTER(INT)], a, b, c.byref(out))
        return bool(out.value)

    def focused(self, element):
        return self.inside(self.ptr(self.client, 8), element)

    def exposed(self, element, rect):
        left, top, right, bottom = rect
        point = w.POINT((left + right) // 2, (top + bottom) // 2)
        return self.inside(self.ptr(self.client, 7, [w.POINT], point), element)

    def inside(self, current, element):
        # A rich editor can expose a child text run at its caret or center.
        walker = self.ptr(self.client, 16)
        for _ in range(5):
            if not current:
                break
            if self.equal(current, element):
                return True
            current = self.ptr(walker, 3, [PTR], current)
        return False

    def editors(self, hwnd):
        root = self.ptr(self.client, 6, [w.HWND], hwnd)
        if not root:
            return []
        result = []
        for control in (EDIT, DOCUMENT):
            value = Variant()
            value.vt = 3  # VT_I4
            value.data.integer = control
            condition = self.ptr(self.client, 23, [INT, Variant], 30003, value)  # ControlType
            array = self.ptr(root, 6, [INT, PTR], 4, condition)  # FindAll(TreeScope_Descendants)
            if not array:
                continue
            for i in range(min(80, self.integer(array, 3))):
                editor = self.ptr(array, 4, [INT], i)
                if not editor:
                    continue
                try:
                    if (
                        not self.integer(editor, 28)
                        or self.integer(editor, 35)
                        or self.integer(editor, 38)
                    ):
                        continue  # disabled, password, offscreen
                    if not self.integer(editor, 27):
                        continue  # cannot accept keyboard input
                    role = self.text(editor, 45).casefold()
                    if role == "searchbox" or (control == DOCUMENT and role != "textbox"):
                        continue
                    name = self.text(editor, 23)
                    help_text = self.text(editor, 31)
                    attributes = self.text(editor, 46).casefold()
                    box = w.RECT()
                    invoke(editor, 43, [c.POINTER(w.RECT)], c.byref(box))
                    if box.right <= box.left or box.bottom <= box.top:
                        continue
                    semantic = composer_name(name) or composer_name(help_text)
                    # A generic or localized field is considered only beside the send arrow.
                    anchored = channel_editor(role, attributes, name, help_text)
                    if semantic or anchored:
                        result.append(
                            (editor, semantic, (box.left, box.top, box.right, box.bottom))
                        )
                except AutomationError:
                    continue
        return result

    def choose(self, editors, arrow):
        exact = [ptr for ptr, named, _ in editors if named]
        if len(exact) == 1:
            return exact[0]
        for ptr in exact:
            if self.focused(ptr):
                return ptr
        matches = [
            ptr for ptr, named, rect in editors if (named or not exact) and arrow_row(rect, arrow)
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def set_focus(element):
        invoke(element, 3, [])
