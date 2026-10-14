# Dashboard recovery on Windows/WSL

For WSL installations without systemd, run `dashboard_watchdog.py` in the
foreground under Windows Task Scheduler. This keeps WSL active while signed in
and brings the dashboard back after a dashboard exit or WSL restart. It does
not manage the gateway.

Requirements: tmux, Python 3, an installed `~/.local/bin/hermes`, and a working
dashboard frontend build. The dashboard binds to 127.0.0.1 only. Native WSL
localhost forwarding provides Windows browser access; no portproxy is needed.

Copy `dashboard_watchdog.py` to `~/.hermes/scripts/` in WSL, then run from
Windows PowerShell as the Windows user who owns the WSL distribution:

```powershell
.\scripts\install_dashboard_watchdog.ps1 -Distribution Ubuntu -LinuxUser vern
```

The dedicated task runs at user logon, retries every minute (including clean
supervisor exits), restarts failures, ignores overlapping invocations and has
no execution time limit. It uses the logged-in user's token; it does not run
before logon or promise service during Windows shutdown/sleep. WSL invocation
failures appear in Task Scheduler's last result; watchdog decisions and child
output go to `~/.hermes/logs/dashboard-watchdog.log` and
`dashboard-service.log`. Supervisor logs rotate at 1 MB; child output rotates
on its next start if larger than 5 MB.

The supervisor uses flock to exclude concurrent checks. A healthy HTTP status
response matching this Hermes home/version is sufficient even if the gateway
reports degraded health. Missing
dashboard processes restart on the next poll (15 seconds); unhealthy owned
sessions get a 300-second grace period, including frontend builds. Attempts
are limited to one every 30 seconds. Only sessions marked as owned by this
configuration may be stopped; foreign listeners and unowned unhealthy tmux
sessions are preserved and logged for investigation. Healthy sessions survive
watchdog restarts. The default command omits the interactive TUI and uses
`--skip-build` so crash recovery does not rebuild the frontend. After updating
Hermes frontend sources, build them with `cd ~/.hermes/hermes-agent/web && npm
run build` before resuming supervision. A custom command may omit that flag
if startup should build the UI; the 300-second grace accommodates that.

For maintenance, pause recovery before stopping/upgrading the dashboard:

```sh
mkdir -p ~/.hermes/state/dashboard-watchdog
touch ~/.hermes/state/dashboard-watchdog/paused
# resume:
rm ~/.hermes/state/dashboard-watchdog/paused
```

Or disable and stop the Windows task (the dashboard stays alive):

```powershell
.\scripts\install_dashboard_watchdog.ps1 -Disable
# enable/start again (also removes the pause marker):
.\scripts\install_dashboard_watchdog.ps1 -Distribution Ubuntu -LinuxUser vern
```

One-time idempotent ensure, when the task is disabled:

```sh
python3 ~/.hermes/scripts/dashboard_watchdog.py --once
```

`started` means launched and awaiting readiness; confirm `/api/status` before
calling a deployment healthy. Custom homes, commands, ports, poll intervals,
grace and cooldown values are explicit CLI arguments. Runtime locks, markers,
logs and backups remain local. Run the isolated lifecycle tests through
`scripts/run_tests.sh tests/scripts/test_dashboard_watchdog.py`.

A secondary dashboard uses the same guardian with a distinct home, port, tmux
session and launcher. For example, after configuring `~/.hermes2` and installing
`~/.local/bin/hermes2`:

```powershell
.\scripts\install_dashboard_watchdog.ps1 -TaskName 'Hermes2 Dashboard Watchdog' `
  -Distribution Ubuntu -LinuxUser vern -LinuxHome /home/vern/.hermes2 `
  -Port 9120 -Session hermes2-dashboard `
  -Command '/home/vern/.local/bin/hermes2 dashboard --no-open --skip-build --host 127.0.0.1 --port 9120'
```

`clone_secondary_home.py` copies live profiles, credentials, identity and skills
locally and retargets their memory endpoint. It creates fresh session/task state,
pauses copied schedules and removes messaging credentials from the second home.
Use it before the secondary home has a config. It never starts a gateway.
Receipts must stay local because model configurations can contain private URLs.
