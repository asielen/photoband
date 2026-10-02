# Windows build: dist\PhotobandSetup-<version>.exe (per-user installer, no admin prompt).
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
#
# Needs: Python 3.12 x64 (the `py` launcher, or set PHOTOBAND_PYTHON), Node.js 22.12+,
#        Inno Setup 6 (winget install JRSoftware.InnoSetup).
# Uses its own virtual environment, .venv-build, with the pinned requirements/build.lock.
#
# Optional code signing (both the app exe and the installer/uninstaller):
#   PHOTOBAND_SIGN_CERT_SHA1   thumbprint of a code-signing certificate in the cert store
#   PHOTOBAND_SIGNTOOL         path to signtool.exe (default: found on PATH / Windows SDK)
#   PHOTOBAND_SIGN_TIMESTAMP   RFC 3161 timestamp URL (default: http://timestamp.digicert.com)
param(
    [switch]$SkipInstaller,  # stop after the PyInstaller build (dist\Photoband)
    # win10 (default): Python 3.12, needs Windows 10+. win81: Python 3.11 (the last to support
    # Windows 8.1) with the Universal C Runtime bundled; best effort, no OCR or app window there.
    [ValidateSet("win10", "win81")][string]$Target = "win10"
)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

function Invoke-Native {
    # Run a program and fail the build if it exits non-zero ($ErrorActionPreference does not
    # cover native commands).
    param([Parameter(Mandatory)][string]$Exe, [string[]]$Arguments = @())
    Write-Host "> $Exe $($Arguments -join ' ')"
    & $Exe @Arguments
    if ($LASTEXITCODE) { throw "$Exe exited with code $LASTEXITCODE" }
}

# ---- version (single source: photoband/__init__.py) ------------------------------------
$init = Get-Content -Raw (Join-Path $Root "photoband\__init__.py")
if ($init -notmatch '(?m)^__version__\s*=\s*"([^"]+)"') { throw "No __version__ in photoband/__init__.py" }
$Version = $Matches[1]
Write-Host "Building Photoband $Version"

# ---- tools ----------------------------------------------------------------------------
$Iscc = $null
if (-not $SkipInstaller) {
    $cands = @(
        (Get-Command iscc -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1),
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) }
    $Iscc = $cands | Select-Object -First 1
    if (-not $Iscc) { throw "Inno Setup 6 not found. Install it: winget install JRSoftware.InnoSetup" }
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw "Node.js 22.12+ is required (winget install OpenJS.NodeJS.LTS)" }
$NodeExe = (Get-Command node | Select-Object -ExpandProperty Source -First 1)
$nodeVer = (& $NodeExe --version).TrimStart("v")
if ([version]$nodeVer -lt [version]"22.12") { throw "Node.js 22.12+ is required; found $nodeVer" }
# npm must run on the Node checked above. npm.cmd runs the node.exe in its own folder, which can be
# another, older install found earlier on PATH: run npm's own script with the checked node instead.
$NpmCli = Join-Path (Split-Path $NodeExe) "node_modules\npm\bin\npm-cli.js"
if (Test-Path $NpmCli) {
    $NpmExe, $NpmArgs = $NodeExe, @($NpmCli)
} else {
    $NpmExe = (Get-Command npm -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1)
    if (-not $NpmExe) { throw "npm was not found next to $NodeExe or on PATH" }
    $npmNode = Join-Path (Split-Path $NpmExe) "node.exe"
    if ((Test-Path $npmNode) -and [version]((& $npmNode --version).TrimStart("v")) -lt [version]"22.12") {
        throw "npm on PATH ($NpmExe) runs on an older Node.js; put Node.js 22.12+ with npm first on PATH"
    }
    $NpmArgs = @()
}

$SignThumb = $env:PHOTOBAND_SIGN_CERT_SHA1
$SignTool = $null
if ($SignThumb) {
    $SignTool = $env:PHOTOBAND_SIGNTOOL
    if (-not $SignTool) {
        $SignTool = (Get-Command signtool -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -First 1)
    }
    if (-not $SignTool) {
        $SignTool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | Select-Object -ExpandProperty FullName -First 1
    }
    if (-not $SignTool -or -not (Test-Path $SignTool)) { throw "PHOTOBAND_SIGN_CERT_SHA1 is set but signtool.exe was not found (set PHOTOBAND_SIGNTOOL)" }
    $Timestamp = if ($env:PHOTOBAND_SIGN_TIMESTAMP) { $env:PHOTOBAND_SIGN_TIMESTAMP } else { "http://timestamp.digicert.com" }
    Write-Host "Code signing with certificate $SignThumb"
} else {
    Write-Host "PHOTOBAND_SIGN_CERT_SHA1 not set: building UNSIGNED (SmartScreen will warn users)."
}

# ---- Python build environment -----------------------------------------------------------
$Win81 = $Target -eq "win81"
$PyWant = if ($Win81) { "3.11" } else { "3.12" }
$Venv = Join-Path $Root $(if ($Win81) { ".venv-build-win81" } else { ".venv-build" })
$Py = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $Py)) {
    if ($env:PHOTOBAND_PYTHON) { Invoke-Native $env:PHOTOBAND_PYTHON @("-m", "venv", $Venv) }
    elseif (Get-Command py -ErrorAction SilentlyContinue) { Invoke-Native "py" @("-$PyWant", "-m", "venv", $Venv) }
    else { Invoke-Native "python" @("-m", "venv", $Venv) }
}
$arch = (& $Py -c "import platform; print(platform.machine())").Trim()
if ($arch -ne "AMD64") { throw "The build Python must be 64-bit x64 (got $arch). Delete .venv-build and use Python 3.12 x64." }
$pyv = (& $Py -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
if ($pyv -ne $PyWant) { throw "The $Target build needs Python $PyWant in $Venv (got $pyv). Delete $Venv and run again." }
Invoke-Native $Py @("-m", "pip", "install", "--disable-pip-version-check", "-q", "--upgrade", "pip")
Invoke-Native $Py @("-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements\build.lock")
Invoke-Native $Py @("-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "-e", ".")

# ---- vendor + UI ------------------------------------------------------------------------
if (-not (Test-Path "vendor\exiftool\exiftool.exe")) {
    Invoke-Native $Py @("scripts\fetch_vendor.py")
}
Push-Location ui
try {
    Invoke-Native $NpmExe ($NpmArgs + @("ci", "--no-audit", "--no-fund"))
    Invoke-Native $NpmExe ($NpmArgs + @("run", "build"))
} finally { Pop-Location }

# ---- PyInstaller ------------------------------------------------------------------------
$DistDir = if ($Win81) { "dist\win81" } else { "dist" }
$AppDir = Join-Path $Root "$DistDir\Photoband"
$env:PHOTOBAND_BUNDLE_UCRT = if ($Win81) { "1" } else { "" }
Invoke-Native $Py @("-m", "PyInstaller", "packaging\photoband.spec", "--noconfirm", "--clean",
    "--distpath", $DistDir, "--workpath", $(if ($Win81) { "build\win81" } else { "build" }))
if ($SignThumb) {
    Invoke-Native $SignTool @("sign", "/fd", "sha256", "/tr", $Timestamp, "/td", "sha256", "/sha1", $SignThumb, "$AppDir\Photoband.exe")
}
if ($SkipInstaller) { Write-Host "App folder written to $AppDir"; exit 0 }

# ---- WebView2 Evergreen bootstrapper (installed by the setup only when missing) ---------
$Redist = Join-Path $Root "build\redist"
New-Item -ItemType Directory -Force -Path $Redist | Out-Null
$WebView2 = Join-Path $Redist "MicrosoftEdgeWebview2Setup.exe"
if (-not (Test-Path $WebView2)) {
    Write-Host "Downloading the WebView2 Evergreen bootstrapper"
    Invoke-WebRequest -UseBasicParsing -Uri "https://go.microsoft.com/fwlink/p/?LinkId=2124703" -OutFile $WebView2
}
$sig = Get-AuthenticodeSignature $WebView2
if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "O=Microsoft Corporation") {
    Remove-Item $WebView2 -Force
    throw "The downloaded WebView2 bootstrapper is not validly signed by Microsoft ($($sig.Status))."
}

# ---- installer --------------------------------------------------------------------------
$isccArgs = @("/DAppVersion=$Version", "/DWebView2Bootstrapper=$WebView2", "/DAppDir=$AppDir",
              "/DMinWinVersion=$(if ($Win81) { '6.3' } else { '10.0' })", "/DOutputSuffix=$(if ($Win81) { '-win81' } else { '' })")
if ($SignThumb) {
    # $q and $f are expanded by Inno Setup (quote, file to sign)
    $isccArgs += '/Sphotoband=$q' + $SignTool + '$q sign /fd sha256 /tr ' + $Timestamp + ' /td sha256 /sha1 ' + $SignThumb + ' $f'
    $isccArgs += "/DSign=1"
}
$isccArgs += "packaging\windows\photoband.iss"
Invoke-Native $Iscc $isccArgs
Write-Host "Installer written to dist\PhotobandSetup-$Version$(if ($Win81) { '-win81' } else { '' }).exe"
