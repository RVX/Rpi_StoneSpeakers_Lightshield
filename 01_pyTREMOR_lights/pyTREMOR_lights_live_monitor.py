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
HISTORY_SEC      = 60.0
FPS_ASSUMED      = 20      # pyTREMOR_lights01 writes ~20 frames / s
HISTORY_FRAMES   = int(HISTORY_SEC * FPS_ASSUMED)

# Match band edges used by pyTREMOR_lights01.py (1.0 – 18.0 Hz, 8 log bands)
BAND_EDGES = np.geomspace(1.0, 18.0, 9)

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
        except Exception as e:
            ui_q.put(("overview_err", f"{type(e).__name__}: {e}"))


# ----------------------------------------------------------------------------
# SSH tail thread
# ----------------------------------------------------------------------------
def tail_thread(ssh_dest, q, stop_event):
    """Spawn `ssh ... tail -F -n 400 /var/log/pytremor_lights.log` and push lines into q."""
    cmd = [
        "ssh",
        "-i", SSH_KEY,
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=15",
        "-o", "ConnectTimeout=10",
    ]
    # IPv6 link-local needs -6
    if ":" in ssh_dest.split("@", 1)[-1]:
        cmd.append("-6")
    # First emit the most recent Fetching lines (so the laptop knows the
    # current cache window), then keep tailing live with line-buffered
    # output so each \r-terminated frame from the Pi arrives immediately
    # (no kernel pipe batching).
    remote = (
        f"grep -a Fetching {LOG_PATH} 2>/dev/null | tail -3 ; "
        f"stdbuf -oL -eL tail -F -n 200 {LOG_PATH}"
    )
    cmd += [ssh_dest, remote]

    print("Connecting:", " ".join(cmd))
    try:
        # binary mode + raw byte reads so we can split on either \r or \n.
        # The Pi terminates each frame with \r (carriage return overwrite);
        # if we used text-mode line iteration we'd block until the next \n
        # (usually only on BURST / Fetching lines), and frames would arrive
        # in bursts instead of in real time.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
    except FileNotFoundError:
        q.put(("error", "ssh not found in PATH — add C:\\Windows\\System32\\OpenSSH"))
        return

    try:
        buf = bytearray()
        while not stop_event.is_set():
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            buf += chunk
            # Flush any complete pieces (split on \r or \n, keep the tail).
            i = 0
            for j, b in enumerate(buf):
                if b == 0x0a or b == 0x0d:  # \n or \r
                    piece = bytes(buf[i:j]).decode("utf-8", errors="replace").strip()
                    if piece:
                        q.put(("line", piece))
                    i = j + 1
            del buf[:i]
    finally:
        proc.terminate()
        q.put(("error", "ssh tail ended"))


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


def _style_axes(ax):
    ax.set_facecolor(PANEL_COLOR)
    for spine in ax.spines.values():
        spine.set_color(GRID_COLOR)
        spine.set_linewidth(0.8)
    ax.tick_params(colors=TEXT_MUTED, labelsize=8, length=3)
    ax.title.set_color(TEXT_PRIMARY)
    ax.xaxis.label.set_color(TEXT_MUTED)
    ax.yaxis.label.set_color(TEXT_MUTED)


def run_ui(q, req_q=None):
    fig = plt.figure(figsize=(13, 8.5), facecolor=BG_COLOR, dpi=100)
    fig.canvas.manager.set_window_title("pyTREMOR · live monitor")
    gs = fig.add_gridspec(
        4, 2,
        width_ratios=[1.0, 1.6],
        height_ratios=[1.0, 1.0, 0.75, 0.20],
        hspace=0.80, wspace=0.22,
        left=0.06, right=0.97, top=0.94, bottom=0.06,
    )
    ax_bars     = fig.add_subplot(gs[0:2, 0])
    ax_water    = fig.add_subplot(gs[0,   1])
    ax_cen      = fig.add_subplot(gs[1,   1])
    ax_overview = fig.add_subplot(gs[2, :])
    ax_status   = fig.add_subplot(gs[3, :])
    ax_status.set_facecolor(BG_COLOR)
    ax_status.axis("off")
    for ax in (ax_bars, ax_water, ax_cen, ax_overview):
        _style_axes(ax)

    # --- bars panel --------------------------------------------------------
    band_labels = [f"{BAND_EDGES[i]:.1f}–{BAND_EDGES[i+1]:.1f} Hz" for i in range(8)]
    # Compact 1-line tick labels so 8 of them never overlap each other or
    # spill onto the panel below. Detailed Hz info still lives in the LED
    # value labels above the bars + the L1–L8 ids in the waterfall.
    short_band_labels = [
        f"L{i+1}\n{BAND_EDGES[i]:.0f}–{BAND_EDGES[i+1]:.0f}"
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
    ax_bars.set_xticklabels(
        short_band_labels,
        fontsize=7.5, color=TEXT_MUTED,
    )
    ax_bars.tick_params(axis="x", pad=18)   # leave room for LED circles
    ax_bars.set_yticks([0, 25, 50, 75, 100])
    ax_bars.set_ylabel("brightness  (%)", color=TEXT_MUTED, fontsize=9)
    ax_bars.set_title("current LED output  ·  live lamp preview",
                      color=TEXT_PRIMARY, fontsize=11, pad=10, fontweight="light")
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
    ax_water.set_xlabel("seconds ago", color=TEXT_MUTED, fontsize=9)
    ax_water.set_title("per-band history  ·  60 s waterfall",
                       color=TEXT_PRIMARY, fontsize=11, pad=10, fontweight="light")
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
    ax_cen.set_xlabel("seconds ago", color=TEXT_MUTED, fontsize=9)
    ax_cen.set_ylabel("centroid  (Hz)", color=TEXT_MUTED, fontsize=9)
    ax_cen.set_title("spectral centroid  →  PWM frequency",
                     color=TEXT_PRIMARY, fontsize=11, pad=10, fontweight="light")
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
    ax_overview.set_xlabel("seconds into seismic cache  ·  blue line = current replay position",
                           color=TEXT_MUTED, fontsize=9)
    ax_overview.set_title("overall sonification  ·  full seismic cache",
                          color=TEXT_PRIMARY, fontsize=11, pad=10, fontweight="light")

    # --- status text ------------------------------------------------------
    status_txt = ax_status.text(
        0.01, 0.55, "waiting for first frame…",
        transform=ax_status.transAxes,
        ha="left", va="center",
        fontsize=10.5, color=TEXT_PRIMARY, family="monospace", alpha=0.9,
    )
    overview_status_txt = ax_status.text(
        0.01, 0.1, "",
        transform=ax_status.transAxes,
        ha="left", va="center",
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
    }
    _glow_rgb = np.array(matplotlib.colors.to_rgb(GLOW_COLOR), dtype=np.float32)
    state["_led_rgba_core"][:, :3] = _glow_rgb
    state["_led_rgba_halo"][:, :3] = _glow_rgb

    def update(_frame):
        while True:
            try:
                kind, payload = q.get_nowait()
            except queue.Empty:
                break

            if kind == "error":
                status_txt.set_text(f"!! {payload}")
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

            line = payload
            state["connected"] = True
            mf = FETCH_RE.search(line)
            if mf:
                net, sta, loc, ch, t0, t1 = mf.groups()
                state["station"] = f"{net}.{sta}.{loc}.{ch}"
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
            ov_dur = max(1.0, state["ov_duration"])
            pass_n = int(state["last_cur"] // ov_dur) + 1
            pass_str = f"pass {pass_n}" if pass_n > 1 else "first pass"
            status_txt.set_text(
                f"station = {state['station']:<18}   "
                f"replay t = {state['last_cur']:7.1f} s   "
                f"centroid = {state['last_cen']:5.2f} Hz   "
                f"PWM = {state['last_pwm']:4d} Hz   "
                f"frames = {state['n_frames']}   "
                f"bursts = {state['n_bursts']}   "
                f"[{pass_str}]"
                + ("" if state["connected"] else "   [waiting for ssh…]")
            )

        # Every animated artist must be returned every frame so blit
        # paints it on top of the restored background. Skipping any of
        # them on off-frames causes flicker (vanish/reappear cycle).
        return [led_core, led_halo, *bars, peak_line, *bar_value_txt,
                ov_im, ov_cursor, ov_played, im, cen_line,
                status_txt, overview_status_txt]

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
    t_tail.start()
    t_ov.start()
    try:
        run_ui(ui_q, req_q)
    finally:
        stop.set()
        req_q.put(None)


if __name__ == "__main__":
    main()
