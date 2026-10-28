"""Windows-only native implementation; importing this module is portable.

Native API use is confined to Native(), allowing offline Pillow rendering and
unittest fakes on other platforms. No color key, cursor hiding, watchdog, driver,
stdin prompt, or keyboard/mouse button injection is used.
"""
from __future__ import annotations

import argparse
import ctypes as C
import json
import math
import os
from pathlib import Path
import sys
import time

PROTOCOL = 1
FRAME_SECONDS = 1 / 60
MOVE_SECONDS = 0.060
PULSE_SECONDS = 0.200
WDA_EXCLUDEFROMCAPTURE = 0x11
OVERLAY_STYLE = 0x00080000 | 0x00000020 | 0x00000008 | 0x00000080 | 0x08000000
FULLSCREEN_LIMITATION = "SHQueryUserNotificationState only reports system-wide exclusive Direct3D fullscreen; it cannot attribute that state to the target HWND or reliably identify every rendering backend. Window size alone is not evidence of exclusive fullscreen."
HOOK_LIMITATION = "Hook installation is verified, but Windows provides no reliable query for later silent removal of a low-level hook. Secure-desktop input is not supported."


try:
    from .guard_ipc import read_json, write_json
except ImportError:  # Invoked as a standalone subprocess.
    from guard_ipc import read_json, write_json


def write_stop(run_dir: Path, reason: str) -> None:
    """Preserve the first stop reason, including an external stop.py request."""
    try:
        with (run_dir / "stop.flag").open("x", encoding="utf-8") as stream:
            stream.write(reason)
    except FileExistsError:
        pass


def smooth_position(start: tuple[float, float], end: tuple[float, float], elapsed: float, duration: float = MOVE_SECONDS) -> tuple[float, float]:
    fraction = max(0.0, min(1.0, elapsed / duration))
    fraction = fraction * fraction * (3 - 2 * fraction)
    return tuple(a + (b - a) * fraction for a, b in zip(start, end))


def fullscreen_info(api, window_id: int | None) -> dict:
    state = api.notification_state()
    return {"api": "SHQueryUserNotificationState", "notification_state": state, "exclusive_detected": state == 3, "target_window": window_id, "target_exclusive": None, "limitation": FULLSCREEN_LIMITATION}


class Renderer:
    """Small antialiased RGBA layers; native rendering uploads only dirty patches."""

    def __init__(self, width: int, height: int, cursor_size: int = 32):
        from PIL import Image, ImageDraw, ImageFont
        self.Image, self.ImageDraw = Image, ImageDraw
        self.width, self.height = width, height
        self.cursor_size = max(16, cursor_size)
        self.font = self.small_font = None
        fonts = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf")]
        for font in fonts:
            try:
                self.font = ImageFont.truetype(str(font), 19)
                self.small_font = ImageFont.truetype(str(font), 16)
                break
            except OSError:
                continue
        if self.font is None:
            if sys.platform == "win32":
                raise RuntimeError("No Chinese font is available for the foreground guard.")
            self.font = ImageFont.load_default(size=19)
            self.small_font = ImageFont.load_default(size=16)
        self._capsule_cache = {}
        self.arrow = self._arrow()

    def _arrow(self):
        size = round(self.cursor_size * 3.5)
        scale = 3
        image = self.Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
        draw = self.ImageDraw.Draw(image)
        # Origin is the arrow tip. All geometry extends right/down from the hotspot.
        points = [(0, 0), (0, 0.84), (0.23, 0.64), (0.42, 1), (0.58, 0.92), (0.39, 0.56), (0.72, 0.56)]
        polygon = [(round(x * (size - 5) * scale + 2 * scale), round(y * (size - 5) * scale + 2 * scale)) for x, y in points]
        shadow = [(x + 2 * scale, y + 3 * scale) for x, y in polygon]
        draw.polygon(shadow, fill=(14, 35, 62, 90))
        draw.polygon(polygon, fill=(145, 209, 255, 205))
        draw.line(polygon + polygon[:1], fill=(255, 255, 255, 255), width=4 * scale, joint="curve")
        return image.resize((size, size), self.Image.Resampling.LANCZOS)

    def _capsule(self, label: str, countdown: float | None, flash: bool):
        secondary = f"{max(1, math.ceil(countdown))} 秒后开始 · {label}" if countdown is not None else label
        key = secondary, flash
        if key in self._capsule_cache:
            return self._capsule_cache[key]
        width = min(660, max(180, self.width - 24))
        image = self.Image.new("RGBA", (width, 80))
        draw = self.ImageDraw.Draw(image)
        draw.rounded_rectangle((1, 1, width - 2, 77), radius=24, fill=(20, 40, 70, 244), outline=(132, 204, 255, 255) if flash else (66, 153, 245, 225), width=3 if flash else 1)
        title = "AI 操控中 · 按 ESC 中止"
        title_width = draw.textlength(title, font=self.font)
        title_x = max(28, (width - title_width) / 2)
        draw.ellipse((title_x - 18, 22, title_x - 10, 30), fill=(94, 192, 255, 255))
        draw.text((title_x, 12), title, font=self.font, fill=(255, 255, 255, 255))
        while secondary and draw.textlength(secondary, font=self.small_font) > width - 38:
            secondary = secondary[:-2].rstrip("…") + "…" if len(secondary) > 2 else ""
        text_width = draw.textlength(secondary, font=self.small_font)
        draw.text(((width - text_width) / 2, 45), secondary, font=self.small_font, fill=(209, 230, 253, 255))
        self._capsule_cache = {key: image}
        return image

    def layers(self, now: float, cursor: tuple[float, float], label: str, countdown: float | None = None, pulse_elapsed: float | None = None, capsule_center: float | None = None, capsule_top: int = 12) -> list:
        alpha = round(155 + 80 * (0.5 + 0.5 * math.sin(math.tau * now / 2)))
        color = (45, 137, 239, alpha)
        w, h = self.width, self.height
        layers = [(0, 0, self.Image.new("RGBA", (w, 3), color)), (0, h - 3, self.Image.new("RGBA", (w, 3), color)), (0, 3, self.Image.new("RGBA", (3, max(1, h - 6)), color)), (w - 3, 3, self.Image.new("RGBA", (3, max(1, h - 6)), color))]
        capsule = self._capsule(label, countdown, countdown is not None and countdown > 1.72)
        center = w / 2 if capsule_center is None else capsule_center
        layers.append((round(center - capsule.width / 2), capsule_top, capsule))
        x, y = round(cursor[0]), round(cursor[1])
        if pulse_elapsed is not None and 0 <= pulse_elapsed < PULSE_SECONDS:
            progress = pulse_elapsed / PULSE_SECONDS
            radius = 28 * (1 - progress) + 4
            image = self.Image.new("RGBA", (70 * 3, 70 * 3))
            self.ImageDraw.Draw(image).ellipse(tuple(round(v * 3) for v in (35 - radius, 35 - radius, 35 + radius, 35 + radius)), outline=(172, 223, 255, round(245 * (1 - progress))), width=3 * 3)
            layers.append((x - 35, y - 35, image.resize((70, 70), self.Image.Resampling.LANCZOS)))
        # The arrow's geometric tip is (2, 2) in the sprite, hence this exact offset.
        layers.append((x - 2, y - 2, self.arrow))
        return layers

    def frame(self, *args, **kwargs):
        image = self.Image.new("RGBA", (self.width, self.height))
        for x, y, layer in self.layers(*args, **kwargs):
            image.alpha_composite(layer, (x, y))
        return image


def dirty_regions(previous: list, current: list, width: int, height: int) -> list[tuple[int, int, int, int]]:
    """Merge overlapping sprite regions, retaining narrow full-screen border strips."""
    regions = []
    for x, y, image in previous + current:
        rect = (max(0, x), max(0, y), min(width, x + image.width), min(height, y + image.height))
        if rect[0] >= rect[2] or rect[1] >= rect[3]:
            continue
        # Do not merge merely touching edges: that would make every frame full-screen.
        index = 0
        while index < len(regions):
            other = regions[index]
            if rect[0] < other[2] and rect[2] > other[0] and rect[1] < other[3] and rect[3] > other[1]:
                rect = (min(rect[0], other[0]), min(rect[1], other[1]), max(rect[2], other[2]), max(rect[3], other[3]))
                regions.pop(index)
                index = 0
            else:
                index += 1
        regions.append(rect)
    return regions


class Native:
    """All pointer-sized signatures and callback references live as long as the HWND/hook."""

    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("The native foreground guard requires Windows.")
        from ctypes import wintypes as W
        self.W = W
        self.user = C.WinDLL("user32", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.gdi = C.WinDLL("gdi32", use_last_error=True)
        self.shell = C.WinDLL("shell32", use_last_error=True)
        self.dwm = C.WinDLL("dwmapi", use_last_error=True)
        self.WNDPROC = C.WINFUNCTYPE(C.c_ssize_t, W.HWND, W.UINT, C.c_size_t, C.c_ssize_t)
        self.HOOKPROC = C.WINFUNCTYPE(C.c_ssize_t, C.c_int, C.c_size_t, C.c_ssize_t)

        class WNDCLASS(C.Structure):
            _fields_ = [("style", W.UINT), ("lpfnWndProc", self.WNDPROC), ("cbClsExtra", C.c_int), ("cbWndExtra", C.c_int), ("hInstance", W.HINSTANCE), ("hIcon", W.HICON), ("hCursor", W.HANDLE), ("hbrBackground", W.HBRUSH), ("lpszMenuName", W.LPCWSTR), ("lpszClassName", W.LPCWSTR)]

        class KEYBOARD(C.Structure):
            _fields_ = [("vkCode", W.DWORD), ("scanCode", W.DWORD), ("flags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", C.c_size_t)]

        class MOUSE(C.Structure):
            _fields_ = [("pt", W.POINT), ("mouseData", W.DWORD), ("flags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", C.c_size_t)]

        self.MOUSE = MOUSE
        self.mouse_hook = None
        self.pulse_target = None
        self.click_started = None

        class BITMAPINFOHEADER(C.Structure):
            _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD), ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD), ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD), ("biClrImportant", W.DWORD)]

        class BITMAPINFO(C.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", W.DWORD * 3)]

        class BLENDFUNCTION(C.Structure):
            _fields_ = [("BlendOp", C.c_ubyte), ("BlendFlags", C.c_ubyte), ("SourceConstantAlpha", C.c_ubyte), ("AlphaFormat", C.c_ubyte)]

        class MONITORINFO(C.Structure):
            _fields_ = [("cbSize", W.DWORD), ("rcMonitor", W.RECT), ("rcWork", W.RECT), ("dwFlags", W.DWORD)]

        self.WNDCLASS, self.KEYBOARD, self.BITMAPINFO, self.BLEND, self.MONITORINFO = WNDCLASS, KEYBOARD, BITMAPINFO, BLENDFUNCTION, MONITORINFO
        self.hwnd = self.hook = self.dc = self.bitmap = self.old_bitmap = self.parent = None
        self.prompt = None
        self.class_name = f"ComputerUseGuard_{os.getpid()}_{id(self)}"
        self.registered = False
        self.failure = None
        self.escape_requested = False
        self.confirmed = None
        self.previous_layers = []
        self.bits = C.c_void_p()
        self._bind()
        self.instance = self.kernel.GetModuleHandleW(None)
        if not self.instance:
            self.fail("GetModuleHandleW")
        # Strong references are retained until after Unhook/Destroy/Unregister.
        self.window_callback = self.WNDPROC(self._wndproc)
        self.hook_callback = self.HOOKPROC(self._hookproc)
        self.mouse_callback = self.HOOKPROC(self._mouseproc)
        self.stop_writer = None
        self.swallow_escape = True

    def _bind(self):
        W = self.W
        bindings = [
            (self.kernel, "GetModuleHandleW", [W.LPCWSTR], W.HMODULE),
            (self.kernel, "OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            (self.kernel, "WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD),
            (self.kernel, "CloseHandle", [W.HANDLE], W.BOOL),
            (self.user, "RegisterClassW", [C.POINTER(self.WNDCLASS)], W.ATOM),
            (self.user, "UnregisterClassW", [W.LPCWSTR, W.HINSTANCE], W.BOOL),
            (self.user, "CreateWindowExW", [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD, C.c_int, C.c_int, C.c_int, C.c_int, W.HWND, W.HMENU, W.HINSTANCE, C.c_void_p], W.HWND),
            (self.user, "DestroyWindow", [W.HWND], W.BOOL),
            (self.user, "DefWindowProcW", [W.HWND, W.UINT, C.c_size_t, C.c_ssize_t], C.c_ssize_t),
            (self.user, "SetWindowDisplayAffinity", [W.HWND, W.DWORD], W.BOOL),
            (self.user, "GetWindowDisplayAffinity", [W.HWND, C.POINTER(W.DWORD)], W.BOOL),
            (self.user, "SetWindowsHookExW", [C.c_int, self.HOOKPROC, W.HINSTANCE, W.DWORD], W.HHOOK),
            (self.user, "UnhookWindowsHookEx", [W.HHOOK], W.BOOL),
            (self.user, "CallNextHookEx", [W.HHOOK, C.c_int, C.c_size_t, C.c_ssize_t], C.c_ssize_t),
            (self.user, "PeekMessageW", [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT], W.BOOL),
            (self.user, "TranslateMessage", [C.POINTER(W.MSG)], W.BOOL),
            (self.user, "DispatchMessageW", [C.POINTER(W.MSG)], C.c_ssize_t),
            (self.user, "MsgWaitForMultipleObjectsEx", [W.DWORD, C.POINTER(W.HANDLE), W.DWORD, W.DWORD, W.DWORD], W.DWORD),
            (self.user, "GetCursorPos", [C.POINTER(W.POINT)], W.BOOL),
            (self.user, "SetCursorPos", [C.c_int, C.c_int], W.BOOL),
            (self.user, "GetSystemMetrics", [C.c_int], C.c_int),
            (self.user, "ShowWindow", [W.HWND, C.c_int], W.BOOL),
            (self.user, "SetWindowPos", [W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT], W.BOOL),
            (self.user, "SetForegroundWindow", [W.HWND], W.BOOL),
            (self.user, "IsWindow", [W.HWND], W.BOOL),
            (self.user, "GetForegroundWindow", [], W.HWND),
            (self.user, "MonitorFromWindow", [W.HWND, W.DWORD], W.HMONITOR),
            (self.user, "GetMonitorInfoW", [W.HMONITOR, C.POINTER(self.MONITORINFO)], W.BOOL),
            (self.user, "GetDC", [W.HWND], W.HDC),
            (self.user, "ReleaseDC", [W.HWND, W.HDC], C.c_int),
            (self.user, "UpdateLayeredWindow", [W.HWND, W.HDC, C.POINTER(W.POINT), C.POINTER(W.SIZE), W.HDC, C.POINTER(W.POINT), W.DWORD, C.POINTER(self.BLEND), W.DWORD], W.BOOL),
            (self.user, "SetThreadDpiAwarenessContext", [C.c_void_p], C.c_void_p),
            (self.gdi, "CreateCompatibleDC", [W.HDC], W.HDC),
            (self.gdi, "DeleteDC", [W.HDC], W.BOOL),
            (self.gdi, "CreateDIBSection", [W.HDC, C.POINTER(self.BITMAPINFO), W.UINT, C.POINTER(C.c_void_p), W.HANDLE, W.DWORD], W.HBITMAP),
            (self.gdi, "SelectObject", [W.HDC, W.HGDIOBJ], W.HGDIOBJ),
            (self.gdi, "DeleteObject", [W.HGDIOBJ], W.BOOL),
            (self.gdi, "GetStockObject", [C.c_int], W.HGDIOBJ),
            (self.user, "SendMessageW", [W.HWND, W.UINT, C.c_size_t, C.c_ssize_t], C.c_ssize_t),
            (self.shell, "SHQueryUserNotificationState", [C.POINTER(C.c_int)], C.c_long),
            (self.dwm, "DwmIsCompositionEnabled", [C.POINTER(W.BOOL)], C.c_long),
        ]
        for library, name, arguments, result in bindings:
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result

    @staticmethod
    def fail(name: str):
        raise OSError(C.get_last_error(), f"{name} failed: {C.FormatError(C.get_last_error())}")

    def _wndproc(self, hwnd, message, wparam, lparam):
        try:
            if hwnd == self.prompt:
                if message == 0x0111 and (wparam & 0xFFFF) in (1, 2):  # WM_COMMAND
                    self.confirmed = (wparam & 0xFFFF) == 1
                    return 0
                if message == 0x0010:  # WM_CLOSE
                    self.confirmed = False
                    return 0
                if message == 0x0100 and wparam == 0x1B:
                    self.escape_requested = True
                    return 0
            elif hwnd == self.hwnd:
                if message == 0x0084:  # WM_NCHITTEST / HTTRANSPARENT
                    return -1
                if message == 0x0021:  # WM_MOUSEACTIVATE / MA_NOACTIVATE
                    return 3
            return self.user.DefWindowProcW(hwnd, message, wparam, lparam)
        except BaseException as exc:
            self.failure = f"Window callback failed: {exc}"
            return 0

    def _hookproc(self, code, wparam, lparam):
        try:
            if code >= 0 and self.swallow_escape and wparam in (0x100, 0x101, 0x104, 0x105):
                event = C.cast(lparam, C.POINTER(self.KEYBOARD)).contents
                if event.vkCode == 0x1B and not (event.flags & 0x10):  # LLKHF_INJECTED is automation, not physical ESC.
                    self.escape_requested = True
                    if self.stop_writer is not None:
                        self.stop_writer()
                    return 1
        except BaseException as exc:
            self.failure = f"Keyboard callback failed: {exc}"
            return 1
        return self.user.CallNextHookEx(self.hook, code, wparam, lparam)

    def _mouseproc(self, code, wparam, lparam):
        try:
            if code >= 0 and self.pulse_target is not None and wparam in (0x201, 0x204, 0x207):
                event = C.cast(lparam, C.POINTER(self.MOUSE)).contents
                if event.flags & 1 and event.dwExtraInfo == 0x43554131 and (event.pt.x, event.pt.y) == self.pulse_target:
                    self.click_started = time.monotonic()
        except BaseException as exc:
            self.failure = f"Mouse callback failed: {exc}"
        return self.user.CallNextHookEx(self.mouse_hook, code, wparam, lparam)

    def setup(self, parent_pid: int | None = None, stop_writer=None, probe: bool = False):
        version = sys.getwindowsversion()
        if (version.major, version.build) < (10, 19041):
            raise RuntimeError("WDA_EXCLUDEFROMCAPTURE requires Windows 10 build 19041 or later.")
        self.old_dpi = self.user.SetThreadDpiAwarenessContext(C.c_void_p(-4))  # per-monitor v2
        if not self.old_dpi:
            self.fail("SetThreadDpiAwarenessContext")
        composition = self.W.BOOL()
        if self.dwm.DwmIsCompositionEnabled(C.byref(composition)) < 0 or not composition.value:
            raise RuntimeError("DWM composition is unavailable.")
        if parent_pid is not None:
            self.parent = self.kernel.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE
            if not self.parent:
                self.fail("OpenProcess(parent)")
        self.stop_writer, self.swallow_escape = stop_writer, not probe
        wc = self.WNDCLASS()
        wc.lpfnWndProc, wc.hInstance, wc.lpszClassName = self.window_callback, self.instance, self.class_name
        wc.hbrBackground = self.gdi.GetStockObject(0)  # WHITE_BRUSH for the confirmation
        if not self.user.RegisterClassW(C.byref(wc)):
            self.fail("RegisterClassW")
        self.registered = True
        self.bounds = tuple(self.user.GetSystemMetrics(metric) for metric in (76, 77, 78, 79))
        left, top, width, height = self.bounds
        if width <= 0 or height <= 0:
            raise RuntimeError("The virtual desktop is unavailable.")
        self.hwnd = self.user.CreateWindowExW(OVERLAY_STYLE, self.class_name, "Computer use guard", 0x80000000, left, top, width, height, None, None, self.instance, None)
        if not self.hwnd:
            self.fail("CreateWindowExW")
        if not self.user.SetWindowDisplayAffinity(self.hwnd, WDA_EXCLUDEFROMCAPTURE):
            self.fail("SetWindowDisplayAffinity")
        self.verify_capture()
        self.hook = self.user.SetWindowsHookExW(13, self.hook_callback, self.instance, 0)  # WH_KEYBOARD_LL
        if not self.hook:
            self.fail("SetWindowsHookExW")
        self.mouse_hook = self.user.SetWindowsHookExW(14, self.mouse_callback, self.instance, 0)  # WH_MOUSE_LL, pass-through click timing only.
        if not self.mouse_hook:
            self.fail("SetWindowsHookExW(mouse)")
        self._create_bitmap(1 if probe else width, 1 if probe else height)
        # Validate the alpha upload on a completely transparent, never-shown HWND.
        self._upload(1 if probe else width, 1 if probe else height)
        self.pump()

    def verify_capture(self):
        affinity = self.W.DWORD()
        if not self.user.GetWindowDisplayAffinity(self.hwnd, C.byref(affinity)) or affinity.value != WDA_EXCLUDEFROMCAPTURE:
            raise RuntimeError("Capture exclusion could not be verified.")

    def _create_bitmap(self, width, height):
        self.dc = self.gdi.CreateCompatibleDC(None)
        if not self.dc:
            self.fail("CreateCompatibleDC")
        info = self.BITMAPINFO()
        header = info.bmiHeader
        header.biSize, header.biWidth, header.biHeight = C.sizeof(header), width, -height
        header.biPlanes, header.biBitCount = 1, 32
        self.bitmap = self.gdi.CreateDIBSection(self.dc, C.byref(info), 0, C.byref(self.bits), None, 0)
        if not self.bitmap or not self.bits.value:
            self.fail("CreateDIBSection")
        self.old_bitmap = self.gdi.SelectObject(self.dc, self.bitmap)
        if not self.old_bitmap or self.old_bitmap == C.c_void_p(-1).value:
            self.fail("SelectObject")
        C.memset(self.bits, 0, width * height * 4)

    def _upload(self, width, height):
        position, size, source = self.W.POINT(*self.bounds[:2]), self.W.SIZE(width, height), self.W.POINT(0, 0)
        blend = self.BLEND(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA; never ULW_COLORKEY
        if not self.user.UpdateLayeredWindow(self.hwnd, None, C.byref(position), C.byref(size), self.dc, C.byref(source), 0, C.byref(blend), 2):
            self.fail("UpdateLayeredWindow")

    def present(self, renderer: Renderer, layers: list):
        width, height = self.bounds[2:]
        for left, top, right, bottom in dirty_regions(self.previous_layers, layers, width, height):
            patch = renderer.Image.new("RGBA", (right - left, bottom - top))
            for x, y, image in layers:
                if x < right and x + image.width > left and y < bottom and y + image.height > top:
                    patch.alpha_composite(image, (x - left, y - top))
            raw = patch.convert("RGBa").tobytes("raw", "BGRa")
            source = C.create_string_buffer(raw)
            stride = patch.width * 4
            for row in range(patch.height):
                C.memmove(self.bits.value + ((top + row) * width + left) * 4, C.addressof(source) + row * stride, stride)
        self.previous_layers = layers
        self._upload(width, height)

    def show(self):
        self.user.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
        if not self.user.SetWindowPos(self.hwnd, self.W.HWND(-1), 0, 0, 0, 0, 0x0013):  # NOACTIVATE | NOMOVE | NOSIZE
            self.fail("SetWindowPos")

    def cursor(self) -> tuple[int, int]:
        point = self.W.POINT()
        if not self.user.GetCursorPos(C.byref(point)):
            self.fail("GetCursorPos")
        return point.x, point.y

    def move_cursor(self, point):
        if not self.user.SetCursorPos(*point):
            self.fail("SetCursorPos")
        if self.cursor() != tuple(point):
            raise RuntimeError("SetCursorPos was constrained or the cursor moved before acknowledgment.")

    def pump(self, raise_errors=True):
        message = self.W.MSG()
        count = 0
        while self.user.PeekMessageW(C.byref(message), None, 0, 0, 1):
            if message.message == 0x0012:
                if raise_errors:
                    raise RuntimeError("Guard received WM_QUIT.")
                continue
            self.user.TranslateMessage(C.byref(message))
            self.user.DispatchMessageW(C.byref(message))
            count += 1
            if count >= 256:
                break
        if self.failure and raise_errors:
            raise RuntimeError(self.failure)

    def wait(self, seconds: float):
        # Message-aware wait: low-level hooks must never wait behind a blocking sleep.
        result = self.user.MsgWaitForMultipleObjectsEx(0, None, max(0, math.ceil(seconds * 1000)), 0x04FF, 0x0004)
        if result == 0xFFFFFFFF:
            self.fail("MsgWaitForMultipleObjectsEx")

    def parent_alive(self):
        if not self.parent:
            return True
        result = self.kernel.WaitForSingleObject(self.parent, 0)
        if result == 0xFFFFFFFF:
            self.fail("WaitForSingleObject(parent)")
        return result == 0x102

    def notification_state(self):
        state = C.c_int()
        result = self.shell.SHQueryUserNotificationState(C.byref(state))
        if result < 0:
            raise RuntimeError(f"SHQueryUserNotificationState failed: HRESULT 0x{result & 0xFFFFFFFF:08x}")
        return state.value

    def capsule_placement(self, window_id):
        if window_id is not None and not self.user.IsWindow(window_id):
            raise RuntimeError("Target window no longer exists.")
        hwnd = window_id or self.user.GetForegroundWindow()
        monitor = self.user.MonitorFromWindow(hwnd, 1)
        info = self.MONITORINFO()
        info.cbSize = C.sizeof(info)
        if not self.user.GetMonitorInfoW(monitor, C.byref(info)):
            self.fail("GetMonitorInfoW")
        return (info.rcMonitor.left + info.rcMonitor.right) / 2 - self.bounds[0], info.rcMonitor.top - self.bounds[1] + 12

    def open_confirmation(self):
        if self.prompt:
            return
        self.confirmed = None
        center, top = self.capsule_placement(None)
        left = self.bounds[0] + round(center) - 300
        top = self.bounds[1] + top + 80
        self.prompt = self.user.CreateWindowExW(0x00000008 | 0x00000080, self.class_name, "AI 操控 · 独占全屏确认", 0x80C80000, left, top, 600, 220, None, None, self.instance, None)
        if not self.prompt:
            self.fail("CreateWindowExW(confirmation)")
        if not self.user.SetWindowDisplayAffinity(self.prompt, WDA_EXCLUDEFROMCAPTURE):
            self.fail("SetWindowDisplayAffinity(confirmation)")
        controls = [("STATIC", "Windows 检测到独占全屏 Direct3D 应用。\n覆盖层可能不可见，ESC 仍可用。是否继续？", 20, 20, 555, 85, 0), ("BUTTON", "继续", 290, 125, 120, 32, 1), ("BUTTON", "取消", 430, 125, 120, 32, 2)]
        for kind, text, x, y, width, height, identifier in controls:
            control = self.user.CreateWindowExW(0, kind, text, 0x50000000, x, y, width, height, self.prompt, self.W.HMENU(identifier), self.instance, None)
            if not control:
                self.fail("CreateWindowExW(confirmation control)")
            self.user.SendMessageW(control, 0x0030, self.gdi.GetStockObject(17), 1)  # DEFAULT_GUI_FONT
        self.user.ShowWindow(self.prompt, 5)
        self.user.SetForegroundWindow(self.prompt)  # Windows may deny focus; only explicit buttons authorize.

    def close_confirmation(self):
        if self.prompt:
            self.user.DestroyWindow(self.prompt)
            self.prompt = None

    def close(self):
        failures = []
        def release(function, *args):
            if not function(*args):
                failures.append(function.__name__)
        if self.mouse_hook:
            release(self.user.UnhookWindowsHookEx, self.mouse_hook)
            self.mouse_hook = None
        if self.hook:
            release(self.user.UnhookWindowsHookEx, self.hook)
            self.hook = None
        if self.prompt:
            release(self.user.DestroyWindow, self.prompt)
            self.prompt = None
        if self.hwnd:
            release(self.user.DestroyWindow, self.hwnd)
            self.hwnd = None
        if self.old_bitmap and self.dc:
            self.gdi.SelectObject(self.dc, self.old_bitmap)
            self.old_bitmap = None
        if self.bitmap:
            release(self.gdi.DeleteObject, self.bitmap)
            self.bitmap = None
        if self.dc:
            release(self.gdi.DeleteDC, self.dc)
            self.dc = None
        if self.registered:
            release(self.user.UnregisterClassW, self.class_name, self.instance)
            self.registered = False
        if self.parent:
            release(self.kernel.CloseHandle, self.parent)
            self.parent = None
        if getattr(self, "old_dpi", None):
            self.user.SetThreadDpiAwarenessContext(self.old_dpi)
            self.old_dpi = None
        if failures:
            raise RuntimeError("Native cleanup failed: " + ", ".join(failures))


class Runtime:
    def __init__(self, run_dir: Path, parent_pid: int, token: str, window_id: int | None, api):
        self.run_dir, self.parent_pid, self.token, self.window_id, self.api = run_dir, parent_pid, token, window_id, api
        self.ack = 0
        self.last_sequence = 0
        self.phase = "starting"
        self.countdown_start = None
        self.countdown_complete = False
        self.exclusive_confirmed = False
        self.warnings = [FULLSCREEN_LIMITATION, HOOK_LIMITATION]
        self.label = "准备开始"
        self.movement = None
        self.pulse_started = None
        self.pending_sequence = 0
        self.confirmation_started = None
        self.last_status = -math.inf
        self.last_verify = -math.inf
        self.shown = False
        self.error = None

    def status(self, force=False):
        now = time.monotonic()
        if force or now - self.last_status >= 0.25:
            try:
                write_json(self.run_dir / "guard-status.json", {"protocol": PROTOCOL, "token": self.token, "pid": os.getpid(), "parent_pid": self.parent_pid, "heartbeat": now, "phase": self.phase, "ack": self.ack, "ready": self.phase == "ready", "capture_excluded": bool(self.api.hwnd), "escape_hook": bool(self.api.hook), "mouse_observer": bool(self.api.mouse_hook), "countdown_complete": self.countdown_complete, "warnings": self.warnings, "error": self.error})
            except PermissionError:
                return  # An overlapping reader must not tear down an authorized guard.
            self.last_status = now

    def stopped(self):
        self.api.pump()
        if self.api.escape_requested:
            write_stop(self.run_dir, "user_escape")
        close = read_json(self.run_dir / "guard-close.json")
        if not self.api.parent_alive() or (close is not None and close.get("protocol") == PROTOCOL and close.get("token") == self.token):
            return True
        if (self.run_dir / "stop.flag").exists():
            if not self.countdown_complete:
                return True  # No input could have been authorized yet.
            self.phase = "stopping"
            self.label = "正在停止 · 等待驱动确认"
            self.movement = None
            self.pending_pulse = False
            self.api.pulse_target = None
            # Keep hook/overlay until the parent finishes daemon cancellation.
        return False

    def check_fullscreen(self):
        try:
            info = fullscreen_info(self.api, self.window_id)
        except (OSError, RuntimeError) as exc:
            # Lack of reliable evidence is reported, never replaced with a size heuristic.
            message = f"Exclusive fullscreen detection unavailable: {exc}"
            if message not in self.warnings:
                self.warnings.append(message)
            return False
        if info["exclusive_detected"] and not self.exclusive_confirmed:
            self.phase = "confirming"
            self.confirmation_started = time.monotonic()
            self.api.open_confirmation()
            self.status(True)
            return True
        return False

    def begin_countdown_or_ready(self):
        if self.countdown_complete:
            self.phase = "ready"
            self.ack = self.pending_sequence
        else:
            self.phase = "countdown"
            self.countdown_start = None  # Start only after a successful visible frame.

    def command(self, now):
        command = read_json(self.run_dir / "guard-command.json")
        if not command or command.get("protocol") != PROTOCOL or command.get("token") != self.token:
            return
        sequence = command.get("seq")
        if type(sequence) is not int or sequence <= self.last_sequence:
            return
        if sequence != self.last_sequence + 1 or self.phase != "ready":
            raise RuntimeError("Out-of-order guard command.")
        self.last_sequence, self.pending_sequence = sequence, sequence
        kind = command.get("kind")
        if kind == "ensure":
            self.window_id = command.get("window_id")
            self.placement = self.api.capsule_placement(self.window_id)
            if not self.check_fullscreen():
                self.begin_countdown_or_ready()
        elif kind == "action":
            label = command.get("label")
            if not isinstance(label, str) or not label.strip():
                raise RuntimeError("Invalid action label.")
            self.label = label[:240]
            target = command.get("target")
            self.phase = "action"
            self.pending_pulse = False
            self.api.click_started = None
            self.api.pulse_target = None
            if target is not None:
                if not isinstance(target, dict) or any(type(target.get(key)) is not int for key in ("x", "y")):
                    raise RuntimeError("Invalid coordinate target.")
                destination = target["x"], target["y"]
                left, top, width, height = self.api.bounds
                if not left <= destination[0] < left + width or not top <= destination[1] < top + height:
                    raise RuntimeError("Coordinate target is outside the virtual desktop.")
                self.api.pulse_target = destination if command.get("pulse") is True else None
                self.movement = (self.api.cursor(), destination, now)
            else:
                self.movement = None
        else:
            raise RuntimeError("Unknown guard command.")

    def run(self):
        api = self.api
        api.setup(self.parent_pid, lambda: write_stop(self.run_dir, "user_escape"))
        self.status(True)
        self.renderer = Renderer(*api.bounds[2:], api.user.GetSystemMetrics(13))
        self.placement = api.capsule_placement(self.window_id)
        self.visual_cursor = tuple(map(float, api.cursor()))
        self.follow_origin = self.follow_target = self.visual_cursor
        self.follow_start = time.monotonic()
        if not self.check_fullscreen():
            self.begin_countdown_or_ready()
        next_frame = time.monotonic()
        while not self.stopped():
            now = time.monotonic()
            if now < next_frame:
                api.wait(next_frame - now)
                continue
            next_frame = now + FRAME_SECONDS
            if now - self.last_verify >= 0.5:
                if tuple(api.user.GetSystemMetrics(metric) for metric in (76, 77, 78, 79)) != api.bounds:
                    raise RuntimeError("Display topology changed; restart the run before foreground input.")
                api.verify_capture()
                self.last_verify = now
            if self.phase == "confirming":
                if api.confirmed is False or now - self.confirmation_started >= 120:
                    write_stop(self.run_dir, "user_cancel")
                    break
                if api.confirmed is True:
                    self.exclusive_confirmed = True
                    self.warnings.append("Exclusive fullscreen was reported by Windows; the user accepted that the overlay may be invisible. ESC remains active.")
                    api.close_confirmation()
                    self.begin_countdown_or_ready()
                else:
                    self.status()
                    continue
            if self.phase == "ready":
                self.command(now)
                if self.phase == "confirming":
                    continue
            if self.stopped():
                break
            action_finished = self.phase == "action"
            if self.movement is not None:
                origin, destination, started = self.movement
                position = smooth_position(origin, destination, now - started)
                api.move_cursor(tuple(round(value) for value in position))
                # Same easing drives both the real cursor and overlay; no lag at click time.
                self.visual_cursor = position
                action_finished = now - started >= MOVE_SECONDS
                if action_finished:
                    self.visual_cursor = tuple(map(float, destination))
                    self.movement = None
                self.follow_origin = self.follow_target = self.visual_cursor
                self.follow_start = now
            else:
                actual = api.cursor()
                # Advance the previous tween before retargeting; resetting first would
                # freeze the visual cursor throughout continuous physical movement.
                self.visual_cursor = smooth_position(self.follow_origin, self.follow_target, now - self.follow_start)
                if actual != self.follow_target:
                    self.follow_origin, self.follow_target, self.follow_start = self.visual_cursor, actual, now
            if self.api.click_started is not None:
                self.pulse_started = self.api.click_started
                self.api.click_started = None
            countdown = None
            if self.phase == "countdown":
                countdown = 2.0 if self.countdown_start is None else max(0.0, 2.0 - (now - self.countdown_start))
            local_cursor = self.visual_cursor[0] - api.bounds[0], self.visual_cursor[1] - api.bounds[1]
            pulse_elapsed = None if self.pulse_started is None else now - self.pulse_started
            layers = self.renderer.layers(now, local_cursor, self.label, countdown, pulse_elapsed, *self.placement)
            api.present(self.renderer, layers)
            if not self.shown:
                api.show()
                self.shown = True
            if self.stopped():
                break
            if self.phase == "countdown":
                if self.countdown_start is None:
                    self.countdown_start = time.monotonic()
                elif countdown == 0:
                    self.countdown_complete = True
                    self.phase = "ready"
                    self.ack = self.pending_sequence
                    self.status(True)
            elif action_finished:
                self.phase = "ready"
                self.ack = self.pending_sequence
                self.status(True)
            else:
                self.status()
        self.phase = "stopped"
        self.status(True)


def probe_native() -> dict:
    api = None
    result = {"available": False, "error": None}
    try:
        api = Native()
        api.setup(probe=True)
        # Render one small tile to verify Pillow/font/premultiplication without UI.
        renderer = Renderer(800, 600)
        renderer._capsule("能力检测", None, False).convert("RGBa").tobytes("raw", "BGRa")
        result = {"available": True, "error": None, "capture_excluded": True, "escape_hook": True, "mouse_observer": True, "per_pixel_alpha": True, "fullscreen_detection": fullscreen_info(api, None), "limitations": [FULLSCREEN_LIMITATION, HOOK_LIMITATION]}
    except Exception as exc:
        result = {"available": False, "error": str(exc)}
    finally:
        if api is not None:
            try:
                api.close()
            except Exception as exc:
                result = {"available": False, "error": f"Native cleanup failed: {exc}"}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--parent-pid", type=int)
    parser.add_argument("--token")
    parser.add_argument("--window-id", type=int)
    args = parser.parse_args(argv)
    if args.probe:
        result = probe_native()
        print(json.dumps(result, ensure_ascii=True))
        return 0 if result["available"] else 1
    if args.run_dir is None or not args.parent_pid or not args.token:
        parser.error("--run-dir, --parent-pid, and --token are required")
    api = runtime = None
    exit_code = 0
    try:
        api = Native()
        runtime = Runtime(args.run_dir, args.parent_pid, args.token, args.window_id, api)
        runtime.run()
    except BaseException as exc:
        exit_code = 1
        if runtime is not None:
            runtime.phase, runtime.error = "error", str(exc)
            try:
                runtime.status(True)
            except OSError:
                pass
        else:
            try:
                write_json(args.run_dir / "guard-status.json", {"protocol": PROTOCOL, "token": args.token, "pid": os.getpid(), "parent_pid": args.parent_pid, "heartbeat": time.monotonic(), "phase": "error", "error": str(exc)})
            except OSError:
                pass
        if runtime is not None and runtime.countdown_complete:
            # IPC loss must not uninstall an authorized guard while cancellation
            # transport may also be unavailable. No files are needed for this wait.
            while api.parent_alive():
                close = read_json(args.run_dir / "guard-close.json")
                if close and close.get("protocol") == PROTOCOL and close.get("token") == args.token:
                    break
                api.pump(raise_errors=False)
                api.wait(FRAME_SECONDS)
    finally:
        if api is not None:
            try:
                api.close()
            except Exception as exc:
                exit_code = 1
                if runtime is not None:
                    runtime.phase, runtime.error = "error", str(exc)
                    try:
                        runtime.status(True)
                    except OSError:
                        pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
