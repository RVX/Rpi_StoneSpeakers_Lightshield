# Raspberry Pi Setup Guide — Underwater Lighting (CORRER 2026)

Tested on: **Raspberry Pi 4, Debian Trixie (13)**  
Script: `UNDERWATER_LIGTHING.py`  
GPIO pins used: 12, 13, 17, 19, 27, 22

---

## Deployed Units

| Hostname | IP Address | Status |
|----------|------------|--------|
| correr1  | 192.168.88.153 | running |
| correr2  | 192.168.88.150 | running |
| correr3  | 192.168.88.152 | running |
| correr4  | 192.168.88.151 | running |

Password for all units: `corrercorrer`

---

## Prerequisites (on your Windows machine)

- OpenSSH available at `C:\Windows\System32\OpenSSH`
- The script `UNDERWATER_LIGTHING.py` in this folder

---

## Step 1 — Connect to the Pi via SSH

Open a PowerShell terminal and run:

```powershell
$env:PATH += ";C:\Windows\System32\OpenSSH"
ssh correr2@<PI_IP_ADDRESS>
```

Password: `corrercorrer`

> To find the Pi's IP address, check your router or run `hostname -I` directly on the Pi's terminal.

---

## Step 2 — Install build dependencies

`pigpio` is **not available via apt on Debian Trixie** — it must be built from source.

First, install the required build tools:

```bash
echo corrercorrer | sudo -S apt install -y git build-essential
```

---

## Step 3 — Build and install pigpio from source

```bash
cd /tmp
rm -rf pigpio
git clone https://github.com/joan2937/pigpio.git
cd pigpio
make
echo corrercorrer | sudo -S make install
```

This installs:
- `/usr/local/bin/pigpiod` — the daemon
- `/usr/local/bin/pigs` — command-line tool
- Python bindings (importable as `import pigpio`)

---

## Step 4 — Create the pigpiod systemd service

This makes `pigpiod` start automatically on every boot.

```bash
echo corrercorrer | sudo -S true && printf '[Unit]\nDescription=Pigpio daemon\nAfter=network.target\n\n[Service]\nType=forking\nExecStart=/usr/local/bin/pigpiod\nPIDFile=/var/run/pigpio.pid\nRestart=on-failure\nTimeoutStopSec=5\nKillMode=process\n\n[Install]\nWantedBy=multi-user.target\n' | sudo tee /etc/systemd/system/pigpiod.service
```

Then enable and start it:

```bash
echo corrercorrer | sudo -S systemctl daemon-reload
echo corrercorrer | sudo -S systemctl enable --now pigpiod
```

Verify it is running:

```bash
systemctl status pigpiod
```

You should see: `Active: active (running)`

---

## Step 5 — Copy the lighting script to the Pi

Run this from your **Windows PowerShell** (not on the Pi):

```powershell
$env:PATH += ";C:\Windows\System32\OpenSSH"
scp -o StrictHostKeyChecking=no "UNDERWATER_LIGTHING.py" correrN@<PI_IP_ADDRESS>:/home/correrN/UNDERWATER_LIGTHING.py
```

Password: `corrercorrer`

---

## Step 6 — Run the script

On the Pi terminal:

```bash
python3 /home/correrN/UNDERWATER_LIGTHING.py
```

You should see output like:
```
--- Step 1/30 ---
Target Brightness: 25%, Target Frequency: 800Hz, Duration: 10s
[HH:MM:SS]: Brightness: 25%, Frequency: 526Hz
```

Press `Ctrl+C` to stop.

---

## Optional — Auto-start the script on boot

If you want the lighting script to run automatically on reboot (no keyboard/screen needed), create a second systemd service:

```bash
echo corrercorrer | sudo -S true && printf '[Unit]\nDescription=Underwater Lighting Script\nAfter=pigpiod.service\nRequires=pigpiod.service\n\n[Service]\nExecStart=/usr/bin/python3 /home/correrN/UNDERWATER_LIGTHING.py\nRestart=always\nUser=correrN\n\n[Install]\nWantedBy=multi-user.target\n' | sudo tee /etc/systemd/system/underwater-lights.service
echo corrercorrer | sudo -S systemctl daemon-reload
echo corrercorrer | sudo -S systemctl enable --now underwater-lights.service
```

Check it:
```bash
systemctl status underwater-lights.service
```

---

## Quick checklist for each new Pi

- [ ] SSH into Pi
- [ ] `apt install git build-essential`
- [ ] Clone + build + install pigpio from source
- [ ] Create and enable `pigpiod.service`
- [ ] SCP `UNDERWATER_LIGTHING.py` to `/home/correrN/`
- [ ] Run `python3 /home/correrN/UNDERWATER_LIGTHING.py`
- [ ] (Optional) Create `underwater-lights.service` for auto-start on boot
