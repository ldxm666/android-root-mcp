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

[CmdletBinding()]
param(
    [string]$Serial      = "975aaf17",
    [string]$Adb         = "D:\Tools\platform-tools\adb.exe",
    [string]$Javac       = "D:\JDK\bin\javac.exe",
    [string]$Jar         = "D:\JDK\bin\jar.exe",
    [string]$D8          = "C:\Users\Administrator\AppData\Local\Android\Sdk\build-tools\36.0.0\d8.bat",
    [string]$AndroidJar  = "C:\Users\Administrator\AppData\Local\Android\Sdk\platforms\android-36\android.jar",
    [string]$DeviceDir   = "/data/local/tmp",
    [string]$OutJar      = "osbridge.jar",
    [switch]$SkipPush
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

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
