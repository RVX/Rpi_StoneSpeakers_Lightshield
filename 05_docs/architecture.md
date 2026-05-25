# pyTREMOR lights — system architecture

Open this file with VS Code's Markdown preview (`Ctrl+Shift+V`) to see the rendered diagram.

```mermaid
flowchart TB
    subgraph EARTHSCOPE["🌐 EarthScope FDSN"]
        FDSN[("seismic waveform<br/>service")]
    end

    subgraph PI["🥧 Raspberry Pi  (sjc1 · IPv6 link-local)"]
        PYT["pyTREMOR_lights01.py<br/><i>main loop ~20 fps</i>"]
        FETCH["FDSN fetch<br/>(bandpass 1–18 Hz)"]
        STFT["STFT → 8 log bands<br/>+ centroid + PWM"]
        GPIO["PWM driver →<br/>8 LED channels"]
        LOG[("/var/log/pytremor_lights.log<br/><i>[ts] cur= cen= pwm= bands=...</i>")]
        PYT --> FETCH --> STFT --> GPIO
        STFT --> LOG
    end

    subgraph PC["💻 Windows monitor host"]
        subgraph THREADS["background threads"]
            TAIL["tail_thread<br/><i>ssh + tail -F (raw bytes)</i>"]
            OV["overview_thread<br/><i>FDSN + STFT</i>"]
        end
        Q[/"thread-safe queue"/]
        UI["run_ui (main thread)<br/>FuncAnimation 60 fps · blit"]

        subgraph PANELS["matplotlib panels"]
            BARS["8 LED bars + peak hold<br/>+ LED preview row<br/>(lerp 0.55, alpha-only)"]
            WATER["waterfall + centroid line<br/><i>throttled ¼</i>"]
            OVPLOT["overview spectrogram<br/>+ blue cursor + played overlay<br/><i>throttled ⅓</i>"]
            STATUS["status bar<br/>station / cur / pwm / pass N"]
        end

        GEOM[("%APPDATA%/<br/>pyTREMOR_monitor_geometry.json")]

        TAIL -->|"parsed frames"| Q
        OV   -->|"overview matrix"| Q
        Q    --> UI
        UI   --> BARS & WATER & OVPLOT & STATUS
        UI <-.->|restore/save<br/>on open/close| GEOM
    end

    LOG -.->|SSH IPv6 tail -F| TAIL
    FDSN -.->|obspy FDSNClient<br/>1 h window| OV
    FDSN -.->|obspy on-Pi| FETCH

    classDef pi fill:#21100c,stroke:#ff7c3a,color:#ffd9b0
    classDef pc fill:#0c1a21,stroke:#7ad7ff,color:#b0d9ff
    classDef ext fill:#1a1a1a,stroke:#888,color:#ccc
    class PYT,FETCH,STFT,GPIO,LOG pi
    class TAIL,OV,Q,UI,BARS,WATER,OVPLOT,STATUS,GEOM pc
    class FDSN ext
```

## Data flow

- **Raspberry Pi** independently does the actual work: pulls ~1 h of seismic
  data from EarthScope every cycle, runs STFT into 8 log-spaced bands
  (1–18 Hz), drives the LEDs via PWM, and prints one timestamped frame
  per replay step to `/var/log/pytremor_lights.log`.
- **Monitor host (Windows)** is purely observational. Three background
  threads feed one queue:
  - `tail_thread` — SSHes in over IPv6 link-local and streams the log
    byte-by-byte (raw `Popen(bufsize=0)`, splits on `\r`/`\n`).
  - `overview_thread` — downloads the same FDSN window locally with
    `obspy` and computes the big background spectrogram.
  - `pi_health_thread` — every 30 s runs one short SSH command on the
    Pi that bundles `vcgencmd`-free reads of
    `/sys/class/thermal/thermal_zone0/temp`, `hostname -I`,
    `systemctl is-active pytremor_lights`, `uptime -p`, `/proc/loadavg`,
    `free -m`, `df -h /`, and a 24 h error count from
    `journalctl -u pytremor_lights -p err`. Result is rendered as a
    small multi-line block in the top-right of the window (under the
    SSH destination), with colour cues: muted-grey when healthy,
    orange when there are errors in the last 24 h or CPU ≥ 70 °C, red
    when the service is not `active`, and “pi offline (ssh failed)” if
    the snapshot times out.
- **`run_ui`** drains the queue at 60 fps with `blit=True`. Only the LED
  preview row updates every frame; heavier panels (waterfall, overview,
  status text) have their data refreshed on a throttle but are always
  re-listed for blit so they never flicker.
- **Window geometry** persists across launches via JSON in `%APPDATA%`.
  Delete `%APPDATA%\pyTREMOR_monitor_geometry.json` if the window opens
  off-screen (e.g. after a monitor change).

## Launch

```powershell
$env:PATH += ";C:\Windows\System32\OpenSSH"
python 01_pyTREMOR_lights\pyTREMOR_lights_live_monitor.py
```

Requires system Python with `matplotlib`, `PySide6` (for QtAgg backend),
`numpy`, `scipy`, `obspy`. The repo `.venv` does **not** currently have
these — use the system interpreter at
`C:\Users\ubema\AppData\Local\Programs\Python\Python311\python.exe`.
