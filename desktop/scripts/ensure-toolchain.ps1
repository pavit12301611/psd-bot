<#
.SYNOPSIS
  Make sure everything needed to BUILD the psd.ai desktop app exists on this
  Windows PC, installing whatever is missing. Called by run.bat.

  What it checks / installs:
    1. Node.js  (portable zip -> <repo>\.tools\node, no admin needed)
    2. Rust     (rustup-init.exe -y, installs to %USERPROFILE%\.cargo, no admin)
    3. MSVC C++ Build Tools + Windows SDK  (required by Rust on Windows;
       downloaded from Microsoft, silent install, triggers ONE UAC prompt)
    4. WebView2 runtime (usually already present on Win10/11)

  Everything is idempotent - re-running is fast when things are installed.
  Exit code 0 = toolchain ready, 1 = something failed (details printed).
#>
param(
    [Parameter(Mandatory = $true)][string]$ToolsDir
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = "SilentlyContinue"

$NodeVersion = "v22.12.0"
$NodeZipUrl  = "https://nodejs.org/dist/$NodeVersion/node-$NodeVersion-win-x64.zip"
$RustupUrl   = "https://win.rustup.rs/x86_64"
$VsBuildUrl  = "https://aka.ms/vs/17/release/vs_BuildTools.exe"
$WebView2Url = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
$dl = Join-Path $ToolsDir "downloads"
New-Item -ItemType Directory -Force -Path $dl | Out-Null

function Say($msg)  { Write-Host "      $msg" }
function Step($msg) { Write-Host " ==> $msg" }

function Download($url, $dest) {
    if (Test-Path $dest) { return }
    Say "downloading $url"
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        & curl.exe -L --fail --retry 3 --retry-delay 2 -o "$dest.part" $url
        if ($LASTEXITCODE -ne 0) { throw "download failed: $url" }
    } else {
        Invoke-WebRequest -Uri $url -OutFile "$dest.part" -UseBasicParsing
    }
    Move-Item -Force "$dest.part" $dest
}

function Add-PathFront($dir) {
    if (-not (Test-Path $dir)) { return }
    if (($env:Path -split ";") -notcontains $dir) { $env:Path = "$dir;$env:Path" }
}

# ------------------------------------------------------------------
# 1. Node.js
# ------------------------------------------------------------------
Step "Checking Node.js..."
$nodeDir = Join-Path $ToolsDir "node"
Add-PathFront $nodeDir
$node = Get-Command node.exe -ErrorAction SilentlyContinue
$nodeOk = $false
if ($node) {
    $v = (& node.exe -v) -replace "^v", ""
    if ([int]($v.Split(".")[0]) -ge 18) { $nodeOk = $true; Say "Node.js $v found ($($node.Source))" }
    else { Say "Node.js $v is too old (need 18+), installing a portable copy" }
}
if (-not $nodeOk) {
    $zip = Join-Path $dl "node-$NodeVersion-win-x64.zip"
    Download $NodeZipUrl $zip
    Say "extracting Node.js to $nodeDir"
    if (Test-Path $nodeDir) { Remove-Item -Recurse -Force $nodeDir }
    $tmp = Join-Path $ToolsDir "node-tmp"
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    Move-Item (Join-Path $tmp "node-$NodeVersion-win-x64") $nodeDir
    Remove-Item -Recurse -Force $tmp
    Add-PathFront $nodeDir
    Say "Node.js $(& node.exe -v) ready"
}

# ------------------------------------------------------------------
# 2. MSVC Build Tools (must exist before rustc can link anything)
# ------------------------------------------------------------------
Step "Checking Microsoft C++ Build Tools..."
function Test-Msvc {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path $vswhere)) { return $false }
    $p = & $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath -latest 2>$null
    if (-not $p) { return $false }
    # Also need a Windows SDK (kernel32.lib etc.)
    $sdk = & $vswhere -products * -requires Microsoft.VisualStudio.Component.Windows11SDK.22621 Microsoft.VisualStudio.Component.Windows10SDK.19041 Microsoft.VisualStudio.Component.Windows10SDK.20348 Microsoft.VisualStudio.Component.Windows11SDK.26100 -property installationPath -latest 2>$null
    $sdkRoot = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\Lib"
    return ($sdk -or (Test-Path $sdkRoot))
}
if (Test-Msvc) {
    Say "C++ Build Tools found"
} else {
    $vs = Join-Path $dl "vs_BuildTools.exe"
    Download $VsBuildUrl $vs
    Say "installing Visual Studio C++ Build Tools (silent, ~2-3 GB, 5-15 min)."
    Say "Windows will show ONE 'allow this app to make changes' prompt - click Yes."
    $args = @(
        "--quiet", "--wait", "--norestart", "--nocache",
        "--add", "Microsoft.VisualStudio.Workload.VCTools",
        "--add", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
        "--add", "Microsoft.VisualStudio.Component.Windows11SDK.22621",
        "--includeRecommended"
    )
    $p = Start-Process -FilePath $vs -ArgumentList $args -Verb RunAs -Wait -PassThru
    # 0 = ok, 3010 = ok but reboot recommended
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) {
        throw "Visual Studio Build Tools installer exited with code $($p.ExitCode)"
    }
    if (-not (Test-Msvc)) { throw "C++ Build Tools still not detected after install" }
    Say "C++ Build Tools installed"
}

# ------------------------------------------------------------------
# 3. Rust
# ------------------------------------------------------------------
Step "Checking Rust..."
$cargoBin = Join-Path $env:USERPROFILE ".cargo\bin"
Add-PathFront $cargoBin
$cargo = Get-Command cargo.exe -ErrorAction SilentlyContinue
if ($cargo) {
    Say "Rust found: $(& cargo.exe --version)"
} else {
    $rustup = Join-Path $dl "rustup-init.exe"
    Download $RustupUrl $rustup
    Say "installing Rust (stable, minimal profile) into $cargoBin ..."
    & $rustup -y --default-toolchain stable --profile minimal --default-host x86_64-pc-windows-msvc --no-modify-path
    if ($LASTEXITCODE -ne 0) { throw "rustup-init failed with code $LASTEXITCODE" }
    Add-PathFront $cargoBin
    $cargo = Get-Command cargo.exe -ErrorAction SilentlyContinue
    if (-not $cargo) { throw "cargo.exe not found after rustup install" }
    Say "Rust installed: $(& cargo.exe --version)"
}

# ------------------------------------------------------------------
# 4. WebView2 runtime
# ------------------------------------------------------------------
Step "Checking WebView2 runtime..."
$wvKeys = @(
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
)
$hasWv = $false
foreach ($k in $wvKeys) { if (Test-Path $k) { $hasWv = $true; break } }
if ($hasWv) {
    Say "WebView2 found"
} else {
    $wv = Join-Path $dl "MicrosoftEdgeWebview2Setup.exe"
    Download $WebView2Url $wv
    Say "installing WebView2 runtime..."
    Start-Process -FilePath $wv -ArgumentList "/silent", "/install" -Wait
    Say "WebView2 installed"
}

# Persist the PATH additions for run.bat (it re-reads this file).
$pathFile = Join-Path $ToolsDir "path.txt"
@($nodeDir, $cargoBin) | Where-Object { Test-Path $_ } | Set-Content -Path $pathFile
Step "Toolchain ready."
exit 0
