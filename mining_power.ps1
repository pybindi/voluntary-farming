param(
    [Parameter(Mandatory=$true)][int]$OwnerPid,
    [Parameter(Mandatory=$true)][string]$GpuUuid,
    [Parameter(Mandatory=$true)][double]$TargetWatts,
    [Parameter(Mandatory=$true)][double]$OriginalWatts,
    [Parameter(Mandatory=$true)][string]$SessionDirectory
)
# Elevated, short-lived GPU power helper. Never launches the miner.
$ErrorActionPreference = 'Stop'
$smi = Join-Path $env:windir 'System32\nvidia-smi.exe'
$changed = $false
$restored = $false
$owner = $null
function Publish-State([string]$name, [string]$message) {
    $path = Join-Path $SessionDirectory ($name + '.json')
    $text = @{ state = $name; message = $message } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText(($path + '.tmp'), $text)
    Move-Item -LiteralPath ($path + '.tmp') -Destination $path -Force
}
function Read-Power {
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo.FileName = $smi
    $process.StartInfo.Arguments = "-i $GpuUuid --query-gpu=power.limit --format=csv,noheader,nounits"
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    $process.StartInfo.CreateNoWindow = $true
    [void]$process.Start()
    if (-not $process.WaitForExit(3000)) { $process.Kill(); throw 'GPU query timed out' }
    if ($process.ExitCode -ne 0) { throw 'GPU query failed' }
    return [double]::Parse($process.StandardOutput.ReadToEnd().Trim(), [Globalization.CultureInfo]::InvariantCulture)
}
function Set-Power([double]$watts) {
    $value = $watts.ToString([Globalization.CultureInfo]::InvariantCulture)
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo.FileName = $smi
    $process.StartInfo.Arguments = "-i $GpuUuid -pl $value"
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    $process.StartInfo.CreateNoWindow = $true
    [void]$process.Start()
    if (-not $process.WaitForExit(3000)) { $process.Kill(); throw 'GPU power write timed out' }
    $details = $process.StandardOutput.ReadToEnd() + $process.StandardError.ReadToEnd()
    if ($process.ExitCode -ne 0) { throw "GPU power write failed (code $($process.ExitCode)): $details" }
    if ([Math]::Abs((Read-Power) - $watts) -gt 0.5) { throw 'GPU power verification failed' }
}
try {
    if ($GpuUuid -notmatch '^GPU-[0-9a-fA-F-]{36}$') { throw 'Invalid GPU UUID' }
    if ($TargetWatts -lt 1 -or $TargetWatts -gt 123) { throw 'Invalid target power' }
    if ($OriginalWatts -lt 1 -or $OriginalWatts -gt 145) { throw 'Invalid original power' }
    $owner = [Diagnostics.Process]::GetProcessById($OwnerPid)
    # Acquire and retain a real process handle, rather than polling a reusable PID.
    $null = $owner.Handle
    if ($owner.HasExited) { throw 'Controller already exited' }
    if (Test-Path -LiteralPath (Join-Path $SessionDirectory 'stop')) { throw 'Session cancelled before start' }
    if ([Math]::Abs((Read-Power) - $OriginalWatts) -gt 0.5) { throw 'Original power changed before UAC approval' }
    $changed = $true
    Set-Power $TargetWatts
    if ($owner.HasExited -or (Test-Path -LiteralPath (Join-Path $SessionDirectory 'stop'))) { throw 'Session cancelled during setup' }
    Publish-State 'ready' 'Power limit applied and verified'
    $deadline = [DateTime]::UtcNow.AddHours(9)
    while (-not $owner.HasExited -and -not (Test-Path -LiteralPath (Join-Path $SessionDirectory 'stop')) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }
    if ($owner.HasExited) {
        # Allow the OS kill-on-close job to finish the workload before raising power.
        Start-Sleep -Seconds 5
    } elseif ([DateTime]::UtcNow -ge $deadline) {
        # Do not raise the cap if a hung controller may still own a live workload.
        throw 'Power helper deadline reached; controller still alive, recovery deferred'
    }
    Set-Power $OriginalWatts
    $restored = $true
    Publish-State 'restored' 'Original power limit restored and verified'
} catch {
    Publish-State 'error' $_.Exception.Message
} finally {
    # On preflight/write failure, the miner has never been authorized to start.
    if ($changed -and -not $restored -and -not (Test-Path -LiteralPath (Join-Path $SessionDirectory 'ready.json'))) {
        try {
            Set-Power $OriginalWatts
            Publish-State 'restored' 'Original power restored after setup failure'
        } catch { Publish-State 'error' ('Recovery failed: ' + $_.Exception.Message) }
    }
    if ($null -ne $owner) { $owner.Dispose() }
}
