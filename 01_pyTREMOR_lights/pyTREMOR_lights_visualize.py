# pyTREMOR_lights — visualization tool
#
# Replays the same band-splitting / centroid / burst-detection logic as
# pyTREMOR_lights01.py against the same seismic data, but renders the
# result as a multi-panel PNG figure instead of driving GPIOs.
#
# Useful for tuning GAIN / BASE_BRIGHTNESS / BURST_SIGMA / band edges
# without having to watch the physical installation.
#
# Usage (on the Pi or any machine with obspy + matplotlib):
#     /home/sjc1/venv_tremor/bin/python3 pyTREMOR_lights_visualize.py
#
# Output:
#     pyTREMOR_lights01_visualization.png
#
# All parameters are imported from pyTREMOR_lights01, so editing that
# file and re-running this script reflects the new behaviour.

import math
from collections import deque

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import gridspec
from obspy.clients.fdsn import Client
from obspy import UTCDateTime

import pyTREMOR_lights01 as cfg

OUTPUT_PNG = "pyTREMOR_lights01_visualization.png"

# Skip the first N seconds of the cache for *display only* — the zero-phase
# bandpass filter rings hard at the edges and produces a huge spike that
# dominates the y-axis. The main script does the same for playback.
DISPLAY_SKIP_SEC = 30.0

# Dark theme palette — chosen for high mutual contrast at small line widths
BG          = "#000000"
FG          = "#e8e8e8"
GRID        = "#262626"
WAVE_COLOR  = "#80d8ff"   # pale blue — waveform
RMS_COLOR   = "#ffffff"   # bright white — instantaneous RMS
MU_COLOR    = "#ffd000"   # saturated yellow — running mean μ
THR_COLOR   = "#ff2d6f"   # hot pink — μ+σ threshold (very distinct from yellow)
BURST_COLOR = "#ff2d6f"   # same hot pink — burst event verticals
EDGE_COLOR  = "#00e5ff"   # cyan — band edges on spectrogram
CENT_COLOR  = "#00ff9c"   # bright green — centroid (highly distinct from PWM)
PWM_COLOR   = "#ff8a3d"   # warm orange — PWM frequency


def fetch():
    client = Client(cfg.FDSN_BASE, timeout=60)
    t_end   = UTCDateTime.now() - cfg.FETCH_LAG_SEC
    t_start = t_end - cfg.FETCH_HOURS * 3600
    last_err = None
    for net, sta, loc, ch in cfg.STATIONS:
        print(f"Trying {net}.{sta}.{loc}.{ch} ...")
        try:
            st = client.get_waveforms(net, sta, loc, ch, t_start, t_end)
        except Exception as e:
            print(f"  no data ({type(e).__name__})")
            last_err = e
            continue
        st.merge(fill_value="interpolate")
        st.detrend("demean")
        st.filter("bandpass", freqmin=cfg.BANDPASS_MIN,
                  freqmax=cfg.BANDPASS_MAX, corners=4, zerophase=True)
        tr = st[0]
        data = tr.data.astype(np.float32)
        sr   = float(tr.stats.sampling_rate)
        peak = float(np.max(np.abs(data))) or 1.0
        return data / peak, sr, (net, sta, loc, ch), tr.stats.starttime, peak
    raise RuntimeError(f"No station returned data: {last_err}")


def replay(data, sr):
    """Replay the same logic as run() and return time series arrays."""
    n_bands = len(cfg.LED_PINS)
    edges   = np.geomspace(cfg.BANDPASS_MIN, cfg.BANDPASS_MAX, n_bands + 1)

    win_samples = int(cfg.SPECTRUM_WIN * sr)
    advance     = (cfg.SPEED_FACTOR * cfg.UPDATE_INTERVAL) * sr

    smoothed = np.full(n_bands, cfg.BASE_BRIGHTNESS, dtype=np.float32)
    alpha    = 1.0 - math.exp(-cfg.UPDATE_INTERVAL / cfg.SMOOTH_TAU)
    rms_hist = deque(maxlen=400)

    times, centroids, pwms = [], [], []
    bright = [[] for _ in range(n_bands)]
    rms_list, mu_list, thr_list = [], [], []
    burst_times = []

    cursor = float(win_samples)
    last_burst = -1e9
    frame_t    = 0.0

    while cursor < len(data):
        i0 = max(0, int(cursor) - win_samples)
        i1 = int(cursor)
        win = data[i0:i1]
        if len(win) < 8:
            cursor += advance
            frame_t += cfg.UPDATE_INTERVAL
            continue

        spec  = np.abs(np.fft.rfft(win * np.hanning(len(win))))
        freqs = np.fft.rfftfreq(len(win), d=1.0/sr)

        # Per-band RMS
        bands = np.zeros(n_bands, dtype=np.float32)
        for i in range(n_bands):
            m = (freqs >= edges[i]) & (freqs < edges[i+1])
            if m.any():
                bands[i] = float(np.sqrt(np.mean(spec[m]**2)))
        if bands.max() > 0:
            bands = bands / (np.median(bands[bands > 0]) + 1e-9)
        targets = np.clip(cfg.BASE_BRIGHTNESS + bands * cfg.GAIN * 8.0,
                          cfg.MIN_BRIGHTNESS, cfg.MAX_BRIGHTNESS)
        smoothed += alpha * (targets - smoothed)

        # Spectral centroid → PWM
        m = (freqs >= cfg.BANDPASS_MIN) & (freqs <= cfg.BANDPASS_MAX)
        s, f = spec[m], freqs[m]
        tot = s.sum()
        centroid = float((f * s).sum()/tot) if tot > 0 else (
            (cfg.BANDPASS_MIN + cfg.BANDPASS_MAX) * 0.5)
        t = (centroid - cfg.BANDPASS_MIN) / (cfg.BANDPASS_MAX - cfg.BANDPASS_MIN)
        t = max(0.0, min(1.0, t))
        pwm = int(cfg.MIN_FREQUENCY + t * (cfg.MAX_FREQUENCY - cfg.MIN_FREQUENCY))

        # Burst
        rms = float(np.sqrt(np.mean(win**2)))
        rms_hist.append(rms)
        if len(rms_hist) > 50:
            mu  = float(np.mean(rms_hist))
            sig = float(np.std(rms_hist)) + 1e-9
            thr = mu + cfg.BURST_SIGMA * sig
            if (rms > thr) and (frame_t - last_burst > cfg.BURST_MIN_GAP):
                last_burst = frame_t
                burst_times.append(frame_t)
        else:
            mu, thr = rms, rms

        times.append(frame_t)
        centroids.append(centroid)
        pwms.append(pwm)
        for i in range(n_bands):
            bright[i].append(float(smoothed[i]))
        rms_list.append(rms)
        mu_list.append(mu)
        thr_list.append(thr)

        cursor += advance
        frame_t += cfg.UPDATE_INTERVAL

    return {
        "t": np.array(times),
        "centroid": np.array(centroids),
        "pwm": np.array(pwms),
        "bright": np.array(bright),       # shape (8, N)
        "rms": np.array(rms_list),
        "mu":  np.array(mu_list),
        "thr": np.array(thr_list),
        "bursts": np.array(burst_times),
        "edges": edges,
    }


def plot(data, sr, station, starttime, peak, ts):
    # Trim the first DISPLAY_SKIP_SEC for display only — bandpass edge ringing
    skip = int(DISPLAY_SKIP_SEC * sr)
    data_disp = data[skip:]
    t_wave = (np.arange(len(data_disp)) + skip) / sr
    n_bands = len(cfg.LED_PINS)

    plt.rcParams.update({
        "axes.facecolor":    BG,
        "figure.facecolor":  BG,
        "savefig.facecolor": BG,
        "axes.edgecolor":    FG,
        "axes.labelcolor":   FG,
        "xtick.color":       FG,
        "ytick.color":       FG,
        "text.color":        FG,
        "axes.titlecolor":   FG,
        "grid.color":        GRID,
    })

    fig = plt.figure(figsize=(16, 16))
    gs  = gridspec.GridSpec(6, 1,
                            height_ratios=[1.0, 1.4, 2.6, 1.0, 1.0, 0.9],
                            hspace=0.55)

    title = (f"pyTREMOR_lights01 — {station[0]}.{station[1]}.{station[2]}.{station[3]}"
             f"   |   {cfg.FETCH_HOURS*60:.0f} min ending {starttime + len(data)/sr}"
             f"   |   replay {cfg.SPEED_FACTOR}×   raw peak abs={peak:.2g}")
    fig.suptitle(title, fontsize=12, color=FG)

    # 1. Raw bandpass-filtered waveform (display y-clipped at 99.5th percentile
    #    so a single large transient doesn't squash the whole trace).
    #    x-axis in seconds, formatted as minutes — must match panel 2 because
    #    they share x.
    ax0 = fig.add_subplot(gs[0])
    ax0.plot(t_wave, data_disp, lw=0.4, color=WAVE_COLOR)
    ax0.set_xlim(t_wave[0], t_wave[-1])
    ylim = float(np.percentile(np.abs(data_disp), 99.5)) * 1.4 or 1.0
    ax0.set_ylim(-ylim, ylim)
    ax0.set_ylabel("Bandpass\nwaveform\n(normalised)")
    ax0.set_xlabel("")
    ax0.grid(alpha=0.3)
    ax0.set_title("1.  raw ground motion, 1–18 Hz bandpass  (vertical marks = burst flashes)",
                  fontsize=9, color="#bbbbbb", loc="left", pad=4)
    # mark bursts in real-seismic seconds (replay_seconds * SPEED_FACTOR)
    for bt in ts["bursts"]:
        ax0.axvline(bt * cfg.SPEED_FACTOR, color=BURST_COLOR, alpha=0.55, lw=0.6)

    # 2. Spectrogram (1–18 Hz) — x in seconds, formatted as minutes
    ax1 = fig.add_subplot(gs[1], sharex=ax0)
    NFFT = int(sr * 4)
    Pxx, freqs, bins, im = ax1.specgram(data_disp, NFFT=NFFT, Fs=sr,
                                        noverlap=NFFT // 2,
                                        cmap="inferno",
                                        xextent=(t_wave[0], t_wave[-1]))
    ax1.set_ylim(cfg.BANDPASS_MIN, cfg.BANDPASS_MAX)
    xticks = np.linspace(t_wave[0], t_wave[-1], 7)
    ax1.set_xticks(xticks)
    ax1.set_xticklabels([f"{x/60:.0f}" for x in xticks])
    ax1.set_xlabel("Real-seismic time (minutes)")
    ax1.set_ylabel("Spectrogram\nfreq (Hz)")
    ax1.set_title("2.  energy at each frequency over time  (cyan lines = LED1…LED8 band edges)",
                  fontsize=9, color="#bbbbbb", loc="left", pad=4)
    # overlay band edges
    for e in ts["edges"]:
        ax1.axhline(e, color=EDGE_COLOR, alpha=0.45, lw=0.5, ls=":")

    # 3. 8 LED brightness traces (stacked)
    ax2 = fig.add_subplot(gs[2])
    edges = ts["edges"]
    # in *replay* minutes (this is how fast the LEDs actually move)
    t_replay_min = ts["t"] / 60
    offset = 110
    cmap = plt.get_cmap("viridis")
    for i in range(n_bands):
        y = ts["bright"][i] + i * offset
        ax2.fill_between(t_replay_min, i*offset, y, color=cmap(i/(n_bands-1)),
                         alpha=0.75, lw=0)
        ax2.plot(t_replay_min, y, color=cmap(i/(n_bands-1)), lw=0.8)
        label = f"LED{i+1}  {edges[i]:.1f}–{edges[i+1]:.1f} Hz  (pin {cfg.LED_PINS[i]})"
        ax2.text(0.002 * t_replay_min[-1], i*offset + offset*0.55, label,
                 fontsize=8, color=FG,
                 bbox=dict(boxstyle="round,pad=0.15", fc="#111111", ec="none", alpha=0.75))
    for bt in ts["bursts"]:
        ax2.axvline(bt/60, color=BURST_COLOR, alpha=0.55, lw=0.7)
    ax2.set_ylim(0, n_bands * offset)
    ax2.set_xlim(0, t_replay_min[-1])
    ax2.set_yticks([])
    ax2.set_ylabel("Per-LED brightness (0–100%)")
    ax2.set_xlabel("Replay time (minutes)")
    ax2.grid(alpha=0.3, axis="x")
    ax2.set_title("3.  each LED's brightness = RMS energy inside its own frequency band",
                  fontsize=9, color="#bbbbbb", loc="left", pad=4)

    # 4. Centroid + PWM frequency
    ax3 = fig.add_subplot(gs[3], sharex=ax2)
    # Plot PWM first (orange, behind) then centroid (green, on top) so the
    # important signal sits on top of the derived one.
    ax3b = ax3.twinx()
    ax3b.plot(t_replay_min, ts["pwm"], color=PWM_COLOR, lw=1.2, alpha=0.9,
              label="PWM Hz", zorder=2)
    ax3b.set_ylim(cfg.MIN_FREQUENCY, cfg.MAX_FREQUENCY)
    ax3b.set_ylabel("PWM freq (Hz)", color=PWM_COLOR)
    ax3b.tick_params(axis='y', labelcolor=PWM_COLOR)
    ax3b.spines["top"].set_color(FG)
    ax3b.spines["right"].set_color(PWM_COLOR)
    ax3b.spines["left"].set_color(CENT_COLOR)
    ax3.plot(t_replay_min, ts["centroid"], color=CENT_COLOR, lw=1.4,
             label="centroid (Hz)", zorder=3)
    ax3.set_ylabel("Centroid (Hz)", color=CENT_COLOR)
    ax3.tick_params(axis='y', labelcolor=CENT_COLOR)
    ax3.set_ylim(cfg.BANDPASS_MIN, cfg.BANDPASS_MAX)
    ax3.set_zorder(ax3b.get_zorder() + 1)
    ax3.patch.set_visible(False)
    ax3.grid(alpha=0.3)
    ax3.set_title("4.  green = where the spectral energy is centred (Hz)   →   orange = LED flicker rate (PWM Hz)",
                  fontsize=9, color="#bbbbbb", loc="left", pad=4)

    # 5. RMS + burst threshold
    ax4 = fig.add_subplot(gs[4], sharex=ax2)
    ax4.plot(t_replay_min, ts["rms"], color=RMS_COLOR, lw=1.0, label="RMS (loudness now)")
    ax4.plot(t_replay_min, ts["mu"],  color=MU_COLOR,  lw=1.2,
             label="running mean μ (baseline)")
    ax4.plot(t_replay_min, ts["thr"], color=THR_COLOR, lw=1.4, ls="--",
             label=f"burst threshold μ + {cfg.BURST_SIGMA:.1f}σ")
    for bt in ts["bursts"]:
        ax4.axvline(bt/60, color=BURST_COLOR, alpha=0.55, lw=0.7)
    ax4.set_ylabel("RMS")
    ax4.set_xlabel("Replay time (minutes)")
    leg = ax4.legend(loc="upper right", fontsize=8, facecolor="#111111",
                     edgecolor=GRID, labelcolor=FG)
    ax4.grid(alpha=0.3)
    ax4.set_title("5.  total loudness vs. its own statistics  →  RMS spike above pink dashed line = synchronous burst flash",
                  fontsize=9, color="#bbbbbb", loc="left", pad=4)

    # 6. Embedded key — 3-layer explanation
    ax5 = fig.add_subplot(gs[5])
    ax5.set_xlim(0, 1); ax5.set_ylim(0, 1)
    ax5.axis("off")
    key_text = (
        "HOW TO READ THIS GRAPH — three independent layers of expression\n"
        "\n"
        "   LED brightness (panel 3)   ←   per-band RMS energy inside each LED's narrow frequency window (FFT)\n"
        f"   LED flicker rate (panel 4, orange)   ←   spectral centroid (panel 4, green) mapped to {cfg.MIN_FREQUENCY}–{cfg.MAX_FREQUENCY} Hz PWM\n"
        f"   Synchronous all-LED flash (red lines)   ←   total RMS (panel 5, white) crossing μ + {cfg.BURST_SIGMA:.1f}σ\n"
        "\n"
        "In short:   spectrum shape → which LED is bright   |   dominant frequency → how fast they flicker   |   sudden loudness → all flash together"
    )
    ax5.text(0.0, 0.95, key_text, ha="left", va="top",
             fontsize=9.5, color=FG, family="monospace",
             bbox=dict(boxstyle="round,pad=0.6", fc="#0a0a0a",
                       ec=GRID, lw=0.8))

    footer = (f"GAIN={cfg.GAIN}   BASE_BRIGHTNESS={cfg.BASE_BRIGHTNESS}   "
              f"SMOOTH_TAU={cfg.SMOOTH_TAU}s   SPECTRUM_WIN={cfg.SPECTRUM_WIN}s   "
              f"SPEED={cfg.SPEED_FACTOR}×   "
              f"BURST_SIGMA={cfg.BURST_SIGMA}   BURST_MIN_GAP={cfg.BURST_MIN_GAP}s   "
              f"PWM range {cfg.MIN_FREQUENCY}–{cfg.MAX_FREQUENCY} Hz   "
              f"bursts detected: {len(ts['bursts'])}   "
              f"(first {DISPLAY_SKIP_SEC:.0f}s of waveform hidden — filter edge ringing)")
    fig.text(0.5, 0.005, footer, ha="center", fontsize=8, color="#aaaaaa")

    fig.savefig(OUTPUT_PNG, dpi=130, bbox_inches="tight", facecolor=BG)
    print(f"Saved {OUTPUT_PNG}")


def main():
    data, sr, station, starttime, peak = fetch()
    print(f"Got {len(data)} samples @ {sr} Hz from "
          f"{station[0]}.{station[1]}.{station[2]}.{station[3]}")
    ts = replay(data, sr)
    print(f"Replayed {len(ts['t'])} frames covering "
          f"{ts['t'][-1]:.1f}s of replay time "
          f"({ts['t'][-1]*cfg.SPEED_FACTOR/60:.1f} min of seismic data)")
    print(f"Bursts detected: {len(ts['bursts'])}")
    plot(data, sr, station, starttime, peak, ts)


if __name__ == "__main__":
    main()
