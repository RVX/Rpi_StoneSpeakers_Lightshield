# pyTREMOR_lights — Volcanic Seismic LED Driver

Live LED installation driven by real volcanic-tremor seismic data.

For the Julian Charrière piece *CORRER VENICE 2026*. Eight underwater LEDs
flicker in response to ground motion measured at a remote seismometer
(default: **IU.HNR.00.BHZ**, Honiara, Solomon Islands), streamed live over
the FDSN web service from EarthScope.

---

## Files

| File | Role |
|---|---|
| [pyTREMOR_lights01.py](pyTREMOR_lights01.py) | Main driver. Runs on the Raspberry Pi, fetches seismic data, drives the 8 PWM-controlled LEDs via pigpio. |
| [pyTREMOR_lights_visualize.py](pyTREMOR_lights_visualize.py) | Offline visualizer. Re-runs the exact same logic against the same data and renders a 5-panel diagnostic PNG for tuning. |
| [run_tremor.sh](run_tremor.sh) | Launcher: kills any prior instance, starts the driver detached under `nohup setsid`, logs to `/tmp/tremor.log`. |
| [probe_fdsn.py](probe_fdsn.py) | Diagnostic — checks which FDSN stations currently return data. |
| [pyTREMOR_lights01_visualization.png](pyTREMOR_lights01_visualization.png) | Latest visualizer output. |

---

## Hardware

| LED | GPIO pin | Band (Hz) | Notes |
|---|---|---|---|
| LED1 | 4  | 1.0 – 1.4  | low-frequency ocean microseism |
| LED2 | 18 | 1.4 – 2.1  | |
| LED3 | 17 | 2.1 – 3.0  | **typically quiet** — real gap between microseism and tremor bands |
| LED4 | 27 | 3.0 – 4.3  | start of volcanic tremor band |
| LED5 | 22 | 4.3 – 6.1  | dominant tremor |
| LED6 | 5  | 6.1 – 8.7  | dominant tremor |
| LED7 | 12 | 8.7 – 12.5 | upper tremor / local quakes |
| LED8 | 13 | 12.5 – 17.9 | local & cultural noise |

Driver: pigpio (hardware-timed PWM, 170–800 Hz visible flicker → smooth).

---

## Signal pipeline

```
FDSN.get_waveforms(60 min)
  └─ merge gaps (interpolate)
  └─ detrend (demean)
  └─ bandpass 1–18 Hz (4-pole zero-phase Butterworth)
  └─ normalise by peak
  └─ playback cursor (5× real-time)
        ├─ FFT on rolling 1 s window
        │     └─ 8 log-spaced bands → RMS per band
        │           └─ median-normalised → ×GAIN → BASE..MAX clip
        │                 └─ exponential smoothing (SMOOTH_TAU)
        │                       └─ PWM duty cycle per LED (0–100 %)
        ├─ spectral centroid (Hz)
        │     └─ linear map to PWM frequency (MIN_FREQ..MAX_FREQ Hz)
        └─ window RMS
              └─ rolling μ, σ over last ~40 s
                    └─ if RMS > μ + BURST_SIGMA·σ AND >BURST_MIN_GAP since last
                          └─ all 8 LEDs → 100 % for BURST_DURATION s
```

---

## Tuning parameters

Edit at the top of [pyTREMOR_lights01.py](pyTREMOR_lights01.py); both the
driver and the visualizer read from there.

### Acquisition

| Constant | Default | Effect |
|---|---|---|
| `STATIONS` | `[HNR, DAV, MAJO, PET, SNZO]` | List of FDSN `(net, sta, loc, ch)` tuples tried in order on data failure. |
| `FETCH_HOURS` | `1.0` | Length of seismic cache. 1 h is the sweet spot — long enough to ride out network hiccups, short enough that the wall-clock and seismic time stay close. |
| `FETCH_LAG_SEC` | `600` | Seconds of delay from "now" — EarthScope's last few minutes are often missing. |
| `BANDPASS_MIN` / `BANDPASS_MAX` | `1.0` / `18.0` Hz | Filter limits; also the bounds of the 8 LED bands. |

### LED behaviour

| Constant | Default | Effect on graph |
|---|---|---|
| `SPEED_FACTOR` | `5.0` | Seismic-seconds per wall-second. ↑ = faster, ↓ = slower & more meditative. |
| `SMOOTH_TAU` | `0.30` s | Per-LED time constant. ↓ (e.g. 0.1) → snappy & jittery. ↑ (e.g. 1.0) → liquid & glacial. |
| `SPECTRUM_WIN` | `1.0` s | FFT analysis window. ↑ → finer frequency resolution but laggier. |
| `BASE_BRIGHTNESS` | `8` | Floor (%) — LEDs never go fully off. |
| `MIN_BRIGHTNESS` / `MAX_BRIGHTNESS` | `0` / `100` | Final clip in %. |
| `GAIN` | `6.0` | Master sensitivity. ↑ = LEDs swing harder per unit of seismic energy. |

### Burst flash

| Constant | Default | Effect on graph |
|---|---|---|
| `BURST_SIGMA` | `3.0` | Threshold for the synchronous all-LED flash, in σ above the rolling mean. ↑ to **4.0–5.0** to make flashes rare & special. ↓ to 2.0 for frequent. |
| `BURST_MIN_GAP` | `2.0` s | Minimum spacing between flashes (replay-seconds). |
| `BURST_DURATION` | `0.18` s | Length of each flash. |

### PWM mapping

| Constant | Default | Effect |
|---|---|---|
| `MIN_FREQUENCY` / `MAX_FREQUENCY` | `170` / `800` Hz | Centroid maps linearly to PWM frequency. 170 Hz = visible flicker on the LED, 800 Hz = smooth. |
| `UPDATE_INTERVAL` | `0.05` s | LED refresh rate (20 Hz). |

---

## How to read the visualizer PNG

### The three independent layers of expression

The installation does **not** map one seismic signal to one LED. Three
separate signals are extracted from the same ground-motion trace and each
drives a different aspect of the light:

| Layer | What you perceive | Driven by | Visible in panel |
|---|---|---|---|
| **Per-LED brightness** | Which LED is brighter than its neighbour | RMS energy *inside that LED's own frequency band* (FFT bin) | 3 |
| **PWM flicker rate** | Whether LEDs look smooth or visibly buzzing | **Spectral centroid** — where the spectrum's energy is centred on average | 4 |
| **Synchronous flashes** | All 8 LEDs spike to 100 % together | **Total RMS** crossing μ + BURST_SIGMA·σ | 5 (and red verticals everywhere) |

So:
- *spectrum shape* → which LED is bright
- *dominant frequency* → how fast they flicker
- *sudden loudness* → all flash together

### Per-LED brightness — the FFT band split

Every 50 ms the script:

1. takes the last 1 s of seismic data
2. runs an FFT → "how much energy is at each frequency?"
3. splits the 1–18 Hz range into 8 log-spaced bins (LED1 = lowest, LED8 = highest)
4. measures RMS energy inside each bin → one number per LED
5. scales (× `GAIN`), smooths (`SMOOTH_TAU`), clips 0–100 %
6. that number is the LED's PWM duty cycle = its brightness

Each LED only "listens" to its own slice of the spectrum. Look at the
spectrogram (panel 2) and panel 3 together: bright orange stripe at 5 Hz
in panel 2 → LED5's trace bulges in panel 3.

### Centroid — the "mood" indicator

The **spectral centroid** is a single number per frame that summarises
where energy is *concentrated*:

$$ \text{centroid} = \frac{\sum_f f \cdot S(f)}{\sum_f S(f)} $$

- Low-frequency microseism dominant → centroid drops toward ~2 Hz
- High-frequency tremor or local quake → centroid climbs toward ~10 Hz
- Balanced → ~5–6 Hz

The centroid does **not** affect any LED's brightness. Instead it is
mapped linearly into the PWM frequency range (`MIN_FREQUENCY` ..
`MAX_FREQUENCY`, default 170–800 Hz), so:

- Low centroid → ~170 Hz PWM → flicker is **visible** (adds a slow buzzy
  texture during quiet seismic moments)
- High centroid → ~800 Hz PWM → flicker too fast for the eye → LEDs look
  **smoothly** lit

This is a second, perpendicular dimension of expression: brightness tells
*intensity*, PWM rate tells *seismic mood* — low and slow vs. high and
snappy.

In panel 4: **green line = centroid** (left axis, Hz), **orange line =
PWM frequency** (right axis, Hz). They are the same shape because PWM is
a linear rescaling of the centroid.

### RMS — the burst detector

RMS (root-mean-square) is the simplest loudness measure: average the
squared amplitude over the last 1 s, take the square root. It ignores
frequency entirely.

$$ \mu = \text{average RMS over last } \sim 40\text{ s} $$
$$ \sigma = \text{standard deviation of RMS over the same window} $$
$$ \text{threshold} = \mu + \text{BURST\_SIGMA} \cdot \sigma $$

In panel 5:
- **White line** — instantaneous RMS (loudness *now*)
- **Yellow line** — μ, the slow baseline of "normal" loudness
- **Pink dashed line** — the trigger threshold

Every time the white line punches through the pink dashed line, a
**synchronous all-LED 100 % flash** fires for `BURST_DURATION` s. Those
are the pink verticals visible in all four lower panels.

RMS answers: *"is this moment statistically unusual compared to the last
~40 s of background?"* If yes → flash.

### Putting it together

A single seismic burst usually causes:
1. several band-LEDs brightening (multiple frequencies excited)
2. centroid jumping → PWM frequency changes
3. RMS spiking above threshold → synchronous flash

But subtler events show in only one layer — e.g. a quiet shift of
dominant frequency from 4 to 7 Hz changes PWM speed without triggering a
flash and without changing total brightness much.

### Panel-by-panel reference

Run the visualizer to render the last 60 min of seismic data through the
current parameter set:

```bash
/home/sjc1/venv_tremor/bin/python3 pyTREMOR_lights_visualize.py
```

Output is `pyTREMOR_lights01_visualization.png`. Five stacked panels:

1. **Bandpass waveform** — the actual ground-motion trace after the 1–18 Hz
   filter. Red verticals mark when burst flashes fire (mapped back to
   real-seismic time). The first ~30 s of the cache is hidden from display
   because the zero-phase Butterworth filter rings violently at the edges
   and a single huge transient would otherwise squash the y-axis. The
   driver skips the same 30 s at playback start.
2. **Spectrogram** (1–18 Hz) — energy at each frequency over time. Cyan
   dotted horizontals show the 8 band edges that map onto LED1–LED8. **Look
   for the dim horizontal stripe around 2.1–3 Hz — that's the gap between
   ocean microseism and volcanic tremor that produces the quiet LED3.**
3. **Per-LED brightness** (stacked, replay-time) — each band's brightness
   over time. Each colour band is one LED; the height is the live PWM duty
   cycle (0–100 %). Burst flashes shown as pink verticals.
4. **Centroid + PWM** — **green line = spectral centroid** (dominant
   frequency in Hz, per FFT frame). **Orange line on twin axis = PWM
   frequency** in Hz that the centroid is mapped to.
5. **RMS + burst threshold** — **white line = instantaneous loudness**,
   **yellow = rolling mean μ**, **pink dashed = μ + BURST_SIGMA·σ
   threshold**. Flashes fire when the white line crosses the pink dashed.
6. **Key panel** at the bottom of the PNG — embedded summary of the three
   layers of expression.

### The LED3 (2.1–3 Hz) flat-line — this is real

It is not a bug. The site IU.HNR (Honiara) consistently shows a quiet
band between:

- **Ocean microseism** (~0.05–0.5 Hz primary, ~0.1–0.3 Hz secondary, with
  some leakage up to ~2 Hz)
- **Volcanic tremor and local seismicity** (~3–10 Hz)

Around 2.1–3 Hz there is genuinely very little energy at this station.
You can confirm visually by looking at the spectrogram panel — there will
be a dark horizontal stripe right where LED3's band sits. If you want
LED3 to participate more, two options:

1. **Switch to linear band edges** in `pyTREMOR_lights01.py` — change
   `np.geomspace(BANDPASS_MIN, BANDPASS_MAX, n_bands + 1)` to
   `np.linspace(...)`. This spreads bands uniformly in Hz and avoids
   packing them around the quiet 2–3 Hz region.
2. **Choose a different station** — pick one with a flatter spectrum.
   `IU.MAJO` (Japan) and `IU.DAV` (Philippines) have stronger cultural and
   microseism activity that fills in this band. Reorder `STATIONS`.

---

## Tuning workflow

```text
edit constants in pyTREMOR_lights01.py
  └─ scp it to the Pi
  └─ run pyTREMOR_lights_visualize.py on the Pi  ── outputs PNG
  └─ scp PNG back, inspect
  └─ when happy, restart the driver:  ssh pi ./run_tremor.sh
```

PowerShell one-liner (from the project workspace):

```powershell
$env:PATH += ";C:\Windows\System32\OpenSSH"
$PI = "sjc1@fe80::8aa2:9eff:fed7:9f99%18"
$KEY = "$env:USERPROFILE\.ssh\id_ed25519_pis"
scp -i $KEY -6 pyTREMOR_lights01.py pyTREMOR_lights_visualize.py "${PI}:/home/sjc1/" ; `
ssh -i $KEY -6 $PI "/home/sjc1/venv_tremor/bin/python3 /home/sjc1/pyTREMOR_lights_visualize.py" ; `
scp -i $KEY -6 "${PI}:/home/sjc1/pyTREMOR_lights01_visualization.png" .
```

To restart the live driver after tuning:

```powershell
ssh -i $KEY -6 $PI "bash /home/sjc1/run_tremor.sh"
```

---

## Failsafes

- `SIGTERM` handler turns LEDs to 25 % / 800 Hz before exit (so a hard
  kill from `systemd` or `pkill` still leaves the installation lit).
- `try/except/finally` failsafe at the same default if the FDSN client
  raises or the loop crashes.
- On FDSN HTTP 204 / 5xx, the driver walks down `STATIONS` until one
  succeeds.
- Refetches data 1 minute before the local cache runs out.
- Only one pigpio script may run at a time — `run_tremor.sh` always
  `pkill`s any prior instance before starting.

---

## Dependencies (on the Pi)

```
sudo apt install pigpiod
sudo systemctl enable --now pigpiod

python3 -m venv --system-site-packages /home/sjc1/venv_tremor
source /home/sjc1/venv_tremor/bin/activate
pip install obspy pigpio numpy scipy matplotlib
```
