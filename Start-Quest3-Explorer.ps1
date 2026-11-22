# Quest 3 Storage Explorer - first-run installer and everyday launcher.
# Run this via Start-Quest3-Explorer.cmd, or with PowerShell -File.
# The launcher keeps ADB and its virtual environment in this folder,
# and installs Python here too if no usable installation exists.
# Designed for Windows PowerShell 5.1; requires network only for missing parts.

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Root = $PSScriptRoot
$App = Join-Path $Root 'quest3_app_explorer.py'
$ToolsDir = Join-Path $Root 'platform-tools'
$AdbExe = Join-Path $ToolsDir 'adb.exe'
$VenvDir = Join-Path $Root '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'

function Say([string]$message) {
    Write-Host "`n==> $message" -ForegroundColor Cyan
}

function Get-WorkingPython {
    $candidates = New-Object System.Collections.ArrayList

    # A private Python installed by this launcher is the most reliable fallback.
    [void]$candidates.Add(@{Exe=(Join-Path $Root 'python-runtime\python.exe'); Prefix=@()})

    # Prefer a known-good supported Windows Python installed for this user.
    foreach ($version in @('Python313', 'Python312', 'Python311', 'Python310', 'Python314')) {
        $path = Join-Path $env:LOCALAPPDATA "Programs\Python\$version\python.exe"
        [void]$candidates.Add(@{Exe=$path; Prefix=@()})
        if ($env:ProgramFiles) {
            [void]$candidates.Add(@{Exe=(Join-Path $env:ProgramFiles "$version\python.exe"); Prefix=@()})
        }
    }

    # Optional override for PCs with a custom Python location.
    if ($env:QUEST3_PYTHON) {
        [void]$candidates.Insert(0, @{Exe=$env:QUEST3_PYTHON; Prefix=@()})
    }

    # A Python launcher may exist even though python.exe isn't on PATH.
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($sel in @('-3.13','-3.12','-3.11','-3.10','-3')) {
            [void]$candidates.Add(@{Exe=$py.Source; Prefix=@($sel)})
        }
    }

    # Handle custom installations, including a user-installed Python on PATH.
    foreach ($commandName in @('python.exe', 'python3.exe')) {
        foreach ($cmd in @(Get-Command $commandName -All -ErrorAction SilentlyContinue)) {
            if ($null -ne $cmd -and $cmd.Source -and
                $cmd.Source -notmatch '(?i)\\Microsoft\\WindowsApps\\') {
                [void]$candidates.Add(@{Exe=$cmd.Source; Prefix=@()})
            }
        }
    }

    $pythonRoot = Join-Path $env:LOCALAPPDATA 'Programs\Python'
    if (Test-Path -LiteralPath $pythonRoot) {
        foreach ($folder in @(Get-ChildItem -LiteralPath $pythonRoot -Directory -ErrorAction SilentlyContinue)) {
            [void]$candidates.Add(@{Exe=(Join-Path $folder.FullName 'python.exe'); Prefix=@()})
        }
    }

    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate.Exe -PathType Leaf)) { continue }
        try {
            $prefix = @($candidate.Prefix)
            # No nested string quotes in -c: Windows PowerShell 5.1 can strip
            # them when passing arguments to native Windows executables.
            $result = & $candidate.Exe @prefix -c 'import sys;print(int(sys.version_info >= (3,10) and sys.maxsize > 4294967296))' 2>$null
            if ($LASTEXITCODE -eq 0 -and ($result -join '').Trim() -eq '1') {
                return $candidate
            }
        } catch {
            # Do not let an invalid Python shim prevent checking other copies.
        }
    }
    return $null
}

function Install-PythonIfNeeded {
    $python = Get-WorkingPython
    if ($python) {
        $prefix = @($python.Prefix)
        $version = & $python.Exe @prefix --version
        Write-Host "Python already installed: $version" -ForegroundColor Green
        Write-Host "Using: $($python.Exe) $($python.Prefix -join ' ')" -ForegroundColor Green
        return $python
    }

    Say 'Python 3.10+ (64-bit) not found - installing a private Python 3.13 into this folder'
    Write-Host 'No working 64-bit Python was detected. Skipping winget to avoid duplicate installations and delays.' -ForegroundColor Yellow

    # Use the official python.org installer pinned to a known download checksum.
    # TargetDir avoids relying on PATH refresh, launcher registration, or
    # guessing the directory selected by the installer.
    $version = '3.13.17'
    $url = "https://www.python.org/ftp/python/$version/python-$version-amd64.exe"
    $expected = '5da012101ba2e17113a9a1cd901e9ce1441086f2b7ecb5de06ec94163899a190'
    $setup = Join-Path $env:TEMP "quest3-python-$version-amd64.exe"
    $target = Join-Path $Root 'python-runtime'
    Say 'Downloading Python from python.org (verified SHA-256)'
    try {
        Invoke-WebRequest -Uri $url -OutFile $setup -UseBasicParsing
        $actual = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected) { throw 'Python installer SHA-256 did not match python.org; installation cancelled.' }
        Say 'Installing private Python runtime into this application folder'
        # Wrap TargetDir in quotes for paths containing spaces.
        $targetArgument = 'TargetDir="' + $target + '"'
        $proc = Start-Process -FilePath $setup -ArgumentList @('/quiet','InstallAllUsers=0', 'PrependPath=0',
            'Include_pip=1','Include_launcher=0','Include_test=0', $targetArgument) -PassThru -Wait
        if ($proc.ExitCode -notin @(0, 3010)) {
            throw "Python installer exited with code $($proc.ExitCode)."
        }
    } finally {
        Remove-Item -LiteralPath $setup -Force -ErrorAction SilentlyContinue
    }

    $python = Get-WorkingPython
    if (-not $python) {
        throw "Python installer finished, but Python could not be detected. Expected $target\python.exe . Test the installation in PowerShell with: & '$target\python.exe' --version"
    }
    return $python
}

function Ensure-LocalAdb {
    Say 'Checking local Android platform-tools (ADB)'
    if (Test-Path -LiteralPath $AdbExe -PathType Leaf) {
        try {
            & $AdbExe version *> $null
            if ($LASTEXITCODE -eq 0) {
                Write-Host "ADB already installed: $AdbExe" -ForegroundColor Green
                return
            }
        } catch { }
        Write-Host 'Existing local ADB appears broken; downloading a fresh copy.' -ForegroundColor Yellow
    }
    $url = 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip'
    $zip = Join-Path $Root 'platform-tools-download.zip'
    Say 'Downloading official Android SDK Platform-Tools from Google'
    try {
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
        Expand-Archive -LiteralPath $zip -DestinationPath $Root -Force
    } finally {
        Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
    }
    if (-not (Test-Path -LiteralPath $AdbExe -PathType Leaf)) {
        throw "Google platform-tools ZIP extracted, but adb.exe was not found at $AdbExe"
    }
    & $AdbExe version | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'The downloaded adb.exe could not run.' }
}

function Ensure-PythonDependencies($python) {
    Say 'Checking local Python environment'
    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        Write-Host 'Creating .venv for this app (can reuse globally installed packages, if present)...'
        $prefix = @($python.Prefix)
        # system-site-packages means existing Androguard installs can be reused.
        & $python.Exe @prefix -m venv --system-site-packages $VenvDir
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
            throw 'Could not create the local Python virtual environment.'
        }
    } else {
        Write-Host 'Local Python environment already exists.' -ForegroundColor Green
    }

    $pipOK = $false
    try {
        & $VenvPython -m pip --version 1>$null 2>$null
        $pipOK = ($LASTEXITCODE -eq 0)
    } catch { $pipOK = $false }
    if (-not $pipOK) {
        Write-Host 'pip missing from the local environment; enabling pip...'
        & $VenvPython -m ensurepip --upgrade
        if ($LASTEXITCODE -ne 0) { throw 'Unable to enable pip.' }
    }

    $androguardOK = $false
    try {
        & $VenvPython -c 'from androguard.core.apk import APK' 1>$null 2>$null
        $androguardOK = ($LASTEXITCODE -eq 0)
    } catch { $androguardOK = $false }

    if ($androguardOK) {
        Write-Host 'Androguard and its dependencies already available.' -ForegroundColor Green
    } else {
        Say 'Installing Androguard and its required dependencies into .venv'
        & $VenvPython -m pip install --disable-pip-version-check androguard
        if ($LASTEXITCODE -ne 0) { throw 'Androguard installation failed.' }
        & $VenvPython -c 'from androguard.core.apk import APK; print(1)'
        if ($LASTEXITCODE -ne 0) { throw 'Androguard installed but cannot be imported.' }
    }
}

try {
    Write-Host '=========================================' -ForegroundColor Cyan
    Write-Host '   QUEST 3 STORAGE EXPLORER - LAUNCHER' -ForegroundColor Cyan
    Write-Host '=========================================' -ForegroundColor Cyan
    Write-Host "Folder: $Root"
    if (-not (Test-Path -LiteralPath $App -PathType Leaf)) {
        throw "Missing quest3_app_explorer.py in the same folder as the launcher: $App"
    }

    $python = Install-PythonIfNeeded
    Ensure-LocalAdb
    Ensure-PythonDependencies $python

    # Only change PATH for this launcher process and the Explorer it starts.
    # This ensures the app always uses the ADB alongside this launcher.
    $env:PATH = "$ToolsDir;$env:PATH"

    Say 'Checking Quest connection'
    $devices = & $AdbExe devices
    $devices | Out-Host
    $ready = @($devices | Where-Object { $_ -match '^\S+\s+device\s*$' })
    if ($ready.Count -ne 1) {
        throw "Expected exactly one authorised Quest headset, found $($ready.Count). Connect via USB, enable Developer Mode and approve the USB debugging prompt in-headset."
    }

    Say 'Starting the Python storage explorer'
    Push-Location $Root
    try {
        # Forward optional explorer arguments, e.g. --depth 5 --large-file-mb 64.
        & $VenvPython $App @args
        if ($LASTEXITCODE -ne 0) { throw "Quest 3 App Explorer returned exit code $LASTEXITCODE." }
    } finally {
        Pop-Location
    }
    Write-Host "`nSuccess - your browser report and JSON snapshot are in: $Root" -ForegroundColor Green
    exit 0
} catch {
    Write-Host "`nERROR: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Nothing on your Quest was deleted or modified by this launcher.' -ForegroundColor Yellow
    exit 1
}
