param(
    [string]$DriveLetter = "R",
    [int]$SizeMB = 192,
    [string]$PythonExe = '',
    # wal_corruption runs first on purpose. disk_full consumes the entire
    # volume and only returns the space in a `finally`, so putting it last
    # means no drill ever starts on a volume another drill has filled. Both
    # are pure Python and need no kernel facility Windows lacks; fsync_stall
    # is deliberately absent because Windows has no dm-delay equivalent and a
    # FUSE shim cannot back SQLite's -shm mmap (see
    # docs/OFFHOST_FAULT_DRILL_FEASIBILITY_20260812_ZH.md).
    [string[]]$Drills = @("wal_corruption", "disk_full")
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$artifactRoot = Join-Path $repoRoot "artifacts\windows_ntfs_vhd"
New-Item -ItemType Directory -Force -Path $artifactRoot | Out-Null
$artifactRoot = (Resolve-Path $artifactRoot).Path
$vhdPath = Join-Path $artifactRoot "ibems-ntfs-drill.vhd"
$diskpartCreate = Join-Path $artifactRoot "diskpart-create.txt"
$diskpartDetach = Join-Path $artifactRoot "diskpart-detach.txt"
$fenceDir = Join-Path $artifactRoot "fence"
New-Item -ItemType Directory -Force -Path $fenceDir | Out-Null

if ($DriveLetter -notmatch '^[E-Z]$') {
    throw "DriveLetter must be one unused letter from E through Z"
}
if (Test-Path "${DriveLetter}:\") {
    throw "Drive ${DriveLetter}: is already in use"
}
if ($SizeMB -lt 128 -or $SizeMB -gt 512) {
    throw "SizeMB must be between 128 and 512"
}
if (Test-Path $vhdPath) {
    throw "Refusing to overwrite existing VHD: $vhdPath"
}
if (-not $Drills -or $Drills.Count -eq 0) {
    throw "Drills must name at least one drill"
}
$supportedDrills = @("disk_full", "wal_corruption")
foreach ($drill in $Drills) {
    if ($supportedDrills -notcontains $drill) {
        throw "Unsupported drill '$drill'. Supported on this runner: $($supportedDrills -join ', ')"
    }
}

# Resolve the interpreter before any disk work: a missing interpreter must not
# leave an attached VHD behind. `.venv312` is the local Windows convention;
# `.venv` is what `uv sync` produces on an isolated runner.
if (-not $PythonExe) {
    $pythonCandidates = @(
        (Join-Path $repoRoot ".venv312\python.exe"),
        (Join-Path $repoRoot ".venv312\Scripts\python.exe"),
        (Join-Path $repoRoot ".venv\Scripts\python.exe"),
        (Join-Path $repoRoot ".venv\python.exe")
    )
    $repoPython = $pythonCandidates |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1
    if (-not $repoPython) {
        throw "No project interpreter found. Run 'uv sync --locked --extra dev' or pass -PythonExe."
    }
    $PythonExe = (Resolve-Path -LiteralPath $repoPython).Path
}
elseif (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "PythonExe does not exist: $PythonExe"
}

$create = @"
create vdisk file="$vhdPath" maximum=$SizeMB type=fixed
select vdisk file="$vhdPath"
attach vdisk
create partition primary
format fs=ntfs quick label=IBEMS_DRILL
assign letter=$DriveLetter
"@
$detach = @"
select vdisk file="$vhdPath"
detach vdisk
"@
Set-Content -LiteralPath $diskpartCreate -Value $create -Encoding ASCII
Set-Content -LiteralPath $diskpartDetach -Value $detach -Encoding ASCII

function Invoke-DiskPartBounded([string]$ScriptPath, [int]$TimeoutSeconds) {
    $process = Start-Process -FilePath "diskpart.exe" `
        -ArgumentList @("/s", $ScriptPath) `
        -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw "diskpart timed out after $TimeoutSeconds seconds"
    }
    if ($process.ExitCode -ne 0) {
        throw "diskpart failed with exit code $($process.ExitCode)"
    }
}

try {
    Invoke-DiskPartBounded $diskpartCreate 60
    if (-not (Test-Path "${DriveLetter}:\")) {
        throw "diskpart failed to provision the isolated NTFS VHD"
    }
    # One invocation per drill rather than `--drill all`: `all` would pull in
    # fsync_stall, which cannot run here, and a separate evidence bundle per
    # drill keeps a failure in one from being read as a verdict on the other.
    #
    # Every requested drill runs even if an earlier one fails, then the whole
    # run fails at the end. Aborting on the first failure would let a
    # platform-specific problem in one drill silently withhold the other
    # drill's result, which is the opposite of what this evidence is for. The
    # drills already isolate themselves: separate work directory, fence and
    # witness per drill.
    $failures = @()
    foreach ($drill in $Drills) {
        Write-Host "=== real NTFS drill: $drill ==="
        & $PythonExe (Join-Path $repoRoot "scripts\run_storage_fault_drill.py") `
            --journal-volume "${DriveLetter}:\" `
            --fence-dir $fenceDir `
            --output-root "artifacts/windows_ntfs_vhd/evidence" `
            --drill $drill
        if ($LASTEXITCODE -ne 0) {
            $failures += "$drill (exit $LASTEXITCODE)"
            Write-Warning "real NTFS $drill drill failed with exit code $LASTEXITCODE"
        }
    }
    if ($failures.Count -gt 0) {
        throw "real NTFS drills failed: $($failures -join ', ')"
    }
}
finally {
    if (Test-Path $vhdPath) {
        try {
            Invoke-DiskPartBounded $diskpartDetach 30
        }
        catch {
            Write-Warning "VHD detach failed: $_"
        }
    }
    if (Test-Path $vhdPath) {
        $resolvedVhd = (Resolve-Path $vhdPath).Path
        if (-not $resolvedVhd.StartsWith($artifactRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove VHD outside artifact root: $resolvedVhd"
        }
        Remove-Item -LiteralPath $resolvedVhd -Force
    }
}
