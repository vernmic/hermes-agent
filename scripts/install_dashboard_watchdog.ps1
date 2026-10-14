param(
    [string]$TaskName = 'Hermes Dashboard Watchdog',
    [string]$Distribution = 'Ubuntu',
    [string]$LinuxUser = $env:USERNAME.ToLower(),
    [string]$LinuxHome = "/home/$LinuxUser/.hermes",
    [int]$Port = 9119,
    [string]$Session = "hermes-dashboard",
    [string]$Command = "",
    [switch]$Disable
)
$ErrorActionPreference = 'Stop'
# Install dashboard_watchdog.py under LinuxHome/scripts before registering.
foreach ($value in @($Distribution, $LinuxUser, $LinuxHome, $Session)) {
    if ($value -notmatch '^[a-zA-Z0-9_./-]+$') {
        throw 'Distribution, LinuxUser and LinuxHome must use simple WSL names/paths.'
    }
}
$wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
if ($Disable) {
    # The marker also stops an orphaned Linux supervisor from undoing maintenance.
    & $wsl -d $Distribution -u $LinuxUser --exec /usr/bin/mkdir -p "$LinuxHome/state/dashboard-watchdog"
    if ($LASTEXITCODE) { throw 'Cannot create dashboard pause directory.' }
    & $wsl -d $Distribution -u $LinuxUser --exec /usr/bin/touch "$LinuxHome/state/dashboard-watchdog/paused"
    if ($LASTEXITCODE) { throw 'Cannot pause dashboard recovery.' }
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
    Stop-ScheduledTask -TaskName $TaskName
    Write-Output "Disabled $TaskName. The running dashboard is preserved."
    exit 0
}
& $wsl -d $Distribution -u $LinuxUser --exec /usr/bin/rm -f "$LinuxHome/state/dashboard-watchdog/paused"
if ($LASTEXITCODE) { throw 'Cannot resume dashboard recovery.' }
$arguments = "-d $Distribution -u $LinuxUser --exec /usr/bin/python3 $LinuxHome/scripts/dashboard_watchdog.py --home $LinuxHome"
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Port must be between 1 and 65535.' }
$arguments += " --port $Port --session $Session"
if ($Command) {
    if ($Command.Contains('"') -or $Command.Contains("`n") -or $Command.Contains("`r")) {
        throw 'Command must be a single line without double quotes.'
    }
    $arguments += " --command `"$Command`""
}
$action = New-ScheduledTaskAction -Execute $wsl -Argument $arguments
$logon = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
# Task Scheduler ignores overlapping invocations. Repetition also recovers a
# supervisor that exits successfully, which RestartOnFailure alone would miss.
$retry = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logon, $retry) `
    -Settings $settings -Principal $principal -Description 'Supervise the loopback Hermes dashboard in WSL; recover missing processes without restarting healthy ones.' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output "Installed and started $TaskName."
