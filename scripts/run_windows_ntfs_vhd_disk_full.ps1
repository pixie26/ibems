param(
    [string]$DriveLetter = "R",
    [int]$SizeMB = 192
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
    $python = Join-Path $repoRoot ".venv312\python.exe"
    & $python (Join-Path $repoRoot "scripts\run_storage_fault_drill.py") `
        --journal-volume "${DriveLetter}:\" `
        --fence-dir $fenceDir `
        --output-root "artifacts/windows_ntfs_vhd/evidence" `
        --drill disk_full
    if ($LASTEXITCODE -ne 0) {
        throw "real NTFS disk-full drill failed with exit code $LASTEXITCODE"
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
