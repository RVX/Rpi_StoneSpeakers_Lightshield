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
# Live UI
# ----------------------------------------------------------------------------
def run_ui(q):
    plt.style.use("dark_background")
    fig = plt.figure(figsize=(13, 7))
    fig.canvas.manager.set_window_title("pyTREMOR · live monitor")
    gs = fig.add_gridspec(
        3, 2,
        width_ratios=[1.0, 1.6],
        height_ratios=[1.0, 1.0, 0.18],
        hspace=0.35, wspace=0.25,
    )
    ax_bars   = fig.add_subplot(gs[0:2, 0])
    ax_water  = fig.add_subplot(gs[0,   1])
    ax_cen    = fig.add_subplot(gs[1,   1])
    ax_status = fig.add_subplot(gs[2, :])
    ax_status.axis("off")

    # --- bars panel --------------------------------------------------------
    bar_colors = plt.cm.plasma(np.linspace(0.05, 0.95, 8))
    band_labels = [
        f"{BAND_EDGES[i]:.1f}–{BAND_EDGES[i+1]:.1f} Hz"
        for i in range(8)
    ]
    bars = ax_bars.bar(range(8), [0]*8, color=bar_colors, edgecolor="white", linewidth=0.5)
    ax_bars.set_ylim(0, 100)
    ax_bars.set_xticks(range(8))
    ax_bars.set_xticklabels([f"LED{i+1}\n{band_labels[i]}" for i in range(8)],
                            fontsize=8, color="lightgray")
    ax_bars.set_ylabel("Brightness  (%)", color="lightgray")
    ax_bars.set_title("Current LED output", color="white", fontsize=11)
    ax_bars.grid(axis="y", alpha=0.2)

    # --- waterfall panel ---------------------------------------------------
    waterfall = np.zeros((8, HISTORY_FRAMES), dtype=np.float32)
    im = ax_water.imshow(
        waterfall, aspect="auto", origin="lower",
        cmap="plasma", vmin=0, vmax=100,
        extent=[-HISTORY_SEC, 0, 0.5, 8.5],
    )
    ax_water.set_yticks(range(1, 9))
    ax_water.set_yticklabels([f"L{i+1}" for i in range(8)], color="lightgray", fontsize=8)
    ax_water.set_xlabel("seconds ago", color="lightgray")
    ax_water.set_title("Per-band history (60 s)", color="white", fontsize=11)
    cbar = fig.colorbar(im, ax=ax_water, fraction=0.04, pad=0.02)
    cbar.set_label("%", color="lightgray")
    cbar.ax.yaxis.set_tick_params(color="lightgray")
    plt.setp(cbar.ax.get_yticklabels(), color="lightgray")

    # --- centroid panel ----------------------------------------------------
    cen_hist = deque([np.nan] * HISTORY_FRAMES, maxlen=HISTORY_FRAMES)
    t_axis = np.linspace(-HISTORY_SEC, 0, HISTORY_FRAMES)
    (cen_line,) = ax_cen.plot(t_axis, list(cen_hist), color="#39ff14", lw=1.4)
    ax_cen.set_ylim(BAND_EDGES[0], BAND_EDGES[-1])
    ax_cen.set_xlim(-HISTORY_SEC, 0)
    ax_cen.set_xlabel("seconds ago", color="lightgray")
    ax_cen.set_ylabel("centroid  (Hz)", color="lightgray")
    ax_cen.set_title("Spectral centroid → drives PWM frequency", color="white", fontsize=11)
    ax_cen.grid(alpha=0.2)

    # --- status text -------------------------------------------------------
    status_txt = ax_status.text(
        0.01, 0.5, "Waiting for first frame…",
        transform=ax_status.transAxes,
        ha="left", va="center",
        fontsize=11, color="white", family="monospace",
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
