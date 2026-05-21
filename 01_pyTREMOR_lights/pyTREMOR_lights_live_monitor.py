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
import subprocess
import threading
import queue
from collections import deque

import numpy as np
import matplotlib
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

# ----------------------------------------------------------------------------
# Connection settings — override on command line or via environment
# ----------------------------------------------------------------------------
SSH_DEST_DEFAULT = "sjc1@fe80::8aa2:9eff:fed7:9f99%19"  # IPv6 link-local on WiFi
SSH_KEY          = os.path.expandvars(r"%USERPROFILE%\.ssh\id_ed25519_pis")
LOG_PATH         = "/var/log/pytremor_lights.log"
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
FETCH_RE = re.compile(r"Fetching\s+([A-Z0-9.]+)\s")
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
    cmd += [ssh_dest, f"tail -F -n 400 {LOG_PATH}"]

    print("Connecting:", " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        q.put(("error", "ssh not found in PATH — add C:\\Windows\\System32\\OpenSSH"))
        return

    try:
        for line in proc.stdout:
            if stop_event.is_set():
                break
            # Pi script uses \r to overwrite frames; many frames + occasional
            # BURST events may be packed in one \n-terminated chunk. Split on
            # \r so the parser sees each frame separately.
            for piece in line.replace("\r", "\n").split("\n"):
                piece = piece.strip()
                if piece:
                    q.put(("line", piece))
    finally:
        proc.terminate()
        q.put(("error", "ssh tail ended"))


# ----------------------------------------------------------------------------
# Live UI — soft, low-contrast palette designed to be easy on the eyes
# ----------------------------------------------------------------------------
BG_COLOR     = "#161a22"   # warm near-black
PANEL_COLOR  = "#1d222c"   # slightly lifted panel fill
GRID_COLOR   = "#2c3340"
TEXT_PRIMARY = "#e6dccb"   # warm cream
TEXT_MUTED   = "#9aa0a9"   # cool gray
ACCENT       = "#f0c987"   # soft amber for the centroid line

# Soft 8-step palette for the LED bars — sunset → sea, all desaturated
BAR_PALETTE = [
    "#e2a8a8",  # dusty rose
    "#eac49a",  # soft peach
    "#e8d6a1",  # warm sand
    "#cdd6a4",  # pale moss
    "#a9d0b6",  # sage
    "#9ec8c8",  # soft teal
    "#a8bcd6",  # dusty blue
    "#b9aed3",  # lavender
]

# Custom soft waterfall colormap (deep indigo → mauve → coral → cream)
SOFT_CMAP = matplotlib.colors.LinearSegmentedColormap.from_list(
    "soft_tremor",
    [
        (0.00, "#1a1d2a"),
        (0.20, "#332e44"),
        (0.45, "#7a5773"),
        (0.65, "#c08272"),
        (0.85, "#e6b58c"),
        (1.00, "#f4e1c5"),
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


def run_ui(q):
    fig = plt.figure(figsize=(13, 7), facecolor=BG_COLOR)
    fig.canvas.manager.set_window_title("pyTREMOR · live monitor")
    gs = fig.add_gridspec(
        3, 2,
        width_ratios=[1.0, 1.6],
        height_ratios=[1.0, 1.0, 0.18],
        hspace=0.42, wspace=0.22,
        left=0.06, right=0.97, top=0.93, bottom=0.08,
    )
    ax_bars   = fig.add_subplot(gs[0:2, 0])
    ax_water  = fig.add_subplot(gs[0,   1])
    ax_cen    = fig.add_subplot(gs[1,   1])
    ax_status = fig.add_subplot(gs[2, :])
    ax_status.set_facecolor(BG_COLOR)
    ax_status.axis("off")
    for ax in (ax_bars, ax_water, ax_cen):
        _style_axes(ax)

    # --- bars panel --------------------------------------------------------
    band_labels = [f"{BAND_EDGES[i]:.1f}–{BAND_EDGES[i+1]:.1f} Hz" for i in range(8)]
    bars = ax_bars.bar(
        range(8), [0]*8,
        color=BAR_PALETTE,
        edgecolor=BG_COLOR, linewidth=1.2,
        width=0.72,
    )
    ax_bars.set_ylim(0, 100)
    ax_bars.set_xticks(range(8))
    ax_bars.set_xticklabels(
        [f"LED{i+1}\n{band_labels[i]}" for i in range(8)],
        fontsize=8, color=TEXT_MUTED,
    )
    ax_bars.set_ylabel("brightness  (%)", color=TEXT_MUTED, fontsize=9)
    ax_bars.set_title("current LED output", color=TEXT_PRIMARY,
                      fontsize=11, pad=10, fontweight="light")
    ax_bars.grid(axis="y", color=GRID_COLOR, alpha=0.6, linewidth=0.6)
    ax_bars.set_axisbelow(True)

    # --- waterfall panel ---------------------------------------------------
    waterfall = np.full((8, HISTORY_FRAMES), np.nan, dtype=np.float32)
    im = ax_water.imshow(
        waterfall, aspect="auto", origin="lower",
        cmap=SOFT_CMAP, vmin=0, vmax=100,
        extent=[-HISTORY_SEC, 0, 0.5, 8.5],
        interpolation="bilinear",
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

    # --- centroid panel ----------------------------------------------------
    cen_hist = deque([np.nan] * HISTORY_FRAMES, maxlen=HISTORY_FRAMES)
    t_axis = np.linspace(-HISTORY_SEC, 0, HISTORY_FRAMES)
    (cen_line,) = ax_cen.plot(t_axis, list(cen_hist), color=ACCENT, lw=1.6, alpha=0.95)
    # subtle band-edge guidelines on the centroid plot
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

    # --- status text -------------------------------------------------------
    status_txt = ax_status.text(
        0.01, 0.5, "waiting for first frame…",
        transform=ax_status.transAxes,
        ha="left", va="center",
        fontsize=10.5, color=TEXT_PRIMARY, family="monospace", alpha=0.9,
    )

    # --- shared state ------------------------------------------------------
    state = {
        "station": "?",
        "n_frames": 0,
        "n_bursts": 0,
        "last_cur": 0.0,
        "last_cen": 0.0,
        "last_pwm": 0,
        "connected": False,
    }

    def update(_frame):
        # drain queue
        drained = 0
        while True:
            try:
                kind, payload = q.get_nowait()
            except queue.Empty:
                break
            drained += 1
            if kind == "error":
                status_txt.set_text(f"!! {payload}")
                continue
            line = payload
            state["connected"] = True
            mf = FETCH_RE.search(line)
            if mf:
                state["station"] = mf.group(1)
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
            # shift waterfall left, push new column at right
            waterfall[:, :-1] = waterfall[:, 1:]
            waterfall[:, -1]  = fr["bands"]
            # bars (only update once per UI frame using latest)
            latest_bands = fr["bands"]
            for b, h in zip(bars, latest_bands):
                b.set_height(h)
            cen_hist.append(fr["cen"])

        # push updated arrays to artists
        im.set_data(waterfall)
        cen_line.set_ydata(list(cen_hist))

        status_txt.set_text(
            f"station = {state['station']:<14}   "
            f"replay t = {state['last_cur']:7.1f} s   "
            f"centroid = {state['last_cen']:5.2f} Hz   "
            f"PWM freq = {state['last_pwm']:4d} Hz   "
            f"frames = {state['n_frames']}   "
            f"bursts = {state['n_bursts']}"
            + ("" if state["connected"] else "   [waiting for ssh…]")
        )
        return [im, cen_line, status_txt, *bars]

    ani = FuncAnimation(fig, update, interval=100, blit=False, cache_frame_data=False)
    plt.show()


# ----------------------------------------------------------------------------
def main():
    ssh_dest = sys.argv[1] if len(sys.argv) > 1 else SSH_DEST_DEFAULT
    q = queue.Queue()
    stop = threading.Event()
    t = threading.Thread(target=tail_thread, args=(ssh_dest, q, stop), daemon=True)
    t.start()
    try:
        run_ui(q)
    finally:
        stop.set()


if __name__ == "__main__":
    main()
