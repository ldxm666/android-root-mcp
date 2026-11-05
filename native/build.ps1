<#
  native/build.ps1 — build pfdaemon.jar and push it to the device.

  Nothing here downloads anything. It looks for a JDK and the Android SDK that are
  already on the box, and says exactly what is missing if one is not.

  Usage:
    .\build.ps1                 # build, push, chmod
    .\build.ps1 -NoPush         # build only
    .\build.ps1 -Serial <your-device-serial>
#>
[CmdletBinding()]
param(
    [string]$Serial = $env:ANDROID_SERIAL,
    [string]$Adb    = $env:ADB_PATH,
    [switch]$NoPush
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
if (-not $Adb) {
    $onPathAdb = Get-Command adb.exe -ErrorAction SilentlyContinue
    if ($onPathAdb) { $Adb = $onPathAdb.Source }
    else {
        $sdkAdb = Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe"
        if ($env:LOCALAPPDATA -and (Test-Path -LiteralPath $sdkAdb)) { $Adb = $sdkAdb }
    }
}
if (-not $Adb) {
    $onPathAdb = Get-Command adb.exe -ErrorAction SilentlyContinue
    if ($onPathAdb) { $Adb = $onPathAdb.Source }
    else {
        $sdkAdb = Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe"
        if ($env:LOCALAPPDATA -and (Test-Path -LiteralPath $sdkAdb)) { $Adb = $sdkAdb }
    }
}
$src  = Join-Path $root "pfdaemon\com\pf\Daemon.java"
$outDir   = Join-Path $root "pfdaemon\classes"
$dexDir   = Join-Path $root "pfdaemon\out"
$jarPath  = Join-Path $root "pfdaemon\pfdaemon.jar"

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

if (-not (Test-Path $src)) { Fail "source not found: $src" }

# ---- locate a JDK -----------------------------------------------------------
$javac = $null
$candidates = @()
if ($env:JAVA_HOME) { $candidates += (Join-Path $env:JAVA_HOME "bin\javac.exe") }
$onPath = Get-Command javac.exe -ErrorAction SilentlyContinue
if ($onPath) { $candidates += $onPath.Source }
foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { $javac = $c; break } }
if (-not $javac) { Fail "no javac.exe found. Install a JDK 8+ or set JAVA_HOME." }
$jarExe = Join-Path (Split-Path (Split-Path $javac -Parent) -Parent) "bin\jar.exe"
if (-not (Test-Path $jarExe)) { $jarExe = "jar.exe" }

$javacVer = (& $javac -version 2>&1 | Out-String).Trim()
Write-Host "javac : $javac  ($javacVer)"

# ---- locate the Android SDK -------------------------------------------------
$sdkRoots = @()
if ($env:ANDROID_HOME)    { $sdkRoots += $env:ANDROID_HOME }
if ($env:ANDROID_SDK_ROOT){ $sdkRoots += $env:ANDROID_SDK_ROOT }
$sdkRoots += (Join-Path $env:LOCALAPPDATA "Android\Sdk")
$sdkRoots += @("C:\Android\Sdk", "D:\Android\Sdk")
$sdk = $sdkRoots | Where-Object { $_ -and (Test-Path (Join-Path $_ "platforms")) } | Select-Object -First 1
if (-not $sdk) { Fail "no Android SDK found (looked for <root>\platforms). Set ANDROID_HOME." }

# newest android.jar
$androidJar = Get-ChildItem (Join-Path $sdk "platforms") -Directory -Filter "android-*" -ErrorAction SilentlyContinue |
    Sort-Object { [int]($_.Name -replace 'android-','') } -ErrorAction SilentlyContinue |
    ForEach-Object { Join-Path $_.FullName "android.jar" } |
    Where-Object { Test-Path $_ } | Select-Object -Last 1
if (-not $androidJar) { Fail "no android.jar under $sdk\platforms. Install a platform via sdkmanager." }
Write-Host "android.jar : $androidJar"

# newest d8
$d8 = Get-ChildItem (Join-Path $sdk "build-tools") -Directory -ErrorAction SilentlyContinue |
    Sort-Object { try { [version]$_.Name } catch { [version]"0.0.0" } } |
    ForEach-Object { Join-Path $_.FullName "d8.bat" } |
    Where-Object { Test-Path $_ } | Select-Object -Last 1

$dx = Get-ChildItem (Join-Path $sdk "build-tools") -Directory -ErrorAction SilentlyContinue |
    ForEach-Object { Join-Path $_.FullName "dx.bat" } |
    Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $d8 -and -not $dx) {
    Write-Host ""
    Write-Host "NO DEXER FOUND." -ForegroundColor Yellow
    Write-Host "  Looked under: $sdk\build-tools\*\d8.bat and dx.bat"
    Write-Host "  Missing: d8.bat (or dx.bat) — install build-tools, e.g.:"
    Write-Host "    sdkmanager `"build-tools;36.0.0`""
    Write-Host ""
    Write-Host "  The Java source is complete and compiles; only the .dex step is blocked."
    Write-Host "  Manual alternative: run d8 on another machine and copy classes.dex into a jar,"
    Write-Host "  or use the pure-shell fallback: python native\fallback.py"
    exit 2
}
Write-Host "dexer : $(if ($d8) { $d8 } else { $dx })"

# ---- compile ----------------------------------------------------------------
if (Test-Path $outDir) { Remove-Item $outDir -Recurse -Force }
if (Test-Path $dexDir) { Remove-Item $dexDir -Recurse -Force }
New-Item -ItemType Directory -Path $outDir | Out-Null
New-Item -ItemType Directory -Path $dexDir | Out-Null

Write-Host "compiling..."
& $javac --release 8 -nowarn -cp $androidJar -d $outDir $src
if ($LASTEXITCODE -ne 0) { Fail "javac failed" }

$classes = Get-ChildItem $outDir -Recurse -Filter "*.class" | ForEach-Object { $_.FullName }
if (-not $classes) { Fail "no .class produced" }

Write-Host "dexing..."
if ($d8) {
    & $d8 --lib $androidJar --min-api 30 --output $dexDir @classes
} else {
    & $dx --dex --output (Join-Path $dexDir "classes.dex") $outDir
}
if ($LASTEXITCODE -ne 0) { Fail "dexing failed" }

$dexFile = Join-Path $dexDir "classes.dex"
if (-not (Test-Path $dexFile)) { Fail "classes.dex not produced" }

if (Test-Path $jarPath) { Remove-Item $jarPath -Force }
& $jarExe cf $jarPath -C $dexDir classes.dex
if ($LASTEXITCODE -ne 0) { Fail "jar failed" }

$size = (Get-Item $jarPath).Length
Write-Host "built : $jarPath ($size bytes)"

if ($NoPush) { Write-Host "(-NoPush: skipped deploy)"; exit 0 }

# ---- deploy -----------------------------------------------------------------
# Content-hashed name: never collide with other tools writing into /data/local/tmp.
$sha  = (Get-FileHash $jarPath -Algorithm SHA256).Hash.ToLower()
$tag  = $sha.Substring(0, 8)
$remote = "/data/local/tmp/pfdaemon-$tag.jar"

if (-not (Test-Path $Adb)) { Fail "adb not found at $Adb (pass -Adb <path>)" }
$adbArgs = @()
if ($Serial) { $adbArgs += @("-s", $Serial) }

Write-Host "pushing to $remote ..."
& $Adb @adbArgs push $jarPath $remote
if ($LASTEXITCODE -ne 0) { Fail "adb push failed" }
& $Adb @adbArgs shell "chmod 644 $remote"

Write-Host ""
Write-Host "OK. Deployed: $remote" -ForegroundColor Green
Write-Host "Start it with: python $(Join-Path $root 'run.py') --start"
