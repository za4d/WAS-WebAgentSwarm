# RB-01 CI ONLY: privileged disposable Task Scheduler / LocalSystem
# launch controller. No account/password in task arguments. Reads
# randomly generated synthetic account material from ACL-restricted CI dir.
# Calls CreateProcessAsUserW with explicit process + thread DACL while
# Node is CREATE_SUSPENDED, then awaits a bounded pre-resume worker check.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$TrustedDir)
$ErrorActionPreference="Stop"
$pidForCleanup=0
$created=$false
$statePath=Join-Path $TrustedDir 'system-launch-status.json'
$resumeMarker=Join-Path $TrustedDir 'resume-after-worker-check'
$resumedMarker=Join-Path $TrustedDir 'system-resumed'
try {
  Add-Type -Path (Join-Path $PSScriptRoot 'cr01-rb01-createprotected-ci.cs') -ErrorAction Stop
  $private=Get-Content -LiteralPath (Join-Path $TrustedDir 'system-test-identity.json') -Raw -ErrorAction Stop | ConvertFrom-Json
  $envBlock=[string[]]@(
    ('SystemRoot='+$env:SystemRoot),('WINDIR='+$env:WINDIR),
    ('Path='+$env:Path),('TEMP='+$TrustedDir),('TMP='+$TrustedDir),
    'WEB_SWARM_BIND=127.0.0.1',('WEB_SWARM_PORT='+[string]$private.port),
    'WEB_SWARM_SEND_MODE=relay_required',
    ('WEB_SWARM_RELAY_PERMIT_KEY='+[string]$private.signer),
    ('WEB_SWARM_BRIDGE_TOKEN='+[string]$private.bridge_token),
    ('WEB_SWARM_RELAY_RECEIPTS_DIR='+(Join-Path $TrustedDir 'receipts')),
    ('WEB_SWARM_BRIDGE_TOKEN_FILE='+(Join-Path $TrustedDir 'bridge-token-file'))
  )
  $pidForCleanup=[RB01CreateProtected]::LaunchSuspended(
    [string]$private.username,[string]$private.temporary_account_credential,
    [string]$private.node_executable,(Join-Path $TrustedDir 'bridge\server.js'),
    (Join-Path $TrustedDir 'bridge'),[string]$private.trusted_sid,$envBlock)
  $created=$true
  # Delete temporary password before yielding to worker. CI fixture only.
  Remove-Item -LiteralPath (Join-Path $TrustedDir 'system-test-identity.json') -Force
  [IO.File]::WriteAllText($statePath,(@{
    stage='SUSPENDED_PROTECTED_AT_CREATION'
    target_pid=$pidForCleanup
    launcher_identity=[Security.Principal.WindowsIdentity]::GetCurrent().Name
    process_dacl_at_creation=$true
    thread_dacl_at_creation=$true
  }|ConvertTo-Json -Compress),[Text.Encoding]::UTF8)
  $resumeRequested=$false
  for($i=0;$i -lt 240;$i++){
    if(Test-Path -LiteralPath $resumeMarker){$resumeRequested=$true;break}
    Start-Sleep -Milliseconds 250
  }
  if(-not $resumeRequested){throw 'PRE_RESUME_WORKER_PROBE_TIMEOUT'}
  [RB01CreateProtected]::Resume($pidForCleanup)
  $created=$false
  [IO.File]::WriteAllText($resumedMarker,'RESUMED_AFTER_DENIED_VM_READ',[Text.Encoding]::UTF8)
  exit 0
} catch {
  # Any error must leave no suspended process to be resumed accidentally.
  if($created){try{[RB01CreateProtected]::StopSuspended($pidForCleanup)}catch{}}
  [IO.File]::WriteAllText($statePath,(@{
    stage='FAIL_CLOSED'
    error_class=$_.Exception.GetType().Name
    error=$_.Exception.Message
  }|ConvertTo-Json -Compress),[Text.Encoding]::UTF8)
  exit 1
}
