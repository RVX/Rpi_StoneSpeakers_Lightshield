# UNDERWATER RESONANCES FOR J.C.STUDIO
# USING THE CUSTOM SHIELD FROM STONE SPEAKERS
# VMG 2026 — TREMOR VERSION (live seismic data from a volcano station)
#
# Drives the 8 LED outputs from real volcanic-tremor data fetched from
# the EarthScope FDSN service via ObsPy.  The 1–18 Hz seismic band is
# split into 8 logarithmic sub-bands; each LED tracks the RMS energy
# of one band.  The overall spectral centroid sets the PWM frequency,
# and strong amplitude transients trigger an all-LED flash.
#
# Default station: IU.PET.BHZ (Petropavlovsk-Kamchatsky, Kamchatka) —
# almost always tremoring.  Change STATION_* constants below to pick
# another configured pyTREMOR station (RABL, HNR, DAV, GSI, etc.).
#
# Requires: pip install obspy numpy pigpio

import pigpio
import time
import signal
import math
from datetime import datetime
from collections import deque

import numpy as np
from obspy.clients.fdsn import Client
from obspy import UTCDateTime

# --- Hardware -----------------------------------------------------------------
LED_PINS = [4, 18, 17, 27, 22, 5, 12, 13]   # OUT1 – OUT8

MIN_BRIGHTNESS  = 0
MAX_BRIGHTNESS  = 100
MIN_FREQUENCY   = 170
MAX_FREQUENCY   = 800
UPDATE_INTERVAL = 0.05    # seconds between PWM writes (~20 Hz visual refresh)

# --- Seismic source -----------------------------------------------------------
FDSN_BASE       = "https://service.earthscope.org"

# Primary + fallback stations (network, station, location, channel).
# The script tries them in order; the first one that returns data wins.
# All are GSN IU stations in active volcanic regions.
STATIONS = [
    ("IU", "HNR",  "00", "BHZ"),   # Honiara, Solomon Islands
    ("IU", "DAV",  "00", "BHZ"),   # Davao, Philippines (Mt Apo)
    ("IU", "MAJO", "00", "BHZ"),   # Matsushiro, Japan
    ("IU", "PET",  "00", "BHZ"),   # Petropavlovsk, Kamchatka (often offline)
    ("IU", "SNZO", "00", "BHZ"),   # Wellington, New Zealand
]

FETCH_HOURS     = 1.0       # how much past data to download per refresh
FETCH_LAG_SEC   = 600       # EarthScope often lacks the last few minutes — stay 10 min behind realtime
REFETCH_MARGIN  = 60        # re-download this many seconds before cache runs out
BANDPASS_MIN    = 1.0       # Hz — pyTREMOR default
BANDPASS_MAX    = 18.0      # Hz — pyTREMOR default for 40 sps BHZ

# --- Mapping ------------------------------------------------------------------
SPEED_FACTOR    = 5.0       # 1.0 = real time; 5.0 = 5x faster
SMOOTH_TAU      = 0.30      # exponential smoothing for per-band brightness (s)
SPECTRUM_WIN    = 1.0       # seconds of seismic data per spectrum frame
BURST_SIGMA     = 3.0       # transient flash threshold: mean + N·σ
BURST_MIN_GAP   = 2.0       # seconds between consecutive flashes
BURST_DURATION  = 0.18      # how long the bright flash holds
BASE_BRIGHTNESS = 8         # minimum % brightness so installation is never fully dark
GAIN            = 6.0       # overall sensitivity multiplier on per-band level
# ------------------------------------------------------------------------------


def _band_edges(n_bands, fmin, fmax):
    """Logarithmically spaced band edges between fmin and fmax."""
    return np.geomspace(fmin, fmax, n_bands + 1)


def set_pin(pi, pin, brightness, freq):
    dc = max(0,             min(255,           int(brightness * 2.55)))
    f  = max(MIN_FREQUENCY, min(MAX_FREQUENCY, int(freq)))
    pi.set_PWM_frequency(pin, f)
    pi.set_PWM_dutycycle(pin, dc)


def set_all(pi, brightness, freq):
    dc = max(0,             min(255,           int(brightness * 2.55)))
    f  = max(MIN_FREQUENCY, min(MAX_FREQUENCY, int(freq)))
    for pin in LED_PINS:
        pi.set_PWM_frequency(pin, f)
        pi.set_PWM_dutycycle(pin, dc)


def failsafe(pi):
    print("\nFailsafe: 25% brightness, 800 Hz on all outputs.")
    set_all(pi, 25, 800)


def fetch_stream(client, t_end):
    """Download FETCH_HOURS up to t_end (minus the realtime lag), processed.

    Tries each station in STATIONS in order; returns the first one that
    returns data.  Returns (data, sr, station_tuple).
    """
    t_end   = t_end - FETCH_LAG_SEC
    t_start = t_end - FETCH_HOURS * 3600
    last_err = None
    for net, sta, loc, ch in STATIONS:
        print(f"  Fetching {net}.{sta}.{loc}.{ch} "
              f"{t_start} → {t_end} …", flush=True)
        try:
            st = client.get_waveforms(net, sta, loc, ch, t_start, t_end)
        except Exception as e:
            print(f"    -> no data ({type(e).__name__}); trying next station")
            last_err = e
            continue
        st.merge(fill_value="interpolate")
        st.detrend("demean")
        st.filter("bandpass", freqmin=BANDPASS_MIN, freqmax=BANDPASS_MAX,
                  corners=4, zerophase=True)
        tr = st[0]
        data = tr.data.astype(np.float32)
        sr   = float(tr.stats.sampling_rate)
        peak = float(np.max(np.abs(data))) or 1.0
        data = data / peak
        print(f"  Got {len(data)} samples @ {sr:.0f} Hz "
              f"({len(data)/sr/60:.1f} min, peak abs={peak:.3g})",
              flush=True)
        return data, sr, (net, sta, loc, ch)
    raise RuntimeError(f"No station returned data. Last error: {last_err}")


def spectrum_bands(window, sr, edges):
    """Return RMS amplitude in each band for the given time-window."""
    n = len(window)
    if n < 8:
        return np.zeros(len(edges) - 1, dtype=np.float32)
    # real FFT
    spec = np.abs(np.fft.rfft(window * np.hanning(n)))
    freqs = np.fft.rfftfreq(n, d=1.0 / sr)
    out = np.zeros(len(edges) - 1, dtype=np.float32)
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        mask = (freqs >= lo) & (freqs < hi)
        if mask.any():
            out[i] = float(np.sqrt(np.mean(spec[mask] ** 2)))
    return out


def spectral_centroid(window, sr):
    n = len(window)
    if n < 8:
        return (BANDPASS_MIN + BANDPASS_MAX) * 0.5
    spec = np.abs(np.fft.rfft(window * np.hanning(n)))
    freqs = np.fft.rfftfreq(n, d=1.0 / sr)
    mask = (freqs >= BANDPASS_MIN) & (freqs <= BANDPASS_MAX)
    s, f = spec[mask], freqs[mask]
    total = s.sum()
    if total <= 0:
        return (BANDPASS_MIN + BANDPASS_MAX) * 0.5
    return float((f * s).sum() / total)


def map_centroid_to_pwm(centroid_hz):
    """Low seismic freq → low PWM (visible flicker); high → smooth."""
    t = (centroid_hz - BANDPASS_MIN) / (BANDPASS_MAX - BANDPASS_MIN)
    t = max(0.0, min(1.0, t))
    return int(MIN_FREQUENCY + t * (MAX_FREQUENCY - MIN_FREQUENCY))


def ambient_pulse(pi, duration):
    """Soft sine pulse while initial data fetch is in flight."""
    print(f"  Ambient pulse for {duration:.0f}s while fetching data…")
    t0 = time.monotonic()
    while time.monotonic() - t0 < duration:
        phase = (time.monotonic() - t0) * 0.6
        b = 15 + 10 * (math.sin(phase) + 1) * 0.5
        set_all(pi, b, 800)
        time.sleep(UPDATE_INTERVAL)


def run(pi):
    n_bands = len(LED_PINS)
    edges   = _band_edges(n_bands, BANDPASS_MIN, BANDPASS_MAX)

    print(f"=== UNDERWATER TREMOR — live seismic LED driver ===")
    print(f"    Stations   : {[s[1] for s in STATIONS]} (in fallback order)")
    print(f"    Bandpass   : {BANDPASS_MIN}–{BANDPASS_MAX} Hz "
          f"split into {n_bands} log bands")
    print(f"    Outputs    : {LED_PINS}")
    print(f"    Speed      : {SPEED_FACTOR}× real time")
    print(f"    Range      : {MIN_BRIGHTNESS}% – {MAX_BRIGHTNESS}%\n")

    client = Client(FDSN_BASE, timeout=60)

    # Smoothing state per band + running stats for burst detection
    smoothed   = np.full(n_bands, BASE_BRIGHTNESS, dtype=np.float32)
    rms_hist   = deque(maxlen=400)   # ~20s of running history at 20Hz
    last_burst = -1e9
    alpha      = 1.0 - math.exp(-UPDATE_INTERVAL / SMOOTH_TAU)

    # Initial fetch
    try:
        data, sr, active = fetch_stream(client, UTCDateTime.now())
    except Exception as e:
        print(f"  ! initial fetch failed: {e}")
        ambient_pulse(pi, 20)
        data, sr, active = fetch_stream(client, UTCDateTime.now())
    print(f"    Active station: {active[0]}.{active[1]}.{active[2]}.{active[3]}\n")

    win_samples   = int(SPECTRUM_WIN * sr)
    advance_per_frame = (SPEED_FACTOR * UPDATE_INTERVAL) * sr   # seismic samples per visual frame
    # Skip the first 30 s to avoid zero-phase bandpass edge ringing
    cursor = float(max(win_samples, int(30 * sr)))

    while True:
        # Need to refetch?
        seconds_left = (len(data) - cursor) / sr
        if seconds_left < REFETCH_MARGIN:
            try:
                data, sr, active = fetch_stream(client, UTCDateTime.now())
                cursor = float(win_samples)
                win_samples = int(SPECTRUM_WIN * sr)
                advance_per_frame = (SPEED_FACTOR * UPDATE_INTERVAL) * sr
            except Exception as e:
                print(f"  ! refetch failed, looping cache: {e}")
                cursor = float(win_samples)   # rewind and loop

        i0 = int(cursor) - win_samples
        i1 = int(cursor)
        if i0 < 0:
            i0, i1 = 0, win_samples
        window = data[i0:i1]

        # Per-band energies → target brightness 0-100
        bands  = spectrum_bands(window, sr, edges)
        if bands.max() > 0:
            bands = bands / (np.median(bands[bands > 0]) + 1e-9)
        targets = np.clip(BASE_BRIGHTNESS + bands * GAIN * 8.0,
                          MIN_BRIGHTNESS, MAX_BRIGHTNESS)

        # Exponential smoothing
        smoothed += alpha * (targets - smoothed)

        # Overall RMS + transient detection
        rms = float(np.sqrt(np.mean(window ** 2))) if len(window) else 0.0
        rms_hist.append(rms)
        if len(rms_hist) > 50:
            mu  = float(np.mean(rms_hist))
            sig = float(np.std(rms_hist)) + 1e-9
            t_now = time.monotonic()
            if (rms > mu + BURST_SIGMA * sig) and (t_now - last_burst > BURST_MIN_GAP):
                last_burst = t_now
                centroid   = spectral_centroid(window, sr)
                pwm        = map_centroid_to_pwm(centroid)
                print(f"  ! BURST  rms={rms:.3f}  centroid={centroid:.1f}Hz")
                set_all(pi, 100, pwm)
                time.sleep(BURST_DURATION)
                # let the smoothing recover naturally next frames

        # PWM frequency from spectral centroid
        centroid = spectral_centroid(window, sr)
        pwm      = map_centroid_to_pwm(centroid)

        # Push per-pin brightness
        for pin, b in zip(LED_PINS, smoothed):
            set_pin(pi, pin, float(b), pwm)

        # Status line
        ts = datetime.now().strftime("%H:%M:%S")
        bands_str = " ".join(f"{int(b):3d}" for b in smoothed)
        print(f"\r[{ts}] cur={cursor/sr:6.1f}s  "
              f"cen={centroid:4.1f}Hz  pwm={pwm:4d}  "
              f"bands={bands_str}",
              end="", flush=True)

        cursor += advance_per_frame
        time.sleep(UPDATE_INTERVAL)


def _sigterm(signum, frame):
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, _sigterm)


if __name__ == "__main__":
    pi = pigpio.pi()
    if not pi.connected:
        raise RuntimeError("Cannot connect to pigpiod — is the daemon running?")
    try:
        run(pi)
    except KeyboardInterrupt:
        pass
    finally:
        failsafe(pi)
        pi.stop()
