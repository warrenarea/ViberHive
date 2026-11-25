"""Child process: stdin is untrusted raster data, stdout is one freshly encoded image."""

import ctypes
import os
import struct
import sys
import warnings
from io import BytesIO

MIB = 1024**2
MAX_FRAMES = 320
MAX_FRAME_PIXELS = 6_000_000
MAX_TOTAL_PIXELS = 40_000_000


def lower_integrity(kernel):
    from ctypes import wintypes as w

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)

    class Label(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", w.DWORD)]

    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.AdjustTokenPrivileges.argtypes = [
        w.HANDLE,
        w.BOOL,
        ctypes.c_void_p,
        w.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    advapi.ConvertStringSidToSidW.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi.GetLengthSid.restype = w.DWORD
    advapi.SetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    token, sid = w.HANDLE(), ctypes.c_void_p()
    try:
        if not advapi.OpenProcessToken(
            kernel.GetCurrentProcess(), 0x80 | 0x20 | 0x8, ctypes.byref(token)
        ):
            raise RuntimeError("Could not lower the Windows decoder integrity level.")
        if not advapi.ConvertStringSidToSidW("S-1-16-4096", ctypes.byref(sid)):
            raise RuntimeError("Could not lower the Windows decoder integrity level.")
        label = Label(sid, 0x20)
        if not (
            advapi.AdjustTokenPrivileges(token, True, None, 0, None, None)
            and advapi.SetTokenInformation(
                token, 25, ctypes.byref(label), ctypes.sizeof(label) + advapi.GetLengthSid(sid)
            )
        ):
            raise RuntimeError("Could not lower the Windows decoder integrity level.")
    finally:
        if token:
            kernel.CloseHandle(token)
        if sid:
            kernel.LocalFree(sid)


def resource_limit():
    if os.name != "nt":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (560 * MIB, 560 * MIB))
        return
    from ctypes import wintypes as w

    u = ctypes.c_size_t

    class Basic(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_longlong),
            ("job_time", ctypes.c_longlong),
            ("flags", w.DWORD),
            ("working_min", u),
            ("working_max", u),
            ("active", w.DWORD),
            ("affinity", u),
            ("priority", w.DWORD),
            ("scheduling", w.DWORD),
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("basic", Basic),
            ("io", ctypes.c_ulonglong * 6),
            ("process_memory", u),
            ("job_memory", u),
            ("peak_process", u),
            ("peak_job", u),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.GetCurrentProcess.restype = w.HANDLE
    job = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags = 0x100 | 0x8  # per-process memory and active process limit
    limits.basic.active = 1
    limits.process_memory = 560 * MIB
    if not (
        job
        and kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
        and kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess())
    ):
        raise RuntimeError("Could not apply the Windows decoder limits.")
    lower_integrity(kernel)


class CappedIO(BytesIO):
    def write(self, data):
        if self.tell() + len(data) > 24 * MIB:
            raise ValueError("The rebuilt file exceeds 24 MiB.")
        return super().write(data)


def poster_png(frame):
    image = frame.convert("RGBA")
    image.thumbnail((360, 250))
    image.info.clear()
    output = BytesIO()
    image.save(output, "PNG", compress_level=4)
    return output.getvalue()


def decode(raw, mode, publish_frame=None):
    from PIL import Image

    warnings.simplefilter("error", Image.DecompressionBombWarning)
    Image.MAX_IMAGE_PIXELS = MAX_FRAME_PIXELS
    if mode not in ("gif", "poster", "gif_with_poster"):
        raise ValueError("Unknown output format.")
    kinds = ("GIF", "WEBP", "JPEG", "PNG") if mode == "poster" else ("GIF", "WEBP")
    with Image.open(BytesIO(raw), formats=kinds) as src:
        w, h = src.size
        if not w or not h or max(w, h) > 4096 or w * h > MAX_FRAME_PIXELS:
            raise ValueError("The image exceeds the pixel limit.")
        out = CappedIO()
        if mode == "poster":
            return poster_png(src)
        else:
            frames, delays = [], []
            loop = src.info.get("loop")
            opaque = True
            for i in range(MAX_FRAMES + 1):
                try:
                    src.seek(i)
                except EOFError:
                    break
                if src.size != (w, h) or i >= MAX_FRAMES or (i + 1) * w * h > MAX_TOTAL_PIXELS:
                    raise ValueError("The animation exceeds the frame or pixel budget.")
                frame = src.convert("RGBA")
                opaque = opaque and frame.getchannel("A").getextrema() == (255, 255)
                frame.info.clear()
                if i == 0 and mode == "gif_with_poster" and publish_frame is not None:
                    publish_frame(poster_png(frame))
                frames.append(frame)
                delays.append(max(20, min(655350, int(src.info.get("duration", 100)))))
            if len(frames) < 2:
                raise ValueError("This result is a still image.")
            if opaque:
                # Pillow's RGB quantizer retains detail from opaque source palettes.
                for i in range(len(frames)):
                    frames[i] = frames[i].convert("RGB")
            args = {"loop": max(0, min(65535, loop))} if type(loop) is int else {}
            frames[0].save(
                out,
                "GIF",
                save_all=True,
                append_images=frames[1:],
                duration=delays,
                disposal=2,
                optimize=False,
                **args,
            )
            if mode == "gif_with_poster" and publish_frame is None:
                poster = poster_png(frames[0])
                return struct.pack(">I", len(poster)) + poster + out.getvalue()
        return out.getvalue()


def write_first_frame(png):
    sys.stdout.buffer.write(struct.pack(">I", len(png)) + png)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        resource_limit()
        blob = sys.stdin.buffer.read(24 * MIB + 1)
        if len(blob) > 24 * MIB:
            raise ValueError("The download exceeds 24 MiB.")
        sys.stdout.buffer.write(decode(blob, sys.argv[1], write_first_frame))
    except Exception as exc:  # noqa: BLE001 -- untrusted input ends at this process boundary.
        # Only our own fixed validation messages may go back to the parent.
        if type(exc) in (ValueError, RuntimeError) and str(exc) in (
            "This result is a still image.",
            "The animation exceeds the frame or pixel budget.",
            "The image exceeds the pixel limit.",
            "The rebuilt file exceeds 24 MiB.",
            "Could not apply the Windows decoder limits.",
            "Could not lower the Windows decoder integrity level.",
        ):
            sys.stderr.write(str(exc))
        sys.exit(2)
