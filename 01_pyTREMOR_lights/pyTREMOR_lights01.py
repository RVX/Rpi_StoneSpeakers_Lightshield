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

import os
import glob
import pigpio
import time
import signal
import math
import tempfile
from datetime import datetime
from collections import deque

import numpy as np
from obspy.clients.fdsn import Client
from obspy import UTCDateTime, read as obspy_read

# --- Hardware -----------------------------------------------------------------
LED_PINS = [4, 18, 17, 27, 22, 5, 12, 13]   # OUT1 – OUT8

MIN_BRIGHTNESS  = 0
MAX_BRIGHTNESS  = 100
MIN_FREQUENCY   = 100
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

# --- Resilience: local seismic cache ------------------------------------------
# When every FDSN station fails (network down, EarthScope outage, station
# offline) we fall back to the most recent successfully-downloaded window
# stored on disk. The lamp keeps tremoring from cached data instead of
# going dark. The cache survives reboots, so even a fresh Pi boot during
# a network outage still produces a real seismic signal.
CACHE_DIR       = "/var/lib/pytremor"
CACHE_MAX_FILES = 6         # keep ~6 most-recent windows (~6 hours of seismic)

# --- Mapping ------------------------------------------------------------------
SPEED_FACTOR    = 5.0       # 1.0 = real time; 5.0 = 5x faster
SMOOTH_TAU      = 0.12      # exponential smoothing for per-band brightness (s)
SPECTRUM_WIN    = 1.0       # seconds of seismic data per spectrum frame
BURST_SIGMA     = 4.0       # transient flash threshold: mean + N·σ
BURST_MIN_GAP   = 2.0       # seconds between consecutive flashes
BURST_DURATION  = 0.35      # how long the bright flash holds
BASE_BRIGHTNESS = 2         # minimum % brightness so installation is never fully dark
GAIN            = 12.0      # overall sensitivity multiplier on per-band level
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


def _ensure_cache_dir():
    """Create CACHE_DIR if writable; fall back to a temp dir otherwise.

    Under systemd we get `/var/lib/pytremor` from StateDirectory=. When run
    by hand as a normal user that path may not exist — degrade gracefully
    to a per-user fallback so the resilience layer never crashes the lamp.
    """
    global CACHE_DIR
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        # Probe write access
        with tempfile.NamedTemporaryFile(dir=CACHE_DIR, delete=True):
            pass
        return CACHE_DIR
    except (OSError, PermissionError):
        fallback = os.path.join(tempfile.gettempdir(), "pytremor_cache")
        os.makedirs(fallback, exist_ok=True)
        CACHE_DIR = fallback
        return CACHE_DIR


def _cache_path(net, sta, loc, ch, t_end):
    """Deterministic filename per station + end-time bucket."""
    ts = UTCDateTime(t_end).datetime.strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(CACHE_DIR, f"cache_{net}_{sta}_{loc}_{ch}_{ts}.mseed")


def _save_cache(stream, path):
    """Atomic mseed write so a crash mid-write never leaves a partial file."""
    try:
        tmp = path + ".tmp"
        stream.write(tmp, format="MSEED")
        os.replace(tmp, path)
        # Trim oldest files beyond CACHE_MAX_FILES
        files = sorted(
            glob.glob(os.path.join(CACHE_DIR, "cache_*.mseed")),
            key=os.path.getmtime,
        )
        for old in files[:-CACHE_MAX_FILES]:
            try:
                os.remove(old)
            except OSError:
                pass
    except Exception as e:
        print(f"    -> cache save failed: {type(e).__name__}: {e}", flush=True)


def _load_latest_cache():
    """Return (stream, station_tuple) for the newest cached window, or None.

    Used when every live FDSN station fails. Keeps the installation alive
    on real seismic data instead of falling back to ambient pulse.
    """
    files = sorted(
        glob.glob(os.path.join(CACHE_DIR, "cache_*.mseed")),
        key=os.path.getmtime,
        reverse=True,
    )
    for path in files:
        try:
            st = obspy_read(path)
            # Filename format: cache_NET_STA_LOC_CH_TIMESTAMP.mseed
            stem = os.path.basename(path)[:-len(".mseed")].split("_")
            # stem == ["cache", NET, STA, LOC, CH, TIMESTAMP]
            if len(stem) >= 5:
                _, net, sta, loc, ch = stem[0], stem[1], stem[2], stem[3], stem[4]
            else:
                tr0 = st[0]
                net, sta = tr0.stats.network, tr0.stats.station
                loc, ch  = tr0.stats.location, tr0.stats.channel
            return st, (net, sta, loc, ch), path
        except Exception as e:
            print(f"    -> bad cache file {path}: {e}", flush=True)
            continue
    return None


def _make_client():
    """Try to build an FDSN Client; return None on failure.

    Client(...) does service discovery in __init__, which raises
    FDSNNoServiceException when EarthScope is unreachable. Returning None
    on failure lets the caller fall back to cached data instead of
    crash-looping under systemd.
    """
    try:
        return Client(FDSN_BASE, timeout=60)
    except Exception as e:
        print(f"  ! FDSN client init failed ({type(e).__name__}): {e}",
              flush=True)
        return None


def _replay_cache():
    """Load + process the newest cached miniSEED. Used when no live client.

    Returns (data, sr, station_tuple, "cache") or raises RuntimeError if
    the cache is empty.
    """
    cached = _load_latest_cache()
    if cached is None:
        raise RuntimeError("No FDSN client and no cached miniSEED available.")
    st, station, path = cached
    print(f"  ↻ Replaying cached {station[0]}.{station[1]}."
          f"{station[2]}.{station[3]} from {os.path.basename(path)}",
          flush=True)
    st.merge(fill_value="interpolate")
    st.detrend("demean")
    st.filter("bandpass", freqmin=BANDPASS_MIN, freqmax=BANDPASS_MAX,
              corners=4, zerophase=True)
    tr = st[0]
    data = tr.data.astype(np.float32)
    sr   = float(tr.stats.sampling_rate)
    peak = float(np.max(np.abs(data))) or 1.0
    data = data / peak
    print(f"  Got {len(data)} cached samples @ {sr:.0f} Hz "
          f"({len(data)/sr/60:.1f} min, peak abs={peak:.3g})",
          flush=True)
    return data, sr, station, "cache"


def fetch_stream(client, t_end):
    """Download FETCH_HOURS up to t_end (minus the realtime lag), processed.

    Tries each station in STATIONS in order; returns the first one that
    returns data. If `client` is None (FDSN unreachable at startup) or if
    ALL stations fail, falls back to the most recent successfully-cached
    miniSEED on disk so the lamp keeps tremoring on real seismic data.
    Returns (data, sr, station_tuple, source_label) where source_label is
    "live" or "cache".
    """
    if client is None:
        return _replay_cache()
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
        # Cache the RAW stream BEFORE filtering so a future replay can
        # apply the same processing chain consistently.
        _save_cache(st.copy(), _cache_path(net, sta, loc, ch, t_end))
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
        return data, sr, (net, sta, loc, ch), "live"

    # --- All live stations failed: fall back to local cache ---------------
    print(f"  ! All {len(STATIONS)} stations failed. Last error: {last_err}",
          flush=True)
    cached = _load_latest_cache()
    if cached is None:
        raise RuntimeError(
            f"No station returned data and no cached miniSEED available. "
            f"Last error: {last_err}"
        )
    st, station, path = cached
    print(f"  ↻ Falling back to cached {station[0]}.{station[1]}."
          f"{station[2]}.{station[3]} from {os.path.basename(path)}",
          flush=True)
    st.merge(fill_value="interpolate")
    st.detrend("demean")
    st.filter("bandpass", freqmin=BANDPASS_MIN, freqmax=BANDPASS_MAX,
              corners=4, zerophase=True)
    tr = st[0]
    data = tr.data.astype(np.float32)
    sr   = float(tr.stats.sampling_rate)
    peak = float(np.max(np.abs(data))) or 1.0
    data = data / peak
    print(f"  Got {len(data)} cached samples @ {sr:.0f} Hz "
          f"({len(data)/sr/60:.1f} min, peak abs={peak:.3g})",
          flush=True)
    return data, sr, station, "cache"


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

    cache_dir = _ensure_cache_dir()
    print(f"=== UNDERWATER TREMOR — live seismic LED driver ===")
    print(f"    Stations   : {[s[1] for s in STATIONS]} (in fallback order)")
    print(f"    Cache dir  : {cache_dir}")
    print(f"    Bandpass   : {BANDPASS_MIN}–{BANDPASS_MAX} Hz "
          f"split into {n_bands} log bands")
    print(f"    Outputs    : {LED_PINS}")
    print(f"    Speed      : {SPEED_FACTOR}× real time")
    print(f"    Range      : {MIN_BRIGHTNESS}% – {MAX_BRIGHTNESS}%\n")

    client = _make_client()
    if client is None:
        print("  ! starting in CACHE-ONLY mode; will keep trying to "
              "reconnect to FDSN in the background.", flush=True)

    # Smoothing state per band + running stats for burst detection
    smoothed   = np.full(n_bands, BASE_BRIGHTNESS, dtype=np.float32)
    rms_hist   = deque(maxlen=400)   # ~20s of running history at 20Hz
    last_burst = -1e9
    alpha      = 1.0 - math.exp(-UPDATE_INTERVAL / SMOOTH_TAU)

    # Initial fetch — retry until we get SOMETHING (live or cache).
    # Without this loop, a cold start during an FDSN outage with empty
    # cache would crash → systemd restart → eventually StartLimitBurst
    # kills the service for good.
    while True:
        try:
            data, sr, active, source = fetch_stream(client, UTCDateTime.now())
            break
        except Exception as e:
            print(f"  ! initial fetch failed: {e}", flush=True)
            ambient_pulse(pi, 30)
            if client is None:
                client = _make_client()  # retry FDSN discovery
    print(f"    Active station: {active[0]}.{active[1]}.{active[2]}.{active[3]} ({source})\n")

    win_samples   = int(SPECTRUM_WIN * sr)
    advance_per_frame = (SPEED_FACTOR * UPDATE_INTERVAL) * sr   # seismic samples per visual frame
    # Skip the first 30 s to avoid zero-phase bandpass edge ringing
    cursor = float(max(win_samples, int(30 * sr)))

    while True:
        # Need to refetch?
        seconds_left = (len(data) - cursor) / sr
        if seconds_left < REFETCH_MARGIN:
            # If we're running cache-only, try once more to re-establish
            # the FDSN client; it's cheap and lets us return to live.
            if client is None:
                client = _make_client()
            try:
                data, sr, active, source = fetch_stream(client, UTCDateTime.now())
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
