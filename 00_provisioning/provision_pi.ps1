<#
.SYNOPSIS
    Provision a fresh Raspberry Pi (already imaged with Raspberry Pi OS Lite,
    user=sjc1) into a pyTREMOR_lights node. Idempotent.

.PARAMETER PiHost
    The new Pi's reachable host string for SSH, e.g.
    "fe80::8aa2:9eff:fed7:9f99%19" or "sjc2.local". The "sjc1@" prefix is
    added automatically.

.PARAMETER NewHostname
    sjc2 / sjc3 / sjc4 / sjc5.

.PARAMETER SeedCacheFromPi
    Optional. SSH host string of an already-provisioned Pi to pre-seed the
    mseed cache from (typically sjc1). If omitted, the new Pi starts with an
    empty cache and waits for its first FDSN fetch.

.EXAMPLE
    .\00_provisioning\provision_pi.ps1 `
        -PiHost 'fe80::aaaa:bbff:fecc:dddd%19' `
        -NewHostname sjc2 `
        -SeedCacheFromPi 'fe80::8aa2:9eff:fed7:9f99%19'
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $PiHost,
    [Parameter(Mandatory)] [ValidatePattern('^sjc[2-5]$')] [string] $NewHostname,
    [string] $SeedCacheFromPi
)

$ErrorActionPreference = 'Stop'
$env:PATH += ';C:\Windows\System32\OpenSSH'
$key = "$env:USERPROFILE\.ssh\id_ed25519_pis"
if (-not (Test-Path $key)) { throw "SSH key not found: $key" }

$repo = Split-Path -Parent $PSScriptRoot
$payload = @(
    "$repo\01_pyTREMOR_lights\pyTREMOR_lights01.py",
    "$repo\01_pyTREMOR_lights\pytremor_lights.service",
    "$repo\01_pyTREMOR_lights\pytremor_lights.logrotate",
    "$PSScriptRoot\bootstrap_pi.sh"
)
foreach ($f in $payload) {
    if (-not (Test-Path $f)) { throw "missing payload file: $f" }
}

$dest = "sjc1@$PiHost"
$sshArgs   = @('-i', $key, '-6', '-o', 'StrictHostKeyChecking=accept-new')
$scpArgs   = @('-i', $key, '-6', '-O', '-o', 'StrictHostKeyChecking=accept-new')

Write-Host "==> Provisioning $dest as $NewHostname" -ForegroundColor Cyan

# 1) ensure remote temp dir
& ssh @sshArgs $dest 'mkdir -p /tmp/pytremor_provision && rm -f /tmp/pytremor_provision/*'
if ($LASTEXITCODE) { throw "ssh prep failed" }

# 2) optional: pull mseed cache from a running Pi into a local tmp dir
$seedRemote = ''
if ($SeedCacheFromPi) {
    $tmpSeed = Join-Path $env:TEMP "pytremor_seed_$([guid]::NewGuid().ToString('N').Substring(0,8))"
    New-Item -ItemType Directory -Path $tmpSeed | Out-Null
    Write-Host "==> Pulling mseed cache from sjc1@$SeedCacheFromPi" -ForegroundColor Cyan
    & scp @scpArgs "sjc1@${SeedCacheFromPi}:/var/lib/pytremor/*.mseed" "$tmpSeed/"
    if ($LASTEXITCODE) {
        Write-Warning "cache pull failed; continuing without seed"
    } else {
        Write-Host "==> Pushing cache to new Pi" -ForegroundColor Cyan
        & scp @scpArgs (Get-ChildItem "$tmpSeed\*.mseed").FullName "${dest}:/tmp/pytremor_provision/"
        if ($LASTEXITCODE) { throw "cache push failed" }
        $seedRemote = '/tmp/pytremor_provision'
    }
}

# 3) ship payload
Write-Host "==> Uploading payload to ${dest}:/tmp/pytremor_provision/" -ForegroundColor Cyan
& scp @scpArgs $payload "${dest}:/tmp/pytremor_provision/"
if ($LASTEXITCODE) { throw "scp payload failed" }

# 4) run bootstrap with sudo
Write-Host "==> Running bootstrap on remote (this can take ~10-20 min on first run for obspy)" -ForegroundColor Cyan
$seedEnv = if ($seedRemote) { "SEED_CACHE_DIR=$seedRemote " } else { '' }
$cmd = "sudo -p 'SUDOPASS: ' env HOSTNAME=$NewHostname ${seedEnv}bash /tmp/pytremor_provision/bootstrap_pi.sh"
& ssh -tt @sshArgs $dest $cmd
if ($LASTEXITCODE) { throw "bootstrap failed (exit $LASTEXITCODE)" }

Write-Host ""
Write-Host "==> Done. To watch the new lamp:" -ForegroundColor Green
Write-Host "   Start-Process -FilePath 'C:\Users\ubema\AppData\Local\Programs\Python\Python311\pythonw.exe' \\"
Write-Host "       -ArgumentList '01_pyTREMOR_lights\pyTREMOR_lights_live_monitor.py','$dest' \\"
Write-Host "       -WorkingDirectory `$PWD"
Write-Host ""
Write-Host "Recommended: ssh in and 'sudo reboot' to finalise hostname/SSH-key changes." -ForegroundColor Yellow
