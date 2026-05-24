# Pi resilience deployment guide

Steps to roll the three resilience changes onto the production Pi
(`sjc1@fe80::8aa2:9eff:fed7:9f99%19`).

## 1. Push the new `pyTREMOR_lights01.py` to the Pi

From the laptop:

```powershell
$env:PATH += ";C:\Windows\System32\OpenSSH"
$pi = "sjc1@fe80::8aa2:9eff:fed7:9f99%19"
$key = "$env:USERPROFILE\.ssh\id_ed25519_pis"

scp -i $key -6 `
    "01_pyTREMOR_lights\pyTREMOR_lights01.py" `
    "${pi}:/home/sjc1/pyTREMOR_lights01.py"
```

## 2. Install the systemd unit + create the cache dir

```powershell
scp -i $key -6 `
    "01_pyTREMOR_lights\pytremor_lights.service" `
    "${pi}:/tmp/pytremor_lights.service"

ssh -i $key -6 $pi @'
sudo install -o root -g root -m 0644 /tmp/pytremor_lights.service /etc/systemd/system/pytremor_lights.service
sudo systemctl daemon-reload
sudo systemctl enable pytremor_lights
sudo systemctl restart pytremor_lights
sudo systemctl status pytremor_lights --no-pager | head -25
ls -la /var/lib/pytremor/ 2>/dev/null || echo "(StateDirectory created on first start)"
'@
```

`StateDirectory=pytremor` makes systemd create `/var/lib/pytremor` with
`sjc1:sjc1` ownership automatically — no manual `mkdir`/`chown` needed.

## 3. Install logrotate config

```powershell
scp -i $key -6 `
    "01_pyTREMOR_lights\pytremor_lights.logrotate" `
    "${pi}:/tmp/pytremor_lights.logrotate"

ssh -i $key -6 $pi @'
sudo install -o root -g root -m 0644 /tmp/pytremor_lights.logrotate /etc/logrotate.d/pytremor_lights
sudo logrotate -d /etc/logrotate.d/pytremor_lights 2>&1 | tail -20   # dry run
'@
```

## 4. Verify

```powershell
ssh -i $key -6 $pi @'
echo "--- service ---"
systemctl is-active pytremor_lights
systemctl is-enabled pytremor_lights
echo "--- recent log ---"
sudo tail -5 /var/log/pytremor_lights.log
echo "--- cache contents ---"
ls -la /var/lib/pytremor/ 2>/dev/null
'@
```

After ~1 hour you should see the first `cache_IU_*.mseed` appear (one
per successful FDSN fetch). To force a fallback test, temporarily block
outbound 443 on the Pi (`sudo iptables -A OUTPUT -p tcp --dport 443 -j REJECT`),
wait for the next refetch cycle (~55 min), and confirm the log shows
`↻ Falling back to cached IU.…`. Don't forget to flush iptables after
the test.

## What changed vs. the pre-resilience build

| Layer    | Before                                | After                                                          |
|----------|---------------------------------------|----------------------------------------------------------------|
| Process  | nohup, dies silently on crash         | systemd `Restart=always`, `RestartSec=10`, start-limit guarded |
| FDSN     | 5-station fallback, RuntimeError if all fail | + local miniSEED cache (`/var/lib/pytremor`), keeps tremoring on disk data |
| Logs     | unbounded `/var/log/pytremor_lights.log` (would eventually fill SD) | rotated daily, 7 day keep, gzipped, `copytruncate` so monitor SSH tail survives |
| Monitor  | `ssh tail` thread quietly exits on disconnect | exponential-backoff reconnect (5→60 s), status surfaced in UI |
| Monitor  | empty overview panel for ~15 s on launch     | last successful overview restored from `%APPDATA%/pyTREMOR_last_overview.npz` |
