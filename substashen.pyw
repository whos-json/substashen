"""Substashen: an always-on-top instrument panel for two RTX 5080s and the MSI PSU's 12V-2x6 pin currents.
Data comes from MSI Afterburner's shared memory (GPU graphs + PSU plugin enabled in Settings > Monitoring).

Protection
  * any 12V-2x6 pin over 8 A (twice in a row, or once over 10 A), or the PSU's own Safeguard+ alarm:
    closes every program on the NVIDIA GPUs (again every 3 s while the fault lasts), sounds a warning
    and covers every monitor with a flashing alert until acknowledged
  * PSU +12V output outside 11.6-12.6 V for 3 s: warning and cover, nothing closed
  * no fresh pin data for 30 s (Afterburner closed or hung, PSU plugin off or frozen): warning and cover once

Run:  pythonw substashen.pyw
      python substashen.pyw --selftest     check the rules and the Afterburner name mapping
      python substashen.pyw --demo         fake data (--demo-fault adds an 8.4 A pin; demo never closes programs)
Drag to move (remembered). TEST rings the alarm, DATA lists every reading, X closes (asks first).
"""
import array, collections, ctypes, datetime, json, math, os, re, struct, subprocess, sys, tempfile, threading, time
import traceback, wave, winsound
import tkinter as tk
import tkinter.font as tkfont

APP = "Substashen"
PIN_MAX_A, PIN_WARN_A, PIN_HARD_A, PIN_SANE_A = 8.0, 6.0, 10.0, 30.0  # limit / amber / alert on one sample / garbage
V_MIN, V_MAX = 11.6, 12.6  # checked on the PSU's +12V output (Afterburner has no GPU-side 12 V input reading)
GPU_TDP_W = 360            # RTX 5080 default power limit; fixed scale for the power trace
STALE_S, FROZEN_S, OFFLINE_ALERT_S, STARTUP_GRACE_S = 5, 8, 30, 120  # frozen: real pins jitter every second under load
SNOOZE_S, KILL_EVERY_S, TRACE_EVERY_S = 60, 3, 3
PIN_RE = re.compile(r"\bpin", re.I)
GPU_RE = re.compile(r"GPU \[#(\d+)\]: NVIDIA")
ALARM_TEXT = {1: "pin over its limit", 2: "pins out of balance", 3: "over 18 A"}  # PSU GPU Safeguard+ status codes
SAFE_DIRS = (os.environ.get("SystemRoot", r"C:\Windows").lower().rstrip("\\") + "\\",)  # not C:\Windows.old
SAFE_EXE = {"msiafterburner.exe", "rtss.exe"}
HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE, LOG_FILE, ICON_FILE = (os.path.join(HERE, n) for n in ("settings.json", "substashen.log", "substashen.ico"))
ZOOM_MIN, ZOOM_MAX = 0.8, 2.0  # below 0.8 the smallest text drops under 10 px
NOWIN = 0x08000000  # CREATE_NO_WINDOW for child processes
DEMO = DEMO_FAULT = False

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # crisp text on scaled displays
except Exception:
    pass
for _f in os.listdir(os.path.join(HERE, "fonts")) if os.path.isdir(os.path.join(HERE, "fonts")) else []:
    if _f.endswith(".ttf"):  # IBM Plex (Google Fonts, OFL), registered for this process only
        ctypes.windll.gdi32.AddFontResourceExW(os.path.join(HERE, "fonts", _f), 0x10, 0)


def log(msg):
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 1 << 20:
            os.replace(LOG_FILE, LOG_FILE + ".old")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except OSError:
        pass


# ---------- MSI Afterburner shared memory (MAHM v2) ----------
k32 = ctypes.windll.kernel32
k32.OpenFileMappingW.restype = ctypes.c_void_p
k32.MapViewOfFile.restype = ctypes.c_void_p
k32.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_size_t]
k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
k32.CloseHandle.argtypes = [ctypes.c_void_p]
k32.VirtualQuery.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
k32.OpenProcess.restype = ctypes.c_void_p


class _MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p), ("AllocationProtect", ctypes.c_ulong),
                ("PartitionId", ctypes.c_ushort), ("RegionSize", ctypes.c_size_t), ("State", ctypes.c_ulong),
                ("Protect", ctypes.c_ulong), ("Type", ctypes.c_ulong)]


_map = None  # (handle, view, size)

PSU_MAP = {"PSU 12V current": "+12V Current", "PSU 5V current": "+5V Current", "PSU 3.3V current": "+3.3V Current",
           "PSU power": "PSU Power Out", "PSU efficiency": "PSU Efficiency", "PSU temperature": "PSU Temperature",
           "PSU fan speed": "PSU Fan", "PSU 12V voltage": "+12V", "PSU 5V voltage": "+5V", "PSU 3.3V voltage": "+3.3V"}
GPU_MAP = {"temperature": "GPU Temperature", "usage": "GPU Core Load", "fb usage": "GPU Memory Controller Load",
           "memory usage": "GPU Memory Allocated", "core clock": "GPU Clock", "memory clock": "GPU Memory Clock",
           "power": "GPU Power", "power percent": "GPU Power %", "fan speed": "GPU Fan %", "fan tachometer": "GPU Fan1",
           "fan tachometer 2": "GPU Fan2", "bus usage": "GPU Bus Load"}
BRANDS = {"1028": "Dell", "1462": "MSI", "1043": "ASUS", "3842": "EVGA", "1458": "Gigabyte", "19da": "Zotac",
          "10de": "NVIDIA", "1569": "Palit", "196e": "PNY"}


def _s(b):
    return b.split(b"\0")[0].decode("latin1")


def _unmap():
    global _map, _layout
    _layout = None
    if _map:
        k32.UnmapViewOfFile(_map[1])
        k32.CloseHandle(_map[0])
    _map = None


def read_afterburner():
    """Returns (normalised readings, seconds since Afterburner last updated), or None if Afterburner isn't there."""
    global _map
    if _map is None:
        h = k32.OpenFileMappingW(4, False, "MAHMSharedMemory")
        if not h:
            return None
        view = k32.MapViewOfFile(h, 4, 0, 0, 0)
        mbi = _MBI()
        if not view or not k32.VirtualQuery(view, ctypes.byref(mbi), ctypes.sizeof(mbi)):
            k32.CloseHandle(h)
            return None
        _map = (h, view, mbi.RegionSize)
    h, view, size = _map
    sig, _ver, hs, n, es, t, ng, gs = struct.unpack("<IIIIIiII", ctypes.string_at(view, 32))
    total = hs + n * es + ng * gs
    if sig != 0x4D41484D or es < 1320 or gs < 1304 or n > 4096 or ng > 64 or total > size:
        _unmap()  # 'MAHM' gone (Afterburner wrote 0xDEAD on exit) or header mid-rewrite: never read past the view
        return None
    global _layout
    key = (hs, n, es, ng, gs)
    # rebuild on a new header, a renamed/moved sensor, or while incomplete (Afterburner may still be filling names in)
    if _layout is None or _layout[0] != key or not _layout[2] or any(ctypes.string_at(view + b, len(raw)) != raw
                                                                     for b, raw, *_ in _layout[1]):
        # map the sensors we use once; every read re-checks their names, so a reordered list is re-mapped
        m = ctypes.string_at(view, total)
        nv = nvidia_cards([(_s(m[b:b + 260]), _s(m[b + 520:b + 780])) for b in (hs + n * es + j * gs for j in range(ng))])
        rows = []
        for i in range(n):
            b = hs + i * es
            raw = m[b:b + 260].split(b"\0")[0] + b"\0"
            meta = classify(raw[:-1].decode("latin1"), _s(m[b + 260:b + 520]), struct.unpack_from("<I", m, b + 1316)[0], nv)
            if meta:
                rows.append((b, raw, *meta))
        complete = sum(1 for r in rows if "Pin" in r[3]) >= 12 and len({r[2] for r in rows if r[2] != "MSI PSU"}) >= 2
        _layout = (key, rows, complete)
    out = []
    for b, _raw, sensor, label, unit in _layout[1]:
        val = ctypes.c_float.from_address(view + b + 1300).value  # read just the value, in place
        if math.isfinite(val) and abs(val) <= 1e30:  # NaN/inf, or FLT_MAX = Afterburner's "no data"
            out.append((sensor, label, val, unit))
    return out, time.time() - t, t


_layout = None  # (header key, [(entry offset, name bytes, sensor, label, unit)], complete)


def nvidia_cards(gpus):
    """Afterburner GPU index -> panel sensor name. Renumbered #0, #1 over NVIDIA cards only (skips the iGPU)."""
    nv = {}
    for j, (gid, dev) in enumerate(gpus):
        if "VEN_10DE" in gid.upper():
            sub = re.search(r"SUBSYS_[0-9A-F]{4}([0-9A-F]{4})", gid.upper())
            nv[j] = f"GPU [#{len(nv)}]: {dev}: {BRANDS.get(sub.group(1).lower(), '') if sub else ''}"
    return nv


def classify(name, unit, gpu, nv):
    """Afterburner name -> (sensor, label, unit) the panel uses, or None for sensors it ignores."""
    if name.startswith("PSU "):
        pin = re.fullmatch(r"PSU 12VHPWR(\d)_(\d) current", name)
        alarm = re.fullmatch(r"PSU 12VHPWR(\d) alarm", name)
        label = (f"+12V-2x6_{pin.group(1)} Pin{pin.group(2)} Current" if pin
                 else f"12V-2x6_{alarm.group(1)} Alarm" if alarm else PSU_MAP.get(name, name))
        return "MSI PSU", label, "A" if pin else unit
    if gpu in nv and name.startswith("GPU"):
        return nv[gpu], GPU_MAP.get(re.sub(r"^GPU\d*\s+", "", name).lower(), name), unit
    return None


def normalise(entries, gpus):
    """(name, unit, value, gpu index) entries -> panel readings; drops ignored sensors and "no data" values."""
    nv = nvidia_cards(gpus)
    out = []
    for name, unit, val, gpu in entries:
        meta = classify(name, unit, gpu, nv)
        if meta and math.isfinite(val) and abs(val) <= 1e30:
            out.append((meta[0], meta[1], val, meta[2]))
    return out


def read_oc():
    """Afterburner's applied settings per NVIDIA card, {"0": {"power": %, "core": MHz, "mem": MHz, "temp": C}}.
    Read-only view of its control memory (MACMSharedMemory, SDK v2.0); a field is None if the card doesn't expose it."""
    if DEMO:
        return {g: {"power": 75, "core": -502, "mem": 0, "temp": None, "bus": b} for g, b in (("0", 1), ("1", 3))}
    h = k32.OpenFileMappingW(4, False, "MACMSharedMemory")  # FILE_MAP_READ: this view can never write a setting
    if not h:
        return {}
    view = k32.MapViewOfFile(h, 4, 0, 0, 0)
    try:
        mbi = _MBI()
        if not view or not k32.VirtualQuery(view, ctypes.byref(mbi), ctypes.sizeof(mbi)):
            return {}
        sig, _ver, hs, n, es = struct.unpack("<5I", ctypes.string_at(view, 20))
        if sig != 0x4D41434D or es < 600 or n > 64 or hs + n * es > mbi.RegionSize:
            return {}
        m = ctypes.string_at(view + hs, n * es)
    finally:
        if view:
            k32.UnmapViewOfFile(view)
        k32.CloseHandle(h)
    return parse_oc(m, n, es)


def parse_oc(m, n, es):
    out, nv = {}, 0
    for j in range(n):
        b = j * es
        if "VEN_10DE" not in _s(m[b + es - 260:b + es]).upper():
            continue  # numbered over NVIDIA cards only, the same way nvidia_cards() does
        d = struct.unpack_from("<69i", m, b)  # DWORD/LONG fields of MACM_SHARED_MEMORY_GPU_ENTRY
        fl = d[0] & 0xFFFFFFFF
        bus = re.search(r"BUS_(\d+)", _s(m[b + es - 260:b + es]).upper())
        out[str(nv)] = {"power": d[43] if fl & 0x400 else None, "core": round(d[47] / 1000) if fl & 0x800 else None,
                        "mem": round(d[51] / 1000) if fl & 0x1000 else None, "temp": d[55] if fl & 0x2000 else None,
                       "bus": int(bus.group(1)) if bus else None}
        nv += 1
    return out


class _TS(ctypes.Structure):  # NV_GPU_THERMAL_SENSORS v2 (undocumented NVAPI query, values in 1/256 degC)
    _fields_ = [("version", ctypes.c_uint32), ("mask", ctypes.c_uint32), ("res", ctypes.c_int32 * 8),
                ("t", ctypes.c_int32 * 32)]


_nvapi = None


def mem_temps():
    """{PCI bus: GDDR7 temperature} per NVIDIA card from NVAPI's thermal sensors. A read-only query; nothing here
    can change a setting. shortcut: sensor 2 is memory on RTX 50 (NVIDIA dropped hotspot there); older cards differ."""
    global _nvapi
    if DEMO:
        return {1: 52.0, 3: 55.0}
    try:
        if _nvapi is None:
            _nvapi = False  # one attempt: no driver API means no memory temperature, never a retry loop
            qi = ctypes.WinDLL("nvapi64.dll").nvapi_QueryInterface
            qi.restype, qi.argtypes = ctypes.c_void_p, [ctypes.c_uint32]

            def fn(i, *args):
                p = qi(i)
                if not p:
                    raise OSError(f"nvapi {i:#x} missing")
                return ctypes.CFUNCTYPE(ctypes.c_int, *args)(p)
            if fn(0x0150E828)() != 0:  # NvAPI_Initialize
                return {}
            hs, n = (ctypes.c_void_p * 64)(), ctypes.c_uint32()
            if fn(0xE5AC921F, ctypes.c_void_p, ctypes.c_void_p)(ctypes.byref(hs), ctypes.byref(n)) != 0:
                return {}
            get_bus = fn(0x1BE0B8E5, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
            by_bus = {}
            for k in range(min(n.value, 64)):
                b = ctypes.c_uint32()
                if get_bus(hs[k], ctypes.byref(b)) == 0:
                    by_bus[b.value] = hs[k]
            _nvapi = (fn(0x65FE3AAD, ctypes.c_void_p, ctypes.POINTER(_TS)), by_bus)
        if not _nvapi:
            return {}
        get, by_bus = _nvapi
        out = {}
        for bus, h in by_bus.items():
            s = _TS(version=ctypes.sizeof(_TS) | 2 << 16, mask=0b111)
            if get(h, ctypes.byref(s)) == 0 and 0 < s.t[2] / 256 < 150:  # 255 = no sensor
                out[bus] = s.t[2] / 256
        return out
    except Exception:
        log("memory temperature unavailable:\n" + traceback.format_exc())
        _nvapi = False
        return {}


def demo_readings():
    t, r = time.time(), []
    for g, brand, base in ((0, "Dell", 62), (1, "MSI", 38)):
        s = f"GPU [#{g}]: NVIDIA GeForce RTX 5080: {brand}"
        ld = max(0.0, base + 25 * math.sin(t / 9 + g) + 8 * math.sin(t / 2.3))
        r += [(s, "GPU Temperature", 38 + ld / 3, "\xb0C"), (s, "GPU Core Load", ld, "%"),
              (s, "GPU Memory Controller Load", ld / 2, "%"), (s, "GPU Memory Allocated", 6200, "MB"),
              (s, "GPU Clock", 2600 + ld, "MHz"), (s, "GPU Memory Clock", 15001, "MHz"), (s, "GPU Power", ld * 3.2, "W"),
              (s, "GPU Power %", ld * 0.9, "%"), (s, "GPU Fan %", 30 + ld / 3, "%"), (s, "GPU Fan1", 900 + ld * 10, "RPM"),
              (s, "GPU Fan2", 860 + ld * 10, "RPM"), (s, "GPU1 power limit", 1.0 if ld > 80 else 0.0, ""),
              (s, "GPU1 voltage limit", 0.0, "")]
    psu = "MSI PSU"
    r += [(psu, "PSU Temperature", 39, "\xb0C"), (psu, "PSU Fan", 620, "RPM"),
          (psu, "+12V", 12.09, "V"), (psu, "+5V", 5.05, "V"), (psu, "+3.3V", 3.33, "V"),
          (psu, "+12V Current", 41, "A"), (psu, "+5V Current", 2.9, "A"), (psu, "+3.3V Current", 2.6, "A"),
          (psu, "PSU Power Out", 520, "W"), (psu, "PSU Efficiency", 91.5, "%"),
          (psu, "12V-2x6_1 Alarm", 0, ""), (psu, "12V-2x6_2 Alarm", 0, "")]
    for c in (1, 2):
        for p in range(1, 7):
            v = 8.4 if DEMO_FAULT and (c, p) == (1, 4) else 4.6 + 1.6 * math.sin(t / 3 + p * 1.7 + c)
            r.append((psu, f"+12V-2x6_{c} Pin{p} Current", v, "A"))
    return r, 0.0, time.time()


# ---------- rules ----------
def pin_name(label):
    m = re.search(r"_(\d+)\s*pin\s*(\d+)", label, re.I)
    return f"Cable {m.group(1)}, pin {m.group(2)}" if m else label


def check(readings):
    """One sample -> list of fault dicts (pin faults first). Debouncing happens in App.evaluate."""
    faults = []
    for sensor, label, val, unit in readings:
        if not math.isfinite(val):  # defence in depth: one bad float must never stop the rules running
            continue
        if sensor == "MSI PSU" and label == "+12V" and not (V_MIN <= val <= V_MAX):
            faults.append({"kind": "volt", "title": "12 V rail out of range", "where": "PSU +12V output",
                           "val": val, "unit": "V", "limit": f"{V_MIN}-{V_MAX} V"})
        if unit == "A" and PIN_RE.search(label) and PIN_MAX_A < val < PIN_SANE_A:
            faults.append({"kind": "pin", "title": "12V-2x6 overcurrent", "where": pin_name(label), "hard": val > PIN_HARD_A,
                           "val": val, "unit": "A", "limit": f"{PIN_MAX_A:.1f} A per pin"})
        alarm = re.fullmatch(r"12V-2x6_(\d) Alarm", label)
        if alarm and val:
            faults.append({"kind": "pin", "title": "PSU Safeguard+ alarm", "where": f"Cable {alarm.group(1)}",
                           "hard": True, "show": "ALARM", "val": val, "unit": "",
                           "limit": f"PSU reports {ALARM_TEXT.get(int(val), f'status {int(val)}')}"})
    faults.sort(key=lambda f: f["kind"] != "pin")
    return faults


def coverage(readings, age):
    """None when the pin protection has what it needs, else a short reason."""
    if age > STALE_S:
        return f"Afterburner stopped updating {age:.0f} s ago"
    pins = sum(1 for s, l, v, u in readings if s == "MSI PSU" and u == "A" and PIN_RE.search(l) and 0 <= v < PIN_SANE_A)
    if pins < 12:
        return f"only {pins} of 12 pin readings" if pins else "no PSU pin readings"
    if not any(s == "MSI PSU" and l == "+12V" for s, l, v, u in readings):
        return "no PSU 12 V reading"
    return None


def fault_value(f):
    return f.get("show") or f"{f['val']:.2f} {f['unit']}"


VIEWS = ("full", "standard")
SEVERITY = {"test": 0, "offline": 1, "volt": 2, "pin": 3}  # a test never outranks or snoozes a real fault

# Warn-only rules: amber WARNING + incident log, never close anything. Thresholds are starting points to tune
# against incidents.log; the 8 A pin rule above stays the only one that closes programs.
IMB_SPREAD_A, IMB_MIN_TOTAL_A, IMB_HOLD_S = 4.0, 15.0, 5  # one cable's pins far apart under real load
CAP_MARGIN_W, CAP_HOLD_S = 20, 30                          # card above its power cap (settings "power_cap_w")
HEAT_RMS_A, HEAT_TAU_S = 7.0, 120                          # a pin carrying a lot for minutes, not just a spike
INCIDENT_FILE = os.path.join(HERE, "incidents.log")


def advisory_checks(pins, gpu_power, cap_w, mem, now, dt):
    """Warn-only rules. `mem` keeps timers and running averages between calls. Returns {key: short message}."""
    active = {}
    for n, vals in pins.items():
        vals = [(pin, a) for pin, a in vals if 0 <= a < PIN_SANE_A]  # garbage readings are the 8 A rule's business
        amps = [a for _, a in vals]
        k = f"imb{n}"
        if len(amps) >= 2 and sum(amps) >= IMB_MIN_TOTAL_A and max(amps) - min(amps) >= IMB_SPREAD_A:
            mem.setdefault(k, now)
            if now - mem[k] >= IMB_HOLD_S:
                active[k] = f"Cable {n} uneven: pins differ by {max(amps) - min(amps):.1f} A"
        else:
            mem.pop(k, None)
        for pin, a in vals:  # exponential average of I^2: heat in a contact goes with current squared
            k = f"heat{n}_{pin}"
            mem[k] = mem.get(k, 0.0) + (a * a - mem.get(k, 0.0)) * min(1.0, dt / HEAT_TAU_S)  # builds up from 0
            if mem[k] ** 0.5 >= HEAT_RMS_A:
                active[k] = f"Cable {n} pin {pin} hot: {mem[k] ** 0.5:.1f} A sustained"
    for g, w in gpu_power.items():
        k = f"cap{g}"
        if w is not None and w > cap_w + CAP_MARGIN_W:
            mem.setdefault(k, now)
            if now - mem[k] >= CAP_HOLD_S:
                active[k] = f"GPU{g} {w:.0f} W, over your {cap_w:.0f} W cap"
        else:
            mem.pop(k, None)
    return active


def incident(kind, text, snap=""):
    """One line per event in incidents.log, so what happened while you were away can be read later."""
    if DEMO:
        return
    try:
        if os.path.exists(INCIDENT_FILE) and os.path.getsize(INCIDENT_FILE) > 1 << 20:
            os.replace(INCIDENT_FILE, INCIDENT_FILE + ".old")
        with open(INCIDENT_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {kind:<8} {text}" + (f"  |  {snap}" if snap else "") + "\n")
    except OSError:
        pass


# ---------- closing GPU programs ----------
def _exe_path(pid):
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf, n = ctypes.create_unicode_buffer(1024), ctypes.c_ulong(1024)
        return buf.value if k32.QueryFullProcessImageNameW(ctypes.c_void_p(h), 0, buf, ctypes.byref(n)) else None
    finally:
        k32.CloseHandle(h)


def run_bounded(args, timeout):
    """subprocess.run waits for a killed child on Windows; a driver-hung nvidia-smi would block forever."""
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, creationflags=NOWIN)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out
    except subprocess.TimeoutExpired:
        p.kill()  # don't wait for it
        raise


def gpu_pids():
    """(PIDs, ok). Compute query plus the full table, which also lists pure-graphics (G) games.
    ok is False when nvidia-smi failed, so the alert never claims 'nothing to close' on a failed lookup."""
    pids, ok = set(), True
    for args, rx in ((["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader"], r"^\s*(\d+)\s*$"),
                     (["nvidia-smi"], r"^\|\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(?:C\+G|C|G)\s")):
        try:
            code, out = run_bounded(args, 8)
        except Exception as e:
            log(f"nvidia-smi failed: {e}")
            ok = False
            continue
        if code != 0 or (len(args) == 1 and "Processes:" not in out):
            log(f"nvidia-smi {' '.join(args[1:]) or 'table'} returned {code}")
            ok = False
        found = re.findall(rx, out, re.M)
        if len(args) == 1 and code == 0 and not found and "No running processes found" not in out:
            log("nvidia-smi process table in an unrecognised format")  # never report "nothing to close" on that
            ok = False
        pids.update(int(p) for p in found)
    return pids, ok


def kill_gpu_tasks():
    """Returns (closed names, names that couldn't be closed). Spares Windows, Afterburner/RTSS and this monitor."""
    if DEMO:
        return ["nothing (demo mode)"], []
    closed, failed = set(), set()
    pids, ok = gpu_pids()
    if not ok:
        failed.add("(couldn't list GPU programs: nvidia-smi failed)")
    for pid in pids - {os.getpid()}:
        path = _exe_path(pid) or ""
        name = os.path.basename(path) or f"PID {pid}"
        if path.lower().startswith(SAFE_DIRS) or name.lower() in SAFE_EXE:
            continue
        try:
            ok = subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5,
                                creationflags=NOWIN).returncode == 0
        except Exception:
            ok = False
        (closed if ok else failed).add(name)
    if closed or failed:
        log(f"closed {sorted(closed)}; could not close {sorted(failed)}")
    return sorted(closed), sorted(failed - closed)


# ---------- warning tones ----------
# Square-ish tones (odd harmonics) at ~90 % of full scale with 4 ms ramps: loud and unmistakably a warning,
# without the clicks of a raw square wave. Each pattern loops until acknowledged.
TONES = {
    "pin": [(1450, .11), (0, .04), (950, .11), (0, .04)] * 3 + [(0, .30)],  # fast hi-lo: master warning
    "volt": [(880, .22), (0, .12), (880, .22), (0, .60)],                    # urgent double beep
    "offline": [(660, .35), (0, .90)],                                       # single caution tone
}


def tone_file(kind):
    path = os.path.join(tempfile.gettempdir(), f"substashen_{kind}_v3.wav")
    if not os.path.exists(path) or os.path.getsize(path) < 20000:
        sr, buf = 44100, []
        for freq, dur in TONES[kind]:
            n = int(sr * dur)
            for i in range(n):
                if not freq:
                    buf.append(0.0)
                    continue
                w = 2 * math.pi * freq * i / sr
                ramp = min(1.0, i / (0.004 * sr), (n - i) / (0.004 * sr))
                buf.append((math.sin(w) + math.sin(3 * w) / 3 + math.sin(5 * w) / 5 + math.sin(7 * w) / 7) * ramp)
        gain = 0.92 * 32767 / max(abs(v) for v in buf)  # normalise: loud, but never clipping
        out = array.array("h", (int(v * gain) for v in buf))
        tmp = path + f".{os.getpid()}.tmp"
        with wave.open(tmp, "wb") as wf:
            wf.setnchannels(1), wf.setsampwidth(2), wf.setframerate(sr)
            wf.writeframes(out.tobytes())
        os.replace(tmp, path)  # a half-written file would mean a silent alarm
    return path


def alarm(kind):
    try:
        winsound.PlaySound(tone_file(kind), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
    except Exception as e:  # no audio device, unwritable temp: fall back to the system sound
        log(f"alarm sound failed: {e}")
        winsound.MessageBeep(0x10)


def alarm_stop():
    try:
        winsound.PlaySound(None, 0)
    except Exception:
        pass


# ---------- small JSON stores ----------
def load_json(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)  # never leave a half-written file behind
    except OSError as e:  # OneDrive / antivirus holding the file must never stop monitoring
        log(f"save {os.path.basename(path)} failed: {e}")


# ---------- PSU energy data: runtime counters from the PSU over USB, kWh from MSI Center's PSU record ----------
# The PSU itself stores only two counters (seconds since power-up, lifetime seconds). kWh and the weekly chart are
# computed by MSI Center from the PSU's readings and kept in its profile file; Substashen reads both, logs nothing.
PSU_VID_PID = "vid_0db0&pid_aa6f"
PSU_READ_ONLY = {0xD0: "runtime since power-up", 0xD1: "lifetime runtime"}  # the only commands this app can send
PSU_MUTEX = "Global\\MSI_PSU_Mutex"  # MSI Center holds this around every PSU transaction
PSU_POLL_S = 60
MSI_RECORD = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "MSI", "MSI Center", "Power Supply Unit",
                          "Profile", "Profile.cfg")
_k = ctypes.WinDLL("kernel32", use_last_error=True)
_k.CreateFileW.restype = _k.CreateEventW.restype = _k.OpenMutexW.restype = ctypes.c_void_p
_k.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                           ctypes.c_ulong, ctypes.c_void_p]
for _fn in ("CloseHandle", "ReleaseMutex", "CancelIo"):
    getattr(_k, _fn).argtypes = [ctypes.c_void_p]
_k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
_k.ReadFile.argtypes = _k.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                                                ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
_k.GetOverlappedResult.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int]


class _GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort), ("d", ctypes.c_ubyte * 8)]


class _OVL(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p), ("Offset", ctypes.c_ulong),
                ("OffsetHigh", ctypes.c_ulong), ("hEvent", ctypes.c_void_p)]


def psu_path():
    """HID interface path of the PSU, found by VID/PID (so another USB port is fine)."""
    guid, size = _GUID(), ctypes.c_ulong()
    ctypes.windll.hid.HidD_GetHidGuid(ctypes.byref(guid))
    cfg = ctypes.windll.cfgmgr32
    if cfg.CM_Get_Device_Interface_List_SizeW(ctypes.byref(size), ctypes.byref(guid), None, 0) or not size.value:
        return None
    buf = ctypes.create_unicode_buffer(size.value)
    if cfg.CM_Get_Device_Interface_ListW(ctypes.byref(guid), None, buf, size, 0):
        return None
    return next((x for x in ctypes.wstring_at(ctypes.addressof(buf), size.value).split("\0") if PSU_VID_PID in x.lower()),
                None)


def _hid_io(h, fn, buf, ms):
    """One overlapped 65-byte ReadFile/WriteFile with a timeout; bytes moved or None."""
    ov, n = _OVL(), ctypes.c_ulong()
    ov.hEvent = _k.CreateEventW(None, True, False, None)
    try:
        if not fn(h, buf, 65, ctypes.byref(n), ctypes.byref(ov)):
            if ctypes.get_last_error() != 997:  # ERROR_IO_PENDING
                return None
            if _k.WaitForSingleObject(ov.hEvent, ms) != 0:
                _k.CancelIo(h)
                _k.GetOverlappedResult(h, ctypes.byref(ov), ctypes.byref(n), True)  # let the cancel finish
                return None
            if not _k.GetOverlappedResult(h, ctypes.byref(ov), ctypes.byref(n), False):
                return None
        return n.value
    finally:
        _k.CloseHandle(ov.hEvent)


def _psu_transact(cmd, ms):
    path = psu_path()
    if not path:
        return None
    # MSI's service creates this lock with rights normal programs don't get. Use it when allowed; otherwise a
    # collision only makes a read fail or come back busy, and the caller retries. Nothing here can change settings.
    mutex = _k.OpenMutexW(0x00100000, False, PSU_MUTEX)  # SYNCHRONIZE
    if mutex and _k.WaitForSingleObject(mutex, 2000) not in (0, 0x80):
        _k.CloseHandle(mutex)
        return None
    try:
        h = _k.CreateFileW(path, 0xC0000000, 3, None, 3, 0x40000000, None)  # read|write, shared, overlapped
        if not h or h == ctypes.c_void_p(-1).value:
            return None
        try:
            if _hid_io(h, _k.WriteFile, (ctypes.c_ubyte * 65)(0, 0x51, cmd), ms) != 65:  # report 0, READ, command
                return None
            end, buf = time.time() + ms / 1000, (ctypes.c_ubyte * 65)()
            while time.time() < end:
                if not _hid_io(h, _k.ReadFile, buf, max(1, int((end - time.time()) * 1000))):
                    return None
                b = bytes(buf)
                if b[1] == 0x51 and b[2] == cmd:  # other programs' replies share the pipe: match the echo
                    return None if b[3] == 0xFE and not any(b[4:]) else b[3:]
            return None
        finally:
            _k.CloseHandle(h)
    finally:
        if mutex:
            _k.ReleaseMutex(mutex)
            _k.CloseHandle(mutex)


def psu_read(cmd, ms=500, tries=3):
    """Sends `00 51 <cmd>` (READ) and returns the reply bytes after the echo, or None. Only PSU_READ_ONLY commands."""
    if cmd not in PSU_READ_ONLY:
        raise ValueError(f"refusing to send PSU command 0x{cmd:02X}: not on the read-only allowlist")
    for attempt in range(tries):
        r = _psu_transact(cmd, ms)
        if r is not None:
            return r
        time.sleep(0.1 + 0.1 * attempt)
    return None


def psu_runtimes():
    """(seconds since PSU power-up, lifetime seconds) straight from the PSU, or None."""
    if DEMO:
        return 6706, 6894646
    out = []
    for cmd in (0xD0, 0xD1):
        r = psu_read(cmd)
        if r is None:
            return None
        out.append(int.from_bytes(r[0:4], "little"))
    return tuple(out)


def read_msi_record():
    """MSI Center's PSU record: lifetime kWh, all-time averages, kWh per day. {} if MSI Center isn't installed."""
    if DEMO:
        today = datetime.date.today()
        return {"kwh_total": 271.2, "avg_w": 141.6, "avg_eff": 81.4,
                "daily": {today - datetime.timedelta(d): v for d, v in
                          ((6, 1.21), (5, 2.14), (4, 0.98), (3, 0.64), (2, 1.87), (1, 1.32), (0, 0.35))}}
    import configparser
    ini = configparser.ConfigParser()
    try:
        if not ini.read(MSI_RECORD, encoding="utf-8-sig"):
            return {}
        d = ini["Data"]
        count = float(d.get("Count", 0)) or None
        daily = {}
        for k, v in (ini["WeeklyChart"].items() if ini.has_section("WeeklyChart") else []):
            try:
                daily[datetime.datetime.strptime(k, "%m-%d-%Y").date()] = float(v)
            except ValueError:
                pass
        return {"kwh_total": float(d.get("kWh_Total", "nan")),
                "avg_w": float(d.get("Wattage_Total", 0)) / count if count else None,
                "avg_eff": float(d.get("Efficiency_Total", 0)) / count if count else None,
                "daily": daily}
    except (configparser.Error, KeyError, ValueError, OSError) as e:
        log(f"MSI Center record unreadable: {e}")
        return {}


# ---------- drawing: instrument-panel language ----------
# Neutral graphite, hairlines, square corners. Colour carries meaning only: amber = near a limit or unprotected,
# red = over a limit, green = protected.
BG, PANEL, LINE, GRID = "#0b0c0e", "#0b0c0e", "#23272c", "#16191d"
TXT, TXT2, SUB, DIM = "#eceef0", "#cdd2d7", "#aab2b9", "#7a838c"
OK, WARN, BAD = "#4fd18b", "#ffb020", "#ff453a"
GOLD = "#6e6446"  # card edge fingers: muted so nothing normal looks like caution amber
FONT = {"mono": "Consolas", "mono_m": "Consolas", "mono_b": "Consolas", "cond": "Segoe UI", "cond_b": "Segoe UI"}
SCALE = 1.0  # Windows display scaling (1.25 at 125 %). Layout is written at 100 %, then canvas.scale()d.
_fonts = {}


def F(size, kind="mono"):
    return (FONT[kind], -round(max(size, 11) * SCALE))  # negative = pixels; 11 px floor keeps every label legible


def font(size, kind="mono"):
    key = F(size, kind)
    if key not in _fonts:
        _fonts[key] = tkfont.Font(font=key)  # cached: creating Tk fonts every frame would be the slowest thing here
    return _fonts[key]


def setup_fonts():
    fams = set(tkfont.families())
    for k, name in (("mono", "IBM Plex Mono"), ("mono_m", "IBM Plex Mono Medium"), ("mono_b", "IBM Plex Mono SemiBold"),
                    ("cond", "IBM Plex Sans Condensed"), ("cond_b", "IBM Plex Sans Condensed SemiBol")):  # GDI truncates
        if name in fams:
            FONT[k] = name


def mix(a, b, t):
    pa, pb = (int(a[i:i + 2], 16) for i in (1, 3, 5)), (int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


def level(a, warn, limit):
    """Colour for a value against its thresholds."""
    return BAD if a > limit else WARN if a >= warn else TXT2


def num(v, nd=0):
    return "-" if v is None else f"{v:,.{nd}f}"


def section(c, x0, y0, x1, code, title, right="", right_color=SUB):
    c.create_text(x0 + 14, y0 + 16, text=code, fill=DIM, font=F(10), anchor="w")
    c.create_text(x0 + 36, y0 + 16, text=title.upper(), fill=TXT2, font=F(12, "cond_b"), anchor="w")
    if right:
        c.create_text(x1 - 14, y0 + 16, text=right, fill=right_color, font=F(11), anchor="e")


def readout(c, x, y, label, value, unit="", size=12, color=TXT, label_w=None):
    """LABEL  value unit, in one line (label_w set) or label above value."""
    if label_w:
        c.create_text(x, y, text=label.upper(), fill=DIM, font=F(10, "cond_b"), anchor="w")
        t = c.create_text(x + label_w, y, text=value, fill=color, font=F(size), anchor="w")
    else:
        c.create_text(x, y, text=label.upper(), fill=DIM, font=F(10, "cond_b"), anchor="w")
        t = c.create_text(x, y + size * 0.55 + 12, text=value, fill=color, font=F(size, "mono_m"), anchor="w")
    if unit:
        x2 = c.bbox(t)[2] / SCALE if SCALE == 1 else x + (label_w or 0) + font(size, "mono_m" if not label_w else "mono").measure(value) / SCALE
        c.create_text(x2 + 3, (y if label_w else y + size * 0.55 + 12) + (0 if label_w else size * 0.12), text=unit,
                      fill=SUB, font=F(max(10, size * 0.5)), anchor="w")


def button(c, x1, y, text, cmd, fg=SUB, fill=PANEL, edge=LINE, size=10, hover=None):
    """Right-aligned rectangular text button; returns its left edge."""
    w = font(size, "mono_m").measure(text) / SCALE + 16
    r = c.create_rectangle(x1 - w, y - 11, x1, y + 11, fill=fill, outline=edge)
    t = c.create_text(x1 - w / 2, y, text=text, fill=fg, font=F(size, "mono_m"))
    for item in (r, t):
        c.tag_bind(item, "<Button-1>", lambda e: cmd())
        c.tag_bind(item, "<Enter>", lambda e: c.itemconfig(r, outline=SUB))
        c.tag_bind(item, "<Leave>", lambda e: c.itemconfig(r, outline=edge))
        if hover:
            hover(item)
    return x1 - w


def draw_rail(c, x0, x1, y, val, h=4):
    """Value on an 11.3-12.9 V scale; the allowed band is the lighter segment, limits ticked."""
    lo, hi = 11.3, 12.9
    sx = lambda v: x0 + (min(max(v, lo), hi) - lo) / (hi - lo) * (x1 - x0)
    c.create_rectangle(x0, y, x1, y + h, fill=GRID, outline="")
    c.create_rectangle(sx(V_MIN), y, sx(V_MAX), y + h, fill="#2a3036", outline="")
    for v in (V_MIN, V_MAX):
        c.create_line(sx(v), y - 2, sx(v), y + h + 2, fill=SUB)
    if val is not None:
        col = TXT if V_MIN <= val <= V_MAX else BAD
        c.create_polygon(sx(val) - 4, y - 5, sx(val) + 4, y - 5, sx(val), y + 1, fill=col, outline="")


def draw_pins(c, x0, x1, bt, bb, pins, size=10, volts=None, alarms=None):
    """Pin-current chart: 0-10 A axis with gridlines, six bars per cable, every value printed, the 8 A limit drawn."""
    top_a = 10.0
    ay = lambda a: bb - min(max(a, 0), top_a) / top_a * (bb - bt)
    px0 = x0 + size * 3.2  # room for the axis labels
    for a in range(0, 11, 2):
        c.create_line(px0, ay(a), x1, ay(a), fill=GRID)
        c.create_text(px0 - 6, ay(a), text=f"{a}", fill=DIM, font=F(size - 1), anchor="e")
    c.create_text(px0 - 6, bt - size * 1.4, text="A", fill=DIM, font=F(size - 1), anchor="e")
    groups = sorted(pins.items())
    gap = size * 2.4
    gw = (x1 - px0 - gap * (len(groups) - 1)) / len(groups)
    for gi, (name, vals) in enumerate(groups):
        gx = px0 + gi * (gw + gap)
        vals = sorted(vals)
        bw = gw / len(vals)
        for k, (pin, a) in enumerate(vals):
            bx0, bx1 = gx + k * bw + bw * 0.18, gx + (k + 1) * bw - bw * 0.18
            col = level(a, PIN_WARN_A, PIN_MAX_A)
            if a > 0.02:
                c.create_rectangle(bx0, ay(a), bx1, bb, fill=col if col != TXT2 else "#5b636b", outline="")
            c.create_text((bx0 + bx1) / 2, ay(a) - size * 0.9, text=f"{a:.2f}", fill=col, font=F(size - 1))
            c.create_text((bx0 + bx1) / 2, bb + size * 1.1, text=f"P{pin}", fill=SUB, font=F(size - 1))
        cable = re.search(r"(\d+)$", name)  # pins are keyed by cable number
        n = cable.group(1) if cable else ""
        parts = [f"CABLE {n}" if n else name.upper()]
        if volts:
            parts.append(f"{sum(a for _, a in vals) * volts:,.0f} W")
        status = (alarms or {}).get(n, 0)
        if alarms is not None:
            parts.append(f"PSU {ALARM_TEXT.get(int(status), 'ALARM').upper()}" if status else "PSU OK")
        c.create_line(gx, bb + size * 2.2, gx + gw, bb + size * 2.2, fill=LINE)
        c.create_text(gx + gw / 2, bb + size * 3.3, text="  \u00b7  ".join(parts), fill=BAD if status else SUB,
                      font=F(size, "cond_b"))
    ly = ay(PIN_MAX_A)
    c.create_line(px0, ly, x1, ly, fill=BAD, dash=(4, 3))
    c.create_text(x1, ly - size * 0.9, text=f"LIMIT {PIN_MAX_A:.1f} A", fill=BAD, font=F(size - 1), anchor="e")


def draw_power_trace(c, x0, x1, y0, y1, hist):
    """Fixed 0-360 W axis (the card's power limit), 90 s window, value printed at the live end."""
    wy = lambda w: y1 - min(max(w, 0), GPU_TDP_W) / GPU_TDP_W * (y1 - y0)
    px1 = x1 - 26  # room for axis labels on the right
    for w in (0, GPU_TDP_W / 2, GPU_TDP_W):
        c.create_line(x0, wy(w), px1, wy(w), fill=GRID)
        c.create_text(x1, wy(w), text=f"{w:.0f}", fill=DIM, font=F(9), anchor="e")
    if len(hist) < 2:
        return
    step = (px1 - x0) / (hist.maxlen - 1)
    pts = [p for k, w in enumerate(hist) for p in (px1 - (len(hist) - 1 - k) * step, wy(w))]
    c.create_polygon([pts[0], y1] + pts + [pts[-2], y1], fill="#14181c", outline="")
    c.create_line(pts, fill=TXT2, width=1.2 * SCALE)
    c.create_rectangle(pts[-2] - 2, pts[-1] - 2, pts[-2] + 2, pts[-1] + 2, fill=TXT, outline="")


# ---------- the two cards, as line drawings ----------
def fan_path(cx, cy, rh, r, a, style, blades):
    """Every blade's curved leading edge as ONE polyline (out to the rim and back; the joins sit under the hub),
    so turning a fan is a single coords() call instead of one per blade."""
    sweep = 48 if style == "dell" else 56
    pts = []
    for b in range(blades):
        edge = []
        for t in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
            rr = rh + (r - rh) * t
            ang = math.radians(a + 360 * b / blades + sweep * t * (1.2 - 0.2 * t))
            edge.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
        for x, y in edge + edge[::-1]:
            pts += (x, y)
    return pts


GPU_LOOK = {  # front views of the two cards in this PC, simplified to outlines
    "dell": {"body": "#101215", "edge": "#5d646b", "rib": "#1c2024", "ring": "#3e454c", "blade": "#8a9299", "hub": "#6b7178",
             "blades": 11},
    "msi": {"body": "#1a1d21", "edge": "#c4c9ce", "rib": None, "ring": "#c4c9ce", "blade": "#8a9299", "hub": "#c4c9ce",
            "blades": 9},
}


def draw_gpu_art(c, x0, y0, w, h, style, angles):
    """Returns fan specs (cx, cy, hub r, r, blade items, style) so the blades can be turned between redraws."""
    L = GPU_LOOK[style]
    steel = "#6b7178"
    # both cards in the same view: fans towards you, I/O bracket (a thin plate) left, PCIe fingers along the bottom
    bx0, bx1 = x0 + 6, x0 + w
    c.create_line(x0 + w * 0.14, y0 + h + 2, x0 + w * 0.42, y0 + h + 2, fill=GOLD, width=3 * SCALE)
    c.create_rectangle(x0 + 1, y0 - 6, x0 + 3, y0 + h + 5, fill=steel, outline="")
    c.create_rectangle(bx0, y0, bx1, y0 + h, fill=L["body"], outline=L["edge"])
    if L["rib"]:
        for yy in range(int(y0 + 4), int(y0 + h - 1), 4):
            c.create_line(bx0 + 2, yy, bx1 - 2, yy, fill=L["rib"])
    fw = (bx1 - bx0) / 3
    r = min(h - 10, fw - 6) / 2
    cy = y0 + h / 2
    if style == "msi":
        mx0, mx1 = bx0 + fw * 0.8, bx0 + fw * 2.2
        c.create_rectangle(mx0, cy + r * 0.2, mx1, y0 + h, fill="#0d0f11", outline="")
        for xx in range(int(mx0), int(mx1), 5):  # hatching marks the dark section, CAD-style
            c.create_line(xx, y0 + h, min(xx + (h / 2 - r * 0.2), mx1), cy + r * 0.2, fill="#23272c")
    specs = []
    for i in range(3):
        cx = bx0 + fw * (i + 0.5)
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#07080a", outline=L["ring"])
        if style == "msi":
            c.create_oval(cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2, outline=L["ring"])
        rh = r * 0.28
        item = c.create_line(fan_path(cx, cy, rh, r - 2, angles[i], style, L["blades"]), fill=L["blade"],
                             width=1.2 * SCALE, joinstyle="round")
        c.create_oval(cx - rh, cy - rh, cx + rh, cy + rh, fill=L["body"], outline=L["hub"])  # drawn on top: hides joins
        specs.append((cx, cy, rh, r - 2, item, style))
    return specs


def monitors():
    """(x, y, w, h, scale) of every monitor, in physical pixels."""
    rects = []

    def add(h, dc, r, d):
        dpi = ctypes.c_uint(96)
        ctypes.windll.shcore.GetDpiForMonitor(ctypes.c_void_p(h), 0, ctypes.byref(dpi), ctypes.byref(ctypes.c_uint()))
        rects.append((r[0][0], r[0][1], r[0][2] - r[0][0], r[0][3] - r[0][1], dpi.value / 96))
        return 1
    proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_long * 4), ctypes.c_void_p)
    ctypes.windll.user32.EnumDisplayMonitors(None, None, proc(add), None)
    return rects or [(0, 0, ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1), SCALE)]


def home_monitor():
    """Portrait monitor if there is one (the panel's intended home), else the primary one."""
    mons = monitors()
    return next((m for m in mons if m[3] > m[2]), next((m for m in mons if m[:2] == (0, 0)), mons[0]))


# ---------- layout: a landscape band for the top of the portrait monitor ----------
W, HEAD_H, LINK_H = 1120, 40, 26
DIAG_Y, DIAG_H = HEAD_H + LINK_H, 364      # one-line diagram
LOW_Y, LOW_H = DIAG_Y + DIAG_H, 196        # rails / trends / energy
H = LOW_Y + LOW_H
FEEDER_Y = {"1": DIAG_Y + 116, "2": DIAG_Y + 272}
MEM_WARN_C, MEM_MAX_C = 90, 100  # GDDR7: amber from 90 degC, red from 100 (it throttles around 105)
GPU_COLS = (0, 104, 200)  # x offsets of the three GPU readout columns; fits "1,622 rpm" and "MEMORY max 55"
AB_EXE = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "MSI Afterburner", "MSIAfterburner.exe")
# SCADA one-line conventions: yellow busbars, cyan equipment IDs, magenta names, muted-red measurement labels,
# green values. A real alarm is bright red (BAD), so it never blends with the labels.
LINK_HELP = ("MSI Afterburner: LIVE while its data is fresh; shows the age when it stalls.",
             "GPUs seen in Afterburner's data (2 expected).",
             "Pin current sensors from the PSU (12 expected). Protection needs all 12.",
             "The PSU's +12V output voltage reading.",
             "Uptime counters read from the PSU over USB once a minute (read-only).")
# Calm when healthy (ISA-101): grey labels, light values; yellow bus/cyan IDs give structure; colour = attention.
S_BUS, S_ID, S_NAME, S_LBL, S_VAL, S_EQ = "#8e9aa6", "#5cc8ee", "#9aa3ab", "#8d969e", "#e2e6ea", "#3a7a90"
BAR_OK = "#6f7a84"  # a pin bar that is fine: neutral, so amber/red ones stand out


class App:
    def __init__(self):
        global SCALE
        try:  # its own taskbar button and icon instead of Python's
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Substashen.Panel")
        except Exception:
            pass
        mx, my, mw, _mh, self.dpi = home_monitor()
        self.settings = load_json(SETTINGS_FILE)
        try:
            self.zoom = min(ZOOM_MAX, max(ZOOM_MIN, float(self.settings.get("zoom", 1.0))))
        except (TypeError, ValueError):
            self.zoom = 1.0
        SCALE = self.dpi * self.zoom
        self.root = r = tk.Tk()
        r.report_callback_exception = lambda *exc: log("Tk callback error:\n" + "".join(traceback.format_exception(*exc)))
        r.title(APP)
        if os.path.exists(ICON_FILE):
            r.iconbitmap(default=ICON_FILE)
        setup_fonts()
        r.overrideredirect(True)  # borderless; the panel draws its own header, minimize and resize grip
        r.attributes("-topmost", True)
        r.configure(bg=BG)
        r.bind("<Map>", self.on_map)
        r.after(50, self.taskbar_button)
        pos = self.settings.get("pos")
        on_screen = pos and any(x <= pos[0] < x + w - 50 and y <= pos[1] < y + h - 50 for x, y, w, h, _ in monitors())
        r.geometry(f"+{pos[0]}+{pos[1]}" if on_screen else
                   f"+{mx + max(0, round((mw - W * SCALE) / 2))}+{my + round(16 * SCALE)}")  # top of home monitor
        # two sizes: full (everything) and standard (no trend/energy row); an old saved "compact" becomes standard
        self.view = self.settings.get("view") or "standard"
        self.view = self.view if self.view in VIEWS else "standard"
        self.night, self.night_force = False, None  # night dimming: automatic by hour, N key forces on/off
        self.idle_alpha = float(self.settings.get("idle_opacity", 1.0))  # 1.0 = off; O key toggles 0.8
        self.c = c = tk.Canvas(r, width=W * SCALE, height=self.ph() * SCALE, bg=BG, highlightthickness=0)
        c.pack()
        c.bind("<ButtonPress-1>", self.press)
        c.bind("<B1-Motion>", self.drag)
        c.bind("<ButtonRelease-1>", self.release)
        c.bind("<Motion>", lambda e: c.config(cursor="size_nw_se" if self.in_grip(e) else ""))
        c.bind("<Double-Button-1>", lambda e: self.toggle_compact() if e.y < HEAD_H * SCALE else None)
        c.bind("<Enter>", lambda e: r.attributes("-alpha", 1.0))
        c.bind("<Leave>", lambda e: r.attributes("-alpha", self.idle_alpha))
        r.bind("<Key>", self.on_key)  # keys work once the panel has been clicked (it takes focus on click)
        r.attributes("-alpha", self.idle_alpha)
        self.resizing, self.zoom_job, self.started = False, None, time.time()
        self.warnings, self.adv_mem, self.last_collect, self.last_tick = {}, {}, None, time.time()
        self.recent = collections.deque(maxlen=8)  # recent incidents, shown in the DATA view
        self.day_stats = self.new_day()
        self.table = tk.Text(r, bg=BG, fg=TXT2, font=(FONT["mono"], -round(11 * SCALE)), bd=0, width=120, height=34,
                             highlightthickness=1, highlightbackground=LINE, padx=14, pady=10)
        self.table.tag_config("h", foreground=SUB)
        self.table.tag_config("bad", foreground=BAD)
        self.show_table = False
        self.hist = collections.defaultdict(lambda: collections.deque(maxlen=90))
        self.runtime, self.record, self.usb_ok, self.usb_thread, self.last_usb = None, {}, None, None, 0.0
        self.oc, self.oc_at = {}, 0.0  # Afterburner's applied clocks/limits, re-read every 5 s
        self.memt = {}  # PCI bus -> GDDR7 temperature, re-read with them
        self.pins, self.psu, self.gpus, self.popup, self.popup_sev, self.popup_items = {}, {}, {}, None, 0, []
        self.snooze_until, self.snooze_sev = 0, 0
        self.streak, self.arrivals = collections.Counter(), collections.deque(maxlen=11)
        self.unprotected_since, self.offline_alerted, self.reason = None, False, None
        self.frozen = (None, time.time())
        self.last_kill, self.kill_thread, self.closed, self.failed = 0, None, set(), set()
        self.confirm_until, self.error, self.last_frame = 0, None, None
        self.fan_specs, self.fan_speed, self.fan_vis, self.fan_angle = [], {}, {}, collections.defaultdict(float)
        self.anim_t, self.flash_items, self.flash_on = time.time(), [], True
        self.cable_map, self.map_score = dict(self.settings.get("cable_map") or {"1": "0", "2": "1"}), 0.0
        self.items, self.cache, self.sig = {}, {}, None
        self.help_text, self.link_now, self.link_idle = None, (None, None), ""
        r.protocol("WM_DELETE_WINDOW", self.ask_quit)
        self.tick()
        self.animate()

    # --- window: move, resize, minimize; position and size are remembered ---
    def in_grip(self, e):
        grip = max(18, round(16 * SCALE))  # matches the drawn grip at every size
        return e.x > self.c.winfo_width() - grip and e.y > self.c.winfo_height() - grip

    def ph(self):
        """Panel height: the standard view stops above the trend/energy row."""
        return LOW_Y if self.view == "standard" else H

    def on_key(self, e):
        action = {"t": self.test_alert, "d": self.toggle_table, "m": self.minimize, "c": self.toggle_compact,
                  "n": self.toggle_night, "o": self.toggle_opacity}.get(e.keysym.lower())
        if action:
            action()

    def toggle_compact(self):  # C key / double-click header: full <-> standard
        self.view = VIEWS[(VIEWS.index(self.view) + 1) % len(VIEWS)]
        self.settings["view"] = self.view
        self.settings.pop("compact", None)
        save_json(SETTINGS_FILE, self.settings)
        self.c.config(height=self.ph() * SCALE)
        self.sig = None
        self.render(self.last_frame)

    def toggle_night(self):
        self.night_force = not self.night
        self.render(self.last_frame)

    def toggle_opacity(self):
        self.idle_alpha = 0.8 if self.idle_alpha >= 1.0 else 1.0
        self.settings["idle_opacity"] = self.idle_alpha
        save_json(SETTINGS_FILE, self.settings)

    def press(self, e):
        self.c.focus_set()
        self.resizing = self.in_grip(e)
        self._drag = (e.x, e.y)

    def drag(self, e):
        if self.resizing:  # aspect ratio is fixed; width decides the zoom
            rx = self.root.winfo_rootx()
            mon = next((m for m in monitors() if m[0] <= rx < m[0] + m[2]), None)
            cap = min(ZOOM_MAX, (mon[0] + mon[2] - rx) / (W * self.dpi)) if mon else ZOOM_MAX  # stay on this monitor
            want = max(ZOOM_MIN, min(cap, (e.x_root - rx) / (W * self.dpi)))
            self._want_zoom = want
            if not self.zoom_job:  # throttle rebuilds while dragging
                self.zoom_job = self.root.after(120, self.apply_zoom)
        else:
            self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def release(self, e):
        if self.resizing:
            self.resizing = False
            self.apply_zoom()
        self.remember_pos()

    def apply_zoom(self):
        global SCALE
        self.zoom_job = None
        z = getattr(self, "_want_zoom", self.zoom)
        if abs(z - self.zoom) < 0.005:
            return
        self.zoom, SCALE = z, self.dpi * z
        self.c.config(width=W * SCALE, height=self.ph() * SCALE)
        self.table.config(font=(FONT["mono"], -round(11 * SCALE)))
        self.sig = None  # full rebuild at the new size
        self.render(self.last_frame)

    def remember_pos(self):
        self.settings["pos"] = [self.root.winfo_x(), self.root.winfo_y()]
        self.settings["zoom"] = round(self.zoom, 3)
        save_json(SETTINGS_FILE, self.settings)

    def minimize(self):
        """Borderless windows can't be minimized; give it a frame for the trip to the taskbar."""
        self.root.overrideredirect(False)
        self.root.iconify()

    def on_map(self, e):
        if e.widget is self.root and not self.root.overrideredirect() and self.root.state() == "normal":
            self.root.overrideredirect(True)  # back from the taskbar: borderless and on top again
            self.root.attributes("-topmost", True)
            self.root.after(10, self.taskbar_button)

    def taskbar_button(self):
        """Borderless Tk windows get no taskbar button; WS_EX_APPWINDOW gives it one (shown after a hide/show)."""
        u32 = ctypes.windll.user32
        hwnd = u32.GetParent(self.root.winfo_id())
        style = u32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
        want = (style & ~0x80) | 0x40000      # drop WS_EX_TOOLWINDOW, add WS_EX_APPWINDOW
        if want != style:
            u32.SetWindowLongW(hwnd, -20, want)
            self.root.withdraw()
            self.root.after(10, lambda: self.popup or self.root.deiconify())  # never over an alert cover

    def visible(self):
        return not self.popup and self.root.state() == "normal"

    def ask_quit(self):
        self.confirm_until = time.time() + 6  # the header turns into a "stop protection?" prompt for 6 s
        self.root.after(6100, lambda: self.render(self.last_frame))
        self.render(self.last_frame)

    def cancel_quit(self):
        self.confirm_until = 0
        self.render(self.last_frame)

    def relaunch(self):
        """Start a fresh copy, then exit. The new copy waits for this process to end before it takes over."""
        exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        args = [a for a in sys.argv[1:] if a.startswith("--demo")]
        try:
            subprocess.Popen([exe if os.path.exists(exe) else sys.executable, os.path.abspath(__file__),
                              "--after", str(os.getpid())] + args, cwd=os.path.dirname(os.path.abspath(__file__)),
                             creationflags=0x00000008, close_fds=True)  # DETACHED_PROCESS
        except OSError as e:  # couldn't start the new copy: keep this one running rather than leave nothing
            log(f"relaunch failed: {e}")
            return
        self.quit("relaunch")

    def quit(self, why="closed by the user"):
        self.write_day("DAY-SO-FAR")
        self.event("STOP", why)
        if not DEMO:
            self.remember_pos()
        alarm_stop()
        # Logs and settings are written (synchronously) above. Tearing Tk down then let Tcl 9 hit an
        # "alloc: invalid block" panic on exit, so end the process here; Windows reclaims the windows and handles.
        os._exit(0)

    def open_afterburner(self):
        try:
            os.startfile(AB_EXE)  # starts it, or brings the running one forward (Windows may ask for admin)
        except OSError as e:
            log(f"open Afterburner failed: {e}")

    # --- loop: nothing in here may stop the next tick from being scheduled ---
    def tick(self):
        now = time.time()
        gap, self.last_tick = now - self.last_tick, now
        if gap > 5:  # the PC slept or the app stalled: nothing was checked meanwhile; restart the timers cleanly
            self.event("RESUME", f"no checks for {gap:.0f} s (sleep or stall)")
            self.frozen, self.last_sample = (None, now), None
            self.streak.clear()
            self.arrivals.clear()
        try:
            self.step()
            self.error = None
        except Exception:
            self.error = traceback.format_exc().strip().splitlines()[-1]
            log("tick failed:\n" + traceback.format_exc())
            try:  # a tick that keeps failing checks nothing, so it counts as unprotected too
                now = time.time()
                self.unprotected_since = self.unprotected_since or now
                if not self.offline_alerted and now - self.unprotected_since >= OFFLINE_ALERT_S:
                    self.offline_alerted = self.raise_alert([self.offline_fault(f"internal error: {self.error}")], now)
                self.render(self.last_frame)
            except Exception:
                pass
        finally:
            self.root.after(1000, self.tick)

    def snapshot(self):
        pins = "  ".join(f"C{n} " + " ".join(f"{a:.2f}" for _, a in v) for n, v in sorted(self.pins.items()))
        gpus = " ".join(f"GPU{g} {d['v'].get('GPU Power', 0):.0f}W" for g, d in sorted(self.gpus.items()))
        return f"{pins}  |  {gpus}  |  12V {self.psu.get('+12V', 0):.3f}"

    def event(self, kind, text, snap=""):
        self.recent.appendleft(f"{time.strftime('%H:%M:%S')}  {kind:<8} {text}")
        incident(kind, text, snap)

    @staticmethod
    def new_day():
        return {"date": datetime.date.today(), "peak": {}, "pin_peak": {}, "above_warn_s": 0.0, "max_spread": 0.0, "alarms": 0,
                "warnings": 0}

    def write_day(self, label="DAILY"):
        d = self.day_stats
        if not d["peak"]:
            return
        peaks = ", ".join(f"cable {n} {a:.2f} A" for n, a in sorted(d["peak"].items()))
        self.event(label, f"{d['date']}: peak {peaks}; {d['above_warn_s'] / 60:.0f} min at or over {PIN_WARN_A:.0f} A; "
                          f"widest pin spread {d['max_spread']:.1f} A; {d['alarms']} alarms, {d['warnings']} warnings")

    def step(self):
        got = demo_readings() if DEMO else read_afterburner()
        readings, age, sample = (tuple(got) + (None,))[:3] if got else (None, 0, None)  # sample id is optional
        now = time.time()
        reason = "Afterburner isn't running" if readings is None else coverage(readings, age)
        if readings is not None and reason is None:
            reason = self.frozen_reason(readings, now)
        self.reason = reason
        if reason:
            self.unprotected_since = self.unprotected_since or now
        else:
            self.unprotected_since, self.offline_alerted = None, False
        faults = self.evaluate(check(readings) if readings else [], sample)
        if reason and not self.offline_alerted and now - self.unprotected_since >= OFFLINE_ALERT_S \
                and now - self.started >= STARTUP_GRACE_S:  # at sign-in Afterburner may still be loading
            faults.append(self.offline_fault(reason))
        if any(f["kind"] == "pin" for f in faults):
            self.protect(now)
        if faults and self.raise_alert(faults, now) and any(f["kind"] == "offline" for f in faults):
            self.offline_alerted = True
        if readings is not None:
            self.collect(readings)
            if now - self.oc_at >= 5:
                self.oc, self.oc_at, self.memt = read_oc(), now, mem_temps()
        self.last_frame = (readings, faults, age)
        if self.visible():  # hidden behind an alert or minimized: skip the drawing work
            self.render(self.last_frame)
            if int(now) % 5 == 0:
                self.root.attributes("-topmost", True)  # re-assert: some full-screen apps push topmost windows down
        if self.show_table and readings:
            self.fill_table(readings)

    @staticmethod
    def offline_fault(reason):
        return {"kind": "offline", "title": "Protection offline", "where": reason, "show": "NO DATA",
                "val": 0, "unit": "", "limit": "pin currents can't be checked"}

    def frozen_reason(self, readings, now):
        """Afterburner can keep running while its PSU plugin stops updating. Real pins under load always jitter,
        so identical values for FROZEN_S while the GPUs draw real power means the plugin froze."""
        pins = tuple(v for s, l, v, u in readings if u == "A" and PIN_RE.search(l))
        if pins != self.frozen[0]:
            self.frozen = (pins, now)
            return None
        load = sum(v for s, l, v, u in readings if l == "GPU Power")
        if load >= 150 and now - self.frozen[1] > FROZEN_S:
            return f"PSU readings frozen for {now - self.frozen[1]:.0f} s"
        return None

    def evaluate(self, faults, sample=None):
        """Debounce: pin faults need 2 samples in a row (or 1 hard one); volt faults need 3.
        Counts new Afterburner samples, so one reading seen on two ticks is still one sample."""
        kinds = {f["kind"] for f in faults}
        new = sample is None or sample != getattr(self, "last_sample", None)
        if new and sample is not None:  # arrival times of fresh Afterburner samples -> measured polling period
            self.arrivals.append(time.time())
        self.last_sample = sample
        for k in ("pin", "volt"):
            if k not in kinds:
                self.streak[k] = 0
            elif new:
                self.streak[k] += 1
        return [f for f in faults if f.get("hard") or self.streak[f["kind"]] >= (2 if f["kind"] == "pin" else 3)]

    def protect(self, now):
        """Close GPU programs now and every few seconds while the pin fault lasts, even with the alert snoozed."""
        busy = self.kill_thread and self.kill_thread.is_alive()
        if busy and now - self.last_kill > 30:  # stuck in a hung child process: report it and start a fresh attempt
            self.failed.add("(closing is stuck: nvidia-smi or taskkill not responding)")
            busy = False
        if now - self.last_kill < KILL_EVERY_S or busy:
            return
        self.last_kill = now

        def run():
            closed, failed = kill_gpu_tasks()
            self.closed.update(closed)
            self.failed = (self.failed | set(failed)) - self.closed

        self.kill_thread = threading.Thread(target=run, daemon=True)
        self.kill_thread.start()

    def collect(self, readings):
        pins, psu, gpus = collections.defaultdict(list), {}, {}
        for sensor, label, val, unit in readings:
            if sensor == "MSI PSU":
                psu[label] = val
                if unit == "A" and PIN_RE.search(label):
                    m = re.search(r"_(\d+)\s*pin\s*(\d+)", label, re.I)
                    if m:
                        pins[m.group(1)].append((int(m.group(2)), val))
            g = GPU_RE.search(sensor)
            if g:
                gpus.setdefault(g.group(1), {"name": sensor, "v": {}})["v"].setdefault(label, val)
                if label == "GPU Power":
                    self.hist[g.group(1)].append(val)
        for vals in pins.values():
            vals.sort()
        self.pins, self.psu, self.gpus = dict(pins), psu, gpus
        now = time.time()
        dt = min(now - self.last_collect, 5) if self.last_collect else 1.0
        self.last_collect = now
        every = [a for v in self.pins.values() for _, a in v]
        if every:
            self.hist["worst"].append(max(every))
        cap = float(self.settings.get("power_cap_w", 270))
        active = advisory_checks(self.pins, {g: d["v"].get("GPU Power") for g, d in gpus.items()}, cap,
                                 self.adv_mem, now, dt)
        for k in active.keys() - self.warnings.keys():
            self.event("WARNING", active[k], self.snapshot())
            self.day_stats["warnings"] += 1
        for k in self.warnings.keys() - active.keys():
            self.event("CLEARED", self.warnings[k])
        self.warnings = active
        d = self.day_stats  # daily summary: written when the date changes
        if d["date"] != datetime.date.today():
            self.write_day()
            self.day_stats = d = self.new_day()
        for n, v in self.pins.items():
            amps = [a for _, a in v]
            d["peak"][n] = max(d["peak"].get(n, 0.0), max(amps))
            for pin, a in v:
                d["pin_peak"][n, pin] = max(d["pin_peak"].get((n, pin), 0.0), a)
            d["max_spread"] = max(d["max_spread"], max(amps) - min(amps))
        if every and max(every) >= PIN_WARN_A:
            d["above_warn_s"] += dt
        self.infer_cable_map()
        self.poll_psu_usb()

    def infer_cable_map(self):
        """Afterburner doesn't say which cable feeds which card: match cable watts to card watts while under load."""
        v12, g = self.psu.get("+12V"), self.gpus
        if not v12 or set(self.pins) != {"1", "2"} or set(g) != {"0", "1"}:
            return
        cw = {n: sum(a for _, a in p) * v12 for n, p in self.pins.items()}
        gw = {k: g[k]["v"].get("GPU Power") or 0 for k in g}
        if max(gw.values()) < 80 or abs(gw["0"] - gw["1"]) < 40:
            return  # idle or equal load: no evidence either way
        straight = abs(cw["1"] - gw["0"]) + abs(cw["2"] - gw["1"])
        crossed = abs(cw["1"] - gw["1"]) + abs(cw["2"] - gw["0"])
        self.map_score = max(-300.0, min(300.0, self.map_score * 0.98 + (straight - crossed)))
        want = {"1": "1", "2": "0"} if self.map_score > 150 else {"1": "0", "2": "1"} if self.map_score < -150 else None
        if want and want != self.cable_map:
            self.cable_map = want
            self.settings["cable_map"] = want
            save_json(SETTINGS_FILE, self.settings)
            log(f"cable map now {want}")

    def toggle_table(self):
        self.show_table = not self.show_table
        self.table.pack() if self.show_table else self.table.pack_forget()

    def poll_psu_usb(self):
        """Once a minute, off the UI thread: the PSU's runtime counters over USB and MSI Center's kWh record."""
        now = time.time()
        if now - self.last_usb < PSU_POLL_S or (self.usb_thread and self.usb_thread.is_alive()):
            return
        self.last_usb = now

        def run():
            try:
                rt = psu_runtimes()
            except Exception as e:
                log(f"PSU USB read failed: {e}")
                rt = None
            if rt:
                self.runtime = (*rt, time.time())
            self.usb_ok = rt is not None
            self.record = read_msi_record() or self.record

        self.usb_thread = threading.Thread(target=run, daemon=True)
        self.usb_thread.start()

    # --- retained drawing: build the static panel once, then only touch what changed ---
    def tip(self, items, text):
        """Explain an element in plain words in the LINK row while the pointer is on it."""
        for it in items if isinstance(items, (list, tuple)) else [items]:
            self.c.tag_bind(it, "<Enter>", lambda e, t=text: self.set_help(t), add="+")
            self.c.tag_bind(it, "<Leave>", lambda e: self.set_help(None), add="+")
        return items

    def set_help(self, text):
        self.help_text = text
        self.show_issue()

    def show_issue(self):
        issue, fix = self.link_now
        self.set("issue", text=issue or self.link_idle, fill=WARN if issue else DIM)
        self.set("fix_btn", state="normal" if fix else "hidden")
        self.set("fix_btn_t", text=fix or "", state="normal" if fix else "hidden")
        # hover help covers the whole LINK row while the pointer is on something, then the row comes back
        shown = "normal" if self.help_text else "hidden"
        self.set("help_bg", state=shown)
        self.set("help_t", text=self.help_text or "", state=shown)

    def put(self, name, item):
        self.items[name] = item
        return item

    def dimmed(self, col):
        """Night mode: everything normal fades toward the background; warnings and alarms stay full strength."""
        if self.night and isinstance(col, str) and col.startswith("#") and col not in (BAD, WARN, BG, PANEL):
            return mix(col, BG, 0.45)
        return col

    def set(self, name, **kw):
        for k in ("fill", "outline"):
            if k in kw:
                kw[k] = self.dimmed(kw[k])
        key = tuple(sorted(kw.items()))
        if self.cache.get(name) != key:
            self.cache[name] = key
            self.c.itemconfig(self.items[name], **kw)

    def move(self, name, pts):
        key = tuple(round(p, 1) for p in pts)
        if self.cache.get("@" + name) != key:
            self.cache["@" + name] = key
            self.c.coords(self.items[name], *[p * SCALE for p in pts])

    def text(self, name, x, y, value="", color=S_VAL, size=11, kind="mono", anchor="w"):
        return self.put(name, self.c.create_text(x, y, text=value, fill=color, font=F(size, kind), anchor=anchor))

    def link_state(self, readings, age):
        """(issue text or None, button label or None, [(segment, status colour, text)])."""
        if readings is None:
            return ("MSI Afterburner isn't running. Start it to restore protection.", "START AFTERBURNER",
                    [("AFTERBURNER", BAD, "DOWN"), ("GPUS", DIM, "-"), ("PIN SENSORS", DIM, "-"), ("12V READING", DIM, "-"),
                     ("PSU USB", DIM, "-")])
        ngpu = len({GPU_RE.search(s).group(1) for s, *_ in readings if GPU_RE.search(s)})
        npin = sum(1 for s, l, v, u in readings if s == "MSI PSU" and u == "A" and PIN_RE.search(l))
        has12 = any(s == "MSI PSU" and l == "+12V" for s, l, v, u in readings)
        stale = age > STALE_S
        # "LIVE" stays put while data is fresh, so this text doesn't force a repaint every second
        segs = [("AFTERBURNER", WARN if stale else OK, "LIVE" if age < 2 else f"\u0394t {age:.0f} s"),
                ("GPUS", OK if ngpu >= 2 else WARN, f"{ngpu}/2"),
                ("PIN SENSORS", OK if npin == 12 else BAD if npin == 0 else WARN, f"{npin}/12"),
                ("12V READING", OK if has12 else WARN, "OK" if has12 else "MISSING"),
                ("PSU USB", DIM if self.usb_ok is None else OK if self.usb_ok else WARN,
                 "..." if self.usb_ok is None else "OK" if self.usb_ok else "NO REPLY")]
        if stale:
            return f"Afterburner stalled {age:.0f} s: restart it", "OPEN AFTERBURNER", segs
        if npin < 12:
            return ("No pin readings: enable MSI PSU graphs in Afterburner" if not npin
                    else f"Only {npin} of 12 pin readings: enable all pin graphs"), "OPEN AFTERBURNER", segs
        if not has12:
            return "No 12 V reading: enable the PSU 12V graph", "OPEN AFTERBURNER", segs
        if self.reason:
            return f"{self.reason}: check PSU USB cable", "OPEN AFTERBURNER", segs
        if ngpu < 2:
            return "GPU readings missing: enable GPU graphs in Afterburner", "OPEN AFTERBURNER", segs
        return None, None, segs

    def render(self, frame):
        readings, faults, age = frame or (None, [], 0)
        gpu_sig = tuple((k, self.gpus[k]["name"]) for k in sorted(self.gpus)) if readings else ()
        sig = (readings is None, gpu_sig, tuple((k, len(v)) for k, v in sorted(self.pins.items())),
               tuple(sorted(self.cable_map.items())),
               time.time() < self.confirm_until, datetime.date.today(), round(SCALE, 3), self.view,
               self.night_now())
        if sig != self.sig:
            self.build(readings is not None)
            self.sig = sig
        self.update(readings, faults, age)

    def night_now(self):
        hours = self.settings.get("night_hours")  # off unless set, e.g. [23, 7]; N still toggles it by hand
        h = datetime.datetime.now().hour
        auto = bool(hours) and ((h >= hours[0] or h < hours[1]) if hours[0] > hours[1] else hours[0] <= h < hours[1])
        self.night = auto if self.night_force is None else self.night_force
        return self.night

    def build(self, live):
        c = self.c
        c.delete("all")
        self.items, self.cache, self.fan_specs = {}, {}, []
        c.create_rectangle(0, 0, W - 1, self.ph() - 1, fill=PANEL, outline=LINE)
        # header
        c.create_line(0, HEAD_H, W, HEAD_H, fill=LINE)
        self.tip(c.create_text(14, HEAD_H / 2, text=APP.upper(), fill=TXT, font=F(14, "mono_b"), anchor="w"),
                 "Keys: T test  D data  M minimize  C size (full/standard)  N night  O see-through.  Double-click header: size.")
        self.tip(c.create_text(150, HEAD_H / 2, text="HIGHEST PIN", fill=SUB, font=F(11, "cond_b"), anchor="w"),
                 "The highest of the 12 pin currents right now, and how close it is to the 8 A limit.")
        self.tip(self.text("worst", 236, HEAD_H / 2, "", S_VAL, 19, "mono_b"),
                 "The highest of the 12 pin currents right now, and how close it is to the 8 A limit.")
        self.text("worst_sub", 466, HEAD_H / 2, "", SUB, 12)
        c.create_line(136, HEAD_H / 2 - 10, 136, HEAD_H / 2 + 10, fill=LINE)
        # headroom gauge inline between the value and its caption: 0-10 A, caution zone shaded, 8 A limit ticked
        hx0, hx1, hy = 318, 450, HEAD_H / 2
        hx = lambda a: hx0 + (hx1 - hx0) * a / 10
        c.create_rectangle(hx0, hy - 3, hx1, hy + 3, fill=GRID, outline="")
        c.create_rectangle(hx(PIN_WARN_A), hy - 3, hx(PIN_MAX_A), hy + 3, fill=mix(WARN, BG, 0.8), outline="")
        self.put("hbar", c.create_rectangle(hx0, hy - 3, hx0, hy + 3, fill=S_VAL, outline=""))
        c.create_line(hx(PIN_MAX_A), hy - 7, hx(PIN_MAX_A), hy + 7, fill=BAD, width=1.5 * SCALE)
        self.tip(self.items["hbar"], "Headroom: the highest pin on a 0-10 A scale. Amber zone 6-8 A, red tick = 8 A limit.")
        self.hbar_x = (hx0, hx1, hy)
        x = W - 14
        if time.time() < self.confirm_until:  # in-panel confirmation instead of a system dialog
            x = button(c, x, HEAD_H / 2, "KEEP RUNNING", self.cancel_quit, fg=TXT)
            x = button(c, x - 8, HEAD_H / 2, "CLOSE", self.quit, fg=BG, fill=BAD, edge=BAD)
            c.create_text(x - 12, HEAD_H / 2, text="STOP PROTECTION AND CLOSE?", fill=WARN, font=F(11, "cond_b"), anchor="e")
            x = x - 12 - font(11, "cond_b").measure("STOP PROTECTION AND CLOSE?") / SCALE - 20
        else:
            x = button(c, x, HEAD_H / 2, "X", self.ask_quit,
                       hover=lambda i: self.tip(i, "Close Substashen (asks first). Protection stops while it's closed."))
            x = button(c, x - 6, HEAD_H / 2, "\u2013", self.minimize,
                       hover=lambda i: self.tip(i, "Minimize to the taskbar. Protection keeps running."))
            x = button(c, x - 6, HEAD_H / 2, "DATA", self.toggle_table,
                       hover=lambda i: self.tip(i, "Show or hide every raw reading as a table."))
            x = button(c, x - 6, HEAD_H / 2, "TEST", self.test_alert,
                       hover=lambda i: self.tip(i, "Ring the alarm and show the alert screen. Nothing is closed."))
            x = button(c, x - 6, HEAD_H / 2, "RELAUNCH", self.relaunch,
                       hover=lambda i: self.tip(i, "Close and reopen Substashen (e.g. after an update). "
                                                   "Protection pauses for a few seconds.")) - 18
        sq = x - font(12, "mono_b").measure("NOT PROTECTED") / SCALE - 18  # room for the longest state
        c.create_line(sq - 16, HEAD_H / 2 - 10, sq - 16, HEAD_H / 2 + 10, fill=LINE)
        self.put("status_sq", c.create_rectangle(sq, HEAD_H / 2 - 5, sq + 10, HEAD_H / 2 + 5, fill=OK, outline=""))
        self.tip(self.text("status", sq + 18, HEAD_H / 2, "", OK, 12, "mono_b", "w"),
                 "PROTECTED: all 12 pin sensors are live. NOT PROTECTED: data missing or stale (see LINK).")
        # link row: is the data path healthy, and what to do if not
        ly = HEAD_H + LINK_H / 2
        c.create_rectangle(1, HEAD_H + 1, W - 2, HEAD_H + LINK_H, fill="#0e1013", outline="")
        c.create_line(0, HEAD_H + LINK_H, W, HEAD_H + LINK_H, fill=LINE)
        self.tip(c.create_text(14, ly, text="LINK", fill=DIM, font=F(10, "cond_b"), anchor="w"),
                 "Data links: Afterburner freshness, GPUs seen, pin sensors, PSU 12 V reading, PSU USB.")
        x = 52
        for i, label in enumerate(("AFTERBURNER", "GPUS", "PIN SENSORS", "12V READING", "PSU USB")):
            self.put(f"seg{i}_sq", c.create_rectangle(x, ly - 4, x + 8, ly + 4, fill=DIM, outline=""))
            self.tip(self.items[f"seg{i}_sq"], LINK_HELP[i])
            self.text(f"segl{i}", x + 13, ly, label, SUB, 11, "cond_b")
            # generous gap: at small zooms rounded font sizes render wider than the measure suggests
            self.text(f"seg{i}", x + 13 + font(11, "cond_b").measure(label) / SCALE + 12, ly, "", TXT2, 11)
            x += font(11, "cond_b").measure(label) / SCALE + 72  # even gaps whatever the label length
        bx = W - 14
        bw = font(10, "mono_m").measure("START AFTERBURNER") / SCALE + 16
        self.put("fix_btn", c.create_rectangle(bx - bw, ly - 9, bx, ly + 9, fill=WARN, outline=""))
        self.text("fix_btn_t", bx - bw / 2, ly, "", BG, 10, "mono_m", "center")
        for n in ("fix_btn", "fix_btn_t"):
            c.tag_bind(self.items[n], "<Button-1>", lambda e: self.open_afterburner())
        self.text("issue", bx - bw - 12, ly, "", WARN, 12, "cond_b", "e")
        self.put("help_bg", c.create_rectangle(1, HEAD_H + 1, W - 2, HEAD_H + LINK_H - 1, fill="#14181c", outline="",
                                               state="hidden"))
        self.text("help_t", 14, ly, "", TXT, 12, "cond_b", "w")
        c.itemconfig(self.items["help_t"], state="hidden")
        if not live:
            c.create_text(14, DIAG_Y + 40, text="WAITING FOR MSI AFTERBURNER", fill=WARN, font=F(14, "cond_b"), anchor="w")
            c.create_text(14, DIAG_Y + 66, text="Substashen reads MSI Afterburner's monitoring data and connects "
                          "automatically as soon as it is running.", fill=SUB, font=F(12, "cond"), anchor="w")
        else:
            self.build_diagram()
            if self.view == "full":
                self.build_lower()
        hh = self.ph()
        for d in (4, 8, 12):  # resize grip
            c.create_line(W - 3 - d, hh - 3, W - 3, hh - 3 - d, fill=SUB)
        if self.night:  # one pass over the static drawing; dynamic values are dimmed in set()
            for it in c.find_all():
                for opt in ("fill", "outline"):
                    try:
                        v = c.itemcget(it, opt)
                    except tk.TclError:
                        continue
                    if v and v != self.dimmed(v):
                        c.itemconfig(it, **{opt: self.dimmed(v)})
        c.scale("all", 0, 0, SCALE, SCALE)

    def build_diagram(self):
        c = self.c
        section(c, 0, DIAG_Y, W, "01", "Power path  PSU → cables → GPUs")
        x = W - 14  # colour legend, right-aligned: what the pin colours mean
        for label, col in ((f"> {PIN_MAX_A:.0f} A LIMIT", BAD), (f"\u2265 {PIN_WARN_A:.0f} A CAUTION", WARN), ("NORMAL", BAR_OK)):
            c.create_text(x, DIAG_Y + 16, text=label, fill=SUB, font=F(11, "cond_b"), anchor="e")
            x -= font(11, "cond_b").measure(label) / SCALE + 8
            c.create_rectangle(x - 9, DIAG_Y + 12, x, DIAG_Y + 21, fill=col, outline="")
            x -= 22
        # source: the PSU
        px0, py0, px1, py1 = 16, DIAG_Y + 40, 196, DIAG_Y + 344
        self.tip(c.create_rectangle(px0, py0, px1, py1, outline=S_EQ, fill=BG),
                 "The power supply's own live readings (Afterburner's MSI PSU plugin).")
        c.create_text(px0 + 10, py0 + 16, text="PSU1", fill=S_ID, font=F(13, "mono_b"), anchor="w")
        c.create_text(px0 + 10, py0 + 34, text="MPG AI1300TS", fill=S_NAME, font=F(11, "mono_m"), anchor="w")
        for k, lab in enumerate(("OUTPUT", "EFFICIENCY", "TEMPERATURE", "FAN")):
            y = py0 + 84 + k * 60
            c.create_line(px0 + 10, y - 30, px1 - 10, y - 30, fill=GRID)
            c.create_text(px0 + 10, y, text=lab, fill=S_LBL, font=F(11), anchor="w")
            self.text(f"psu{k}", px1 - 10, y, "", S_VAL, 18, "mono_m", "e")
        bus_x, bus_y0, bus_y1 = 232, DIAG_Y + 52, DIAG_Y + 312
        c.create_line(px1, DIAG_Y + 194, bus_x, DIAG_Y + 194, fill=S_BUS, width=2 * SCALE)
        self.tip(c.create_line(bus_x, bus_y0, bus_x, bus_y1, fill=S_BUS, width=6 * SCALE),
                 "PSU +12V output. Must stay 11.6-12.6 V; outside that for 3 s raises a warning.")
        by = bus_y0 - 10
        c.create_text(bus_x + 12, by, text="+12V BUS", fill=S_ID, font=F(12, "mono_b"), anchor="w")
        self.text("bus_u", bus_x + 96, by, "", S_VAL, 15, "mono_m")
        # the allowed range, beside the reading: band, limit ticks, live marker
        rx0, rx1, ry = bus_x + 200, bus_x + 330, by - 1
        sx = lambda v: rx0 + (min(max(v, 11.3), 12.9) - 11.3) / 1.6 * (rx1 - rx0)
        c.create_text(rx1 + 12, by, text="safe 11.6–12.6 V", fill=SUB, font=F(11, "cond"), anchor="w")
        self.rail_y = ry
        c.create_rectangle(rx0, ry, rx1, ry + 3, fill=GRID, outline="")
        c.create_rectangle(sx(V_MIN), ry, sx(V_MAX), ry + 3, fill="#2a3036", outline="")
        for v in (V_MIN, V_MAX):
            c.create_line(sx(v), ry - 2, sx(v), ry + 5, fill=SUB)
        self.put("u_mark", c.create_polygon(0, 0, 0, 0, 0, 0, fill=TXT, outline=""))
        self.rail_sx = sx
        for n, yf in FEEDER_Y.items():
            self.build_feeder(n, yf, bus_x)

    def build_feeder(self, n, yf, bus_x):
        c = self.c
        c.create_line(bus_x, yf, 256, yf, fill=S_BUS, width=2 * SCALE)
        self.put(f"brk{n}", c.create_rectangle(256, yf - 8, 272, yf + 8, fill=BAD, outline=S_BUS))
        self.tip(self.items[f"brk{n}"], f"Cable {n}. Flashes red if the PSU's own Safeguard+ flags this cable.")
        c.create_text(264, yf - 24, text=f"CABLE {n}", fill=S_ID, font=F(11, "mono_b"))
        c.create_line(272, yf, 292, yf, fill=S_BUS, width=2 * SCALE)
        vals = self.pins.get(n, [])
        for k, (pin, _a) in enumerate(vals):
            yk = yf + (k - (len(vals) - 1) / 2) * 17
            self.put(f"cond{n}_{k}", c.create_line(292, yf, 308, yk, 556, yk, 572, yf, fill=S_BUS, width=1.4 * SCALE))
            row = c.create_rectangle(312, yk - 8, 554, yk + 8, fill=BG, outline="")
            help_txt = (f"Cable {n} pin {pin}: current in this wire. Amber from {PIN_WARN_A:.0f} A. Over {PIN_MAX_A:.0f} A "
                        f"twice (or once over {PIN_HARD_A:.0f} A) closes GPU programs. Notch above the bar = today's peak.")
            c.create_text(314, yk, text=f"P{pin}", fill=S_ID, font=F(11, "mono_m"), anchor="w")
            c.create_rectangle(348, yk - 1.5, 486, yk + 1.5, fill=GRID, outline="")
            self.put(f"bar{n}_{k}", c.create_rectangle(348, yk - 4, 348, yk + 4, fill=BAR_OK, outline=""))
            # today's peak: a notch above the bar (max-hold), so a short spike stays visible after it passes
            self.put(f"pk{n}_{k}", c.create_line(348, yk - 7, 348, yk - 3, fill=TXT2, width=2 * SCALE, state="hidden"))
            lim = 348 + 138 * PIN_MAX_A / 10
            c.create_line(lim, yk - 6, lim, yk + 6, fill=BAD, width=1.5 * SCALE)
            self.text(f"val{n}_{k}", 550, yk, "", S_VAL, 13, "mono_m", "e")
            self.tip([row, self.items[f"bar{n}_{k}"], self.items[f"val{n}_{k}"]], help_txt)
        # balance: a band from this cable's lowest to highest pin. Narrow = pins share evenly; wide = one pin
        # carries more than its share, the pattern that comes before a connector overheats.
        sy = yf + (len(vals) - 1) / 2 * 17 + 18
        self.spread_y = getattr(self, "spread_y", {})
        self.spread_y[n] = sy
        c.create_text(296, sy, text="SPREAD", fill=SUB, font=F(10, "cond_b"), anchor="w")
        c.create_rectangle(348, sy - 1.5, 486, sy + 1.5, fill=GRID, outline="")
        self.put(f"spr{n}", c.create_rectangle(348, sy - 4, 348, sy + 4, fill="#5b636b", outline=""))
        self.put(f"sprm{n}", c.create_line(348, sy - 7, 348, sy + 7, fill=TXT, width=1.5 * SCALE))
        self.text(f"sprv{n}", 550, sy, "", S_VAL, 12, "mono_m", "e")
        self.tip([self.items[f"spr{n}"], self.items[f"sprv{n}"]],
                 f"Cable {n} balance: band = lowest to highest pin, tick = average. Narrow is good; "
                 f"wide means uneven sharing. Warns at {IMB_SPREAD_A:.0f} A apart under load.")
        if n == "1":  # one amp scale under cable 1 serves the bars and the balance strip of both cables
            for amps in (0, 4, 8, 10):
                c.create_text(348 + 138 * amps / 10, sy + 16, text=f"{amps} A" if amps == 10 else str(amps),
                              fill=BAD if amps == 8 else DIM, font=F(11))
        c.create_line(572, yf, 650, yf, fill=S_BUS, width=2 * SCALE)
        self.text(f"fi{n}", 588, yf - 15, "", S_VAL, 13, "mono_m")  # cable total current, above the line
        self.text(f"fp{n}", 588, yf + 15, "", S_VAL, 13, "mono_m")  # and the watts it carries, below
        self.tip([self.items[f"fi{n}"], self.items[f"fp{n}"]], f"Cable {n} total: current and watts delivered to the card.")
        # the load: the card this cable feeds
        g = self.cable_map.get(n)
        gpu = self.gpus.get(g)
        if not gpu:
            c.create_text(660, yf, text="NO GPU DATA", fill=WARN, font=F(11, "cond_b"), anchor="w")
            return
        parts = gpu["name"].split(":")
        brand = parts[2].strip() if len(parts) > 2 else ""
        specs = draw_gpu_art(c, 664, yf - 34, 176, 68, "msi" if brand == "MSI" else "dell",
                             [self.fan_angle[(g, i)] for i in range(3)])
        self.fan_specs += [((g, i), *spec) for i, spec in enumerate(specs)]
        gx = 858
        self.tip(c.create_text(gx, yf - 52, text=f"GPU{g}", fill=S_ID, font=F(14, "mono_b"), anchor="w"),
                 f"GPU{g}: MEMORY = its GDDR7 memory temperature, amber from {MEM_WARN_C} °C. RTX 50 cards no longer report a "
                 f"hotspot. Clocks and every other reading are in DATA.")
        # card model above its drawing; the cards don't report a model name, so settings.json "gpu_models" can set it
        model = (self.settings.get("gpu_models") or {}).get(brand) or f"{brand} GeForce RTX 5080"
        c.create_text(752, yf - 52, text=model.upper(), fill=S_NAME, font=F(11, "mono_m"))
        for r, labs in enumerate((("TEMP", "MEMORY", "POWER"), ("FANS", "VRAM", "LOAD"))):
            y = yf - 22 + r * 46
            for col, (lab, dx) in enumerate(zip(labs, GPU_COLS)):
                lab_i = c.create_text(gx + dx, y - 12, text=lab, fill=S_LBL, font=F(11), anchor="w")
                self.text(f"g{g}_{r}{'abc'[col]}", gx + dx, y + 9, "", S_VAL, 16, "mono_m")
                if r == 0 and col < 2:  # today's highest temperature, right after the TEMP / MEM TEMP label
                    lx = gx + dx + (c.bbox(lab_i)[2] - c.bbox(lab_i)[0]) / SCALE + 7  # text is in pixels already
                    self.text(f"g{g}_max{col}", lx, y - 12, "", DIM, 11)
        # what Afterburner has applied: one quiet line under the drawing and the readouts. Placed by place_oc().
        for k, lab in enumerate(("POWER", "CORE", "MEM", "TEMP LIMIT")):
            self.put(f"g{g}_ocl{k}", c.create_text(664, yf + 68, text=lab, fill=SUB, font=F(11, "cond"), anchor="w"))
            self.tip(self.text(f"g{g}_oc{k}", 664, yf + 68, "", S_VAL, 12, "mono"),
                     f"GPU{g}: settings MSI Afterburner has applied (read only). Power limit is % of the card's "
                     f"{GPU_TDP_W} W default (75% = {GPU_TDP_W * 75 // 100} W); offsets are added to stock clocks; "
                     f"n/a = this card doesn't expose it.")
        self.oc_gx = gx
        self.oc_y = getattr(self, "oc_y", {})
        self.oc_y[g] = yf + 68

    def place_oc(self, g, y):
        """Lay out the settings row from what it shows: MEM and TEMP LIMIT on the first two readout columns when
        everything fits, else four evenly spaced cells (wide values such as a +3000 MHz memory offset)."""
        c, gx = self.c, self.oc_gx
        bw = lambda name: (lambda b: (b[2] - b[0]) / SCALE)(c.bbox(self.items[name]))  # text is in pixels already
        lw = [bw(f"g{g}_ocl{k}") for k in range(4)]
        cw = [lw[k] + 6 + bw(f"g{g}_oc{k}") for k in range(4)]
        gap = (gx - 664 - cw[0] - cw[1]) / 2
        if gap >= 12 and cw[2] <= GPU_COLS[1] - 12 and gx + GPU_COLS[1] + cw[3] <= W - 14:
            xs = [664, 664 + cw[0] + gap, gx, gx + GPU_COLS[1]]
        else:
            gap = max(8, (W - 14 - 664 - sum(cw)) / 3)
            xs = [664 + sum(cw[:k]) + k * gap for k in range(4)]
        for k, x in enumerate(xs):
            self.move(f"g{g}_ocl{k}", (x, y))
            self.move(f"g{g}_oc{k}", (x + lw[k] + 6, y))

    def build_lower(self):
        c = self.c
        self.trace_box = {}
        y0 = LOW_Y
        c.create_line(0, y0, W, y0, fill=LINE)
        c.create_line(746, y0, 746, H, fill=LINE)
        # trends, 90 s, each auto-scaled to a round full-scale so idle and load are both readable
        section(c, 0, y0, 746, "02", "Last 90 seconds")
        rows = [("worst", "HIGHEST PIN", "A")] + [(g, f"GPU{g} POWER", "W") for g in sorted(self.gpus)[:2]]
        for i, (key, label, unit) in enumerate(rows):
            ty0, ty1 = y0 + 48 + i * 52, y0 + 78 + i * 52  # label row sits clear above the plot and its limit line
            tx0, tx1 = 14, 746 - 52
            c.create_text(tx0, ty0 - 11, text=label, fill=S_LBL, font=F(11), anchor="w")
            self.text(f"peak{key}", tx1, ty0 - 11, "", TXT2, 11, "mono", "e")
            c.create_line(tx0, ty1, tx1, ty1, fill=GRID)
            self.text(f"top{key}", tx1 + 44, ty0, "", SUB, 10, "mono", "e")
            if key == "worst":  # the 8 A limit, drawn whenever it falls inside the current scale
                self.put("limworst", c.create_line(tx0, ty0, tx1, ty0, fill=BAD, dash=(4, 3), state="hidden"))
            self.put(f"trace{key}", c.create_line(0, 0, 0, 0, fill=TXT2, width=1.4 * SCALE, state="hidden"))
            self.put(f"tip{key}", c.create_rectangle(0, 0, 0, 0, fill=TXT, outline="", state="hidden"))
            self.trace_box[key] = (tx0, tx1, ty0, ty1, unit)
        # energy
        section(c, 746, y0, W, "03", "Energy", "PSU USB + MSI CENTER", DIM)
        for k, lab in enumerate(("PSU ON FOR", "PSU LIFETIME", "TOTAL ENERGY")):
            c.create_text(760, y0 + 37 + k * 52, text=lab, fill=S_LBL, font=F(11), anchor="w")
            self.text(f"en{k}", 760, y0 + 58 + k * 52, "", S_VAL, 16, "mono_m")
        today = datetime.date.today()
        self.days = [today - datetime.timedelta(d) for d in range(6, -1, -1)]
        bx0, bx1 = 900, W - 14
        c.create_text(bx0, y0 + 37, text="ENERGY PER DAY (kWh)", fill=S_LBL, font=F(11), anchor="w")
        bw = (bx1 - bx0) / 7
        self.ebox = (bx0, bw, y0 + 60, H - 26)  # headroom: the tallest bar's value clears the caption
        c.create_line(bx0, H - 26, bx1, H - 26, fill=LINE)
        for k, d in enumerate(self.days):
            cx = bx0 + (k + 0.5) * bw
            self.put(f"ebar{k}", c.create_rectangle(0, 0, 0, 0, fill=TXT2 if k == 6 else "#3a4047", outline=""))
            self.text(f"eval{k}", cx, H - 40, "", TXT2 if k == 6 else SUB, 10, "mono", "s")
            c.create_text(cx, H - 14, text=d.strftime("%a").upper(), fill=TXT if k == 6 else SUB, font=F(10, "mono_m"))
        self.text("enote", (bx0 + bx1) / 2 - bw / 2, (y0 + 60 + H - 26) / 2, "", DIM, 11, "cond", "center")

    def update(self, readings, faults, age):
        if self.error:
            state, color = "ERROR", BAD
        elif any(f["kind"] in ("pin", "volt") for f in faults):
            state, color = "FAULT", BAD
        elif self.reason:
            state, color = "NOT PROTECTED", WARN
        elif self.warnings:
            state, color = "WARNING", WARN
        else:
            state, color = "PROTECTED", OK
        self.set("status", text=state, fill=color)
        self.set("status_sq", fill=color)
        a = self.arrivals
        gaps = sorted(y - x for x, y in zip(a, list(a)[1:]))
        ab = f"{gaps[len(gaps) // 2]:.1f} s" if gaps else "-"  # median gap: robust to one late sample
        # the green squares already say "all reporting"; this line only adds rates and time, so it fits the row
        self.link_idle = (("DEMO  ·  " if DEMO else "") +
                          f"readings every {ab}  ·  checked every 1 s  ·  {time.strftime('%H:%M')}")
        issue, fix, segs = self.link_state(readings, age)
        if self.error:
            issue, fix = f"Internal error: {self.error[:70]}. Details in substashen.log.", None
        elif not issue and self.warnings:  # warn-only rules: say what, nothing to click
            issue = next(iter(self.warnings.values()))
        calm = not issue and all(col == OK for _l, col, _v in segs)  # all fine: one line, no indicator clutter
        for i, (label, col, val) in enumerate(segs):
            shown = "normal" if not calm or i == 0 else "hidden"
            self.set(f"seg{i}_sq", fill=col, state=shown)
            self.set(f"segl{i}", text="ALL SENSORS OK" if calm else label, fill=SUB, state=shown)
            self.set(f"seg{i}", text=val, fill=col if col != OK else TXT2, state="hidden" if calm else "normal")
        self.link_now = (issue, fix)
        self.show_issue()
        self.c.update_idletasks()  # repaint each region on its own: Tk merges all changes into one box
        if readings is None:
            self.set("worst", text="-", fill=DIM)
            self.set("worst_sub", text="no pin data")
            hx0, _hx1, hy = self.hbar_x
            self.move("hbar", (hx0, hy - 3, hx0, hy + 3))  # no data: empty, never a stale reading
            return
        psu, v12 = self.psu, self.psu.get("+12V")
        self.vcol = V = S_VAL if age <= STALE_S else DIM  # stale values dim instead of looking live
        every = [(a, n, p) for n, v in self.pins.items() for p, a in v]
        if every:
            a, n, p = max(every)
            col = level(a, PIN_WARN_A, PIN_MAX_A)
            self.set("worst", text=f"{a:.2f} A", fill=V if col == TXT2 else col)
            hx0, hx1, hy = self.hbar_x
            self.move("hbar", (hx0, hy - 3, hx0 + (hx1 - hx0) * min(a, 10) / 10, hy + 3))
            self.set("hbar", fill=V if col == TXT2 else col)
            self.set("worst_sub", text=f"{a / PIN_MAX_A * 100:.0f}% of limit  ·  cable {n} pin {p}")
        else:
            self.set("worst", text="-", fill=DIM)
            self.set("worst_sub", text="no pin data")
            hx0, _hx1, hy = self.hbar_x
            self.move("hbar", (hx0, hy - 3, hx0, hy + 3))  # no data: empty, never a stale reading
        vals = (f"{num(psu.get('PSU Power Out'))} W", f"{num(psu.get('PSU Efficiency'), 1)} %",
                f"{num(psu.get('PSU Temperature'))} \u00b0C", f"{num(psu.get('PSU Fan'))} rpm")
        for k, v in enumerate(vals):
            col = level(psu.get("PSU Temperature") or 0, 60, 75) if k == 2 else V
            self.set(f"psu{k}", text=v, fill=V if col == TXT2 else col)
        ok12 = v12 is None or V_MIN <= v12 <= V_MAX
        self.set("bus_u", text=f"{num(v12, 3)} V", fill=V if ok12 else BAD)
        if v12 is not None:
            mx = self.rail_sx(v12)
            ry = self.rail_y
            self.move("u_mark", (mx - 4, ry - 5, mx + 4, ry - 5, mx, ry + 1))
            self.set("u_mark", fill=TXT if ok12 else BAD)
        self.c.update_idletasks()
        blink = int(time.time()) % 2 == 0
        for n in FEEDER_Y:
            vals = self.pins.get(n, [])
            status = psu.get(f"12V-2x6_{n} Alarm") or 0
            self.set(f"brk{n}", fill=(BAD if blink else BG) if status else BG)  # red only on a real alarm
            total = sum(a for _, a in vals)
            self.set(f"fi{n}", text=f"{total:.1f} A" if vals else "-", fill=V if vals else DIM)
            self.set(f"fp{n}", text=f"{total * v12:,.0f} W" if vals and v12 else "-", fill=V if vals else DIM)
            for k, (_pin, a) in enumerate(vals):
                col = level(a, PIN_WARN_A, PIN_MAX_A)
                live = V if col == TXT2 else col
                self.set(f"cond{n}_{k}", fill=S_BUS if col == TXT2 else col)
                yk = FEEDER_Y[n] + (k - (len(vals) - 1) / 2) * 17
                self.move(f"bar{n}_{k}", (348, yk - 4, 348 + 138 * min(a, 10) / 10, yk + 4))
                self.set(f"bar{n}_{k}", fill=BAR_OK if col == TXT2 else col)
                self.set(f"val{n}_{k}", text=f"{a:.2f}", fill=live)
                pk = self.day_stats["pin_peak"].get((n, _pin))
                if pk is not None:
                    px = 348 + 138 * min(pk, 10) / 10
                    self.move(f"pk{n}_{k}", (px, yk - 7, px, yk - 3))
                    pcol = level(pk, PIN_WARN_A, PIN_MAX_A)
                    self.set(f"pk{n}_{k}", fill=TXT2 if pcol == TXT2 else pcol, state="normal")
            if vals and n in getattr(self, "spread_y", {}):
                amps = [a for _, a in vals]
                lo, hi, avg, sy = min(amps), max(amps), sum(amps) / len(amps), self.spread_y[n]
                spread = hi - lo
                loaded = sum(amps) >= IMB_MIN_TOTAL_A
                col = (WARN if loaded and spread >= IMB_SPREAD_A else
                       mix(WARN, BG, 0.45) if loaded and spread >= IMB_SPREAD_A * 0.75 else "#5b636b")
                x = lambda a: 348 + 138 * min(a, 10) / 10
                self.move(f"spr{n}", (x(lo), sy - 4, max(x(hi), x(lo) + 2), sy + 4))
                self.set(f"spr{n}", fill=col)
                self.move(f"sprm{n}", (x(avg), sy - 7, x(avg), sy + 7))
                self.set(f"sprv{n}", text=f"{spread:.2f}", fill=V if col == "#5b636b" else WARN)
            self.c.update_idletasks()
            g = self.cable_map.get(n)
            gpu = self.gpus.get(g)
            if gpu:
                self.update_gpu(g, gpu["v"])
                self.c.update_idletasks()
        if self.view != "full":  # trend and energy rows aren't drawn
            return
        # trends
        for g, (tx0, tx1, ty0, ty1, unit) in getattr(self, "trace_box", {}).items():
            h = self.hist[g]
            fmt = "{:>4.1f}" if unit == "A" else "{:>3.0f}"  # fixed width: the text doesn't shift
            pad = lambda w: fmt.format(w) if w is not None else "  -"
            self.set(f"peak{g}", text=f"NOW {pad(h[-1] if h else None)} {unit}   PEAK {pad(max(h) if h else None)} {unit}")
            steps = (1, 2, 4, 8, 10) if unit == "A" else (60, 120, 240, 360)
            top = next((st for st in steps if (max(h) if h else 0) <= st * 0.9), steps[-1])  # round full scale
            self.set(f"top{g}", text=f"{top:g} {unit}")
            if g == "worst":
                ly = ty1 - PIN_MAX_A / top * (ty1 - ty0)
                self.move("limworst", (tx0, ly, tx1, ly))
                self.set("limworst", state="normal" if PIN_MAX_A <= top else "hidden")
            # redrawing the traces was ~60% of the app's CPU; on a 90 s graph a 3 s step looks the same
            rescaled = self.cache.get("top" + g) != top
            self.cache["top" + g] = top
            if len(h) < 2 or (not rescaled and int(time.time()) % TRACE_EVERY_S and self.cache.get("@trace" + g)):
                continue
            step = (tx1 - tx0) / (h.maxlen - 1)
            pts = [p for k, w in enumerate(h) for p in (tx1 - (len(h) - 1 - k) * step,
                                                       ty1 - min(max(w, 0), top) / top * (ty1 - ty0))]
            self.move(f"trace{g}", pts)
            self.move(f"tip{g}", (pts[-2] - 2, pts[-1] - 2, pts[-2] + 2, pts[-1] + 2))
            for part in ("trace", "tip"):
                self.set(f"{part}{g}", state="normal")
        self.c.update_idletasks()
        # energy: PSU runtime counters (USB) + MSI Center's kWh record; nothing logged by Substashen
        if self.runtime:
            on, total, at = self.runtime
            on += time.time() - at  # count on between the once-a-minute reads
            self.set("en0", text=f"{int(on // 3600)}h {int(on % 3600 // 60):02d}m", fill=S_VAL)
            self.set("en1", text=f"{(total + time.time() - at) / 3600:,.0f} h", fill=S_VAL)
        else:
            self.set("en0", text="reading..." if self.usb_ok is None else "no USB reply", fill=SUB)
            self.set("en1", text="-", fill=SUB)
        kwh = self.record.get("kwh_total")
        self.set("en2", text=f"{kwh:,.1f} kWh" if kwh is not None and math.isfinite(kwh) else "no MSI record")
        daily = self.record.get("daily", {})
        kw = [daily.get(d, 0.0) for d in self.days]
        bx0, bw, bt, bb = self.ebox
        top = max(max(kw), 0.1)
        for k, v in enumerate(kw):
            yv = bb - v / top * (bb - bt - 14)
            self.move(f"ebar{k}", (bx0 + k * bw + bw * 0.22, yv, bx0 + (k + 1) * bw - bw * 0.22, bb))
            self.move(f"eval{k}", (bx0 + (k + 0.5) * bw, yv - 3))
            self.set(f"eval{k}", text=f"{v:.2f}" if v > 0 else "")
        recorded = sum(1 for v in kw if v > 0)
        self.set("enote", text="" if recorded > 1 else "MSI Center has recorded\nonly today so far" if recorded
                 else "no daily record yet")

    def update_gpu(self, g, v):
        rpm = (v.get("GPU Fan1") or 0, v.get("GPU Fan2", v.get("GPU Fan1")) or 0)
        for i in range(3):
            r = rpm[i == 1]
            # deg/s on screen. Real rpm would alias into a strobe; this stays under ~1/3 blade spacing per frame
            # (11 blades = 33 deg apart, 12 fps) while still reading faster as the fans speed up.
            self.fan_speed[(g, i)] = 0.0 if r <= 0 else 30 + 100 * min(r, 3000) / 3000
        if any(self.fan_speed.values()) and not getattr(self, "anim_job", None):
            self.anim_t = time.time()
            self.animate()
        temp, power, used = v.get("GPU Temperature"), v.get("GPU Power"), v.get("GPU Memory Allocated")
        V = getattr(self, "vcol", S_VAL)
        tcol, pcol = level(temp or 0, 75, 85), level(power or 0, GPU_TDP_W * 0.9, GPU_TDP_W)
        self.set(f"g{g}_0a", text=f"{num(temp)} \u00b0C", fill=V if tcol == TXT2 else tcol)
        self.set(f"g{g}_0c", text=f"{num(power)} W", fill=V if pcol == TXT2 else pcol)
        self.set(f"g{g}_1a", text=f"{num(max(rpm))} rpm" if any(rpm) else "stopped", fill=V)
        mt = self.memt.get((self.oc.get(g) or {}).get("bus"))  # GDDR7 temperature, matched to this card by PCI bus
        mcol = level(mt or 0, MEM_WARN_C, MEM_MAX_C)
        self.set(f"g{g}_0b", text=f"{mt:.0f} °C" if mt is not None else "-",
                 fill=DIM if mt is None else V if mcol == TXT2 else mcol)
        load, vram = v.get("GPU Core Load"), v.get("GPU Memory Allocated")
        self.set(f"g{g}_1c", text=f"{num(load)}%" if load is not None else "-", fill=V if load is not None else DIM)
        self.set(f"g{g}_1b", text=f"{vram / 1024:.1f} GB" if vram is not None else "-", fill=V if vram is not None else DIM)
        tmax = self.day_stats.setdefault("gpu_tmax", {})
        if temp is not None:
            tmax[g] = max(tmax.get(g, temp), temp)
        self.set(f"g{g}_max0", text=f"max {tmax[g]:.0f}" if g in tmax else "")
        if mt is not None:
            tmax["m" + g] = max(tmax.get("m" + g, mt), mt)
        self.set(f"g{g}_max1", text=f"max {tmax['m' + g]:.0f}" if "m" + g in tmax else "")
        oc = self.oc.get(g)
        sign = lambda x: f"{x:+d}".replace("-", "−")
        cells = (("-" if not oc else "n/a" if oc["power"] is None else f"{oc['power']}%"),
                 ("-" if not oc else "n/a" if oc["core"] is None else f"{sign(oc['core'])} MHz"),
                 ("-" if not oc else "n/a" if oc["mem"] is None else f"{sign(oc['mem'])} MHz"),
                 ("-" if not oc else "n/a" if oc["temp"] is None else f"{oc['temp']} °C"))
        for k, txt in enumerate(cells):
            self.set(f"g{g}_oc{k}", text=txt, fill=V if txt not in ("-", "n/a") else DIM)
        if self.cache.get("ocrow" + g) != cells:  # re-place only when the text changes (every few seconds at most)
            self.cache["ocrow" + g] = cells
            self.place_oc(g, self.oc_y[g])

    def fill_table(self, readings):
        t = self.table
        t.delete("1.0", "end")
        t.insert("end", "RECENT EVENTS (full history in incidents.log)\n", "h")
        for line in self.recent or ["none yet"]:
            t.insert("end", line + "\n", "bad" if "ALARM" in line else "")
        t.insert("end", "\n")
        rows, gpus, other = {}, set(), []
        for sensor, label, val, unit in readings:
            g = GPU_RE.search(sensor)
            if g:
                gpus.add(g.group(1))
                rows.setdefault((label, unit), {})[g.group(1)] = val
            elif sensor == "MSI PSU":
                other.append((label, val, unit))
        gpus = sorted(gpus)
        t.insert("end", f"{'GPU READING':44}" + "".join(f"{'GPU ' + g:>16}" for g in gpus) + "\n", "h")
        for (label, unit), per in rows.items():
            cells = "".join(f"{per[g]:>11.3f} {unit[:4]:<4}" if g in per else " " * 16 for g in gpus)
            t.insert("end", f"{label[:44]:44}{cells}\n")
        t.insert("end", "\nMSI PSU\n", "h")
        for label, val, unit in other:
            bad = (unit == "A" and PIN_RE.search(label) and val > PIN_MAX_A) or (label.endswith("Alarm") and val) or \
                  (label == "+12V" and not V_MIN <= val <= V_MAX)
            t.insert("end", f"{label[:44]:44}{val:>11.3f} {unit}\n", "bad" if bad else "")

    def animate(self):
        """Turns the fan blades between full redraws; idles while the fans are stopped."""
        now = time.time()
        dt, self.anim_t = min(now - self.anim_t, 0.2), now
        moving = False
        if self.visible():
            try:
                for key, cx, cy, rh, r, item, style in self.fan_specs:
                    target = self.fan_speed.get(key, 0.0)
                    v = self.fan_vis.get(key, 0.0)
                    v += (target - v) * min(1.0, dt * 1.5)  # spin up / wind down over ~2 s, like real fans
                    v = 0.0 if target == 0 and v < 2 else v
                    self.fan_vis[key] = v
                    if not v:
                        continue
                    moving = True
                    self.fan_angle[key] = a = (self.fan_angle[key] + v * dt) % 360
                    self.c.coords(item, *[p * SCALE for p in fan_path(cx, cy, rh, r, a, style, GPU_LOOK[style]["blades"])])
            except tk.TclError:  # items were replaced by a redraw mid-loop
                pass
        # stops completely when nothing turns; update_gpu restarts it when a fan spins up
        self.anim_job = self.root.after(83, self.animate) if moving else None

    # --- alerts ---
    def test_alert(self):
        """Rings the real alarm and covers every screen, without closing anything, so the user knows it works."""
        self.raise_alert([{"kind": "test", "title": "Alarm test", "where": "no fault, this is a test", "show": "TEST",
                           "val": 0, "unit": "", "limit": "sound, flash and screen cover"}], time.time(), force=True)

    def raise_alert(self, faults, now, force=False):
        sev = max(SEVERITY[f["kind"]] for f in faults)
        if not force:
            if self.popup and sev <= self.popup_sev:
                return False
            if not self.popup and now < self.snooze_until and sev <= self.snooze_sev:
                return False
        kind = faults[0]["kind"]
        alarm("pin" if kind in ("pin", "test") else kind)  # sound first: it must get through even if drawing fails
        f0 = faults[0]
        self.event("TEST" if kind == "test" else "ALARM", f"{f0['title']}: {f0['where']} {fault_value(f0)}",
                   "" if kind == "test" else self.snapshot())
        if kind != "test":
            self.day_stats["alarms"] += 1
        self.close_covers()
        self.root.withdraw()  # the alert replaces the panel until acknowledged
        self.popup_sev, self.popup_items, self.flash_items = sev, [], []
        self.popup = []
        try:
            for rect in monitors():
                self.popup.append(self.cover(rect, faults))  # one by one: a failure can't orphan a cover
            self.popup[0].focus_force()
        except Exception:
            log("cover failed:\n" + traceback.format_exc())
            self.close_covers()
            self.popup = [self.fallback_cover(faults)]
        if getattr(self, "flash_job", None):
            self.root.after_cancel(self.flash_job)
        self.flash()
        return True

    def close_covers(self):
        for p in self.popup or []:
            try:
                p.destroy()
            except tk.TclError:
                pass
        self.popup = None

    def fallback_cover(self, faults):
        p = tk.Toplevel(self.root, bg=BG)
        p.attributes("-topmost", True)
        p.protocol("WM_DELETE_WINDOW", self.ack)
        tk.Label(p, text="\n".join(f"{f['title']}: {f['where']}  {fault_value(f)}" for f in faults), bg=BG, fg=TXT,
                 font=("Consolas", 16), padx=40, pady=30).pack()
        tk.Button(p, text="ACKNOWLEDGE", command=self.ack, bg=BAD, fg=BG, font=("Consolas", 14)).pack(pady=20)
        return p

    def closed_text(self):
        if self.kill_thread and self.kill_thread.is_alive() and not self.closed:
            return "closing GPU programs..."
        text = "closed " + (", ".join(sorted(self.closed)) or "nothing (no programs on the GPUs)")
        if self.failed:
            text += "   COULD NOT CLOSE " + ", ".join(sorted(self.failed)) + ": close it yourself or shut down now"
        return text

    def cover(self, rect, faults):
        global SCALE
        keep, SCALE = SCALE, rect[4]
        try:
            return self._cover(rect, faults)
        finally:
            SCALE = keep

    def _cover(self, rect, faults):
        """One borderless topmost window per monitor: flashing frame and band, the reading that tripped, what was done."""
        mx, my, pw, ph, k = rect
        sw, sh = pw / k, ph / k  # layout in 100 % units
        f = faults[0]
        kind = f["kind"]
        pin_fault = kind == "pin"
        accent = WARN if kind == "offline" else BAD
        p = tk.Toplevel(self.root)
        p.overrideredirect(True)
        p.attributes("-topmost", True)
        p.geometry(f"{pw}x{ph}+{mx}+{my}")
        p.protocol("WM_DELETE_WINDOW", self.ack)  # Alt+F4 acknowledges instead of leaving a hidden, looping alarm
        p.bind("<Return>", lambda e: self.ack())
        c = tk.Canvas(p, bg="#000000", highlightthickness=0)
        c.pack(fill="both", expand=True)
        band = c.create_rectangle(0, 0, sw, 52, fill=accent, outline="")
        frame = c.create_rectangle(4, 4, sw - 4, sh - 4, outline=accent, width=8 * k)
        head = c.create_text(24, 26, text=f"{APP.upper()}  /  {'CAUTION' if kind == 'offline' else 'MASTER WARNING'}",
                             fill="#000000", font=F(18, "mono_b"), anchor="w")
        clock = c.create_text(sw - 24, 26, text=time.strftime("%H:%M:%S"), fill="#000000", font=F(18, "mono_b"), anchor="e")
        self.flash_items.append((c, band, frame, head, clock, accent))
        x = max(48, sw * 0.07)
        # wide screens: the facts and the button on the left, every pin large on the right; else one column
        two_col = pin_fault and bool(self.pins) and sw >= 1400
        col_w = min(860, sw * 0.42) if two_col else min(980, sw - 2 * x)
        y = top = max(120, (sh - 500) / 2) if two_col else max(110, sh * 0.12)  # two columns: centred
        c.create_text(x, y, text=f["title"].upper(), fill=accent, font=F(30, "cond_b"), anchor="w")
        y += 26
        big = font(min(170, sw // 7, round(col_w / 4.2)), "mono_b")
        c.create_text(x - 8, y, text=fault_value(f), fill=TXT, font=big, anchor="nw")
        y += big.metrics("linespace") / k + 8
        kv = [("Location", f["where"]), ("Limit", f["limit"])]
        if pin_fault:
            kv.append(("Action", self.closed_text()))
        elif kind == "volt":
            kv.append(("Action", "nothing closed; save your work and check the PSU"))
        elif kind == "offline":
            kv.append(("Action", "check MSI Afterburner is running with the PSU plugin, and the PSU USB cable"))
        else:
            kv.append(("Action", "none, this is a test. Nothing was closed."))
        kv += [("Also", f"{g['title']}: {g['where']}  {fault_value(g)}") for g in faults[1:4]]
        for lab, val in kv:
            c.create_text(x, y, text=lab.upper(), fill=DIM, font=F(16, "cond_b"), anchor="nw")
            t = c.create_text(x + 130, y, text=val, fill=TXT if lab != "Action" or not self.failed else BAD,
                              font=F(18), anchor="nw", width=(col_w - 130) * k)
            if lab == "Action" and pin_fault:
                self.popup_items.append((c, t))
            y += (c.bbox(t)[3] - c.bbox(t)[1]) / k + 12
        if pin_fault:
            c.create_text(x, y + 8, text="Check the 12V-2x6 cable is fully seated at both ends and look for melted or "
                          "discoloured pins before running anything heavy.", fill=SUB, font=F(18, "cond"), anchor="nw",
                          width=col_w * k)
            y += 70
            if self.pins:
                alarms = {re.search(r"_(\d)", kk).group(1): vv for kk, vv in self.psu.items() if kk.endswith("Alarm")}
                if two_col:
                    rx0, rx1 = x + col_w + sw * 0.05, sw - x
                    c.create_text(rx0, top, text="ALL 12 PINS NOW", fill=SUB, font=F(18, "cond_b"), anchor="w")
                    draw_pins(c, rx0, rx1, top + 70, top + min(520, sh * 0.48), self.pins, 18, self.psu.get("+12V"), alarms)
                else:
                    draw_pins(c, x, x + col_w, y + 20, y + 240, self.pins, 14, self.psu.get("+12V"), alarms)
                    y += 320
        elif kind == "volt":
            draw_rail(c, x, x + col_w, y + 20, f["val"], 8)
            y += 70
        y += 10
        b = c.create_rectangle(x, y, x + 320, y + 64, fill=accent, outline="")
        bt = c.create_text(x + 160, y + 32, text="ACKNOWLEDGE  \u23ce", fill="#000000", font=F(20, "mono_b"))
        c.create_text(x + 344, y + 32, text=f"Alerts pause {SNOOZE_S} s after this. A worse fault still alerts, and a pin "
                      "fault keeps closing GPU programs.", fill=SUB, font=F(16, "cond"), anchor="w", width=(col_w - 344) * k)
        for item in (b, bt):
            c.tag_bind(item, "<Button-1>", lambda e: self.ack())
            c.tag_bind(item, "<Enter>", lambda e: (c.itemconfig(b, fill=TXT), c.config(cursor="hand2")))
            c.tag_bind(item, "<Leave>", lambda e: (c.itemconfig(b, fill=accent), c.config(cursor="")))
        c.scale("all", 0, 0, k, k)
        return p

    def flash(self):
        """Blink the band and frame twice a second, and keep the Action line current."""
        self.flash_job = None
        if not self.popup:
            return
        self.flash_on = not self.flash_on
        for c, band, frame, head, clock, accent in self.flash_items:
            try:
                c.itemconfig(band, fill=accent if self.flash_on else "#000000")
                c.itemconfig(frame, outline=accent if self.flash_on else "#000000")
                c.itemconfig(head, fill="#000000" if self.flash_on else accent)
                c.itemconfig(clock, text=time.strftime("%H:%M:%S"), fill="#000000" if self.flash_on else accent)
            except tk.TclError:
                pass
        for c, item in self.popup_items:
            try:
                c.itemconfig(item, text=self.closed_text(), fill=BAD if self.failed else TXT)
            except tk.TclError:
                pass
        self.flash_job = self.root.after(450, self.flash)

    def ack(self):
        self.event("ACK", "alert acknowledged" + (f"; closed {', '.join(sorted(self.closed))}" if self.closed else ""))
        alarm_stop()
        self.snooze_until, self.snooze_sev = time.time() + SNOOZE_S, self.popup_sev
        self.close_covers()
        self.popup_sev, self.popup_items, self.flash_items = 0, [], []
        self.closed, self.failed = set(), set()
        self.root.deiconify()
        self.render(self.last_frame)


def single_instance():
    """Two copies would both close programs and both sound alarms. Returns False if one is already running."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW(None, False, "Local\\SubstashenSingleInstance")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def selftest():
    # Afterburner -> panel names, using the real names seen on this PC
    gpus = [("VEN_10DE&DEV_2C02&SUBSYS_53111028&REV_A1", "NVIDIA GeForce RTX 5080"),
            ("VEN_10DE&DEV_2C02&SUBSYS_53101462&REV_A1", "NVIDIA GeForce RTX 5080"),
            ("VEN_1002&DEV_164E&SUBSYS_88771043&REV_C1", "AMD Radeon(TM) Graphics")]
    raw = [("GPU1 temperature", "\xb0C", 38.0, 0), ("GPU2 power", "W", 12.7, 1), ("GPU3 usage", "%", 0.0, 2),
           ("GPU1 memory usage \\ process", "MB", 3.4e38, 0), ("CPU1 temperature", "\xb0C", 50.0, 0),
           ("GPU1 usage", "%", float("nan"), 0), ("GPU2 fan tachometer 2", "RPM", 900.0, 1),
           ("PSU 12VHPWR2_6 current", "A", 0.125, 0), ("PSU 12V voltage", "V", 12.094, 0), ("PSU 12VHPWR1 alarm", "", 0.0, 0)]
    # Afterburner control memory: an AMD entry is skipped, unsupported fields read as None, kHz -> MHz
    es = 3760
    def entry(gid, flags, power, core_khz, mem_khz):
        d = [0] * 69
        d[0], d[43], d[47], d[51], d[55] = flags, power, core_khz, mem_khz, 83
        return struct.pack("<69i", *d).ljust(es - 260, b"\0") + gid.encode().ljust(260, b"\0")
    m = entry("VEN_1002&DEV_164E", 0, 0, 0, 0) + entry("VEN_10DE&DEV_2C02", 0x400 | 0x800 | 0x1000, 75, -502000, 1000000)
    assert parse_oc(m, 2, es) == {"0": {"power": 75, "core": -502, "mem": 1000, "temp": None, "bus": None}}
    norm = normalise(raw, gpus)
    assert ("GPU [#0]: NVIDIA GeForce RTX 5080: Dell", "GPU Temperature", 38.0, "\xb0C") in norm
    assert ("GPU [#1]: NVIDIA GeForce RTX 5080: MSI", "GPU Power", 12.7, "W") in norm
    assert ("GPU [#1]: NVIDIA GeForce RTX 5080: MSI", "GPU Fan2", 900.0, "RPM") in norm
    assert ("MSI PSU", "+12V-2x6_2 Pin6 Current", 0.125, "A") in norm
    assert ("MSI PSU", "+12V", 12.094, "V") in norm
    assert ("MSI PSU", "12V-2x6_1 Alarm", 0.0, "") in norm
    assert len(norm) == 6, norm  # AMD iGPU, CPU, NaN and FLT_MAX "no data" entries dropped
    psu = "MSI PSU"
    pins = [(psu, f"+12V-2x6_{c} Pin{p} Current", 4.0, "A") for c in (1, 2) for p in range(1, 7)]
    ok = pins + [(psu, "+12V", 12.09, "V"), (psu, "+12V Current", 40.0, "A")]
    assert check(ok) == [] and coverage(ok, 0) is None
    assert coverage(ok, 9).startswith("Afterburner stopped") and coverage(ok[1:], 0) == "only 11 of 12 pin readings"
    f = check(ok + [(psu, "+12V", 11.5, "V")])
    assert len(f) == 1 and f[0]["kind"] == "volt"
    f = check(ok + [(psu, "+12V", 12.7, "V"), (psu, "+12V-2x6_2 Pin4 Current", 8.3, "A")])
    assert [x["kind"] for x in f] == ["pin", "volt"] and f[0]["where"] == "Cable 2, pin 4" and not f[0]["hard"]
    assert check(ok + [(psu, "+12V-2x6_1 Pin1 Current", 10.5, "A")])[0]["hard"]
    assert check(ok + [(psu, "+12V-2x6_1 Pin1 Current", 3.4e5, "A")]) == []  # garbage, not a fault
    f = check(ok + [(psu, "12V-2x6_2 Alarm", 2.0, "")])
    assert f[0]["hard"] and f[0]["limit"] == "PSU reports pins out of balance"
    # debounce: one 8.3 A sample alone does nothing, the second one alerts; volt needs three
    app = App.__new__(App)
    app.streak, app.arrivals = collections.Counter(), collections.deque(maxlen=11)
    soft = check(ok + [(psu, "+12V-2x6_1 Pin1 Current", 8.3, "A")])
    assert app.evaluate(soft) == [] and len(app.evaluate(soft)) == 1
    volt = check(ok + [(psu, "+12V", 11.0, "V")])
    assert app.evaluate(volt) == [] and app.evaluate(volt) == [] and len(app.evaluate(volt)) == 1
    assert level(5, 6, 8) == TXT2 and level(7, 6, 8) == WARN and level(9, 6, 8) == BAD
    for cmd in (0x41, 0x43, 0xC0, 0xC2, 0xF1, 0xE0):  # fan, zero-fan, OCP, buzzer, save-to-NVRAM: never sendable
        try:
            psu_read(cmd)
            raise AssertionError(f"PSU command 0x{cmd:02X} was allowed")
        except ValueError:
            pass
    # warn-only rules: imbalance needs load and 5 s; cap needs 30 s over cap+20 W; heat needs minutes near 7 A
    mem = {}
    uneven = {"1": [(1, 6.0)] + [(p, 2.0) for p in range(2, 7)]}  # 16 A total, 4 A spread
    assert advisory_checks(uneven, {}, 270, mem, 0, 1) == {} and "imb1" in advisory_checks(uneven, {}, 270, mem, 5, 1)
    light = {"1": [(1, 4.5)] + [(p, 0.5) for p in range(2, 7)]}  # same spread, only 7 A total: idle noise, no warning
    assert "imb1" not in advisory_checks(light, {}, 270, {}, 99, 1)
    mem = {}
    assert advisory_checks({}, {"0": 300}, 270, mem, 0, 1) == {} and "cap0" in advisory_checks({}, {"0": 300}, 270, mem, 30, 1)
    assert advisory_checks({}, {"0": 285}, 270, {}, 99, 1) == {}  # within the 20 W margin
    mem, hot = {}, {"2": [(3, 7.5)]}
    assert "heat2_3" not in advisory_checks({"2": [(3, 9.0)]}, {}, 270, {}, 0, 1)  # one spike is not "sustained"
    assert advisory_checks({"1": [(1, 3e5)] + [(p, 4.0) for p in range(2, 7)]}, {}, 270, {}, 0, 600) == {}  # garbage
    assert "heat2_3" not in advisory_checks({"2": [(3, 2.0)]}, {}, 270, mem, 0, 1)
    for t in range(1, 600):
        active = advisory_checks(hot, {}, 270, mem, t, 1)
    assert "heat2_3" in active
    for kind in TONES:  # every alarm file builds and is loud
        w = wave.open(tone_file(kind))
        peak = max(abs(s) for s in array.array("h", w.readframes(w.getnframes())))
        assert peak > 0.8 * 32767, (kind, peak)
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        DEMO_FAULT = "--demo-fault" in sys.argv
        DEMO = DEMO_FAULT or "--demo" in sys.argv
        try:
            if "--after" in sys.argv:  # relaunched: let the old copy exit and release the single-instance lock
                k = ctypes.WinDLL("kernel32")
                k.OpenProcess.restype = ctypes.c_void_p
                k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                k.CloseHandle.argtypes = [ctypes.c_void_p]
                old = k.OpenProcess(0x00100000, False, int(sys.argv[sys.argv.index("--after") + 1]))  # SYNCHRONIZE
                if old:
                    k.WaitForSingleObject(old, 15000)
                    k.CloseHandle(old)
            if DEMO or single_instance():
                incident("START", f"pid {os.getpid()}")
                App().root.mainloop()
            else:
                log("already running; this copy exits")
                ctypes.windll.user32.MessageBoxW(None, "Substashen is already running.", APP, 0x40)
        except Exception:
            log("start-up failed:\n" + traceback.format_exc())
            ctypes.windll.user32.MessageBoxW(None, "Substashen could not start. GPU protection is OFF.\n\n" +
                                             traceback.format_exc()[-700:] + "\nDetails: substashen.log", APP, 0x10)
