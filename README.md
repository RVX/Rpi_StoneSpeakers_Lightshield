# Rpi Lux Monitor BH1750 — Pytremor Shield

Lux-adaptive LED controller for Raspberry Pi 4. Reads ambient light from a BH1750 sensor over I2C and uses it to drive a dynamic brightness floor for an artistic LED installation. Built for **Julian Charrière / Studio — Correr, Venice 2026**.

The LED sequence plays a 30-step underwater tremor effect (varying brightness and strobe frequency). When the room is dark the LEDs automatically get brighter so the installation remains visible at all times. When the room is well lit the sequence runs at its original artistic values.

Lux sensor code adapted from [Rpi_Lux_Monitor_BH1750_Oled](https://github.com/RVX/Rpi_Lux_Monitor_BH1750_Oled).

---

## Repository structure

| Folder | Purpose |
|---|---|
| [01_pyTREMOR_lights/](01_pyTREMOR_lights) | **Live volcanic-tremor LED driver.** Streams real seismic data from a remote seismometer (FDSN/ObsPy) and drives 8 PWM LEDs. Includes its own [README](01_pyTREMOR_lights/README.md), visualizer, and example PNG. |
| [02_underwater_lighting/](02_underwater_lighting) | Original artistic underwater-tremor sequences (independent / synchronous / lux-coupled variants) and `__alloff.py` panic-stop. |
| [03_lux_monitor/](03_lux_monitor) | BH1750 lux sensor + OLED display script and matching systemd `.service` units. |
| [04_hardware_tests/](04_hardware_tests) | Standalone bench tests — GPIO sequential, PWM sequential, single-pin LED tests. Useful for verifying wiring. |
| [05_docs/](05_docs) | Setup guides (MONA_SETUP_GUIDE.md, SETUP_GUIDE.md), the mesh-shield wiring PDF, and the shield photo. |

The two main live programs are:

- [01_pyTREMOR_lights/pyTREMOR_lights01.py](01_pyTREMOR_lights/pyTREMOR_lights01.py) — seismic-data-driven LEDs
- [03_lux_monitor/rpi_lux_monitor_bh1750_oled.py](03_lux_monitor/rpi_lux_monitor_bh1750_oled.py) — lux-adaptive brightness floor

The two can be combined on the same Pi (the .service file launches both in turn).

---

## How the Lux-Adaptive Logic Works

```
Room lux < 50   → brightness floor = 45%  (dark room, full boost)
Room lux > 500  → brightness floor = 0%   (bright room, no boost)
in between      → linear interpolation
```

Effective LED brightness at any moment:
```
effective = max(sequence_brightness, lux_brightness_floor)
```

The artistic sequence shape is always preserved — only the dark-valley steps are boosted up. The sensor is polled every 2 seconds in a background thread.

---

## Hardware

- **Raspberry Pi 4** (also works on Pi 3/5)
- **BH1750** light sensor (one or two units)
  - Primary at `0x23` — `ADDR` pin tied to **GND**
  - Optional secondary at `0x5C` — `ADDR` pin tied to **3.3V**
- **LED channels**: GPIO 12, 13, 17, 19, 27, 22 (6 channels, PWM via pigpio)

Typical I2C wiring (BH1750):
```
VCC → 3.3V
GND → GND
SDA → GPIO2 (pin 3)
SCL → GPIO3 (pin 5)
```

---

## 1) Enable I2C

```bash
sudo raspi-config nonint do_i2c 0
```

Verify sensors are detected:

```bash
i2cdetect -y 1
```

Expected:
- `23` — primary BH1750
- `5c` — secondary BH1750 (if connected)

---

## 2) Install pigpio (Debian Trixie / no apt package)

On Raspberry Pi OS Debian 13 (Trixie), pigpio is not available via `apt` and must be built from source:

```bash
sudo apt install -y git build-essential python3-dev
git clone https://github.com/joan2937/pigpio.git
cd pigpio
make
sudo make install
cd ..
sudo python3 setup.py install
```

Create the systemd service for `pigpiod`:

```bash
sudo tee /etc/systemd/system/pigpiod.service > /dev/null << 'EOF'
[Unit]
Description=Pigpio GPIO daemon
After=network.target

[Service]
Type=forking
ExecStart=/usr/local/bin/pigpiod
KillMode=process

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now pigpiod
```

---

## 3) Install smbus2

```bash
pip install smbus2
```

Or via system packages:

```bash
sudo apt install python3-smbus2
```

---

## 4) Copy and Run the Script

```bash
# Copy to Pi (from Windows / macOS)
scp UNDERWATER_LIGTHING_LUX.py correrN@<ip>:/home/correrN/

# Run on the Pi
python3 /home/correrN/UNDERWATER_LIGTHING_LUX.py
```

---

## 5) systemd Service (Automatic Startup)

```bash
sudo tee /etc/systemd/system/pytremor_lux.service > /dev/null << 'EOF'
[Unit]
Description=Pytremor Lux Adaptive LED Controller
After=pigpiod.service
Requires=pigpiod.service

[Service]
Type=simple
User=correr1
WorkingDirectory=/home/correr1
ExecStart=/usr/bin/python3 /home/correr1/UNDERWATER_LIGTHING_LUX.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable pytremor_lux.service
sudo systemctl start pytremor_lux.service
```

---

## 6) Operation and Diagnostics

Check status:

```bash
sudo systemctl status pytremor_lux.service
sudo journalctl -u pytremor_lux.service -f
```

Check I2C devices:

```bash
i2cdetect -y 1
```

Expected addresses:
- `23` — primary BH1750
- `5c` — secondary BH1750 (optional)

---

## 7) Tuning Parameters

All tuneable constants are at the top of `UNDERWATER_LIGTHING_LUX.py`:

| Parameter | Default | Description |
|---|---|---|
| `LUX_DARK_THRESHOLD` | `50.0` | Below this lux → full boost |
| `LUX_BRIGHT_THRESHOLD` | `500.0` | Above this lux → no boost |
| `BOOST_MAX_BRIGHTNESS` | `45` | Floor % in total darkness |
| `BOOST_MIN_BRIGHTNESS` | `0` | Floor % in bright room |
| `SENSOR_POLL_INTERVAL` | `2.0` | Seconds between lux reads |
| `MAX_BRIGHTNESS` | `60` | Hard cap on LED brightness % |
| `LED_PINS` | `[12,13,17,19,27,22]` | GPIO pins for LED channels |

---

## Console Output

While running the script prints a live status line:

```
--- Step 1/30 ---
Target: 25%  Freq: 800 Hz  Duration: 10s  |  Lux: 107.5  Floor: 39%
[11:17:51]  Seq: 25%  Eff: 39%  Freq: 527 Hz  |  Lux: 107.5  Floor: 39%
```

- **Seq** — raw sequence brightness value
- **Eff** — effective brightness after lux floor is applied
- **Lux** — current ambient lux reading (`n/a` if sensor not found)
- **Floor** — current brightness floor derived from lux

---

## Failsafe

On any stop or crash (`Ctrl+C`, `SIGTERM`, exception) all LED channels snap immediately to **25% brightness at 800 Hz**. LEDs are never left fully off.

---

## Deployed Units — Correr 2026

| Hostname | IP Address | Script |
|---|---|---|
| correr1 | 192.168.88.153 | `UNDERWATER_LIGTHING_LUX.py` |
| correr2 | 192.168.88.150 | `UNDERWATER_LIGTHING_LUX.py` |
| correr3 | 192.168.88.152 | `UNDERWATER_LIGTHING_LUX.py` |
| correr4 | 192.168.88.151 | `UNDERWATER_LIGTHING_LUX.py` |

Password: `corrercorrer`

---

## Related

- [Rpi_Lux_Monitor_BH1750_Oled](https://github.com/RVX/Rpi_Lux_Monitor_BH1750_Oled) — original lux sensor monitor (OLED display version)
- [pigpio](https://github.com/joan2937/pigpio) — GPIO library used for hardware PWM
