# Multi-Pi deployment guide (5 lamps, one room)

The `pyTREMOR_lights01.py` service is designed to run autonomously on each
Pi. The first Pi (`sjc1`) is the reference image; Pis 2–5 are clones with
per-host tweaks. This guide captures the steps that must happen on every
new Pi and the audit notes behind each one.

## 1. Clone the SD card

`dd` (or Win32DiskImager / `rpi-imager` "use existing image") the working
SD card from `sjc1` to a fresh card. The clone carries over:

- venv at `/home/sjc1/venv_tremor` (obspy, numpy, pigpio)
- `/home/sjc1/pyTREMOR_lights01.py`
- systemd unit `/etc/systemd/system/pytremor_lights.service`
- logrotate rule `/etc/logrotate.d/pytremor_lights`
- pigpiod enabled
- the cache directory `/var/lib/pytremor/` (so the new Pi has data to
  replay if FDSN is unreachable on first boot)

## 2. Per-Pi steps after first boot

```bash
# 2.1 unique hostname (sjc1..sjc5)
sudo raspi-config nonint do_hostname sjc2

# 2.2 regenerate SSH host keys so each Pi has a distinct fingerprint
sudo rm -f /etc/ssh/ssh_host_*
sudo dpkg-reconfigure openssh-server

# 2.3 confirm NTP is synchronized (required for FDSN time windows)
timedatectl status | grep -E 'synchronized|NTP'

sudo reboot
```

After reboot, `systemctl status pytremor_lights` should show the service
running on its own. The first fetch within ~10 s either comes back as
`(live)` or — if FDSN/the LAN is down — falls back to `(cache)`.

## 3. Keep `sjc1` as the service user

The systemd unit hardcodes `User=sjc1`, `WorkingDirectory=/home/sjc1`,
and the script path `/home/sjc1/pyTREMOR_lights01.py`. The simplest
maintenance story is: **do not rename the account per Pi.** Only the
hostname differs. SSH login from the laptop becomes:

```
ssh sjc1@sjc2.local
ssh sjc1@sjc3.local
…
```

## 4. Resilience that is already in place

| Risk | Mitigation (already in code) |
|---|---|
| Partial mseed on power loss | atomic write via `.tmp` + `os.replace` |
| Corrupt newest cache file | `_load_latest_cache` iterates, skips bad |
| FDSN outage at boot | `_make_client()` returns `None`, `_replay_cache()` runs |
| FDSN outage mid-run | refetch retries; `_make_client()` re-tried each cycle |
| All 5 stations no-data | falls through to cache |
| Crash loop | `Restart=always`, `RestartSec=10`, `StartLimitBurst=20/600s` |
| Log growth | logrotate daily, keep 7 compressed copies, `copytruncate` |
| PWM stuck on at shutdown | `SIGTERM` + `TimeoutStopSec=15` runs `failsafe()` |
| State dir permissions | `StateDirectory=pytremor` (owned by `User=sjc1`) |
| pigpiod not up first | `Requires=pigpiod.service` + `After=` |

## 5. Cold-start outage edge case

If a Pi boots for the first time with:

- no internet AND
- an empty `/var/lib/pytremor/`

…the lamp pulses gently (`ambient_pulse`) and retries the FDSN client
every 30 s instead of crash-looping. As soon as FDSN comes back, the
service fetches an hour of data and the lamp wakes up. **Pre-seeding
the cache from `sjc1` avoids this entirely:**

```powershell
# laptop, from this workspace
$key = "$env:USERPROFILE\.ssh\id_ed25519_pis"
scp -i $key -6 'sjc1@[fe80::8aa2:9eff:fed7:9f99%19]:/var/lib/pytremor/cache_*.mseed' .\tmp_cache\
# then upload to each new Pi:
scp -i $key .\tmp_cache\cache_*.mseed sjc1@sjc2.local:/tmp/
ssh sjc1@sjc2.local 'sudo install -o sjc1 -g sjc1 -m 644 /tmp/cache_*.mseed /var/lib/pytremor/'
```

## 6. FDSN politeness

Five Pis fetching once per hour from the same NAT'd IP = ~120 requests
per day total — well under EarthScope's published throttling. We
intentionally do **not** stagger the fetches: synchronised requests mean
all five lamps run the same one-hour seismic window in lockstep, which
is the desired installation behaviour.

## 7. Monitoring the lamps

`pyTREMOR_lights_live_monitor.py` displays the Pi identifier (`sjc1`,
`sjc2`, …) prominently in the top-left of its window plus in the OS
window title. To monitor several Pis at once, launch one instance per
host on the laptop:

```powershell
# one terminal per lamp
python 01_pyTREMOR_lights\pyTREMOR_lights_live_monitor.py sjc1@sjc1.local
python 01_pyTREMOR_lights\pyTREMOR_lights_live_monitor.py sjc1@sjc2.local
…
```

Window positions are persisted per-process, so once arranged on screen
they reopen in the same layout.

Each monitor window also shows two telemetry blocks in its header,
refreshed automatically:

**Right — hardware/system snapshot** (one SSH round-trip every 30 s):

```
svc:  active        thr: ok
ip:   192.168.1.42  rtt: 0.42s
temp: 52.3°C
load: 0.45          mem: 234/3848M
disk: 12%           up:  3 hours, 12 minutes
err24h: 0
```

- `thr` decodes `vcgencmd get_throttled`: `ok` / `under-volt (latched)`
  / `throttled NOW` etc. so you can spot a flaky PSU or thermal cap
  that wouldn't otherwise show up in service logs.
- `rtt` is the SSH round-trip time for the snapshot — useful when
  debugging IPv6 link-local quirks.
- `err24h` counts `journalctl -u pytremor_lights -p err` over 24 h.

**Left — operational state of the lamp** (derived from the log stream,
no extra SSH calls):

```
last log:    1.2s ago
fdsn:        live   ·  next fetch in 23m42s
reconnects:  0
```

- `last log` ticks live; goes **orange after 2 min** and **red after
  5 min** of silence — the fastest possible alert that a Pi has frozen.
- `fdsn` is one of `live` / `cache` / `cache (fallback)` /
  `failed (using cache)` / `fetching`. `live` = streaming from
  EarthScope, `cache (fallback)` = FDSN failed mid-run, `failed` = both
  failed and the lamp is replaying stale data.
- `next fetch in` counts down to the Pi's next hourly FDSN download,
  derived from the cached window duration and current replay position.
- `reconnects` is how many times this monitor session has had to
  re-spawn the ssh tail — spikes indicate an unstable link.

Colour cues across both blocks:

| Condition                                                  | Colour |
|------------------------------------------------------------|--------|
| Healthy                                                    | muted grey |
| Errors in last 24 h, CPU ≥ 70 °C, throttling latched,      | orange |
| FDSN in fallback, or > 2 min log silence                   |        |
| `svc != active`, throttling **now**, FDSN failed, or       | red    |
| > 5 min log silence                                        |        |
| SSH snapshot failed (Pi unreachable)                       | orange, text reads `pi offline (ssh failed)` |

The telemetry SSH call uses `BatchMode=yes` and an 8 s connect timeout,
so a missing Pi cannot stall the UI.

## 8. SD card backup

Software resilience cannot protect the SD card itself against bit-rot
or write fatigue. Recommended: every few months, image the cards back
to disk on the laptop (`Win32DiskImager` → Read) and keep one good
copy per Pi.
