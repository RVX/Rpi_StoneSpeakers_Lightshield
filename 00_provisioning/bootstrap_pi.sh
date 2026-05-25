#!/usr/bin/env bash
# bootstrap_pi.sh — provision a fresh Raspberry Pi OS Lite install into a
# pyTREMOR_lights node. Idempotent: safe to re-run.
#
# Assumes:
#   * Raspberry Pi OS Lite (Bookworm 64-bit) already imaged on the SD
#   * Default user is `sjc1` (set via rpi-imager "Advanced options")
#   * This script + the payload files are already in /tmp/pytremor_provision/
#   * Caller runs it as: sudo HOSTNAME=sjc2 bash /tmp/pytremor_provision/bootstrap_pi.sh
#
# Required env vars:
#   HOSTNAME   — sjc2 / sjc3 / sjc4 / sjc5
#
# Optional env vars:
#   SEED_CACHE_DIR — if set, copies *.mseed from here into /var/lib/pytremor

set -euo pipefail
trap 'echo "[bootstrap] FAILED at line $LINENO" >&2' ERR

: "${HOSTNAME:?HOSTNAME env var is required (e.g. sjc2)}"

USER_NAME="sjc1"
HOME_DIR="/home/${USER_NAME}"
VENV_DIR="${HOME_DIR}/venv_tremor"
PAYLOAD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -d "${HOME_DIR}" ]]; then
    echo "[bootstrap] FATAL: ${HOME_DIR} does not exist — image was not created with user '${USER_NAME}'" >&2
    exit 1
fi

echo "[bootstrap] === 1/8 hostname -> ${HOSTNAME} ==="
raspi-config nonint do_hostname "${HOSTNAME}"

echo "[bootstrap] === 2/8 regenerate SSH host keys (distinct fingerprint per Pi) ==="
if [[ ! -f /var/lib/pytremor_provision/ssh_keys_regenerated ]]; then
    rm -f /etc/ssh/ssh_host_*
    dpkg-reconfigure openssh-server
    mkdir -p /var/lib/pytremor_provision
    touch /var/lib/pytremor_provision/ssh_keys_regenerated
fi

echo "[bootstrap] === 3/8 apt packages ==="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3-venv python3-pip python3-dev \
    pigpio python3-pigpio \
    logrotate \
    build-essential libatlas-base-dev gfortran \
    libffi-dev libssl-dev

echo "[bootstrap] === 4/8 enable + start pigpiod ==="
systemctl enable pigpiod
systemctl start pigpiod

echo "[bootstrap] === 5/8 python venv at ${VENV_DIR} ==="
if [[ ! -x "${VENV_DIR}/bin/python3" ]]; then
    sudo -u "${USER_NAME}" python3 -m venv --system-site-packages "${VENV_DIR}"
fi
# --system-site-packages lets the venv reuse the apt-installed python3-pigpio
# (saves ~10 min of pigpio C build) while still letting pip add obspy/numpy.
sudo -u "${USER_NAME}" "${VENV_DIR}/bin/pip" install --upgrade pip wheel
# numpy via apt would be ideal but obspy needs a recent one; pip wheel is fine on aarch64.
sudo -u "${USER_NAME}" "${VENV_DIR}/bin/pip" install --upgrade numpy obspy

echo "[bootstrap] === 6/8 deploy app files ==="
install -o "${USER_NAME}" -g "${USER_NAME}" -m 0755 \
    "${PAYLOAD_DIR}/pyTREMOR_lights01.py" "${HOME_DIR}/pyTREMOR_lights01.py"

# /var/log file with correct ownership so systemd `append:` can open it
touch /var/log/pytremor_lights.log
chown "${USER_NAME}:${USER_NAME}" /var/log/pytremor_lights.log
chmod 0644 /var/log/pytremor_lights.log

install -o root -g root -m 0644 \
    "${PAYLOAD_DIR}/pytremor_lights.service" \
    /etc/systemd/system/pytremor_lights.service
install -o root -g root -m 0644 \
    "${PAYLOAD_DIR}/pytremor_lights.logrotate" \
    /etc/logrotate.d/pytremor_lights

echo "[bootstrap] === 7/8 (optional) seed mseed cache ==="
mkdir -p /var/lib/pytremor
chown "${USER_NAME}:${USER_NAME}" /var/lib/pytremor
if [[ -n "${SEED_CACHE_DIR:-}" && -d "${SEED_CACHE_DIR}" ]]; then
    shopt -s nullglob
    seeds=("${SEED_CACHE_DIR}"/*.mseed)
    if (( ${#seeds[@]} )); then
        cp -v "${seeds[@]}" /var/lib/pytremor/
        chown "${USER_NAME}:${USER_NAME}" /var/lib/pytremor/*.mseed
    fi
    shopt -u nullglob
fi

echo "[bootstrap] === 8/8 enable + start pytremor_lights.service ==="
systemctl daemon-reload
systemctl enable pytremor_lights.service
systemctl restart pytremor_lights.service

sleep 3
systemctl status pytremor_lights --no-pager | head -25 || true
echo
echo "[bootstrap] DONE — reboot recommended for hostname/SSH key changes to fully apply:"
echo "[bootstrap]   sudo reboot"
