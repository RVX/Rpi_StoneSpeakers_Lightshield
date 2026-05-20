# Raspberry Pi Setup Guide — MONA Stone Speakers Exhibition

**Project:** MONA_StoneSpeakers_Lights  
**Venue:** MONA Museum, Hobart, Tasmania  
**Hardware:** Raspberry Pi 4, Debian Trixie (13)  
**SSH key:** `C:\Users\ubema\.ssh\id_ed25519_pis`  
**Last updated:** 2026-05-21

---

## Pi Inventory

| Pi | Hostname | User | Password | Static IP (WiFi) | SSH Key | pigpiod | Service | Status |
|----|----------|------|----------|-----------------|---------|---------|---------|--------|
| 1  | sjc1     | sjc1 | sjcsjc   | 10.22.171.3/20  | ✔       | ✔       | ✔       | ✅ Complete |
| 2  | sjc2     | sjc2 | sjcsjc   | TBD             | ☐       | ☐       | ☐       | ⏳ Pending |
| 3  | sjc3     | sjc3 | sjcsjc   | TBD             | ☐       | ☐       | ☐       | ⏳ Pending |
| 4  | sjc4     | sjc4 | sjcsjc   | TBD             | ☐       | ☐       | ☐       | ⏳ Pending |
| 5  | sjc5     | sjc5 | sjcsjc   | TBD             | ☐       | ☐       | ☐       | ⏳ Pending |

---

## Hardware — GPIO Pin Mapping

Each Pi controls the following GPIO pins. All LED outputs drive external MOSFETs.

### LED Outputs (MOSFET-driven)

| Output | GPIO | BCM Pin | Description |
|--------|------|---------|-------------|
| OUT 1  | GPIO4  | 4  | LED channel 1 via MOSFET |
| OUT 2  | GPIO18 | 18 | LED channel 2 via MOSFET |
| OUT 3  | GPIO17 | 17 | LED channel 3 via MOSFET |
| OUT 4  | GPIO27 | 27 | LED channel 4 via MOSFET |
| OUT 5  | GPIO22 | 22 | LED channel 5 via MOSFET |
| OUT 6  | GPIO5  | 5  | LED channel 6 via MOSFET |
| OUT 7  | GPIO12 | 12 | LED channel 7 via MOSFET (PWM0) |
| OUT 8  | GPIO13 | 13 | LED channel 8 via MOSFET (PWM1) |

### Status / Warning Indicator LEDs

| Label | GPIO | BCM Pin | Description |
|-------|------|---------|-------------|
| L1    | GPIO26 | 26 | Warning / event indicator 1 |
| L2    | GPIO19 | 19 | Warning / event indicator 2 |
| L3    | GPIO6  | 6  | Warning / event indicator 3 |

### I2C — OLED Display

| Signal | GPIO    | Physical Pin | Description |
|--------|---------|--------------|-------------|
| SDA    | GPIO2   | Pin 3        | I2C1 Data — OLED screen |
| SCL    | GPIO3   | Pin 5        | I2C1 Clock — OLED screen |

> I2C address: typically `0x3C` (128×64 SSD1306) or `0x3D`. Verify with `i2cdetect -y 1`.

### BH1750 Lux Sensor (also on I2C bus)

| Signal | GPIO    | Physical Pin | Description |
|--------|---------|--------------|-------------|
| SDA    | GPIO2   | Pin 3        | Shared I2C1 Data |
| SCL    | GPIO3   | Pin 5        | Shared I2C1 Clock |

> Default I2C address: `0x23` (ADDR pin low) or `0x5C` (ADDR pin high).

### UART — DMX Control (testing)

| Signal | GPIO    | Physical Pin | Description |
|--------|---------|--------------|-------------|
| TX     | GPIO14  | Pin 8        | UART0 Transmit — DMX out |
| RX     | GPIO15  | Pin 10       | UART0 Receive — DMX in |

> Enable UART: `sudo raspi-config nonint do_serial_hw 0` then disable serial console: `do_serial_cons 1`.

---

## Network

- **WiFi SSID:** MONA (museum network)
- **Subnet:** 10.22.160.0/20 (mask 255.255.240.0)
- **Gateway:** 10.22.160.1
- **Primary DNS:** 10.1.18.201
- **sjc1 IP:** 10.22.171.3 (DHCP-assigned, consider pinning static)
- **AP Isolation:** ACTIVE — laptop cannot SSH to Pis over WiFi; use **ethernet + mDNS** (`sjcN.local`) during setup
- **Setup method:** Connect ethernet cable laptop ↔ Pi directly; access via `sjcN.local` (mDNS / IPv6 link-local)

---

## SSH Command Templates

```powershell
# Set PATH (run once per PowerShell session)
$env:PATH += ";C:\Windows\System32\OpenSSH"

# First connection (password: sjcsjc) — ethernet only, AP isolation blocks WiFi
ssh -o StrictHostKeyChecking=no sjcN@sjcN.local

# Passwordless SSH after key install
ssh -i "$env:USERPROFILE\.ssh\id_ed25519_pis" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no sjcN@sjcN.local 'COMMAND'

# After reboot: mDNS may resolve to IPv6 link-local — use this form if sjcN.local fails
ssh -i "$env:USERPROFILE\.ssh\id_ed25519_pis" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -6 sjcN@"fe80::XXXX:XXXX:XXXX:XXXX%IFACE" 'COMMAND'
# Get the Pi's IPv6 link-local: ping sjcN.local (shows address in brackets)

# Upload a file
scp -i "$env:USERPROFILE\.ssh\id_ed25519_pis" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no FILE sjcN@sjcN.local:/home/sjcN/FILE
```

---

## Setup Steps (per Pi)

### Step 1 — First Connection & Hostname Check

Connect ethernet cable directly laptop ↔ Pi. Then:

```powershell
$env:PATH += ";C:\Windows\System32\OpenSSH"
ssh -o StrictHostKeyChecking=no sjcN@sjcN.local
# Password: sjcsjc
hostname ; cat /etc/os-release | head -3 ; ip addr show
```

> Expected: Debian GNU/Linux 13 (trixie), Python 3.13.x

### Step 2 — Install SSH Key (enables passwordless access)

```powershell
$pubkey = Get-Content "$env:USERPROFILE\.ssh\id_ed25519_pis.pub"
ssh -o StrictHostKeyChecking=no sjcN@sjcN.local "mkdir -p ~/.ssh && echo '$pubkey' >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys && echo KEY_INSTALLED"
# Password: sjcsjc (last time needed)
```

### Step 3 — Configure Static WiFi IP

```bash
# Check existing WiFi connection name
echo sjcsjc | sudo -S nmcli con show

# Set static IP (replace SSID_NAME and IP)
echo sjcsjc | sudo -S nmcli con mod "SSID_NAME" \
  ipv4.addresses 10.22.171.X/20 \
  ipv4.gateway 10.22.160.1 \
  ipv4.dns 10.1.18.201 \
  ipv4.method manual \
  connection.autoconnect yes
echo sjcsjc | sudo -S nmcli con up "SSID_NAME"
```

### Step 4 — Enable I2C (OLED display + BH1750 lux sensor)

```bash
echo sjcsjc | sudo -S raspi-config nonint do_i2c 0
# Verify (requires i2c-tools):
i2cdetect -y 1
# Expected addresses: 0x23 or 0x5C (BH1750), 0x3C or 0x3D (OLED)
```

### Step 5 — Enable UART (for DMX testing)

```bash
# Enable UART hardware, disable serial console (so GPIO14/15 are free)
echo sjcsjc | sudo -S raspi-config nonint do_serial_hw 0
echo sjcsjc | sudo -S raspi-config nonint do_serial_cons 1
```

### Step 6 — Install Build Dependencies

`pigpio` is **not in the Debian Trixie apt repos** — must be built from source.

```bash
echo sjcsjc | sudo -S apt-get update -qq
echo sjcsjc | sudo -S apt-get install -y git build-essential python3-setuptools i2c-tools
# Note: smbus2 0.4.3 is already present in Debian Trixie system packages
```

### Step 7 — Build & Install pigpio from Source

```bash
cd /tmp
git clone --depth 1 https://github.com/joan2937/pigpio
cd pigpio
make -j4
echo sjcsjc | sudo -S make install
# Verify:
pigpiod --version
```

### Step 8 — Create pigpiod Systemd Service

The `make install` does **not** create a service file — create it manually:

```bash
echo sjcsjc | sudo -S tee /etc/systemd/system/pigpiod.service << 'EOF'
[Unit]
Description=Pigpio daemon
After=network.target

[Service]
Type=forking
ExecStart=/usr/local/bin/pigpiod

[Install]
WantedBy=multi-user.target
EOF

echo sjcsjc | sudo -S systemctl daemon-reload
echo sjcsjc | sudo -S systemctl enable pigpiod
echo sjcsjc | sudo -S systemctl start pigpiod
echo sjcsjc | sudo -S systemctl status pigpiod --no-pager | head -5
```

### Step 9 — Deploy Script

```powershell
# From Windows terminal:
scp -i "$env:USERPROFILE\.ssh\id_ed25519_pis" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no `
  UNDERWATER_LIGTHING_LUX.py sjcN@sjcN.local:/home/sjcN/UNDERWATER_LIGTHING_LUX.py
```

### Step 10 — Install pytremor_lux Systemd Service

```bash
echo sjcsjc | sudo -S tee /etc/systemd/system/pytremor_lux.service << 'EOF'
[Unit]
Description=Pytremor Lux Adaptive LED Controller
After=network.target pigpiod.service
Requires=pigpiod.service

[Service]
Type=simple
User=sjcN
WorkingDirectory=/home/sjcN
ExecStart=/usr/bin/python3 /home/sjcN/UNDERWATER_LIGTHING_LUX.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo sjcsjc | sudo -S systemctl daemon-reload
echo sjcsjc | sudo -S systemctl enable pytremor_lux
echo sjcsjc | sudo -S systemctl start pytremor_lux
echo sjcsjc | sudo -S systemctl status pytremor_lux --no-pager | head -5
```

### Step 11 — Reboot & Verify

```bash
echo sjcsjc | sudo -S reboot
```

Wait ~35 seconds. If `sjcN.local` fails to resolve after reboot, use IPv6 link-local:

```powershell
# Find IPv6 link-local address:
ping sjcN.local  # address shown in brackets, e.g. fe80::8aa2:9eff:fed7:9f99%18

# SSH via IPv6:
ssh -i "$env:USERPROFILE\.ssh\id_ed25519_pis" -o IdentitiesOnly=yes -6 sjcN@"fe80::ADDR%IFACE" `
  'systemctl is-active pigpiod ; systemctl is-active pytremor_lux ; uptime'
```

Expected output:
```
active
active
up X min, ...
```

---

## Useful Commands

### Check which script is running
```bash
ps aux | grep python3 | grep -v grep
```

### Scan I2C bus (verify OLED + lux sensor)
```bash
i2cdetect -y 1
# Expect: 0x23 or 0x5C = BH1750 lux sensor
#         0x3C or 0x3D = SSD1306 OLED display
```

### Quick GPIO test — light all 8 LED outputs
```python
import pigpio
PINS = [4, 18, 17, 27, 22, 5, 12, 13]
pi = pigpio.pi()
for p in PINS:
    pi.set_PWM_frequency(p, 800)
    pi.set_PWM_dutycycle(p, 255)
# Ctrl+C to stop, then pi.stop()
```

### Quick GPIO test — blink warning LEDs
```python
import pigpio, time
WARN = [26, 19, 6]  # L1, L2, L3
pi = pigpio.pi()
for _ in range(5):
    for p in WARN: pi.write(p, 1)
    time.sleep(0.5)
    for p in WARN: pi.write(p, 0)
    time.sleep(0.5)
pi.stop()
```

### Run a script temporarily (for testing)
```bash
echo sjcsjc | sudo -S systemctl stop pytremor_lux
python3 /home/sjcN/UNDERWATER_LIGTHING_LUX.py
# Ctrl+C to stop, then:
echo sjcsjc | sudo -S systemctl start pytremor_lux
```

### Check service logs
```bash
echo sjcsjc | sudo -S systemctl status pytremor_lux.service --no-pager 2>&1 | tail -10
```

### Stop a runaway python process (without killing SSH)
```bash
pkill -f UNDERWATER_LIGTHING_LUX.py
# Do NOT use: pkill -9 -f python3  (kills SSH session too)
```

### Shut down safely
```bash
echo sjcsjc | sudo -S shutdown now
```

---

## Progress Log

### sjc1
- [x] Step 1 — First connection (via ethernet + sjc1.local)
- [x] Step 2 — SSH key installed
- [ ] Step 3 — Static WiFi IP (currently DHCP at 10.22.171.3)
- [x] Step 4 — I2C enabled
- [x] Step 5 — UART enabled
- [x] Step 6 — Build dependencies installed
- [x] Step 7 — pigpio built from source
- [x] Step 8 — pigpiod service created & running
- [x] Step 9 — Script deployed (UNDERWATER_LIGTHING_LUX.py)
- [x] Step 10 — pytremor_lux service installed & enabled
- [x] Step 11 — Verified after reboot: both services `active` ✔

### sjc2
- [ ] Steps 1–11

### sjc3
- [ ] Steps 1–11

### sjc4
- [ ] Steps 1–11

### sjc5
- [ ] Steps 1–11

---

## Known Issues & Lessons Learned

- **AP isolation at MONA** — museum WiFi blocks SSH between clients; always use ethernet + `sjcN.local` (mDNS) for setup
- **mDNS after reboot** — `sjcN.local` may resolve to IPv6 link-local `fe80::...%IFACE`; use `ping sjcN.local` to get the address, then `ssh -6 sjcN@"fe80::ADDR%IFACE"`
- **pigpio not in Debian Trixie apt** — must build from source (`git clone https://github.com/joan2937/pigpio`)
- **`make install` does not create systemd service** — must create `/etc/systemd/system/pigpiod.service` manually
- **smbus2 already present** in Debian Trixie as a system package (0.4.3) — `pip install smbus2` is a no-op
- **`pkill -9 -f python3` kills SSH session** — use `pkill -f SCRIPTNAME` instead
- **sudo inline:** `echo sjcsjc | sudo -S COMMAND`
- **pip on Debian Trixie** requires `--break-system-packages` for user installs outside venv
