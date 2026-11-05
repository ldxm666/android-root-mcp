# Android Root MCP v2: project-local venv and explicit JSON client registration.
[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$AdbPath = "",
    [string]$Serial = "",
    [string]$ClientConfig = "",
    [switch]$WithWebView,
    [switch]$Dev,
    [switch]$SkipDependencyInstall
)
$ErrorActionPreference = "Stop"
$projectDir = $PSScriptRoot
$venvDir = Join-Path $projectDir ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Invoke-PythonChecked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python command failed with exit code $LASTEXITCODE" }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    if (-not $Python) {
        foreach ($candidateName in @("python", "python3", "py")) {
            $candidate = Get-Command $candidateName -ErrorAction SilentlyContinue
            if ($candidate) { $Python = $candidate.Source; break }
        }
    }
    if (-not $Python) { throw "Python 3.10+ not found. Pass -Python 'C:\path\to\python.exe'." }
    $prefix = @()
    if ((Split-Path -Leaf $Python) -eq "py.exe") { $prefix = @("-3") }
    Write-Host "[1/4] Creating project-local Python environment..." -ForegroundColor Cyan
    Invoke-PythonChecked $Python ($prefix + @("-c", "import sys; assert sys.version_info >= (3,10), 'Python 3.10+ required'"))
    Invoke-PythonChecked $Python ($prefix + @("-m", "venv", $venvDir))
}

if (-not $SkipDependencyInstall) {
    Write-Host "[2/4] Installing MCP and Android dependencies..." -ForegroundColor Cyan
    $extras = @()
    if ($WithWebView) { $extras += "webview" }
    if ($Dev) { $extras += "dev" }
    $target = $projectDir
    if ($extras.Count) { $target += "[" + ($extras -join ",") + "]" }
    Invoke-PythonChecked $venvPython @("-m", "pip", "install", "-e", $target)
}
Invoke-PythonChecked $venvPython @("-c", "import mcp, uiautomator2, PIL, pydantic; print('Dependencies OK')")

if (-not $AdbPath) {
    if ($env:ADB_PATH) { $AdbPath = $env:ADB_PATH }
    else {
        $adbCandidate = Get-Command adb.exe -ErrorAction SilentlyContinue
        if ($adbCandidate) { $AdbPath = $adbCandidate.Source }
        elseif ($env:LOCALAPPDATA -and (Test-Path -LiteralPath (Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe"))) {
            $AdbPath = Join-Path $env:LOCALAPPDATA "Android\Sdk\platform-tools\adb.exe"
        }
    }
}
if ($AdbPath -and (-not (Test-Path -LiteralPath $AdbPath) -or $AdbPath -match '\.(bat|cmd)$')) {
    throw "AdbPath must point to native adb.exe, not a .bat/.cmd wrapper."
}
$serverEnv = [ordered]@{
    PYTHONIOENCODING = "utf-8"
    PYTHONUNBUFFERED = "1"
    ANDROID_MCP_STATE_DIR = (Join-Path $projectDir "state")
}
if ($AdbPath) { $serverEnv["ADB_PATH"] = [IO.Path]::GetFullPath($AdbPath) }
if ($Serial) { $serverEnv["ANDROID_SERIAL"] = $Serial }
$entry = [ordered]@{
    command = $venvPython
    args = @((Join-Path $projectDir "src\server.py"))
    env = $serverEnv
}
$config = [ordered]@{ mcpServers = [ordered]@{ "android-root-mcp" = $entry } }
$json = $config | ConvertTo-Json -Depth 30
$localConfig = Join-Path $projectDir "mcp_config.local.json"
[IO.File]::WriteAllText($localConfig, $json, $utf8)

Write-Host "[3/4] Generated $localConfig" -ForegroundColor Cyan
if ($ClientConfig) {
    $clientPath = [IO.Path]::GetFullPath($ClientConfig)
    if ([IO.Path]::GetExtension($clientPath) -ne ".json") { throw "ClientConfig supports JSON only. Import the generated entry manually for TOML clients." }
    if (Test-Path -LiteralPath $clientPath) {
        # Invalid JSON is an error. Never replace unrelated client configuration.
        $clientObject = Get-Content -LiteralPath $clientPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($clientObject -isnot [pscustomobject]) { throw "Client config must be a JSON object" }
        Copy-Item -LiteralPath $clientPath -Destination ($clientPath + ".backup-" + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
    } else {
        $clientObject = [pscustomobject]@{ mcpServers = [pscustomobject]@{} }
    }
    if (-not $clientObject.PSObject.Properties['mcpServers']) {
        $clientObject | Add-Member -MemberType NoteProperty -Name mcpServers -Value ([pscustomobject]@{})
    }
    if ($clientObject.mcpServers -isnot [pscustomobject]) { throw "mcpServers must be a JSON object" }
    $clientObject.mcpServers | Add-Member -MemberType NoteProperty -Name "android-root-mcp" -Value $entry -Force
    $clientParent = Split-Path -Parent $clientPath
    New-Item -ItemType Directory -Path $clientParent -Force | Out-Null
    [IO.File]::WriteAllText($clientPath, ($clientObject | ConvertTo-Json -Depth 50), $utf8)
    Write-Host "Registered android-root-mcp in $clientPath (previous file backed up)."
}
Write-Host "[4/4] Ready. Import the generated MCP entry and restart/refresh your client." -ForegroundColor Green
Write-Host "First calls: tool_catalog -> device_connection -> device_doctor(initialize_u2=true)."
