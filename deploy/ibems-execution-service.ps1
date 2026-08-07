# Production supervisor registration for the execution host (Windows).
#
# THIS FILE IS GATE B1.3a EVIDENCE, NOT AN EXAMPLE.
# See deploy/ibems-execution.service for the exit-code table and the rationale;
# tests/test_supervisor.py parses both and asserts the restart policy.
#
# NSSM is used rather than sc.exe because the Windows SCM's own recovery
# settings default to restarting a failed service, and the whole safety
# argument here is that a non-zero exit stays exited until a human looks.

$ErrorActionPreference = 'Stop'

$Service   = 'ibems-execution'
$Python    = 'C:\opt\ibems\.venv\Scripts\python.exe'

# The journal and the fence MUST be on different volumes. The host verifies
# this at startup via the volume serial number and refuses otherwise; keeping
# them on separate drives here makes the intent visible in the deployment.
$Journal   = 'D:\ibems-data\journal.db'
$Fence     = 'C:\ProgramData\ibems\fatal-fence.json'
$StatusOut = 'D:\ibems-data\status.json'

nssm install $Service $Python `
    '-m' 'ib_execution.execution_host' `
    '--journal' $Journal `
    '--fence'   $Fence `
    '--status'  $StatusOut

# NEVER change AppExit to Restart. Every non-zero exit code from
# execution_host (10 fatal shutdown, 11 not owner, 12 fenced, 13 calendar,
# 14 startup) means a human has to look before the engine reaches a broker
# again. Restarting turns each of them into a crash loop that reconnects.
nssm set $Service AppExit Default Exit
nssm set $Service AppStopMethodConsole 60000
nssm set $Service Start SERVICE_DEMAND_START

# Belt and braces: clear the SCM's own recovery actions, which default to
# restarting and would otherwise override the intent above.
sc.exe failure $Service reset= 0 actions= ""
