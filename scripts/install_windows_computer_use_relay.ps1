$ErrorActionPreference = "Stop"

$appData = $env:LOCALAPPDATA
$hermesDir = Join-Path $appData "Hermes\computer-use"
if (-not (Test-Path $hermesDir)) {
    New-Item -ItemType Directory -Force -Path $hermesDir | Out-Null
}

$tokenBytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($tokenBytes)
$token = [System.BitConverter]::ToString($tokenBytes).Replace("-", "").ToLower()

$tokenPath = Join-Path $hermesDir "relay_token.txt"
Set-Content -Path $tokenPath -Value $token -NoNewline
Write-Host "Token generated and saved to $tokenPath"

$scriptSource = Join-Path $PSScriptRoot "..\tools\computer_use\windows_relay_server.py"
$scriptDest = Join-Path $hermesDir "windows_relay_server.py"
Copy-Item -Path $scriptSource -Destination $scriptDest -Force
Write-Host "Copied relay server to $scriptDest"

$action = New-ScheduledTaskAction -Execute "python.exe" -Argument "`"$scriptDest`""
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
$taskName = "HermesComputerUseRelay"

Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Force | Out-Null
Write-Host "Registered scheduled task $taskName"

Start-ScheduledTask -TaskName $taskName
Write-Host "Started scheduled task $taskName"
