# Makes Substashen a normal Windows startup program for this user: it appears in Settings > Apps > Startup and
# Task Manager > Startup apps, where it can be switched off. No admin needed. Re-running is safe.
$ErrorActionPreference = 'Stop'
$name   = 'Substashen'
$dir    = Split-Path $PSScriptRoot  # the app folder; this script lives in its launcher\ subfolder
# Substashen.exe (built by launcher\build.bat) carries the name and icon Windows shows in the Startup list, then starts substashen.pyw.
$exe    = Join-Path $dir 'Substashen.exe'
$run    = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$approved = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
foreach ($f in $exe, (Join-Path $dir 'substashen.pyw')) { if (-not (Test-Path $f)) { throw "Not found: $f" } }

Set-ItemProperty -Path $run -Name $name -Value "`"$exe`""
# Mark it enabled (02 = on), the record Settings writes when you switch an app on; re-enables it if it was off.
# Run this from Explorer (double-click the .bat): a sandboxed or virtualized shell may write to a private copy of the
# registry that Windows never reads at sign-in.
New-ItemProperty -Path $approved -Name $name -PropertyType Binary -Value ([byte[]](2,0,0,0,0,0,0,0,0,0,0,0)) -Force | Out-Null
# An earlier version used a scheduled task instead; remove it so Substashen isn't started twice.
if (Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $name -TaskPath '\' -Confirm:$false
    Write-Host "Removed the old '\$name' scheduled task."
}

Write-Host "Substashen will start when you sign in."
Write-Host "  Runs:  `"$exe`""
Write-Host "  Manage it in Settings > Apps > Startup, or Task Manager > Startup apps."

# Start it now unless it's already running (a second copy would only show an "already running" message).
if (Get-CimInstance Win32_Process -Filter "Name like 'pythonw%'" | Where-Object { $_.CommandLine -like '*substashen.pyw*' -and $_.CommandLine -notlike '*--demo*' }) {
    Write-Host "Substashen is already running."
} else {
    Start-Process -FilePath $exe -WorkingDirectory $dir
    Write-Host "Substashen started."
}
