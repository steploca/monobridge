#!/usr/bin/env python3
"""
monobridge - keep Monogram Creative Console hardware working after the
official software is gone.

Talks directly to the Monogram Core over its USB serial port and turns
module input into MIDI on a virtual port ("Monobridge") that any DAW or
MIDI-mapping tool can use.

Usage:
    python3 monobridge.py ports              # find the Core
    python3 monobridge.py monitor            # print raw decoded messages
    python3 monobridge.py run [-c map.json]  # MIDI bridge

Quit Monogram Creator (and "Monogram Service") first - only one program
can open the Core's serial port at a time.

Not affiliated with or endorsed by Monogram / Palette Gear.
License: MIT
"""
import argparse
import json
import os
import sys
import threading
import time

try:
    import serial
    import serial.tools.list_ports
    import msgpack
except ImportError:
    sys.exit("Missing dependencies. Run:  pip3 install pyserial msgpack python-rtmidi")

# ---------------------------------------------------------------- protocol
CORE_VID = 0x0483          # STMicroelectronics
CORE_PID = 0xA2C5          # Monogram Core v2 ("Condor")
BAUD = 300000
FLAG, ESC = 0x7E, 0x7D
PING_SECONDS = 3
WATCHDOG_SECONDS = 15

MODULE_TYPES = {
    0: "core", 128: "core", 25: "core",
    1: "button", 129: "button",
    2: "dial", 130: "dial",
    3: "slider", 131: "slider",
    8: "button3x", 9: "dial3x", 10: "slider3x",
    20: "falconmini", 21: "falconmini",
    24: "orbiter",
}


def frame(msg: dict) -> bytes:
    """msgpack-encode a message and wrap it in 0x7E flags with 0x7D escaping."""
    out = bytearray([FLAG])
    for b in msgpack.packb(msg, use_bin_type=True):
        if b in (FLAG, ESC):
            out += bytes([ESC, b ^ 0x20])
        else:
            out.append(b)
    out.append(FLAG)
    return bytes(out)


def unescape(data: bytes) -> bytes:
    out, esc = bytearray(), False
    for b in data:
        if b == ESC:
            esc = True
        elif esc:
            out.append(b ^ 0x20)
            esc = False
        else:
            out.append(b)
    return bytes(out)


def _norm(o):
    """Turn bytes keys/values from a raw msgpack decode into str where sensible."""
    if isinstance(o, dict):
        return {_norm(k): _norm(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_norm(v) for v in o]
    if isinstance(o, bytes):
        try:
            return o.decode("ascii")
        except UnicodeDecodeError:
            return o
    return o


def decode(payload: bytes):
    raw = unescape(payload)
    try:
        return _norm(msgpack.unpackb(raw, raw=True, strict_map_key=False))
    except Exception:
        return None


class Core:
    """Serial connection to the Monogram Core."""

    def __init__(self, port=None):
        self.port_name = port or find_core()
        if not self.port_name:
            sys.exit("Monogram Core not found. Is it plugged in, and is Monogram Creator quit?")
        self.ser = serial.Serial(self.port_name, BAUD, timeout=0.05)
        self._buf = bytearray()
        self.last_rx = time.time()
        self._lock = threading.Lock()
        self._alive = True

    def send(self, msg: dict):
        if not self._alive:
            return
        try:
            with self._lock:
                self.ser.write(frame(msg))
        except Exception:            # unplugged mid-write: the reader loop will notice
            self._alive = False

    def start_pinging(self):
        def loop():
            while self._alive:
                try:
                    self.send({"get_check": True})
                except Exception:
                    return
                time.sleep(PING_SECONDS)
        threading.Thread(target=loop, daemon=True).start()

    def messages(self):
        """Yield decoded messages until the port fails (raises serial.SerialException/OSError)."""
        while True:
            if not self._alive:
                raise serial.SerialException("connection lost")
            chunk = self.ser.read(4096)
            if not chunk:
                # The Core answers every ping, so long silence means it has hung
                if time.time() - self.last_rx > WATCHDOG_SECONDS:
                    raise serial.SerialException("console stopped responding")
                continue
            self.last_rx = time.time()
            self._buf += chunk
            while FLAG in self._buf:
                idx = self._buf.index(FLAG)
                payload = bytes(self._buf[:idx])
                del self._buf[: idx + 1]
                if payload:  # empty = between two flags
                    msg = decode(payload)
                    yield msg if msg is not None else {"_undecodable": payload.hex()}

    def close(self):
        self._alive = False
        try:
            self.ser.close()
        except Exception:
            pass


def find_core():
    for p in serial.tools.list_ports.comports():
        if p.vid == CORE_VID and p.pid == CORE_PID:
            return p.device
    return None


# ---------------------------------------------------------------- layout
def walk_layout(node, out=None, depth=0):
    """Flatten the layout tree: {local_id: {type, uuid, depth}}."""
    out = {} if out is None else out
    if not node:
        return out
    out[node.get("i")] = {
        "type": MODULE_TYPES.get(node.get("t"), f"unknown({node.get('t')})"),
        "type_id": node.get("t"),
        "uuid": node.get("u"),
        "depth": depth,
    }
    for child in node.get("c") or []:
        walk_layout(child, out, depth + 1)
    return out


def s8(v):
    return v - 256 if v > 127 else v


# ---------------------------------------------------------------- LEDs + screen
SUBMODULE_TYPES = {"button3x", "dial3x", "slider3x"}
SCREEN = 240


def hex_rgb(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def set_color(core, local_id, color, sub=None):
    r, g, b = hex_rgb(color) if isinstance(color, str) else color
    rgb = r | g << 8 | b << 16
    if sub is None:
        core.send({"set_module": [local_id, 0x02, rgb]})
    else:
        core.send({"set_module": [local_id, 0x05, rgb | sub << 24]})


def commit_colors(core, local_id):
    core.send({"set_module": [local_id, 0x05, 0x7F << 24]})


def render_screen(icon=None, label="", bg="#000000", rotate=0, size=140):
    """Build a 240x240 image the way Monogram did: icon 140px up top, label below.
    `icon` may be a file path, PNG bytes, or a base64 PNG string."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        sys.exit("Missing Pillow. Run:  pip3 install pillow")
    import base64
    import io
    img = Image.new("RGB", (SCREEN, SCREEN), bg)
    if icon:
        if isinstance(icon, str) and not icon.lower().endswith((".png", ".jpg", ".jpeg")):
            icon = base64.b64decode(icon)
        src = Image.open(io.BytesIO(icon) if isinstance(icon, bytes) else icon).convert("RGBA")
        size = max(40, min(SCREEN, int(size)))
        src = src.resize((size, size), Image.LANCZOS)
        x = (SCREEN - size) // 2
        y = max(0, (170 - size) // 2) if label else (SCREEN - size) // 2
        img.paste(src, (x, y), src)
    if label:
        draw = ImageDraw.Draw(img)
        font = None
        for path in ("/System/Library/Fonts/Helvetica.ttc",
                     "/System/Library/Fonts/SFNS.ttf",
                     "/Library/Fonts/Arial.ttf"):
            try:
                font = ImageFont.truetype(path, 30)
                break
            except Exception:
                pass
        font = font or ImageFont.load_default()
        w = draw.textlength(label, font=font)
        draw.text((max(0, (SCREEN - w) / 2), 180), label, fill="white", font=font)
    if rotate:  # degrees clockwise; negative = anticlockwise
        img = img.rotate(-rotate, resample=Image.BICUBIC, fillcolor=bg)
    return img


def to_rgb565(img):
    out = bytearray(SCREEN * SCREEN * 2)
    for n, (r, g, b) in enumerate(img.convert("RGB").getdata()):
        v = (r >> 3) << 11 | (g >> 2) << 5 | (b >> 3)
        out[2 * n], out[2 * n + 1] = v >> 8, v & 0xFF
    return bytes(out)


def write_screen(core, img, live=False):
    data = to_rgb565(img)
    rows = 4
    size = SCREEN * rows * 2
    if not live:
        core.send({"invoke_display": 0x03})          # display off while drawing
    for n, i in enumerate(range(0, len(data), size)):
        if not getattr(core, "_alive", True):
            return                                   # console went away mid-draw
        core.send({"write_display_slow": [True, 0, n * rows, SCREEN, (n + 1) * rows, data[i:i + size]]})
        time.sleep(0.025)                            # let the Core keep up (~1.5 s per image)
    core.send({"invoke_display": 0x04})              # display on


# ---------------------------------------------------------------- Monogram profiles
SUB_NAMES = {"button3x": ("button", "press"), "slider3x": ("slider", "pos"), "dial3x": ("dial", "")}


def _midi_ctl(a):
    """Translate a Monogram assignment (MIDI, keystroke or scroll) to a monobridge control, or None."""
    if isinstance(a, dict) and "key" in a:
        return {"key": a["key"], "mods": list(a.get("modifiers") or [])}
    if isinstance(a, dict) and "scroll" in a:
        return {"scroll": "h" if a["scroll"] == "horizontal" else "v", "speed": a.get("step", 20)}
    if not isinstance(a, dict) or "midi" not in a:
        return None
    c = {"channel": a.get("channel", 1)}
    if a["midi"] == "cc":
        c["cc"] = a["cc"]
        for k_src, k_dst in (("onValue", "on"), ("offValue", "off"), ("toggle", "toggle")):
            if k_src in a:
                c[k_dst] = a[k_src]
    elif a["midi"] == "note":
        c["note"] = a["note"]
        c["velocity"] = a.get("velocity", 64)
    else:
        return None
    return c


class Profile:
    """A Monogram Creator .monogram profile (JSON) - colours, icon and MIDI assignments."""

    def __init__(self, path):
        with open(path) as f:
            self.data = json.load(f)
        self.title = self.data.get("title", "")
        self.icon = self.data.get("icon")
        self.assign = self.data.get("assignments", {})
        self.unsupported = []

    def for_module(self, mtype, index):
        lst = self.assign.get(mtype) or []
        return lst[index] if index < len(lst) else None

    def controls(self, mtype, entry):
        """-> {control_name: control_dict} for the MIDI parts of this module's assignment."""
        out = {}

        def take(settings, mapping, prefix=""):
            for src, dst in mapping.items():
                if src not in settings:
                    continue
                c = _midi_ctl(settings[src])
                if c:
                    if src == "posePsi":
                        c["mode"] = "absolute"
                    out[dst] = c
                elif src not in ("color", "label"):
                    self.unsupported.append(f"{mtype} {prefix}{src}: {json.dumps(settings[src])}")

        s = entry.get("settings", {})
        subs = entry.get("submodules", {})
        if mtype == "slider":
            take(s, {"pose": "pos"})
        elif mtype == "button":
            take(s, {"press": "press"})
        elif mtype == "dial":
            take(s, {"turn": "turn", "pose": "turn", "press": "press"})
        elif mtype == "orbiter":
            take(s, {"turn": "spin", "posePsi": "spin", "poseX": "x", "tiltX": "x",
                     "poseY": "y", "tiltY": "y"})
        elif mtype in SUB_NAMES:
            base, _ = SUB_NAMES[mtype]
            for k in range(1, 4):
                ss = (subs.get(f"{base}{k}") or {}).get("settings", {})
                if mtype == "button3x":
                    take(ss, {"press": f"press{k}"}, f"button{k} ")
                elif mtype == "slider3x":
                    take(ss, {"pose": f"pos{k}"}, f"slider{k} ")
                else:
                    take(ss, {"turn": f"turn{k}", "pose": f"turn{k}", "press": f"press{k}"}, f"dial{k} ")
        elif mtype == "core":
            for k in (1, 2):
                ss = (subs.get(f"button{k}") or {}).get("settings", {})
                take(ss, {"press": f"press{k}"}, f"button{k} ")
        return out

    def colors(self, mtype, entry):
        """-> (module_color or None, {sub_index: color})"""
        main = (entry.get("settings") or {}).get("color")
        subs = {}
        if mtype in SUB_NAMES:
            base, _ = SUB_NAMES[mtype]
            for k in range(1, 4):
                col = ((entry.get("submodules", {}).get(f"{base}{k}") or {}).get("settings") or {}).get("color")
                if col:
                    subs[k - 1] = col
        return main, subs



# ---------------------------------------------------------------- keystrokes + scrolling (macOS)
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11,
    "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21,
    "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31,
    "u": 32, "[": 33, "i": 34, "p": 35, "return": 36, "enter": 36, "l": 37, "j": 38, "'": 39,
    "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "tab": 48,
    "space": 49, "`": 50, "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111, "f13": 105, "f14": 107, "f15": 113,
    "home": 115, "pageup": 116, "forwarddelete": 117, "end": 119, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "num0": 82, "num1": 83, "num2": 84, "num3": 85, "num4": 86, "num5": 87, "num6": 88,
    "num7": 89, "num8": 91, "num9": 92, "num.": 65, "num*": 67, "num+": 69, "num-": 78,
    "num/": 75, "numenter": 76, "num=": 81, "clear": 71,
}
MODFLAGS = {"shift": 0x20000, "right_shift": 0x20000, "ctrl": 0x40000, "control": 0x40000,
            "alt": 0x80000, "option": 0x80000, "cmd": 0x100000, "command": 0x100000,
            "meta": 0x100000, "win": 0x100000, "windows": 0x100000}


class MacEvents:
    """Posts key and scroll events through Quartz. Needs Accessibility permission
    for the app running the bridge (Terminal): System Settings > Privacy & Security
    > Accessibility."""

    def __init__(self):
        import ctypes
        import ctypes.util
        c = ctypes
        self.ok = False
        try:
            self.cg = c.cdll.LoadLibrary(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            self.cf = c.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        except OSError:
            return
        cg = self.cg
        cg.CGEventCreateKeyboardEvent.restype = c.c_void_p
        cg.CGEventCreateKeyboardEvent.argtypes = [c.c_void_p, c.c_uint16, c.c_bool]
        cg.CGEventSetFlags.argtypes = [c.c_void_p, c.c_uint64]
        cg.CGEventPost.argtypes = [c.c_uint32, c.c_void_p]
        cg.CGEventCreate.restype = c.c_void_p
        cg.CGEventCreate.argtypes = [c.c_void_p]
        cg.CGEventSetType.argtypes = [c.c_void_p, c.c_uint32]
        cg.CGEventSetIntegerValueField.argtypes = [c.c_void_p, c.c_uint32, c.c_int64]
        cg.AXIsProcessTrusted.restype = c.c_bool
        self.cf.CFRelease.argtypes = [c.c_void_p]
        self.ok = True

    def trusted(self):
        return self.ok and bool(self.cg.AXIsProcessTrusted())

    def key(self, name, mods=(), down=True):
        code = KEYCODES.get(str(name).lower())
        if not self.ok or code is None:
            return False
        flags = 0
        for m in mods or ():
            flags |= MODFLAGS.get(str(m).lower(), 0)
        e = self.cg.CGEventCreateKeyboardEvent(None, code, down)
        if flags:
            self.cg.CGEventSetFlags(e, flags)
        self.cg.CGEventPost(0, e)
        self.cf.CFRelease(e)
        return True

    def scroll(self, dy=0, dx=0):
        """Pixel scroll on both axes. Positive dy = scroll up, positive dx = scroll left."""
        if not self.ok or not (dx or dy):
            return
        import ctypes as c
        f = getattr(self.cg, "CGEventCreateScrollWheelEvent2", None)   # macOS 13+
        if f is not None:
            f.restype = c.c_void_p
            f.argtypes = [c.c_void_p, c.c_uint32, c.c_uint32, c.c_int32, c.c_int32, c.c_int32]
            e = f(None, 0, 2, int(dy), int(dx), 0)                       # 0 = pixel units, 2 wheels
            self.cg.CGEventPost(0, e)
            self.cf.CFRelease(e)
            return
        # Older macOS: vertical scroll, and Shift+scroll for horizontal (standard Mac behaviour)
        for amount, shift in ((dy, False), (dx, True)):
            if not amount:
                continue
            e = self.cg.CGEventCreate(None)
            self.cg.CGEventSetType(e, 22)
            self.cg.CGEventSetIntegerValueField(e, 137, 1)
            self.cg.CGEventSetIntegerValueField(e, 96, int(amount))
            self.cg.CGEventSetIntegerValueField(e, 11, int(amount / 10) or (1 if amount > 0 else -1))
            if shift:
                self.cg.CGEventSetFlags(e, 0x20000)
            self.cg.CGEventPost(0, e)
            self.cf.CFRelease(e)


# ---------------------------------------------------------------- MIDI
class Midi:
    verbose = False

    def _send(self, msg):
        if self.verbose:
            kind = {0xB0: "CC  ", 0x90: "NOTE+", 0x80: "NOTE-", 0xC0: "PROG ",
                    0xD0: "PRESS", 0xE0: "BEND "}[msg[0] & 0xF0]
            if msg[0] & 0xF0 == 0xE0:
                print(f"    ch{(msg[0] & 0x0F) + 1} {kind} = {msg[1] | msg[2] << 7}")
            else:
                print(f"    ch{(msg[0] & 0x0F) + 1} {kind} {msg[1]:>3}" + (f" = {msg[2]}" if len(msg) > 2 else ""))
        self.out.send_message(msg)

    def __init__(self, name="Monobridge"):
        try:
            import rtmidi
        except ImportError:
            sys.exit("Missing python-rtmidi. Run:  pip3 install python-rtmidi")
        self.out = rtmidi.MidiOut()
        self.out.open_virtual_port(name)

    def cc(self, ch, cc, val):
        self._send([0xB0 | (ch - 1), cc & 0x7F, max(0, min(127, int(val)))])

    def note(self, ch, note, on, vel=127):
        self._send([(0x90 if on else 0x80) | (ch - 1), note & 0x7F, vel if on else 0])

    def program(self, ch, prog):
        self._send([0xC0 | (ch - 1), prog & 0x7F])

    def pressure(self, ch, val):
        self._send([0xD0 | (ch - 1), max(0, min(127, int(val)))])

    def bend(self, ch, val127):
        self.bend14(ch, round(val127 * 16383 / 127))

    def bend14(self, ch, v):
        v = max(0, min(16383, int(v)))
        self._send([0xE0 | (ch - 1), v & 0x7F, v >> 7])


class Bridge:
    """
    Maps module input to MIDI.

    Mapping file (JSON) is keyed by module UUID (printed at startup); each
    control inside a module is a sub-key. Anything not in the file gets an
    automatic assignment, which is written back with --save.

      {
        "channel": 1,
        "dial_mode": "relative",          // relative (two's complement) | absolute
        "modules": {
          "<uuid>": {"turn": {"cc": 20}, "press": {"note": 36}},
          "<uuid>": {"pos": {"cc": 1}},
          "<uuid>": {"press": {"cc": 64, "toggle": true}}
        }
      }
    """

    def __init__(self, core, midi, mapping, profile=None, rotate=0):
        self.core, self.midi, self.map = core, midi, mapping
        self.profile, self.rotate = profile, rotate
        self._screen_done = False
        self.mac = None
        self._tilt = {}          # (uuid, name) -> (axis, amount px per tick)
        self._held = {}          # (uuid, name) -> (key, mods) currently held down
        self.map.setdefault("channel", 1)
        self.map.setdefault("dial_mode", "relative")
        self.map.setdefault("modules", {})
        self.map.setdefault("colors", {})
        self.modules = {}
        self.state = {}            # per-control remembered values
        self._next_cc, self._next_note = 20, 36

    # -- auto-assignment -------------------------------------------------
    def _auto(self, kind):
        if kind == "cc":
            v, self._next_cc = self._next_cc, self._next_cc + 1
        else:
            v, self._next_note = self._next_note, self._next_note + 1
        return v

    def control(self, uuid, name, kind):
        m = self.map["modules"].setdefault(uuid, {})
        if name not in m:
            m[name] = {kind: self._auto(kind)}
            print(f"  auto-mapped {uuid}:{name} -> {kind} {m[name][kind]}")
        return m[name]

    def on_layout(self, layout):
        self.modules = walk_layout(layout)
        # keep auto-numbering past anything already mapped
        for m in self.map["modules"].values():
            for c in m.values():
                if "cc" in c:
                    self._next_cc = max(self._next_cc, c["cc"] + 1)
                if "note" in c:
                    self._next_note = max(self._next_note, c["note"] + 1)
        if self.profile:
            self.apply_profile()
        self.apply_colors()
        if not self._screen_done and self.map.get("screen"):
            self.schedule_screen()
        dz = self.map.get("orbiter_deadzone")
        if dz and self.core:
            for lid, info in self.modules.items():
                if info["type"] == "orbiter":
                    self.core.send({"set_module": [lid, 0xB4, int(dz)]})
                    print(f"  orbiter #{lid}: deadzone set to {dz}")
        print("Layout:")
        for lid, info in self.modules.items():  # tree (attachment) order
            print(f"  {'  ' * info['depth']}#{lid} {info['type']:<10} uuid={info['uuid']}")

    def apply_profile(self):
        """The first time a profile is loaded it overrides mapping.json; after that,
        edits made in the control panel win and the profile only fills gaps."""
        p, seen = self.profile, {}
        p.unsupported = []
        first = self.map.get("profile") != p.title
        self.map["profile"] = p.title
        for lid, info in self.modules.items():
            t = info["type"]
            idx = seen.get(t, 0)
            seen[t] = idx + 1
            entry = p.for_module(t, idx)
            if not entry:
                continue
            # MIDI assignments from the profile take priority over mapping.json
            ctl = p.controls(t, entry)
            if ctl:
                mod = self.map["modules"].setdefault(info["uuid"], {})
                for k, v in ctl.items():
                    if first or k not in mod:
                        mod[k] = v
            # LED colours: seed from the profile unless already customised
            main, subs = p.colors(t, entry)
            if (main or subs) and (first or info["uuid"] not in self.map["colors"]):
                self.map["colors"][info["uuid"]] = {"main": main, "subs": {str(k): v for k, v in subs.items()}}
        if first or "screen" not in self.map:
            self.map["screen"] = {"icon": p.icon, "label": p.title, "rotate": self.rotate}
        if p.unsupported:
            print("Profile assignments not handled yet (non-MIDI):")
            for u in p.unsupported:
                print("   ", u[:110])

    # -- LEDs + display ----------------------------------------------------
    def apply_colors(self):
        if not self.core:
            return
        for lid, info in self.modules.items():
            c = self.map["colors"].get(info["uuid"])
            if not c:
                continue
            if c.get("main"):
                set_color(self.core, lid, c["main"])
            subs = c.get("subs") or {}
            for k, col in subs.items():
                set_color(self.core, lid, col, int(k))
            if subs:
                commit_colors(self.core, lid)

    def screen_image(self, upright=False):
        s = self.map.get("screen") or {}
        return render_screen(s.get("icon"), s.get("label", ""), rotate=0 if upright else s.get("rotate", 0),
                             size=s.get("size", 140))

    def set_midi(self, uuid, control, cfg):
        self.map["modules"].setdefault(uuid, {})[control] = cfg
        for k in list(self.state):            # forget remembered values for this control
            if k[:2] == (uuid, control):
                del self.state[k]

    def schedule_screen(self, delay=3.0):
        """Draw the display once the layout has settled (the Core reports modules one
        by one right after it starts) and in a background thread, so the reader
        keeps draining the Core's messages while the image is sent."""
        t = getattr(self, "_screen_timer", None)
        if t:
            t.cancel()
        core = self.core

        def go():
            if self.core is core and core is not None:
                print("Drawing the Core display...")
                self.draw_screen()
                self._screen_done = True
        self._screen_timer = threading.Timer(delay, go)
        self._screen_timer.daemon = True
        self._screen_timer.start()

    def draw_screen(self):
        if not (self.core and self.map.get("screen")):
            return
        if not hasattr(self, "_draw_lock"):
            self._draw_lock = threading.Lock()
        with self._draw_lock:                        # never two images at once
            write_screen(self.core, self.screen_image())

    def set_color(self, uuid, color, sub=None):
        c = self.map["colors"].setdefault(uuid, {"main": None, "subs": {}})
        if sub is None:
            c["main"] = color
        else:
            c.setdefault("subs", {})[str(sub)] = color
        lid = next((l for l, i in self.modules.items() if i["uuid"] == uuid), None)
        if lid is not None and self.core:
            if sub is None:
                set_color(self.core, lid, color)
            else:
                set_color(self.core, lid, color, int(sub))
                commit_colors(self.core, lid)

    def keyboard(self):
        if self.mac is None:
            self.mac = MacEvents()
            if not self.mac.ok:
                print("Keystrokes/scrolling are only available on macOS.")
            elif not self.mac.trusted():
                print("To send keystrokes and scrolling, allow Terminal in System Settings > "
                      "Privacy & Security > Accessibility, then restart the bridge.")
        return self.mac

    def _start_scroller(self):
        if getattr(self, "_scroller", None):
            return

        def loop():
            while True:
                dy = sum(a for ax, a in self._tilt.values() if ax == "v")
                dx = sum(a for ax, a in self._tilt.values() if ax == "h")
                if dx or dy:
                    self.keyboard().scroll(dy=round(dy), dx=round(dx))
                time.sleep(0.03)
        self._scroller = threading.Thread(target=loop, daemon=True)
        self._scroller.start()

    # -- outputs ---------------------------------------------------------
    def press(self, uuid, name, pressed):
        key = (uuid, name)
        if self.state.get(key) == pressed:
            return
        first = key not in self.state
        self.state[key] = pressed
        c = self.control(uuid, name, "note")
        if first and not pressed:
            return  # startup state report - don't spam note-offs
        ch = c.get("channel", self.map["channel"])
        if "key" in c:
            mods = c.get("mods") or []
            if pressed:
                self.keyboard().key(c["key"], mods, True)
                self._held[key] = (c["key"], mods)
            else:
                k, m = self._held.pop(key, (c["key"], mods))
                self.keyboard().key(k, m, False)
            return
        if "program" in c:
            if pressed:
                self.midi.program(ch, c["program"])
        elif "note" in c:
            self.midi.note(ch, c["note"], pressed, c.get("velocity", 127))
        elif "cc" in c:
            if c.get("toggle"):
                if pressed:
                    t = not self.state.get(key + ("t",), False)
                    self.state[key + ("t",)] = t
                    self.midi.cc(ch, c["cc"], c.get("on", 127) if t else c.get("off", 0))
            else:
                self.midi.cc(ch, c["cc"], c.get("on", 127) if pressed else c.get("off", 0))

    def turn(self, uuid, name, ticks):
        if not ticks:
            return
        c = self.control(uuid, name, "cc")
        if c.get("scroll"):
            px = ticks * c.get("speed", 10)
            self.keyboard().scroll(dy=px if c["scroll"] == "v" else 0, dx=px if c["scroll"] == "h" else 0)
            return
        ch = c.get("channel", self.map["channel"])
        mode = c.get("mode", self.map["dial_mode"])
        ticks = int(ticks * c.get("scale", 1))
        if mode == "absolute":
            key = (uuid, name, "abs")
            v = max(0, min(127, self.state.get(key, 64) + ticks))
            self.state[key] = v
            self.midi.cc(ch, c["cc"], v)
        else:  # relative, two's complement: 1..63 up, 127..65 down
            ticks = max(-63, min(63, ticks))
            self.midi.cc(ch, c["cc"], ticks & 0x7F)

    def slide(self, uuid, name, pos255):
        v = pos255 >> 1
        key = (uuid, name)
        if self.state.get(key) == v:
            return
        self.state[key] = v
        c = self.control(uuid, name, "cc")
        if c.get("scroll"):
            d = (pos255 - 128) / 127.0
            if abs(d) < 0.08:
                d = 0.0
            self._tilt[key] = (c["scroll"], d * c.get("speed", 20))
            self._start_scroller()
            return
        self._tilt.pop(key, None)
        ch = c.get("channel", self.map["channel"])
        if c.get("bend"):
            self.midi.bend(ch, v)
        elif c.get("pressure"):
            self.midi.pressure(ch, v)
        else:
            self.midi.cc(ch, c["cc"], v)

    def tilt(self, uuid, name, d):
        """Orbiter tilt, d = -127..127 with 0 at rest (the disc springs back to centre)."""
        key = (uuid, name)
        if self.state.get(key) == d:
            return
        self.state[key] = d
        c = self.control(uuid, name, "cc")
        if c.get("scroll"):
            f = d / 127.0
            if abs(f) < 0.08:
                f = 0.0
            self._tilt[key] = (c["scroll"], f * c.get("speed", 20))
            self._start_scroller()
            return
        self._tilt.pop(key, None)
        ch = c.get("channel", self.map["channel"])
        if c.get("invert"):
            d = -d
        if c.get("bend"):                       # centred: rest = 8192
            self.midi.bend14(ch, 8192 + round(d * 8192 / 127))
        elif c.get("mod"):                      # one-sided: rest = 0
            way = c.get("dir", "up")
            amt = d if way == "up" else -d if way == "down" else abs(d)
            self.midi.cc(ch, c.get("cc", 1), max(0, amt))
        elif c.get("pressure"):
            self.midi.pressure(ch, (d + 128) >> 1)
        else:
            self.midi.cc(ch, c["cc"], (d + 128) >> 1)

    # -- input decoding --------------------------------------------------
    def on_input(self, ev):
        info = self.modules.get(ev.get("i"))
        if not info:
            return
        t, u, v = info["type"], info["uuid"], list(ev.get("v") or []) + [0] * 8
        if t == "button":
            self.press(u, "press", v[0] != 0)
        elif t == "slider":
            pos = 0 if v[0] == 1 else 255 if v[0] == 254 else v[0]
            self.slide(u, "pos", pos)
        elif t == "dial":
            self.press(u, "press", v[0] != 0)
            ticks = v[2] if v[1] == 0 else -v[1]
            self.turn(u, "turn", ticks)
        elif t == "core":
            # the Core's own two buttons: bit 1 = left (next to the display), bit 0 = right
            self.press(u, "press1", bool(v[0] & 2))
            self.press(u, "press2", bool(v[0] & 1))
        elif t == "button3x":
            for k in range(3):
                self.press(u, f"press{k + 1}", bool(v[0] >> k & 1))
        elif t == "dial3x":
            for k in range(3):
                self.press(u, f"press{k + 1}", bool(v[0] >> k & 1))
                self.turn(u, f"turn{k + 1}", s8(v[k + 1]))
        elif t == "slider3x":
            for k in range(3):
                self.slide(u, f"pos{k + 1}", v[k + 1])
        elif t == "orbiter":
            # v[5], v[6] = core-computed X/Y in -127..127
            self.tilt(u, "x", s8(v[5]))
            self.tilt(u, "y", s8(v[6]))
            self.turn(u, "spin", s8(v[0]))
        # core / falconmini: nothing mapped yet

    def handle(self, msg):
        if "l" in msg:
            self._l_time = msg.get("l_time", getattr(self, "_l_time", None))
            self.on_layout(msg["l"])
        elif "l_time" in msg and self.core:
            # Modules were attached/removed: the Core's layout timestamp moved on
            if getattr(self, "_l_time", None) not in (None, msg["l_time"]):
                print("Layout changed - re-reading modules")
                self.core.send({"get_layout": True})
            self._l_time = msg["l_time"]
        for ev in msg.get("in") or []:
            if ev:
                self.on_input(ev)


# ---------------------------------------------------------------- web UI
UI_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Monobridge</title><style>
:root{--bg:#f6f6f4;--card:#fff;--ink:#1d1d1f;--mute:#6e6e73;--line:#e3e3e0;--acc:#0a66ff}
@media(prefers-color-scheme:dark){:root{--bg:#161617;--card:#1f1f21;--ink:#f2f2f2;--mute:#9a9aa0;--line:#333336;--acc:#4c8dff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.4 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px 60px}
h1{font-size:22px;margin:0 0 2px}.sub{color:var(--mute);margin:0 0 20px}
.grid{display:grid;grid-template-columns:300px 1fr;gap:20px;align-items:start}@media(max-width:760px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;min-width:0}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);margin:0 0 12px}
.screen{width:240px;height:240px;max-width:100%;border-radius:50%;overflow:hidden;background:#000;margin:0 auto 14px;box-shadow:0 0 0 6px #2a2a2c}
.screen img{width:100%;height:100%;display:block}
label,.lbl{display:block;font-size:13px;color:var(--mute);margin:12px 0 4px}
input[type=text]{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);font:inherit}
input[type=file]{max-width:100%;font-size:13px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
button,select,input[type=number]{font:inherit;font-size:14px;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink)}
button{cursor:pointer;white-space:nowrap}button.pri{background:var(--acc);border-color:var(--acc);color:#fff}
input[type=number]{width:64px}input[type=range]{flex:1;min-width:80px}
.note{font-size:12px;color:var(--mute);margin-top:6px}
.mod{border-top:1px solid var(--line);padding:14px 0}.mod:first-of-type{border-top:0;padding-top:0}
.mtitle{font-weight:600}.muid{color:var(--mute);font-size:12px;margin-left:6px}
table{border-collapse:collapse;width:100%;margin-top:8px}td{padding:4px 6px 4px 0;vertical-align:middle;font-size:14px}
td.n{color:var(--mute);white-space:nowrap;width:1%}
input[type=color]{width:34px;height:34px;border:0;padding:0;background:none;cursor:pointer;vertical-align:middle}
.sw{display:flex;gap:6px;flex-wrap:wrap}.sw span{width:22px;height:22px;border-radius:50%;cursor:pointer;border:1px solid var(--line)}
.status{font-size:13px;color:var(--mute);margin-top:10px;min-height:18px}
.saved{color:#1a7f37}
</style></head><body><main>
<h1>Monobridge</h1><p class="sub" id="sub">Connecting…</p>
<div class="grid">
 <section class="card"><h2>Display</h2>
  <div class="screen"><img id="prev" alt="Core display preview"></div>
  <label for="label">Label</label><input type="text" id="label">
  <span class="lbl">Logo</span><input type="file" id="logo" accept="image/*">
  <div class="row" style="margin-top:6px"><button id="nologo">Remove logo</button></div>
  <span class="lbl">Logo size: <b id="sizev"></b></span>
  <div class="row"><input type="range" id="size" min="40" max="240" step="5"><button id="fit">Fill</button></div>
  <div class="note">Clear the label to centre the logo and use the whole screen.</div>
  <span class="lbl">Rotation on console: <b id="rotv"></b></span>
  <div class="row"><button data-r="-90">↺ 90°</button><button data-r="90">90° ↻</button><button data-r="180">180°</button></div>
  <div class="note">Preview is shown upright. Rotation corrects for how the Core is mounted.</div>
  <div class="row" style="margin-top:14px"><button class="pri" id="send">Send to console</button></div>
  <div class="status" id="sstat"></div>
 </section>
 <section class="card"><h2>Controls</h2>
  <div class="row" style="margin-bottom:14px"><span class="note" style="margin:0">Paint colour:</span><div class="sw" id="sw"></div></div>
  <div id="mods"></div>
  <div class="status" id="lstat"></div>
 </section>
</div></main>
<script>
const $=s=>document.querySelector(s);let st=null,brush=null,timer=null,logo=undefined,rot=0;
const CTRLS={
 button:[['press','Button','press']],slider:[['pos','Slider','pos']],dial:[['turn','Turn','turn'],['press','Press','press']],
 button3x:[1,2,3].map(k=>['press'+k,'Button '+k,'press',k-1]),
 slider3x:[1,2,3].map(k=>['pos'+k,'Slider '+k,'pos',k-1]),
 dial3x:[1,2,3].flatMap(k=>[['turn'+k,'Dial '+k+' turn','turn',k-1],['press'+k,'Dial '+k+' press','press',null]]),
 orbiter:[['x','Tilt X','tilt'],['y','Tilt Y','tilt'],['spin','Ring','turn']],core:[['press1','Button 1','press'],['press2','Button 2','press']]};
const TITLES={core:'Core',orbiter:'Orbiter',button3x:'Buttons',slider3x:'Sliders',dial3x:'Dials',button:'Button',slider:'Slider',dial:'Dial'};
const SINGLE_LED={core:1,orbiter:1,button:1,slider:1,dial:1};
const SW=['#0000ff','#00ff00','#ff00ff','#ffff00','#ff0000','#ff4000','#0cebeb','#ffffff','#000000'];
async function api(p,b){const r=await fetch(p,b?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}:{});return r.json()}
function flash(t){$('#lstat').innerHTML='<span class="saved">'+t+'</span>';clearTimeout(flash.t);flash.t=setTimeout(()=>$('#lstat').textContent='',1800)}
function refreshPrev(){$('#prev').src='/api/preview.png?'+Date.now()}
function colorInput(m,sub,val){const i=document.createElement('input');i.type='color';i.value=val||'#ffffff';i.title='LED colour';
 const go=v=>api('/api/color',{uuid:m.uuid,sub,color:v}).then(()=>flash('Colour saved'));
 i.oninput=()=>{clearTimeout(i._t);i._t=setTimeout(()=>go(i.value),60)};
 i.onclick=e=>{if(brush){e.preventDefault();i.value=brush;go(brush)}};return i}
const TYPES={press:[['cc','CC'],['note','Note'],['program','Program'],['key','Keystroke']],
 pos:[['cc','CC'],['bend','Pitch bend'],['pressure','Aftertouch']],
 tilt:[['bend','Pitch bend'],['mod','Modulation'],['cc','CC'],['pressure','Aftertouch'],['scrollv','Scroll \u2195'],['scrollh','Scroll \u2194']],
 turn:[['cc','CC'],['scrollv','Scroll \u2195'],['scrollh','Scroll \u2194']]};
const NN=['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'];
const MODSYM={cmd:'\u2318',shift:'\u21E7',alt:'\u2325',ctrl:'\u2303',command:'\u2318',option:'\u2325',control:'\u2303',meta:'\u2318'};
function keyLabel(k,mods){if(!k)return'';return (mods||[]).map(m=>MODSYM[m]||m).join('')+(k.length===1?k.toUpperCase():k[0].toUpperCase()+k.slice(1))}
function codeToKey(e){const c=e.code;
 if(/^Key[A-Z]$/.test(c))return c.slice(3).toLowerCase();if(/^Digit\d$/.test(c))return c.slice(5);
 if(/^F\d+$/.test(c))return c.toLowerCase();if(/^Numpad\d$/.test(c))return 'num'+c.slice(6);
 const M={Space:'space',Enter:'return',Tab:'tab',Escape:'escape',Backspace:'delete',Delete:'forwarddelete',ArrowUp:'up',ArrowDown:'down',ArrowLeft:'left',ArrowRight:'right',
  Home:'home',End:'end',PageUp:'pageup',PageDown:'pagedown',Minus:'-',Equal:'=',BracketLeft:'[',BracketRight:']',Backslash:'\\',Semicolon:';',Quote:"'",Comma:',',Period:'.',Slash:'/',Backquote:'`',
  NumpadEnter:'numenter',NumpadAdd:'num+',NumpadSubtract:'num-',NumpadMultiply:'num*',NumpadDivide:'num/',NumpadDecimal:'num.'};return M[c]||null}
function midiCells(m,name,kind,cfg){
 cfg=cfg||{};const td=document.createElement('td');
 const cur='key' in cfg?'key':cfg.mod?'mod':cfg.scroll==='v'?'scrollv':cfg.scroll==='h'?'scrollh':cfg.program!=null?'program':'note' in cfg?'note':cfg.bend?'bend':cfg.pressure?'pressure':'cc';
 const type=document.createElement('select');TYPES[kind].forEach(([v,t])=>type.add(new Option(t,v)));type.value=cur;
 const num=document.createElement('input');num.type='number';num.min=0;num.max=127;
 num.value=cur==='program'?cfg.program:cur==='cc'?(cfg.cc??''):'';
 const noteSel=document.createElement('select');noteSel.title='Note (Logic naming: C3 = 60)';
 for(let n=0;n<128;n++)noteSel.add(new Option(NN[n%12]+(Math.floor(n/12)-2)+'  ('+n+')',n));noteSel.value=cur==='note'?cfg.note:36;
 // keystroke capture
 let kk=cfg.key||'',km=cfg.mods||[];
 const keyBtn=document.createElement('button');keyBtn.style.minWidth='120px';keyBtn.textContent=kk?keyLabel(kk,km):'Click, then press keys';
 keyBtn.onclick=()=>{keyBtn.textContent='Press a key\u2026';const h=e=>{e.preventDefault();e.stopPropagation();
   if(['Meta','Shift','Alt','Control'].includes(e.key))return;const k=codeToKey(e);if(!k){keyBtn.textContent='Unsupported key';return}
   kk=k;km=[];if(e.metaKey)km.push('cmd');if(e.ctrlKey)km.push('ctrl');if(e.altKey)km.push('alt');if(e.shiftKey)km.push('shift');
   keyBtn.textContent=keyLabel(kk,km);window.removeEventListener('keydown',h,true);save()};window.addEventListener('keydown',h,true)};
 // scroll speed + direction
 const speed=document.createElement('input');speed.type='number';speed.min=1;speed.max=200;speed.title='Scroll speed';
 speed.value=cfg.scroll?Math.abs(cfg.speed||20):(kind==='turn'?10:20);
 const dir=document.createElement('select');dir.innerHTML='<option value="1">Normal</option><option value="-1">Reversed</option>';dir.value=((cfg.speed||1)<0||cfg.invert)?'-1':'1';
 const mdir=document.createElement('select');mdir.title='Which way to tilt for modulation';mdir.innerHTML='<option value="up">Tilt up</option><option value="down">Tilt down</option><option value="both">Both ways</option>';mdir.value=cfg.dir||'up';
 const ch=document.createElement('select');ch.title='MIDI channel';for(let c=1;c<=16;c++)ch.add(new Option('Ch '+c,c));ch.value=cfg.channel||st.channel||1;
 const extra=document.createElement('select');
 if(kind==='turn'){extra.innerHTML='<option value="relative">Relative</option><option value="absolute">Absolute</option>';extra.value=cfg.mode||st.dial_mode||'relative';extra.title='Relative = two\u2019s complement (MIDI2LR / Logic \u201cRelative\u201d); Absolute = 0-127'}
 else if(kind==='press'){extra.innerHTML='<option value="mom">Momentary</option><option value="tog">Toggle</option>';extra.value=cfg.toggle?'tog':'mom'}
 const vis=()=>{const t=type.value,isScroll=t.startsWith('scroll'),isMidi=!isScroll&&t!=='key';
  num.style.display=(t==='cc'||t==='program')?'':'none';noteSel.style.display=t==='note'?'':'none';
  keyBtn.style.display=t==='key'?'':'none';speed.style.display=isScroll?'':'none';
  dir.style.display=(isScroll||(kind==='tilt'&&t==='bend'))?'':'none';mdir.style.display=t==='mod'?'':'none';
  ch.style.display=isMidi?'':'none';
  extra.style.display=((kind==='turn'||kind==='press')&&t==='cc')?'':'none'};vis();
 function save(){vis();const t=type.value;let c;
  if(t==='key'){if(!kk)return;c={key:kk,mods:km}}
  else if(t.startsWith('scroll')){c={scroll:t==='scrollv'?'v':'h',speed:(+speed.value||10)*(+dir.value)}}
  else{c={channel:+ch.value};
   if(t==='bend'){c.bend=true;if(kind==='tilt'&&dir.value==='-1')c.invert=true}
   else if(t==='mod'){c.mod=true;c.cc=1;c.dir=mdir.value}
   else if(t==='pressure')c.pressure=true;else if(t==='note'){c.note=+noteSel.value;c.velocity=cfg.velocity||127}
   else{const n=+num.value;if(num.value===''||n<0||n>127)return;if(t==='program')c.program=n;else{c.cc=n;if(kind==='press'){c.on=127;c.off=0;c.toggle=extra.value==='tog'}}}
   if(kind==='turn')c.mode=extra.value}
  api('/api/midi',{uuid:m.uuid,control:name,cfg:c}).then(()=>flash('Saved'))}
 [type,num,noteSel,speed,dir,mdir,ch,extra].forEach(e=>e.onchange=save);
 td.append(type,' ',num,noteSel,keyBtn,speed,' ',dir,mdir,' ',ch,' ',extra);return td}
async function load(){st=await api('/api/state');$('#sub').textContent=st.connected?`Connected · ${st.modules.length} modules · MIDI port “Monobridge”`:'Waiting for the console…';
 $('#label').value=st.screen.label||'';rot=st.screen.rotate||0;$('#rotv').textContent=rot+'°';$('#size').value=st.screen.size||140;$('#sizev').textContent=$('#size').value+' px';
 const box=$('#mods');box.innerHTML='';
 for(const m of st.modules){if(!(m.type in CTRLS))continue;
  const d=document.createElement('div');d.className='mod';const col=st.colors[m.uuid]||{},midi=st.midi[m.uuid]||{};
  d.innerHTML=`<span class="mtitle">${TITLES[m.type]||m.type}</span><span class="muid">#${m.id}</span>`;
  const tb=document.createElement('table');
  if(SINGLE_LED[m.type]){const tr=tb.insertRow();tr.insertCell().className='n';tr.cells[0].textContent='Light';tr.insertCell().append(colorInput(m,null,col.main))}
  for(const [name,title,kind,sub] of CTRLS[m.type]){const tr=tb.insertRow();const n=tr.insertCell();n.className='n';
   if(sub!=null&&sub!==undefined&&!SINGLE_LED[m.type]){n.append(colorInput(m,sub,(col.subs||{})[sub]),' ')}n.append(title);tr.append(midiCells(m,name,kind,midi[name]))}
  d.append(tb);box.append(d)}
 refreshPrev()}
function queueScreen(){clearTimeout(timer);timer=setTimeout(async()=>{const b={label:$('#label').value,rotate:rot,size:+$('#size').value};if(logo!==undefined)b.icon=logo;
 await api('/api/screen',b);logo=undefined;refreshPrev();$('#sstat').textContent='Changed - press “Send to console”'},250)}
$('#label').oninput=queueScreen;
$('#size').oninput=()=>{$('#sizev').textContent=$('#size').value+' px';queueScreen()};
$('#fit').onclick=()=>{$('#size').value=$('#label').value.trim()?170:240;$('#size').oninput()};
document.querySelectorAll('[data-r]').forEach(b=>b.onclick=()=>{rot=(((Math.round(rot/90)*90+(+b.dataset.r))%360)+360)%360;if(rot>180)rot-=360;$('#rotv').textContent=rot+'°';queueScreen()});
$('#logo').onchange=e=>{const f=e.target.files[0];if(!f)return;const r=new FileReader();r.onload=()=>{logo=r.result.split(',')[1];queueScreen()};r.readAsDataURL(f)};
$('#nologo').onclick=()=>{logo=null;$('#logo').value='';queueScreen()};
$('#send').onclick=async()=>{$('#sstat').textContent='Sending… (about 4 s)';await api('/api/send_screen',{});$('#sstat').innerHTML='<span class="saved">Sent ✓</span>'};
SW.forEach(c=>{const s=document.createElement('span');s.style.background=c;s.title=c;s.onclick=()=>{brush=brush===c?null:c;
 document.querySelectorAll('#sw span').forEach(x=>x.style.outline='');if(brush)s.style.outline='2px solid var(--acc)';
 $('#lstat').textContent=brush?`Painting ${brush}: click colour boxes (click the swatch again to stop).`:''};$('#sw').append(s)});
load();setInterval(async()=>{const s=await api('/api/state');if(!st||s.modules.map(m=>m.uuid).join()!==st.modules.map(m=>m.uuid).join())load()},3000);
</script></body></html>"""


def start_ui(bridge, port, on_change):
    import io
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype="application/json", code=200):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/":
                return self._send(UI_HTML, "text/html; charset=utf-8")
            if path == "/api/state":
                return self._send({
                    "connected": bool(bridge.modules),
                    "modules": [{"id": l, **{k: i[k] for k in ("type", "uuid", "depth")}}
                                for l, i in bridge.modules.items()],
                    "colors": bridge.map.get("colors", {}),
                    "midi": bridge.map.get("modules", {}),
                    "channel": bridge.map.get("channel", 1),
                    "dial_mode": bridge.map.get("dial_mode", "relative"),
                    "screen": {k: v for k, v in (bridge.map.get("screen") or {}).items() if k != "icon"},
                })
            if path == "/api/preview.png":
                buf = io.BytesIO()
                bridge.screen_image(upright=True).save(buf, "PNG")
                return self._send(buf.getvalue(), "image/png")
            self._send({"error": "not found"}, code=404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send({"error": "bad json"}, code=400)
            path = self.path.split("?")[0]
            if path == "/api/color":
                bridge.set_color(body["uuid"], body["color"], body.get("sub"))
            elif path == "/api/screen":
                s = bridge.map.setdefault("screen", {"icon": None, "label": "", "rotate": 0})
                for k in ("label", "rotate", "icon", "size"):
                    if k in body:
                        s[k] = body[k]
            elif path == "/api/midi":
                bridge.set_midi(body["uuid"], body["control"], body["cfg"])
            elif path == "/api/send_screen":
                bridge.draw_screen()
            else:
                return self._send({"error": "not found"}, code=404)
            on_change()
            self._send({"ok": True})

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ---------------------------------------------------------------- CLI
def cmd_ports(_):
    for p in serial.tools.list_ports.comports():
        tag = "  <-- Monogram Core" if (p.vid, p.pid) == (CORE_VID, CORE_PID) else ""
        vid = f"{p.vid:04x}:{p.pid:04x}" if p.vid else "----:----"
        print(f"{p.device:<35} {vid}  {p.description}{tag}")


def cmd_monitor(a):
    core = Core(a.port)
    print(f"Connected to {core.port_name}. Move some controls (Ctrl-C to quit).")
    core.send({"get_version": True})
    core.send({"get_layout": True})
    core.start_pinging()
    try:
        for msg in core.messages():
            if a.quiet and set(msg) <= {"l_time"}:
                continue  # ping replies
            print(json.dumps(msg, default=str))
    except KeyboardInterrupt:
        core.close()


class Service:
    """Long-running bridge: waits for the console, reconnects after unplug/sleep,
    keeps one MIDI port and the control panel alive throughout."""

    def __init__(self, config_path, profile=None, ui_port=8765, midi_name="Monobridge",
                 verbose=False, rotate=0, ui=True):
        self.config_path = config_path
        mapping = {}
        if config_path:
            try:
                with open(config_path) as f:
                    mapping = json.load(f)
            except (FileNotFoundError, ValueError):
                pass
        self.midi = Midi(midi_name)
        self.midi.verbose = verbose
        self.bridge = Bridge(None, self.midi, mapping, profile, rotate)
        self.status = "Starting…"
        self.connected = False
        self.ui_port = ui_port
        self._save_lock = threading.Lock()
        self._stop = False
        self.ui_ok = False
        if ui:
            try:
                start_ui(self.bridge, ui_port, self.save)
                self.ui_ok = True
            except OSError as e:
                print(f"Control panel not started ({e})")

    @property
    def url(self):
        return f"http://localhost:{self.ui_port}"

    def save(self):
        if not self.config_path:
            return
        with self._save_lock:
            tmp = self.config_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.bridge.map, f, indent=2)
            os.replace(tmp, self.config_path)

    def _set(self, status, connected=False):
        if status != self.status:
            print(status)
        self.status, self.connected = status, connected

    def stop(self):
        self._stop = True

    def run_forever(self):
        b = self.bridge
        while not self._stop:
            port = find_core()
            if not port:
                self._set("Waiting for the console…")
                time.sleep(2)
                continue
            try:
                core = Core(port)
            except Exception:
                self._set("Console busy - is Monogram Creator still running?")
                time.sleep(3)
                continue
            b.core, b._screen_done, b._l_time = core, False, None
            core.send({"get_version": True})
            core.send({"get_layout": True})
            core.start_pinging()
            self._set("Connected", True)
            try:
                for msg in core.messages():
                    if self._stop:
                        break
                    if "version_core" in msg:
                        print(f"Core firmware {msg['version_core']}")
                    try:
                        b.handle(msg)
                    except Exception as e:            # never let one bad message kill the bridge
                        print(f"Error handling {msg}: {e!r}")
            except (serial.SerialException, OSError):
                pass
            finally:
                core.close()
                b.core, b.modules = None, {}
                for k, mods in list(b._held.values()):   # release any held keys
                    try:
                        b.keyboard().key(k, mods, False)
                    except Exception:
                        pass
                b._held.clear()
                b._tilt.clear()
                self.save()
                self._set("Console disconnected - waiting…")
            time.sleep(1)
        self.save()


def cmd_run(a):
    profile = Profile(a.profile) if a.profile else None
    svc = Service(a.config, profile, a.ui_port, a.name, a.verbose, a.rotate, ui=not a.no_ui)
    if profile:
        print(f"Using Monogram profile '{profile.title}'")
    print(f"MIDI port '{a.name}' is live.")
    if svc.ui_ok:
        print(f"Control panel: {svc.url}")
        if not a.no_browser:
            import webbrowser
            threading.Timer(1.5, lambda: webbrowser.open(svc.url)).start()
    try:
        svc.run_forever()
    except KeyboardInterrupt:
        svc.save()
        if a.config:
            print(f"Settings saved to {a.config}")


def _wait_layout(core, timeout=3):
    core.send({"get_layout": True})
    end = time.time() + timeout
    for msg in core.messages():
        if "l" in msg:
            return walk_layout(msg["l"])
        if time.time() > end:
            sys.exit("No layout received from the Core.")


def cmd_screen(a):
    core = Core(a.port)
    if a.profile:
        p = Profile(a.profile)
        icon, label = p.icon, a.label if a.label is not None else p.title
    else:
        icon, label = a.image, a.label or ""
    img = render_screen(icon, label, rotate=a.rotate)
    if a.preview:
        img.save(a.preview)
        print(f"Preview saved to {a.preview}")
    t = time.time()
    write_screen(core, img)
    time.sleep(0.3)
    print(f"Sent to display in {time.time() - t:.1f}s")
    core.close()


def cmd_leds(a):
    core = Core(a.port)
    mods = _wait_layout(core)
    palette = ["#0000ff", "#00ff00", "#ff00ff", "#ffff00"]   # blue, green, pink, yellow
    for lid, info in mods.items():
        t = info["type"]
        if t in SUBMODULE_TYPES:
            for k in range(3):
                set_color(core, lid, palette[k % 4], k)
            commit_colors(core, lid)
        else:
            set_color(core, lid, palette[3] if t == "core" else palette[0])
        print(f"  #{lid} {t}: coloured")
    time.sleep(0.3)
    core.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (auto-detected by default)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ports").set_defaults(fn=cmd_ports)
    m = sub.add_parser("monitor")
    m.add_argument("-q", "--quiet", action="store_true", help="hide ping replies")
    m.set_defaults(fn=cmd_monitor)
    r = sub.add_parser("run")
    r.add_argument("-c", "--config", default="mapping.json")
    r.add_argument("--save", action="store_true", help="write auto-assignments back to the config")
    r.add_argument("--name", default="Monobridge", help="virtual MIDI port name")
    r.add_argument("-v", "--verbose", action="store_true", help="print every MIDI message sent")
    r.add_argument("-p", "--profile", help="Monogram Creator .monogram profile to load (colours, icon, MIDI)")
    r.add_argument("--rotate", type=int, default=0, help="display rotation in degrees clockwise (negative = anticlockwise)")
    r.add_argument("--ui-port", type=int, default=8765, help="control panel port")
    r.add_argument("--no-ui", action="store_true", help="don't start the control panel")
    r.add_argument("--no-browser", action="store_true", help="don't open the control panel automatically")
    sc = sub.add_parser("screen", help="draw an icon + label on the Core display")
    sc.add_argument("--image", help="PNG/JPG file")
    sc.add_argument("--label", help="text under the icon")
    sc.add_argument("-p", "--profile", help="take icon + title from a .monogram profile")
    sc.add_argument("--rotate", type=int, default=0)
    sc.add_argument("--preview", help="also save the rendered image to this PNG")
    sc.set_defaults(fn=cmd_screen)
    sub.add_parser("leds", help="light every module blue/green/pink/yellow").set_defaults(fn=cmd_leds)
    r.set_defaults(fn=cmd_run)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
