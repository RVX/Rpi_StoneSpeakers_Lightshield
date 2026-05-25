# pyTREMOR_lights_live_monitor.py
#
# Real-time companion display for pyTREMOR_lights01.py running on the Pi.
#
# Connects to the Pi over SSH, tails /var/log/pytremor_lights.log, parses each
# frame line (centroid Hz, PWM, 8 band brightnesses) and renders a live
# matplotlib window so you can see EXACTLY what the LEDs are doing — which
# volcano station is feeding data, where in the replay we are, and how the
# eight seismic bands are modulating in real time.
#
# Usage (from this workspace):
#     python 01_pyTREMOR_lights\pyTREMOR_lights_live_monitor.py
#
# To target a different Pi host / zone-id, override SSH_DEST below or pass it
# on the command line:
#     python pyTREMOR_lights_live_monitor.py "sjc1@10.22.171.3"
#
# Requires on the laptop: numpy, matplotlib, OpenSSH client in PATH.

import os
import re
import sys
import json
import time
import subprocess
import threading
import queue
from collections import deque

import numpy as np
import matplotlib
# Prefer the Qt backend on Windows — it handles per-monitor DPI scaling
# natively and is markedly faster than TkAgg at the imshow + scatter
# updates we do every frame. Falls back silently if Qt isn't installed.
for _be in ("QtAgg", "Qt5Agg"):
    try:
        matplotlib.use(_be, force=True)
        break
    except Exception:
        continue
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

# ----------------------------------------------------------------------------
# Connection settings — override on command line or via environment
# ----------------------------------------------------------------------------
SSH_DEST_DEFAULT = "sjc1@fe80::8aa2:9eff:fed7:9f99%19"  # IPv6 link-local on WiFi
SSH_KEY          = os.path.expandvars(r"%USERPROFILE%\.ssh\id_ed25519_pis")
LOG_PATH         = "/var/log/pytremor_lights.log"
# Persisted window position/size (so the monitor reopens where you left it)
GEOM_FILE        = os.path.join(
    os.path.expandvars(r"%APPDATA%") or os.path.expanduser("~"),
    "pyTREMOR_monitor_geometry.json",
)
# Persisted last successfully-fetched overview (instant repaint on next launch
# instead of an empty overview panel while the FDSN fetch is in flight).
OVERVIEW_CACHE_FILE = os.path.join(
    os.path.expandvars(r"%APPDATA%") or os.path.expanduser("~"),
    "pyTREMOR_last_overview.npz",
)
HISTORY_SEC      = 60.0
FPS_ASSUMED      = 20      # pyTREMOR_lights01 writes ~20 frames / s
HISTORY_FRAMES   = int(HISTORY_SEC * FPS_ASSUMED)

# Match band edges used by pyTREMOR_lights01.py (1.0 – 18.0 Hz, 8 log bands)
BAND_EDGES = np.geomspace(1.0, 18.0, 9)

# Per-station metadata mirrored from STATIONS in pyTREMOR_lights01.py.
# Keyed by the SEED station code (sta).
#   value = (place, lat_deg, lon_deg, region, volcano)
STATION_INFO = {
    "HNR":  ("Honiara, Solomon Islands",        -9.4387,  159.9472,
             "Solomon Islands arc",              "Savo / Kavachi"),
    "DAV":  ("Davao, Philippines",                7.0697,  125.5791,
             "Mindanao volcanic arc",            "Mt. Apo (2954 m)"),
    "MAJO": ("Matsushiro, Japan",                36.5457,  138.2041,
             "Japanese arc · Honshu",            "Mt. Asama / Kusatsu-Shirane"),
    "PET":  ("Petropavlovsk-Kamchatsky, Russia", 53.0233,  158.6499,
             "Kamchatka arc",                    "Avachinsky / Klyuchevskoy"),
    "SNZO": ("South Karori, New Zealand",       -41.3087,  174.7044,
             "Taupo volcanic zone (regional)",   "Mt. Ruapehu / Taupo caldera"),
}


def _station_descriptor(station_id):
    """Return a two-line label describing the station whose code appears in
    `station_id` (e.g. "IU.DAV.00.BHZ"). Falls back gracefully when the
    code is unknown or not yet parsed."""
    if not station_id or station_id == "?":
        return ("station: —  waiting for first fetch …", "")
    parts = station_id.split(".")
    sta = parts[1] if len(parts) >= 2 else station_id
    info = STATION_INFO.get(sta)
    if info is None:
        return (f"station: {station_id}", "")
    place, lat, lon, region, volcano = info
    lat_s = f"{abs(lat):.2f}\u00b0{'N' if lat >= 0 else 'S'}"
    lon_s = f"{abs(lon):.2f}\u00b0{'E' if lon >= 0 else 'W'}"
    line1 = f"station: {station_id}   \u2014   {place}   \u00b7   {lat_s} {lon_s}"
    line2 = f"region: {region}   \u00b7   volcano: {volcano}"
    return (line1, line2)


# ----------------------------------------------------------------------------
# Log line parsers
# ----------------------------------------------------------------------------
FRAME_RE = re.compile(
    r"\[([\d:]+)\]\s+cur=\s*([\d.]+)s\s+"
    r"cen=\s*([\d.]+)Hz\s+pwm=\s*(\d+)\s+bands=\s+"
    r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)"
)
# Pi log line:
#   "  Fetching IU.HNR.00.BHZ 2026-05-21T22:17:48.899704Z → 2026-05-21T23:17:48.899704Z …"
FETCH_RE = re.compile(
    r"Fetching\s+([A-Z0-9]+)\.([A-Z0-9]+)\.([0-9A-Z]*)\.([A-Z0-9]+)\s+"
    r"(\S+Z)\s+\S+\s+(\S+Z)"
)
BURST_RE = re.compile(r"!\s*BURST\s+rms=([\d.]+)\s+centroid=([\d.]+)Hz")
# Pi log emits one of these after every successful waveform load:
#   "  Got 144000 samples @ 40 Hz (60.0 min, peak abs=1.23e+04)"  -- live
#   "  Got 144000 cached samples @ 40 Hz (60.0 min, peak abs=1.23e+04)" -- cache
# We capture the duration (minutes) so we can compute the next-fetch ETA,
# and the optional "cached" word so we know if the lamp is on live data.
GOT_RE = re.compile(
    r"Got\s+\d+\s+(cached\s+)?samples\s+@\s*[\d.]+\s*Hz\s+\(([\d.]+)\s*min"
)
# Pi-side fetch-failure markers. Any of these means FDSN is down; the lamp
# will be running on cached data.
FAIL_RE = re.compile(
    r"!\s*(initial fetch failed|refetch failed|All\s+\d+\s+stations failed|"
    r"FDSN client init failed)"
)
# Explicit cache-mode log lines ("↻ Replaying cached ..." at startup when
# FDSN was unreachable, "↻ Falling back to cached ..." mid-run).
CACHE_MODE_RE = re.compile(r"↻\s*(Replaying|Falling back to)\s+cached")
# Mirrors REFETCH_MARGIN in pyTREMOR_lights01.py — the Pi re-downloads this
# many seconds before the cached window ends. Used to estimate next-fetch ETA.
REFETCH_MARGIN_SEC = 60


def parse_frame(line):
    """Find the LAST frame marker in `line` (Pi uses \\r overwrite so many
    frames may be concatenated in a single chunk). Return parsed dict or None."""
    matches = list(FRAME_RE.finditer(line))
    if not matches:
        return None
    m = matches[-1]
    bands = [int(m.group(i)) for i in range(5, 13)]
    return {
        "wall": m.group(1),
        "cur":  float(m.group(2)),
        "cen":  float(m.group(3)),
        "pwm":  int(m.group(4)),
        "bands": np.array(bands, dtype=np.float32),
    }


# ----------------------------------------------------------------------------
# FDSN overview fetch — pulls the same 1-hour window the Pi just fetched and
# computes an 8-band x N-bin RMS heatmap so the laptop can show the "whole
# picture" alongside the live 60 s view.
# ----------------------------------------------------------------------------
OVERVIEW_BIN_SEC = 2.0      # one column = 2 s of seismic data -> ~1800 cols / hour
FDSN_BASE        = "https://service.earthscope.org"


def compute_overview(net, sta, loc, ch, t0_iso, t1_iso, n_bands=8):
    """Fetch + bandpass + per-band STFT-RMS for one cache window. Returns
    (matrix[n_bands, n_cols], duration_seconds, label) or raises on failure."""
    from obspy.clients.fdsn import Client
    from obspy import UTCDateTime
    from scipy.signal import stft

    client = Client(FDSN_BASE, timeout=60)
    t0 = UTCDateTime(t0_iso)
    t1 = UTCDateTime(t1_iso)
    st = client.get_waveforms(net, sta, loc, ch, t0, t1)
    tr = st.merge(fill_value=0)[0]
    sr = float(tr.stats.sampling_rate)
    tr.detrend("demean")
    tr.filter("bandpass", freqmin=1.0, freqmax=18.0, corners=4, zerophase=True)
    data = np.asarray(tr.data, dtype=np.float32)

    from scipy.signal import stft as _stft
    nperseg = max(8, int(OVERVIEW_BIN_SEC * sr))
    f, _t, Z = _stft(data, fs=sr, nperseg=nperseg, noverlap=0,
                     padded=False, boundary=None)
    psd = (np.abs(Z) ** 2)
    edges = np.geomspace(1.0, 18.0, n_bands + 1)
    mat = np.zeros((n_bands, psd.shape[1]), dtype=np.float32)
    for i in range(n_bands):
        sel = (f >= edges[i]) & (f < edges[i + 1])
        if sel.any():
            mat[i] = np.sqrt(psd[sel].sum(axis=0))
    mat = np.log1p(mat)
    mx = float(mat.max()) + 1e-9
    mat = mat / mx
    duration = float(t1 - t0)
    label = (f"{net}.{sta}.{loc}.{ch}  \u00b7  "
             f"{t0.datetime:%Y-%m-%d %H:%M} \u2192 {t1.datetime:%H:%M} UTC")
    return mat, duration, label


def overview_thread(req_q, ui_q, stop_event):
    """Wait for fetch requests, run compute_overview, push result to ui_q."""
    while not stop_event.is_set():
        try:
            req = req_q.get(timeout=0.5)
        except queue.Empty:
            continue
        if req is None:
            return
        net, sta, loc, ch, t0, t1 = req
        ui_q.put(("overview_status", f"fetching overview {sta} \u2026"))
        try:
            mat, dur, label = compute_overview(net, sta, loc, ch, t0, t1)
            ui_q.put(("overview", (mat, dur, label)))
            # Persist for next launch so the overview panel is never blank
            # on startup. We save the latest successful fetch only —
            # smaller is fine; the operator can see "this is yesterday's
            # cache" the moment a fresh fetch lands.
            try:
                np.savez_compressed(
                    OVERVIEW_CACHE_FILE,
                    mat=mat, dur=np.float32(dur),
                    label=np.array(label), ts=np.float64(time.time()),
                )
            except Exception:
                pass
        except Exception as e:
            ui_q.put(("overview_err", f"{type(e).__name__}: {e}"))


def _load_cached_overview():
    """Return (mat, dur, label, age_seconds) or None if no cache on disk."""
    try:
        with np.load(OVERVIEW_CACHE_FILE, allow_pickle=False) as z:
            mat   = np.asarray(z["mat"], dtype=np.float32)
            dur   = float(z["dur"])
            label = str(z["label"])
            ts    = float(z["ts"])
        age = max(0.0, time.time() - ts)
        return mat, dur, label, age
    except (FileNotFoundError, OSError, KeyError, ValueError):
        return None


# ----------------------------------------------------------------------------
# SSH tail thread
# ----------------------------------------------------------------------------
def _pi_health_snapshot(ssh_dest):
    """Run one short remote command bundle returning a dict of telemetry.

    Polls cheap kernel files (no privileged calls), runs in ~1 s. Returns
    None on any ssh failure so the UI can show a friendly ‘offline’ hint.
    """
    cmd = [
        "ssh",
        "-i", SSH_KEY,
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=8",
        "-o", "BatchMode=yes",
    ]
    if ":" in ssh_dest.split("@", 1)[-1]:
        cmd.append("-6")
    # Single bundled shell command (one ssh round-trip). THR is the raw
    # vcgencmd get_throttled bitmask — lets us see both *current* under-volt
    # / freq-cap / thermal events and *latched* past events (bits 16-19).
    remote = (
        "echo TEMP=$(awk '{printf \"%.1f\",$1/1000}' "
        "/sys/class/thermal/thermal_zone0/temp 2>/dev/null); "
        "echo IP=$(hostname -I 2>/dev/null | awk '{print $1}'); "
        "echo SVC=$(systemctl is-active pytremor_lights 2>/dev/null); "
        "echo UP=$(uptime -p 2>/dev/null | sed 's/^up //'); "
        "echo LOAD=$(awk '{print $1}' /proc/loadavg); "
        "echo MEM=$(free -m | awk '/^Mem:/ {printf \"%d/%dM\",$3,$2}'); "
        "echo DISK=$(df -h / | awk 'NR==2 {print $5}'); "
        "echo ERR24=$(journalctl -u pytremor_lights --since '24h ago' "
        "-p err --no-pager 2>/dev/null | grep -v -- '-- ' | wc -l); "
        "echo THR=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)"
    )
    cmd += [ssh_dest, remote]
    creation = 0
    if os.name == "nt":
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    t0 = time.perf_counter()
    try:
        out = subprocess.check_output(
            cmd, timeout=15,
            stderr=subprocess.DEVNULL,
            creationflags=creation,
        ) if os.name == "nt" else subprocess.check_output(
            cmd, timeout=15, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
    rtt = time.perf_counter() - t0
    info = {"RTT": f"{rtt:.2f}"}
    for ln in out.decode("utf-8", "replace").splitlines():
        if "=" in ln:
            k, _, v = ln.partition("=")
            info[k.strip()] = v.strip()
    return info if len(info) > 1 else None


def _fmt_throttled(hex_str):
    """Translate vcgencmd get_throttled hex bitmask into a short label.

    Returns (label, severity) where severity is "ok"/"warn"/"crit". A
    *current* event (bits 0-3) is critical — it's happening now and the
    Pi is degraded right this second. A *latched* event (bits 16-19) is
    a warning — it happened at some point since boot but isn't active,
    typically a flaky PSU sag at startup or a hot day past.
    """
    try:
        v = int(hex_str, 16)
    except (ValueError, TypeError):
        return ("?", "ok")
    if v == 0:
        return ("ok", "ok")
    now_names  = {0: "under-volt", 1: "freq-cap", 2: "throttled", 3: "soft-temp"}
    past_names = {16: "under-volt", 17: "freq-cap", 18: "throttled", 19: "soft-temp"}
    now_flags  = [n for b, n in now_names.items()  if v & (1 << b)]
    past_flags = [n for b, n in past_names.items() if v & (1 << b)]
    if now_flags:
        return ("/".join(now_flags) + " NOW", "crit")
    return ("/".join(past_flags) + " (latched)", "warn")


def _fmt_eta(window_dur_s, cur_s):
    """Format the time until the Pi will re-download from FDSN."""
    if not window_dur_s or window_dur_s <= 0 or cur_s is None:
        return "?"
    remaining = window_dur_s - REFETCH_MARGIN_SEC - cur_s
    if remaining <= 0:
        return "due now"
    m, s = divmod(int(remaining), 60)
    if m > 0:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _fmt_age(secs):
    """Format ‘time since last log line’ in human units."""
    if secs is None:
        return "no data yet"
    if secs < 60:
        return f"{secs:.1f}s ago"
    m, s = divmod(int(secs), 60)
    if m < 60:
        return f"{m}m{s:02d}s ago"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m ago"


_ORDINAL_WORDS = (
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
    "eighth", "ninth", "tenth", "eleventh", "twelfth", "thirteenth",
    "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth",
    "nineteenth", "twentieth",
)


def _ordinal_word(n):
    """1 -> 'first', 2 -> 'second', … ; falls back to '21st', '42nd' etc."""
    if 1 <= n <= len(_ORDINAL_WORDS):
        return _ORDINAL_WORDS[n - 1]
    suffix = "th"
    if n % 100 not in (11, 12, 13):
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


_UPTIME_RE = re.compile(
    r"(?:(\d+)\s*(?:weeks?|w)\b)?[, ]*"
    r"(?:(\d+)\s*(?:days?|d)\b)?[, ]*"
    r"(?:(\d+)\s*(?:hours?|h)\b)?[, ]*"
    r"(?:(\d+)\s*(?:minutes?|min|m)\b)?",
    re.IGNORECASE,
)


def _fmt_uptime(s):
    """Compact `uptime -p` output -> e.g. '3h 12m' / '2d 4h' / '45m'."""
    if not s or s == "?":
        return "?"
    m = _UPTIME_RE.search(s)
    if not m or not any(m.groups()):
        return s
    w, d, h, mi = (int(x) if x else 0 for x in m.groups())
    parts = []
    if w: parts.append(f"{w}w")
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if mi or not parts: parts.append(f"{mi}m")
    return " ".join(parts[:2])


def pi_health_thread(ssh_dest, q, stop_event, period=30.0):
    """Poll Pi telemetry every `period` seconds and push to the UI.

    Slow cadence on purpose — spawning ssh more often than every ~10 s
    starts to be visible in the laptop's network/CPU and racing the tail
    thread's reconnects. 30 s is plenty for temperature and service
    status drift.
    """
    while not stop_event.is_set():
        info = _pi_health_snapshot(ssh_dest)
        if info is None:
            q.put(("pi_health", None))
        else:
            q.put(("pi_health", info))
        # Short-sleep loop so shutdown is responsive.
        for _ in range(int(period * 10)):
            if stop_event.is_set():
                return
            time.sleep(0.1)


def _spawn_tail(ssh_dest):
    """Build + launch the ssh tail subprocess. Returns Popen or raises."""
    cmd = [
        "ssh",
        "-i", SSH_KEY,
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=15",
        "-o", "ServerAliveCountMax=3",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
    ]
    if ":" in ssh_dest.split("@", 1)[-1]:
        cmd.append("-6")
    remote = (
        f"grep -a Fetching {LOG_PATH} 2>/dev/null | tail -3 ; "
        f"stdbuf -oL -eL tail -F -n 200 {LOG_PATH}"
    )
    cmd += [ssh_dest, remote]
    # CREATE_NO_WINDOW (0x08000000) keeps Windows from popping a black
    # console window every time we (re)spawn ssh. Matters specifically when
    # the monitor is launched via pythonw.exe (no parent console) — each
    # Popen child would otherwise allocate its own console window every
    # reconnect, then close it again, producing a flicker that's both
    # distracting and triggers the 'ssh.exe.txt' shortcut file Windows
    # sometimes leaves behind. Has no effect when there's a parent
    # console (regular python.exe launch).
    popen_kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "bufsize": 0,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        )
    return subprocess.Popen(cmd, **popen_kwargs), cmd


def tail_thread(ssh_dest, q, stop_event):
    """Tail the Pi log over SSH with automatic reconnect.

    Each connection attempt runs `ssh ... tail -F /var/log/pytremor_lights.log`.
    If ssh exits (Pi rebooted, WiFi dropped, key auth glitch, logrotate
    misbehaved, sleep/resume on the laptop, …) we wait `backoff` seconds
    and try again. Backoff grows 5 → 10 → 20 → 40 → 60 s capped, so brief
    outages reconnect fast and long outages don't hammer ssh in a loop.
    The UI sees `("status", "...")` messages describing each retry so the
    operator always knows whether the wire is silent because of an outage
    or because the Pi script is genuinely paused.
    """
    backoff = 5
    BACKOFF_MAX = 60
    first_attempt = True
    reconnects = 0
    while not stop_event.is_set():
        try:
            proc, cmd = _spawn_tail(ssh_dest)
        except FileNotFoundError:
            q.put(("error", "ssh not found in PATH — add C:\\Windows\\System32\\OpenSSH"))
            return
        except Exception as e:
            q.put(("status", f"ssh spawn failed: {e}; retrying in {backoff}s"))
            if stop_event.wait(backoff):
                return
            backoff = min(BACKOFF_MAX, backoff * 2)
            continue

        if first_attempt:
            print("Connecting:", " ".join(cmd))
            first_attempt = False
        else:
            reconnects += 1
            q.put(("reconnect", reconnects))
            q.put(("status", "ssh reconnected — resuming tail"))

        try:
            buf = bytearray()
            got_any = False
            while not stop_event.is_set():
                chunk = proc.stdout.read(256)
                if not chunk:
                    break
                got_any = True
                buf += chunk
                i = 0
                for j, b in enumerate(buf):
                    if b == 0x0a or b == 0x0d:  # \n or \r
                        piece = bytes(buf[i:j]).decode("utf-8", errors="replace").strip()
                        if piece:
                            q.put(("line", piece))
                        i = j + 1
                del buf[:i]
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        if stop_event.is_set():
            return

        # If we actually got data this connection, reset backoff so a
        # transient drop reconnects fast. If we got nothing, the previous
        # error was probably persistent (wrong key, host unreachable) and
        # we should back off harder.
        if got_any:
            backoff = 5
        q.put(("status",
               f"ssh tail dropped — reconnecting in {backoff}s "
               f"(tail will resume from latest log frame)"))
        if stop_event.wait(backoff):
            return
        backoff = min(BACKOFF_MAX, backoff * 2)


# ----------------------------------------------------------------------------
# Live UI — red-shifted volcanic palette (lava / forge / candle)
# ----------------------------------------------------------------------------
BG_COLOR     = "#170807"   # deep blood-brown, near black
PANEL_COLOR  = "#21100c"   # warm panel fill
GRID_COLOR   = "#3d1c14"   # crimson cocoa grid
TEXT_PRIMARY = "#ffd9b0"   # warm cream
TEXT_MUTED   = "#c08a6a"   # toasted clay
ACCENT       = "#ff7c3a"   # vivid coral-orange centroid line
GLOW_COLOR   = "#ffb060"   # LED-strip glow tint

# Warm 8-step palette for the LED bars — crimson → gold (no cool tones)
BAR_PALETTE = [
    "#8a2820",  # deep crimson
    "#b53826",  # blood orange
    "#d44e2a",  # rust
    "#e96a32",  # ember
    "#f3873f",  # paprika
    "#f6a256",  # warm amber
    "#f5bf78",  # honey
    "#f1d8a0",  # candle gold
]

# Custom red-shifted waterfall colormap (black → lava → gold → cream)
SOFT_CMAP = matplotlib.colors.LinearSegmentedColormap.from_list(
    "soft_tremor",
    [
        (0.00, "#150605"),
        (0.14, "#3a0e0a"),
        (0.32, "#74170d"),
        (0.50, "#b03318"),
        (0.68, "#e76a2a"),
        (0.84, "#f4a85a"),
        (1.00, "#fde6c4"),
    ],
)


def _mac_from_link_local(host):
    """If `host` is an EUI-64 fe80:: address, return the embedded MAC.

    fe80::8aa2:9eff:fed7:9f99 -> '88:a2:9e:d7:9f:99'
    Returns None for anything that isn't a recognisable link-local EUI-64.
    """
    h = host.split("%", 1)[0].lower()
    if not h.startswith("fe80:"):
        return None
    # Last four hextets carry the MAC with ff:fe inserted + U/L bit flipped
    parts = h.split(":")
    parts = [p for p in parts if p != ""]
    if len(parts) < 4:
        return None
    try:
        a, b, c, d = (int(p, 16) for p in parts[-4:])
    except ValueError:
        return None
    if (b & 0xFF) != 0xFF or (c >> 8) != 0xFE:
        return None  # no ff:fe marker -> not EUI-64
    b0 = (a >> 8) ^ 0x02   # flip universal/local bit
    b1 = a & 0xFF
    b2 = b >> 8
    b3 = c & 0xFF
    b4 = d >> 8
    b5 = d & 0xFF
    return "{:02x}:{:02x}:{:02x}:{:02x}:{:02x}:{:02x}".format(
        b0, b1, b2, b3, b4, b5
    )


def _pi_label(ssh_dest):
    """Return (short, long) labels for the Pi being monitored.

    Input examples:
        'sjc1@fe80::8aa2:9eff:fed7:9f99%19'  -> ('sjc1', 'sjc1 · fe80::…9f99%19')
        'sjc2@10.22.171.3'                   -> ('sjc2', 'sjc2 · 10.22.171.3')
        'pi@raspberrypi.local'               -> ('pi',   'pi · raspberrypi.local')
    The short form is meant for the window title; the long form is the
    on-figure badge so you can tell 5 simultaneous monitors apart at a
    glance without reading the title bar.
    """
    user, _, host = ssh_dest.partition("@")
    if not host:
        host, user = user, ""
    short = user or host.split(".")[0]
    # Truncate long IPv6 link-local for readability
    if ":" in host and len(host) > 28:
        head, _, tail = host.rpartition(":")
        host_disp = f"{head.split(':',2)[0]}::\u2026{tail}"
    else:
        host_disp = host
    mac = _mac_from_link_local(host)
    # Two-line badge: host on row 1, MAC on row 2 (if derivable)
    long = f"{host_disp}\nmac {mac}" if mac else host_disp
    return short, long


def _style_axes(ax):
    ax.set_facecolor(PANEL_COLOR)
    for spine in ax.spines.values():
        spine.set_color(GRID_COLOR)
        spine.set_linewidth(0.8)
    ax.tick_params(colors=TEXT_MUTED, labelsize=8, length=3)
    ax.title.set_color(TEXT_PRIMARY)
    ax.xaxis.label.set_color(TEXT_MUTED)
    ax.yaxis.label.set_color(TEXT_MUTED)


def run_ui(q, req_q=None, ssh_dest=SSH_DEST_DEFAULT):
    pi_short, pi_long = _pi_label(ssh_dest)
    fig = plt.figure(figsize=(13, 8.5), facecolor=BG_COLOR, dpi=100)
    fig.canvas.manager.set_window_title(
        f"pyTREMOR · live monitor · {pi_short}"
    )
    gs = fig.add_gridspec(
        4, 2,
        width_ratios=[1.0, 1.6],
        # row 0 = status strip (clock + replay/centroid/PWM/frames/bursts)
        # rows 1–2 = bars / waterfall / centroid
        # row 3 = overview spectrogram (full width)
        height_ratios=[0.55, 1.0, 1.0, 0.85],
        hspace=1.15, wspace=0.22,
        left=0.06, right=0.97, top=0.86, bottom=0.07,
    )
    # ------ Centered header block ----------------------------------------
    # Row 1: big bold title combining product + Pi id so 5 windows side
    # by side are unambiguous at a glance.
    title_txt = fig.text(
        0.5, 0.965, f"PYTREMOR  \u00b7  {pi_short.upper()}",
        ha="center", va="center",
        fontsize=22, color=TEXT_PRIMARY, fontweight="bold",
        family="monospace",
    )
    # Row 2: station code + place + lat/lon
    station_line1 = fig.text(
        0.5, 0.925, "station: —  waiting for first fetch …",
        ha="center", va="center",
        fontsize=11, color=ACCENT, family="monospace", fontweight="bold",
    )
    # Row 3: region + volcano
    station_line2 = fig.text(
        0.5, 0.900, "",
        ha="center", va="center",
        fontsize=10, color=TEXT_MUTED, family="monospace",
    )
    # Small SSH destination in top-right for debugging
    fig.text(
        0.985, 0.985, pi_long,
        ha="right", va="top",
        fontsize=8.5, color=TEXT_MUTED, family="monospace", alpha=0.7,
    )
    # Pi telemetry block: hardware/system snapshot (left column) and
    # operational state of the lamp itself (right column), refreshed by
    # pi_health_thread every 30 s plus a tick-based redraw for the live
    # "last log" age and next-fetch ETA. Two text artists side by side so
    # we can colour them independently (hardware vs operational state).
    pi_health_txt = fig.text(
        0.985, 0.965,
        "… querying pi telemetry …",
        ha="right", va="top",
        fontsize=7.5, color=TEXT_MUTED, family="monospace", alpha=0.85,
        linespacing=1.5,
    )
    pi_op_txt = fig.text(
        0.015, 0.965,
        "… waiting for first log line …",
        ha="left", va="top",
        fontsize=7.5, color=TEXT_MUTED, family="monospace", alpha=0.85,
        linespacing=1.5,
    )
    ax_bars     = fig.add_subplot(gs[1:3, 0])
    ax_water    = fig.add_subplot(gs[1,   1])
    ax_cen      = fig.add_subplot(gs[2,   1])
    ax_overview = fig.add_subplot(gs[3, :])
    ax_status   = fig.add_subplot(gs[0, :])
    ax_status.set_facecolor(BG_COLOR)
    ax_status.axis("off")
    for ax in (ax_bars, ax_water, ax_cen, ax_overview):
        _style_axes(ax)

    # --- bars panel --------------------------------------------------------
    band_labels = [f"{BAND_EDGES[i]:.1f}–{BAND_EDGES[i+1]:.1f} Hz" for i in range(8)]
    # Compact 2-line tick labels. Uses 1-decimal so neighbouring bands stay
    # visibly distinct (geomspace 1–18 Hz rounds to 1–1, 1–2, 2–3, … with
    # :.0f, which is unreadable). Detailed Hz info also lives in the LED
    # value labels above the bars + the L1–L8 ids in the waterfall.
    short_band_labels = [
        f"L{i+1}\n{BAND_EDGES[i]:.1f}–{BAND_EDGES[i+1]:.1f} Hz"
        for i in range(8)
    ]
    bars = ax_bars.bar(
        range(8), [0]*8,
        color=BAR_PALETTE,
        edgecolor=BG_COLOR, linewidth=1.2,
        width=0.72,
    )
    # Per-bar live % value labels (legible movement at a glance)
    bar_value_txt = [
        ax_bars.text(i, 2, "", ha="center", va="bottom",
                     fontsize=8, color=TEXT_PRIMARY, alpha=0.9,
                     fontweight="bold")
        for i in range(8)
    ]
    # Peak-hold ticks: a SINGLE Line2D with NaN gaps draws 8 horizontal
    # markers in one blit operation (vs. 8 individual artists previously).
    _peak_xs = np.empty(8 * 3, dtype=np.float32)
    for i in range(8):
        _peak_xs[3 * i + 0] = i - 0.36
        _peak_xs[3 * i + 1] = i + 0.36
        _peak_xs[3 * i + 2] = np.nan
    _peak_ys = np.zeros(8 * 3, dtype=np.float32)
    _peak_ys[2::3] = np.nan
    peak_line, = ax_bars.plot(
        _peak_xs, _peak_ys,
        color=TEXT_PRIMARY, lw=1.6, alpha=0.85, solid_capstyle="round",
    )
    peaks = np.zeros(8, dtype=np.float32)
    PEAK_DECAY = 1.5   # %/frame (~15 %/s at 100 ms UI)

    # LED strip preview: 8 glowing circles below the bars that mirror the
    # physical lamp — alpha + colour both scale with brightness, plus a
    # softer halo behind for the glow. Sized to roughly match bar width.
    ax_bars.set_ylim(-22, 105)
    led_halo = ax_bars.scatter(
        range(8), [-11]*8, s=[260]*8,
        c=[GLOW_COLOR]*8, alpha=0.0, edgecolors="none", zorder=3,
    )
    led_core = ax_bars.scatter(
        range(8), [-11]*8, s=[120]*8,
        c=[GLOW_COLOR]*8, alpha=0.1, edgecolors=GRID_COLOR, linewidths=0.6, zorder=4,
    )
    # baseline line dividing bars from LED preview
    ax_bars.axhline(0, color=GRID_COLOR, lw=0.8, alpha=0.7)
    ax_bars.set_xticks(range(8))
    # Custom 2-row tick labels: bold L# on top, smaller Hz range below.
    # Done as ax.text (not set_xticklabels) because matplotlib tick labels
    # can't mix font sizes/weights between lines within a single label.
    ax_bars.set_xticklabels([""] * 8)
    from matplotlib.transforms import blended_transform_factory
    _bar_tick_trans = blended_transform_factory(ax_bars.transData, ax_bars.transAxes)
    for i in range(8):
        ax_bars.text(
            i, -0.05, f"L{i+1}",
            transform=_bar_tick_trans,
            ha="center", va="top",
            fontsize=9, fontweight="bold", color=TEXT_PRIMARY,
            clip_on=False,
        )
        ax_bars.text(
            i, -0.105, f"{BAND_EDGES[i]:.1f}–{BAND_EDGES[i+1]:.1f}Hz",
            transform=_bar_tick_trans,
            ha="center", va="top",
            fontsize=6, color=TEXT_MUTED,
            clip_on=False,
        )
    ax_bars.tick_params(axis="x", pad=18, length=0)   # leave room for LED circles
    ax_bars.set_yticks([0, 25, 50, 75, 100])
    ax_bars.set_ylabel("LED brightness  (%)", color=TEXT_MUTED, fontsize=9)
    ax_bars.set_title(
        "LED OUTPUT  ·  L1–L8",
        color=TEXT_PRIMARY, fontsize=11, pad=22,
        fontweight="bold", loc="center",
    )
    # NOTE: ax_bars spans TWO gridspec rows while ax_water spans one, so
    # the same axes-fraction y maps to a much larger absolute pixel offset
    # on bars. Subtitle y=1.02 here ≈ same absolute height above the graph
    # as water/cen subtitles at y=1.06 (their axes are ~3× shorter).
    ax_bars.text(
        0.5, 1.02,
        "one bar = brightness of one seismic frequency band\n"
        "low (L1–L3 ≈ 1–3 Hz) = deep tremor  ·  high (L6–L8 ≈ 6–18 Hz) = sharp bursts",
        transform=ax_bars.transAxes,
        ha="center", va="bottom",
        fontsize=7.5, color=TEXT_MUTED, style="italic",
    )
    ax_bars.grid(axis="y", color=GRID_COLOR, alpha=0.5, linewidth=0.6)
    ax_bars.set_axisbelow(True)

    # --- waterfall panel (live 60 s) --------------------------------------
    waterfall = np.full((8, HISTORY_FRAMES), np.nan, dtype=np.float32)
    im = ax_water.imshow(
        waterfall, aspect="auto", origin="lower",
        cmap=SOFT_CMAP, vmin=0, vmax=100,
        extent=[-HISTORY_SEC, 0, 0.5, 8.5],
        interpolation="nearest",
    )
    ax_water.set_yticks(range(1, 9))
    ax_water.set_yticklabels([f"L{i+1}" for i in range(8)],
                             color=TEXT_MUTED, fontsize=8)
    ax_water.set_xlabel("time  (seconds ago  →  now)", color=TEXT_MUTED, fontsize=9)
    ax_water.set_title(
        "PER-BAND HISTORY  ·  60 s WATERFALL",
        color=TEXT_PRIMARY, fontsize=11, pad=22,
        fontweight="bold", loc="center",
    )
    ax_water.text(
        0.5, 1.06,
        "each row = one LED (L1 low → L8 high)\n"
        "colour = brightness %",
        transform=ax_water.transAxes,
        ha="center", va="bottom",
        fontsize=7.5, color=TEXT_MUTED, style="italic",
    )
    cbar = fig.colorbar(im, ax=ax_water, fraction=0.035, pad=0.015)
    cbar.outline.set_edgecolor(GRID_COLOR)
    cbar.ax.tick_params(colors=TEXT_MUTED, labelsize=8, length=2)
    cbar.set_label("%", color=TEXT_MUTED, fontsize=8)

    # --- centroid panel ---------------------------------------------------
    cen_hist = deque([np.nan] * HISTORY_FRAMES, maxlen=HISTORY_FRAMES)
    t_axis = np.linspace(-HISTORY_SEC, 0, HISTORY_FRAMES)
    (cen_line,) = ax_cen.plot(t_axis, list(cen_hist), color=ACCENT, lw=1.6, alpha=0.95)
    for edge in BAND_EDGES[1:-1]:
        ax_cen.axhline(edge, color=GRID_COLOR, lw=0.5, alpha=0.6)
    ax_cen.set_ylim(BAND_EDGES[0], BAND_EDGES[-1])
    ax_cen.set_xlim(-HISTORY_SEC, 0)
    ax_cen.set_xlabel("time  (seconds ago  →  now)", color=TEXT_MUTED, fontsize=9)
    ax_cen.set_ylabel("spectral centroid  (Hz)", color=TEXT_MUTED, fontsize=9)
    ax_cen.set_title(
        "SPECTRAL CENTROID  →  PWM FLICKER FREQUENCY",
        color=TEXT_PRIMARY, fontsize=11, pad=22,
        fontweight="bold", loc="center",
    )
    ax_cen.text(
        0.5, 1.06,
        "centroid = ‘centre of mass’ of the seismic spectrum\n"
        "drives the LED PWM flicker rate",
        transform=ax_cen.transAxes,
        ha="center", va="bottom",
        fontsize=7.5, color=TEXT_MUTED, style="italic",
    )
    ax_cen.grid(color=GRID_COLOR, alpha=0.4, linewidth=0.6)
    ax_cen.set_axisbelow(True)

    # --- overview panel (whole 1-hour seismic cache) ----------------------
    ov_empty = np.full((8, 600), np.nan, dtype=np.float32)
    ov_im = ax_overview.imshow(
        ov_empty, aspect="auto", origin="lower",
        cmap=SOFT_CMAP, vmin=0, vmax=1,
        extent=[0, 3600, 0.5, 8.5],
        interpolation="nearest",
    )
    # Light-blue vertical line marking where the lamp currently is inside
    # the overview window. Chosen to contrast with the warm/red palette so
    # it stays visible against any spectrogram colour.
    ov_cursor = ax_overview.axvline(0, color="#7ad7ff", lw=2.0, alpha=0.95)
    # Translucent overlay covering [0 … cursor]: shows what has already been
    # sonified in the current pass. Resets to zero width on each new loop.
    ov_played = ax_overview.axvspan(0, 0, ymin=0, ymax=1,
                                    color="#7ad7ff", alpha=0.12, lw=0)
    ax_overview.set_yticks(range(1, 9))
    ax_overview.set_yticklabels([f"L{i+1}" for i in range(8)],
                                color=TEXT_MUTED, fontsize=8)
    ax_overview.set_xlabel(
        "time inside the 1-hour seismic cache  (seconds)  ·  blue line = current replay position",
        color=TEXT_MUTED, fontsize=9,
    )
    ax_overview.set_title(
        "FULL SONIFICATION PREVIEW  ·  WHOLE SEISMIC CACHE",
        color=TEXT_PRIMARY, fontsize=11, pad=18,
        fontweight="bold", loc="center",
    )
    ax_overview.text(
        0.5, 1.04,
        "what the lamp will play across the whole hour   ·   "
        "rows = LEDs L1–L8   ·   colour = upcoming brightness per band",
        transform=ax_overview.transAxes,
        ha="center", va="bottom",
        fontsize=7.5, color=TEXT_MUTED, style="italic",
    )

    # --- status strip (sits between the header texts and the plots) ------
    # Three centered rows on ax_status (gs[0, :]) so the operator scans
    # the page top-to-bottom and reaches plots only after they have read
    # what station / window / replay state is being shown.
    status_txt = ax_status.text(
        0.5, 0.85, "waiting for first frame…",
        transform=ax_status.transAxes,
        ha="center", va="center",
        fontsize=10.5, color=TEXT_PRIMARY, family="monospace", alpha=0.9,
    )
    time_txt = ax_status.text(
        0.5, 0.50, "",
        transform=ax_status.transAxes,
        ha="center", va="center",
        fontsize=9.5, color=TEXT_MUTED, family="monospace", alpha=0.95,
    )
    overview_status_txt = ax_status.text(
        0.5, 0.15, "",
        transform=ax_status.transAxes,
        ha="center", va="center",
        fontsize=9, color=TEXT_MUTED, family="monospace", alpha=0.8,
    )

    # --- shared state -----------------------------------------------------
    state = {
        "station":        "?",
        "n_frames":       0,
        "n_bursts":       0,
        "last_cur":       0.0,
        "last_cen":       0.0,
        "last_pwm":       0,
        "last_bands":     np.zeros(8, dtype=np.float32),
        "target_bands":   np.zeros(8, dtype=np.float32),  # most recent frame
        "display_bands":  np.zeros(8, dtype=np.float32),  # interpolated, what we draw
        "connected":      False,
        "ov_duration":    3600.0,
        "ov_label":       "",
        "ov_pass":        1,
        "active_fetch":   None,   # tuple key for dedup
        # Pre-allocated RGBA buffers for the LED scatter colours.
        # RGB columns filled once with GLOW_COLOR; only alpha changes each frame.
        "_led_rgba_core": np.zeros((8, 4), dtype=np.float32),
        "_led_rgba_halo": np.zeros((8, 4), dtype=np.float32),
        # Operational telemetry derived from the log stream + ssh snapshots
        "pi_health":         None,    # last snapshot dict (svc, ip, temp, …)
        "last_line_t":       None,    # wall-clock of last log line consumed
        "fdsn_source":       "—",     # "live" / "cache" / "failed" / …
        "window_dur_s":      None,    # length of current cached seismic window
        "reconnect_count":   0,       # ssh tail re-spawn count
        "_health_prev_text": "",      # text-change diff for draw_idle throttling
        "_op_prev_text":     "",
    }
    _glow_rgb = np.array(matplotlib.colors.to_rgb(GLOW_COLOR), dtype=np.float32)
    state["_led_rgba_core"][:, :3] = _glow_rgb
    state["_led_rgba_halo"][:, :3] = _glow_rgb

    def _refresh_telemetry(force=False):
        """Rebuild both telemetry text blocks. Only triggers a canvas
        redraw if the rendered text actually changed — avoids hammering
        draw_idle() on every UI tick.
        """
        info = state["pi_health"] or {}
        if info:
            thr_label, thr_sev = _fmt_throttled(info.get("THR", ""))
            svc  = info.get("SVC", "?")
            errs = info.get("ERR24", "0")
            temp = info.get("TEMP", "?")
            try:
                temp_hot = float(temp) >= 70.0
            except (TypeError, ValueError):
                temp_hot = False
            # Severity tiers — worst wins
            if svc != "active":
                health_col = "#ff5050"
            elif thr_sev == "crit":
                health_col = "#ff5050"
            elif errs not in ("0", "") or thr_sev == "warn" or temp_hot:
                health_col = "#ff7c3a"
            else:
                health_col = TEXT_MUTED
            health_lines = [
                f"svc: {svc}",
                f"ip: {info.get('IP', '?')}",
                f"rtt: {info.get('RTT', '?')}s",
                f"temp: {temp}\u00b0C",
                f"load: {info.get('LOAD', '?')}",
                f"mem: {info.get('MEM', '?')}",
                f"disk: {info.get('DISK', '?')}",
                f"up: {_fmt_uptime(info.get('UP', '?'))}",
                f"thr: {thr_label}",
                f"err24h: {errs}",
            ]
            health_text = "\n".join(health_lines)
        elif state["pi_health"] is None:
            health_text = "… querying pi telemetry …"
            health_col  = TEXT_MUTED
        else:
            health_text = "pi offline (ssh failed)"
            health_col  = "#ff7c3a"
        # — Operational block (left side) —
        now = time.time()
        last_t = state["last_line_t"]
        age = (now - last_t) if last_t else None
        # Staleness severity: > 5 min = crit, > 2 min = warn
        if age is None:
            op_col = TEXT_MUTED
        elif age > 300:
            op_col = "#ff5050"
        elif age > 120:
            op_col = "#ff7c3a"
        else:
            op_col = TEXT_MUTED
        src = state["fdsn_source"]
        # Source colour overrides staleness if worse
        if src.startswith("failed"):
            op_col = "#ff5050"
        elif src.startswith("cache (fallback)"):
            op_col = "#ff7c3a"
        eta = _fmt_eta(state["window_dur_s"], state["last_cur"])
        pass_n = int(state.get("ov_pass", 1) or 1)
        pass_str = _ordinal_word(pass_n)
        op_lines = [
            f"last log:   {_fmt_age(age)}",
            f"fdsn:       {src}",
            f"pass:       {pass_str}",
            f"next fetch: {eta}",
            f"reconnects: {state['reconnect_count']}",
        ]
        op_text = "\n".join(op_lines)
        # Only update + redraw when content changes (or forced)
        changed = False
        if force or health_text != state["_health_prev_text"]:
            pi_health_txt.set_text(health_text)
            pi_health_txt.set_color(health_col)
            state["_health_prev_text"] = health_text
            changed = True
        if force or op_text != state["_op_prev_text"]:
            pi_op_txt.set_text(op_text)
            pi_op_txt.set_color(op_col)
            state["_op_prev_text"] = op_text
            changed = True
        if changed:
            fig.canvas.draw_idle()

    def update(_frame):
        while True:
            try:
                kind, payload = q.get_nowait()
            except queue.Empty:
                break

            if kind == "error":
                status_txt.set_text(f"!! {payload}")
                continue

            if kind == "status":
                # Non-fatal connection messages (reconnecting, dropped, …)
                # — surface them in the small overview status line so the
                # main status text keeps the most recent frame readout.
                overview_status_txt.set_text(payload)
                state["connected"] = False
                continue

            if kind == "overview":
                mat, dur, label = payload
                ov_im.set_data(mat)
                ov_im.set_extent([0, dur, 0.5, 8.5])
                ax_overview.set_xlim(0, dur)
                state["ov_duration"] = dur
                state["ov_label"]    = label
                overview_status_txt.set_text(f"overview: {label}")
                # Force one full redraw so the new x-axis range is captured
                # in the blit background.
                fig.canvas.draw_idle()
                continue
            if kind == "overview_err":
                overview_status_txt.set_text(f"overview fetch failed: {payload}")
                continue
            if kind == "overview_status":
                overview_status_txt.set_text(payload)
                continue
            if kind == "pi_health":
                state["pi_health"] = payload
                _refresh_telemetry()
                continue
            if kind == "reconnect":
                state["reconnect_count"] = payload
                _refresh_telemetry()
                continue

            line = payload
            state["connected"] = True
            state["last_line_t"] = time.time()
            # FDSN-source detection: scan every log line for outcome markers
            mg = GOT_RE.search(line)
            if mg:
                state["window_dur_s"] = float(mg.group(2)) * 60.0
                state["fdsn_source"] = "cache" if mg.group(1) else "live"
            elif CACHE_MODE_RE.search(line):
                state["fdsn_source"] = "cache (fallback)"
            elif FAIL_RE.search(line):
                state["fdsn_source"] = "failed (using cache)"
            mf = FETCH_RE.search(line)
            if mf:
                state["fdsn_source"] = "fetching"
                net, sta, loc, ch, t0, t1 = mf.groups()
                state["station"] = f"{net}.{sta}.{loc}.{ch}"
                state["fetch_t0"] = t0
                state["fetch_t1"] = t1
                # Refresh the centered station-info header rows. These are
                # figure-level texts (not on a blitted axis) so we force a
                # one-shot full redraw to make them appear/update.
                l1, l2 = _station_descriptor(state["station"])
                station_line1.set_text(l1)
                station_line2.set_text(l2)
                fig.canvas.draw_idle()
                key = (net, sta, loc, ch, t0, t1)
                if req_q is not None and key != state["active_fetch"]:
                    state["active_fetch"] = key
                    try:
                        req_q.put_nowait(key)
                    except queue.Full:
                        pass
                continue
            if BURST_RE.search(line):
                state["n_bursts"] += 1
            fr = parse_frame(line)
            if fr is None:
                continue
            state["n_frames"] += 1
            state["last_cur"] = fr["cur"]
            state["last_cen"] = fr["cen"]
            state["last_pwm"] = fr["pwm"]
            waterfall[:, :-1] = waterfall[:, 1:]
            waterfall[:, -1]  = fr["bands"]
            # Peak hold: jump up immediately on new max
            np.maximum(peaks, fr["bands"], out=peaks)
            # Target value for the visual lerp (bars + LED preview glide
            # toward this between frames so the lamp doesn't look stepped)
            state["target_bands"] = fr["bands"]
            state["last_bands"]   = fr["bands"]
            cen_hist.append(fr["cen"])

        # ---- per-UI-frame refresh -----------------------------------------
        # Snappy lerp (0.55) so the lamp preview tracks the physical strip
        # almost 1-to-1 while still smoothing the burst-style SSH delivery.
        target  = state["target_bands"]
        display = state["display_bands"]
        display += (target - display) * 0.55

        for b, h in zip(bars, display):
            b.set_height(h)

        # Decay peak-hold ticks; write the 8 new y-values into the shared
        # NaN-separated y array (positions 0,1 in each triplet).
        peaks[:] = np.maximum(peaks - PEAK_DECAY, 0.0)
        _peak_ys[0::3] = peaks
        _peak_ys[1::3] = peaks
        peak_line.set_ydata(_peak_ys)

        # LED strip preview — alpha + size scale with current display value
        # so bursts visibly "flash" like the physical lamp.
        norm = np.clip(display / 100.0, 0.0, 1.0)
        led_rgba_core = state["_led_rgba_core"]
        led_rgba_halo = state["_led_rgba_halo"]
        # RGB columns are filled once at init; only alpha changes per frame.
        led_rgba_core[:, 3] = 0.18 + 0.82 * norm
        led_rgba_halo[:, 3] = 0.55 * (norm ** 1.4)
        led_core.set_alpha(None); led_halo.set_alpha(None)
        led_core.set_facecolors(led_rgba_core)
        led_halo.set_facecolors(led_rgba_halo)
        led_core.set_sizes(110 + 160 * norm)
        led_halo.set_sizes(220 + 360 * norm)

        # Live %-value labels on top of each bar
        for i, t in enumerate(bar_value_txt):
            v = float(display[i])
            t.set_text(f"{v:3.0f}")
            t.set_position((i, max(v + 2.0, 3.0)))

        # --- throttled updates ------------------------------------------
        # The lamp preview above runs at full 30 fps. Heavy artists below
        # only have their data refreshed every N frames, but they are
        # always included in the returned list — otherwise blit would
        # restore the cached background (without them) on the off-frames,
        # causing flicker. Throttling the *data update* is what saves CPU;
        # throttling the *artist membership* would just blink them on/off.
        state["tick"] = state.get("tick", 0) + 1
        tick = state["tick"]

        # Operational telemetry (staleness + ETA) ticks even when nothing
        # arrives on the queue — so a frozen Pi visibly ages on screen.
        # Every 5 s (300 ticks @ 60 fps): figure-level fig.text is NOT
        # blit-safe, so each draw_idle() repaints the whole canvas — at
        # 1 Hz that's a visible flicker. 5 s is granular enough for ages.
        if tick % 300 == 0:
            _refresh_telemetry()

        if tick % 4 == 0:   # ~15 fps data refresh for waterfall + centroid
            im.set_data(waterfall)
            cen_line.set_ydata(list(cen_hist))

        if tick % 3 == 0:   # ~20 fps data refresh for cursor + played overlay
            ov_dur = max(1.0, state["ov_duration"])
            cur_pos = state["last_cur"] % ov_dur
            pass_n  = int(state["last_cur"] // ov_dur) + 1
            state["ov_pass"] = pass_n
            ov_cursor.set_xdata([cur_pos, cur_pos])
            # Resize the translucent rectangle covering [0 .. cur_pos] —
            # axvspan returns a Rectangle whose width we just stretch.
            ov_played.set_x(0.0)
            ov_played.set_width(cur_pos)

        if tick % 6 == 0:   # ~10 fps data refresh for status text
            status_txt.set_text(
                f"replay t = {state['last_cur']:7.1f} s   \u00b7   "
                f"centroid = {state['last_cen']:5.2f} Hz   \u00b7   "
                f"PWM = {state['last_pwm']:4d} Hz   \u00b7   "
                f"frames = {state['n_frames']}   \u00b7   "
                f"bursts = {state['n_bursts']}"
                + ("" if state["connected"] else "   [waiting for ssh…]")
            )
            # Live UTC clock + (if known) the FDSN window being replayed,
            # so a glance at the header tells you what calendar moment of
            # seismicity the lamp is currently tremoring on.
            now_utc = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
            window = ""
            t0 = state.get("fetch_t0")
            t1 = state.get("fetch_t1")
            if t0 and t1:
                # 2026-05-25T14:00:45.626Z  ->  2026-05-25 14:00
                def _fmt(ts):
                    return ts.replace("T", " ")[:16]
                window = f"   \u00b7   window: {_fmt(t0)} \u2192 {_fmt(t1)} UTC"
            time_txt.set_text(f"now: {now_utc}{window}")

        # Every animated artist must be returned every frame so blit
        # paints it on top of the restored background. Skipping any of
        # them on off-frames causes flicker (vanish/reappear cycle).
        return [led_core, led_halo, *bars, peak_line, *bar_value_txt,
                ov_im, ov_cursor, ov_played, im, cen_line,
                status_txt, overview_status_txt, time_txt]

    # ---- restore + persist window geometry -------------------------------
    # Qt backend: read/write x, y, width, height. Saved on window close.
    def _restore_geometry():
        try:
            with open(GEOM_FILE, "r", encoding="utf-8") as f:
                g = json.load(f)
            win = fig.canvas.manager.window
            # Qt path
            if hasattr(win, "setGeometry"):
                win.setGeometry(int(g["x"]), int(g["y"]),
                                int(g["w"]), int(g["h"]))
            elif hasattr(win, "wm_geometry"):  # Tk fallback
                win.wm_geometry(f"{int(g['w'])}x{int(g['h'])}+{int(g['x'])}+{int(g['y'])}")
        except (FileNotFoundError, ValueError, KeyError, OSError, AttributeError):
            pass

    def _save_geometry(_evt=None):
        try:
            win = fig.canvas.manager.window
            if hasattr(win, "geometry") and hasattr(win, "x"):  # Qt
                geom = win.geometry()
                payload = {"x": int(win.x()), "y": int(win.y()),
                           "w": int(geom.width()), "h": int(geom.height())}
            elif hasattr(win, "wm_geometry"):  # Tk
                # "WxH+X+Y"
                s = win.wm_geometry()
                wh, _, xy = s.partition("+")
                w, _, h = wh.partition("x")
                x, _, y = xy.partition("+")
                payload = {"x": int(x), "y": int(y), "w": int(w), "h": int(h)}
            else:
                return
            os.makedirs(os.path.dirname(GEOM_FILE), exist_ok=True)
            with open(GEOM_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f)
        except Exception:
            pass

    # Restore once the window has been realised by the backend.
    try:
        fig.canvas.manager.window  # noqa: B018 — make sure manager exists
        _restore_geometry()
    except Exception:
        pass
    fig.canvas.mpl_connect("close_event", _save_geometry)

    # If a previous run cached an overview, push it into the queue so the
    # overview panel paints immediately on startup instead of staying blank
    # for the ~15 s FDSN fetch. It will be overwritten as soon as the live
    # fetch resolves; the stale-age is reflected in the status line.
    cached_ov = _load_cached_overview()
    if cached_ov is not None:
        c_mat, c_dur, c_label, c_age = cached_ov
        q.put(("overview", (c_mat, c_dur, c_label)))
        mins = int(c_age // 60)
        q.put(("overview_status",
               f"showing cached overview ({mins} min old) — "
               f"waiting for live fetch \u2026"))

    # 60 fps cap; blit + throttled heavy panels keep CPU low.
    ani = FuncAnimation(fig, update, interval=16, blit=True, cache_frame_data=False)
    plt.show()


# ----------------------------------------------------------------------------
def main():
    ssh_dest = sys.argv[1] if len(sys.argv) > 1 else SSH_DEST_DEFAULT
    ui_q  = queue.Queue()
    req_q = queue.Queue(maxsize=4)
    stop = threading.Event()
    t_tail = threading.Thread(target=tail_thread,
                              args=(ssh_dest, ui_q, stop), daemon=True)
    t_ov   = threading.Thread(target=overview_thread,
                              args=(req_q, ui_q, stop), daemon=True)
    t_health = threading.Thread(target=pi_health_thread,
                                args=(ssh_dest, ui_q, stop), daemon=True)
    t_tail.start()
    t_ov.start()
    t_health.start()
    try:
        run_ui(ui_q, req_q, ssh_dest=ssh_dest)
    finally:
        stop.set()
        req_q.put(None)


if __name__ == "__main__":
    main()
