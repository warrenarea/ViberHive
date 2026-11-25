"""Create the project launcher as a Unicode Windows Shell Link (MS-SHLLINK)."""

import os
import struct
import tempfile
from pathlib import Path, PureWindowsPath
from uuid import UUID

APP_USER_MODEL_ID = "ViberHive.Desktop.Picker"


def _counted_string(value):
    encoded = value.encode("utf-16-le")
    return struct.pack("<H", len(encoded) // 2) + encoded


def _sized_block(body):
    return struct.pack("<I", len(body) + 4) + body


def _app_id_property():
    # MS-PROPSTORE: PKEY_AppUserModel_ID (PID 5), stored as VT_LPWSTR.
    text = (APP_USER_MODEL_ID + "\0").encode("utf-16-le")
    value = struct.pack("<HHI", 31, 0, len(text) // 2) + text
    value += bytes(-len(value) % 4)
    prop = _sized_block(struct.pack("<IB", 5, 0) + value)
    fmtid = UUID("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3").bytes_le
    storage = _sized_block(b"1SPS" + fmtid + prop + bytes(4))
    return _sized_block(struct.pack("<I", 0xA0000009) + storage + bytes(4))


def shortcut_bytes(folder):
    # An EnvironmentVariableDataBlock resolves the real system command prompt,
    # including on systems where Windows is not installed on drive C.
    target = r"%SystemRoot%\System32\cmd.exe"
    target_data = struct.pack(
        "<II260s520s", 0x314, 0xA0000001, target.encode("ascii"), target.encode("utf-16-le")
    )
    # Name, working directory, arguments, icon, Unicode, expanded target, prefer environment path.
    flags = 0x04 | 0x10 | 0x20 | 0x40 | 0x80 | 0x200 | 0x02000000
    clsid = UUID("00021401-0000-0000-c000-000000000046").bytes_le
    header = struct.pack("<I16sII32xI12x", 76, clsid, flags, 0, 1)
    fields = (
        "Open ViberHive",
        str(folder),
        r"/d /v:off /c .\Run_ViberHive.cmd",
        str(folder / "assets" / "viberhive.ico"),
    )
    return header + b"".join(map(_counted_string, fields)) + target_data + _app_id_property() + bytes(4)


def write_shortcut(project):
    data = shortcut_bytes(PureWindowsPath(project))
    fd, temp = tempfile.mkstemp(dir=project, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temp, project / "ViberHive.lnk")
    finally:
        Path(temp).unlink(missing_ok=True)


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("Run this shortcut setup from Windows.")
    write_shortcut(Path(__file__).resolve().parents[1])
