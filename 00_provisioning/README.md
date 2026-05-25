# Provisioning kit

Turn a freshly imaged Raspberry Pi OS Lite SD card into a
`pyTREMOR_lights` node without cloning a 64 GB image.

See [`../05_docs/MULTI_PI_SETUP.md`](../05_docs/MULTI_PI_SETUP.md) for the
overall multi-Pi guide; this directory holds the automation.

## Files

| File | Purpose |
|---|---|
| `bootstrap_pi.sh` | Runs **on the Pi** under sudo. Installs apt deps, creates venv, deploys app + service + logrotate, optionally seeds the mseed cache, starts the service. Idempotent. |
| `provision_pi.ps1` | Runs **on the laptop**. SCPs the payload + bootstrap to the new Pi and invokes it over SSH, optionally pulling the cache from an already-running Pi first. |

## Prereqs (one-time per new SD)

1. In **Raspberry Pi Imager** → choose **Raspberry Pi OS Lite (64-bit)** → click the gear icon and set:
   - Hostname: leave default (the bootstrap renames it)
   - **Username: `sjc1`** (must match — the systemd unit hardcodes this)
   - Password or public-key auth (paste `~/.ssh/id_ed25519_pis.pub` content)
   - Enable SSH
   - WiFi only if needed; Ethernet is preferred for provisioning.
2. Write to the SD, eject, boot the new Pi with Ethernet attached.
3. Wait ~30 s, find its IPv6 link-local on your laptop:
   ```powershell
   Get-NetNeighbor -InterfaceIndex 19 -AddressFamily IPv6 |
       Where-Object { $_.IPAddress -like 'fe80::*' } |
       Format-Table IPAddress, LinkLayerAddress, State
   ```
   (replace `19` with your USB-Ethernet `ifIndex` from `Get-NetAdapter`).

## Run provisioning

```powershell
.\00_provisioning\provision_pi.ps1 `
    -PiHost 'fe80::NEW:IPV6:HERE%19' `
    -NewHostname sjc2 `
    -SeedCacheFromPi 'fe80::8aa2:9eff:fed7:9f99%19'
```

First run is slow (~10–20 min) because `pip install obspy` pulls + builds a
lot of scientific Python on the Pi. Subsequent re-runs (e.g. to redeploy
the script) take a few seconds.

After it returns, `ssh sjc1@fe80::…%19 sudo reboot` to finalise the new
hostname and SSH host keys.

## Open a monitor for the new lamp

```powershell
Start-Process -FilePath "C:\Users\ubema\AppData\Local\Programs\Python\Python311\pythonw.exe" `
    -ArgumentList "01_pyTREMOR_lights\pyTREMOR_lights_live_monitor.py","sjc1@fe80::NEW:IPV6:HERE%19" `
    -WorkingDirectory $PWD
```
