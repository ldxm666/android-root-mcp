# build.ps1 - build osbridge.jar (classes.dex in a zip) and push it to /data/local/tmp
#
# Pipeline:
#   javac (public android.jar only, all @hide APIs are reached by reflection at runtime)
#     -> jar  (classes -> intermediate classes.jar)
#     -> d8   (classes.jar -> classes.dex, min-api 26)
#     -> jar  (classes.dex -> osbridge.jar, a plain zip with classes.dex at its root)
#     -> adb push + chmod 644
#
# Every command it runs is printed first.

# Every default below comes from the environment or is discovered on this machine.
# Nothing is pinned to a particular checkout, user, or device.
[CmdletBinding()]
param(
    [string]$Serial      = $env:ANDROID_SERIAL,
    [string]$Adb         = $env:ADB_PATH,
    [string]$Javac       = "",
    [string]$Jar         = "",
    [string]$D8          = "",
    [string]$AndroidJar  = "",
    [string]$DeviceDir   = "/data/local/tmp",
    [string]$OutJar      = "osbridge.jar",
    [switch]$SkipPush
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# ---- resolve toolchain -------------------------------------------------------
function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

if (-not $Adb) {
    $onPath = Get-Command adb.exe -ErrorAction SilentlyContinue
    if ($onPath) { $Adb = $onPath.Source }
    else {
        $sdkAdb = Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe"
        if ($env:LOCALAPPDATA -and (Test-Path -LiteralPath $sdkAdb)) { $Adb = $sdkAdb }
    }
}
if (-not $Adb -or -not (Test-Path -LiteralPath $Adb)) {
    Fail "adb not found. Put adb on PATH, set ADB_PATH, or pass -Adb <path>."
}

if (-not $Javac) {
    if ($env:JAVA_HOME) { $Javac = Join-Path $env:JAVA_HOME "bin\javac.exe" }
    if (-not (Test-Path -LiteralPath $Javac)) {
        $onPathJavac = Get-Command javac.exe -ErrorAction SilentlyContinue
        if ($onPathJavac) { $Javac = $onPathJavac.Source }
    }
}
if (-not $Javac -or -not (Test-Path -LiteralPath $Javac)) {
    Fail "javac not found. Install a JDK 8+ or set JAVA_HOME."
}
if (-not $Jar) { $Jar = Join-Path (Split-Path (Split-Path $Javac -Parent) -Parent) "bin\jar.exe" }

if (-not $D8 -or -not $AndroidJar) {
    $sdkRoots = @()
    if ($env:ANDROID_HOME)     { $sdkRoots += $env:ANDROID_HOME }
    if ($env:ANDROID_SDK_ROOT) { $sdkRoots += $env:ANDROID_SDK_ROOT }
    if ($env:LOCALAPPDATA)     { $sdkRoots += (Join-Path $env:LOCALAPPDATA "Android\Sdk") }
    $sdkRoots += @("C:\Android\Sdk")
    $sdk = $sdkRoots | Where-Object { $_ -and (Test-Path (Join-Path $_ "platforms")) } | Select-Object -First 1
    if (-not $sdk) { Fail "no Android SDK found. Set ANDROID_HOME or ANDROID_SDK_ROOT." }
    if (-not $AndroidJar) {
        $AndroidJar = Get-ChildItem (Join-Path $sdk "platforms") -Directory -Filter "android-*" -ErrorAction SilentlyContinue |
            Sort-Object { [int]($_.Name -replace 'android-','') } -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName "android.jar" } |
            Where-Object { Test-Path $_ } | Select-Object -Last 1
    }
    if (-not $D8) {
        $D8 = Get-ChildItem (Join-Path $sdk "build-tools") -Directory -ErrorAction SilentlyContinue |
            Sort-Object { try { [version]$_.Name } catch { [version]"0.0.0" } } |
            ForEach-Object { Join-Path $_.FullName "d8.bat" } |
            Where-Object { Test-Path $_ } | Select-Object -Last 1
    }
}
if (-not $AndroidJar -or -not (Test-Path -LiteralPath $AndroidJar)) {
    Fail 'android.jar not found. Install a platform: sdkmanager "platforms;android-36".'
}
if (-not $D8 -or -not (Test-Path -LiteralPath $D8)) {
    Fail 'd8.bat not found. Install build-tools: sdkmanager "build-tools;36.0.0".'
}


# ---- resolve toolchain -------------------------------------------------------
function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }

if (-not $Adb) {
    $onPath = Get-Command adb.exe -ErrorAction SilentlyContinue
    if ($onPath) { $Adb = $onPath.Source }
    else {
        $sdkAdb = Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe"
        if ($env:LOCALAPPDATA -and (Test-Path -LiteralPath $sdkAdb)) { $Adb = $sdkAdb }
    }
}
if (-not $Adb -or -not (Test-Path -LiteralPath $Adb)) {
    Fail "adb not found. Put adb on PATH, set ADB_PATH, or pass -Adb <path>."
}

if (-not $Javac) {
    if ($env:JAVA_HOME) { $Javac = Join-Path $env:JAVA_HOME "bin\javac.exe" }
    if (-not (Test-Path -LiteralPath $Javac)) {
        $onPathJavac = Get-Command javac.exe -ErrorAction SilentlyContinue
        if ($onPathJavac) { $Javac = $onPathJavac.Source }
    }
}
if (-not $Javac -or -not (Test-Path -LiteralPath $Javac)) {
    Fail "javac not found. Install a JDK 8+ or set JAVA_HOME."
}
if (-not $Jar) { $Jar = Join-Path (Split-Path (Split-Path $Javac -Parent) -Parent) "bin\jar.exe" }

if (-not $D8 -or -not $AndroidJar) {
    $sdkRoots = @()
    if ($env:ANDROID_HOME)     { $sdkRoots += $env:ANDROID_HOME }
    if ($env:ANDROID_SDK_ROOT) { $sdkRoots += $env:ANDROID_SDK_ROOT }
    if ($env:LOCALAPPDATA)     { $sdkRoots += (Join-Path $env:LOCALAPPDATA "Android\Sdk") }
    $sdkRoots += @("C:\Android\Sdk")
    $sdk = $sdkRoots | Where-Object { $_ -and (Test-Path (Join-Path $_ "platforms")) } | Select-Object -First 1
    if (-not $sdk) { Fail "no Android SDK found. Set ANDROID_HOME or ANDROID_SDK_ROOT." }
    if (-not $AndroidJar) {
        $AndroidJar = Get-ChildItem (Join-Path $sdk "platforms") -Directory -Filter "android-*" -ErrorAction SilentlyContinue |
            Sort-Object { [int]($_.Name -replace 'android-','') } -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName "android.jar" } |
            Where-Object { Test-Path $_ } | Select-Object -Last 1
    }
    if (-not $D8) {
        $D8 = Get-ChildItem (Join-Path $sdk "build-tools") -Directory -ErrorAction SilentlyContinue |
            Sort-Object { try { [version]$_.Name } catch { [version]"0.0.0" } } |
            ForEach-Object { Join-Path $_.FullName "d8.bat" } |
            Where-Object { Test-Path $_ } | Select-Object -Last 1
    }
}
if (-not $AndroidJar -or -not (Test-Path -LiteralPath $AndroidJar)) {
    Fail 'android.jar not found. Install a platform: sdkmanager "platforms;android-36".'
}
if (-not $D8 -or -not (Test-Path -LiteralPath $D8)) {
    Fail 'd8.bat not found. Install build-tools: sdkmanager "build-tools;36.0.0".'
}


Write-Host "osbridge build" -ForegroundColor Green
Write-Host "  project    : $PSScriptRoot"
Write-Host "  javac      : $Javac"
Write-Host "  android.jar: $AndroidJar"
Write-Host "  d8         : $D8"

# clean
foreach ($p in @("build\classes", "build\dex", "build\classes.jar")) {
    if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force }
}
New-Item -ItemType Directory -Force -Path "build\classes", "build\dex" | Out-Null

# 1. compile
$javacArgs = @(
    "-encoding", "UTF-8",
    "-source", "11", "-target", "11",
    "-nowarn",
    "-classpath", $AndroidJar,
    "-d", "build\classes",
    "OsBridge.java"
)
Write-Host ("`n> `"{0}`" {1}" -f $Javac, ($javacArgs -join " ")) -ForegroundColor DarkGray
& $Javac @javacArgs
if ($LASTEXITCODE -ne 0) { throw "javac failed with exit code $LASTEXITCODE" }

# 2. classes -> classes.jar
$jarArgs = @("--create", "--file", "build\classes.jar", "-C", "build\classes", ".")
Write-Host ("`n> `"{0}`" {1}" -f $Jar, ($jarArgs -join " ")) -ForegroundColor DarkGray
& $Jar @jarArgs
if ($LASTEXITCODE -ne 0) { throw "jar (classes) failed with exit code $LASTEXITCODE" }

# 3. dex
$d8Args = @("--lib", $AndroidJar, "--min-api", "26", "--output", "build\dex", "build\classes.jar")
Write-Host ("`n> `"{0}`" {1}" -f $D8, ($d8Args -join " ")) -ForegroundColor DarkGray
& $D8 @d8Args
if ($LASTEXITCODE -ne 0) { throw "d8 failed with exit code $LASTEXITCODE" }
if (-not (Test-Path -LiteralPath "build\dex\classes.dex")) { throw "d8 produced no classes.dex" }

# 4. classes.dex -> osbridge.jar
if (Test-Path -LiteralPath $OutJar) { Remove-Item -LiteralPath $OutJar -Force }
$jarArgs2 = @("--create", "--file", $OutJar, "-C", "build\dex", "classes.dex")
Write-Host ("`n> `"{0}`" {1}" -f $Jar, ($jarArgs2 -join " ")) -ForegroundColor DarkGray
& $Jar @jarArgs2
if ($LASTEXITCODE -ne 0) { throw "jar (osbridge.jar) failed with exit code $LASTEXITCODE" }

$sz = (Get-Item -LiteralPath $OutJar).Length
Write-Host ("`nbuilt {0} ({1} bytes, dex {2} bytes)" -f $OutJar, $sz, (Get-Item -LiteralPath "build\dex\classes.dex").Length) -ForegroundColor Green

# 5. push
if (-not $SkipPush) {
    if (-not $Serial) {
        $online = @(& $Adb devices | Select-String -Pattern '^\S+\s+device$' |
            ForEach-Object { ($_.ToString() -split '\s+')[0] })
        if ($online.Count -eq 1) {
            $Serial = $online[0]
            Write-Host "  serial     : $Serial (auto-detected)" -ForegroundColor Green
        } elseif ($online.Count -gt 1) {
            Fail "multiple devices online; pass -Serial <serial> or set ANDROID_SERIAL."
        } else {
            Fail "no device online; connect one, or pass -Serial <serial> / set ANDROID_SERIAL."
        }
    }
    $remote = "$DeviceDir/$OutJar"
    $pushArgs = @("-s", $Serial, "push", $OutJar, $remote)
    Write-Host ("`n> `"{0}`" {1}" -f $Adb, ($pushArgs -join " ")) -ForegroundColor DarkGray
    & $Adb @pushArgs
    if ($LASTEXITCODE -ne 0) { throw "adb push failed with exit code $LASTEXITCODE" }

    $chmodArgs = @("-s", $Serial, "shell", "chmod 644 $remote")
    Write-Host ("`n> `"{0}`" {1}" -f $Adb, ($chmodArgs -join " ")) -ForegroundColor DarkGray
    & $Adb @chmodArgs
    if ($LASTEXITCODE -ne 0) { throw "adb shell chmod failed with exit code $LASTEXITCODE" }

    $lsArgs = @("-s", $Serial, "shell", "ls -l $remote")
    Write-Host ("`n> `"{0}`" {1}" -f $Adb, ($lsArgs -join " ")) -ForegroundColor DarkGray
    & $Adb @lsArgs
}

Write-Host "`ndone." -ForegroundColor Green
