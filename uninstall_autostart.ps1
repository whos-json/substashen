# Stops Substashen starting at sign-in (removes what install_autostart.ps1 created). Does not close a running copy.
$name = 'Substashen'
$removed = $false
foreach ($key in 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run',
                 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run') {
    if (Get-ItemProperty -Path $key -Name $name -ErrorAction SilentlyContinue) {
        Remove-ItemProperty -Path $key -Name $name
        $removed = $true
    }
}
if (Get-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction SilentlyContinue) {  # from an earlier version
    Unregister-ScheduledTask -TaskName $name -TaskPath '\' -Confirm:$false
    $removed = $true
}
Write-Host $(if ($removed) { "Substashen will no longer start at sign-in." } else { "Substashen wasn't set to start at sign-in." })
