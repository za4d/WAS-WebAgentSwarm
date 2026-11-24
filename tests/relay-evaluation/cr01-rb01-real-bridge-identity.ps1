# RB01 real Node strict bridge + two separate STANDARD Windows principals.
# Ephemeral GitHub Windows VM only. NO real key, browser, owner PC or Send.
# Clean up both accounts and test files; always publish a bounded JSON verdict.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$EvidencePath)
$ErrorActionPreference='Stop'
$id=[guid]::NewGuid().ToString('N').Substring(0,8)
$trustedName='wasrb_t_'+$id
$workerName='wasrb_w_'+$id
$root=Join-Path $env:ProgramData ('WAS_RB01_SERVICE_'+$id)
$trusted=Join-Path $root 'trusted'
$worker=Join-Path $root 'worker'
$trustedCred=$null
$workerCred=$null
$nodePid=0
$trustedLauncher=$null
$trustedCreated=$false
$workerCreated=$false
$report=[ordered]@{
  case='RB01-CREATION-TIME-DACL-STRICT-BRIDGE-01'
  provenance='Windows CI creation-time process+thread DACL, suspended Node before secrets, two separate standard identities; fake credentials'
  owner_windows_host_tested=$false
  real_authenticated_browser_used=$false
  actual_windows_service_installed=$false
  actual_bridge_process_running=$false
  trusted_identity_nonadmin=$false
  worker_identity_nonadmin=$false
  trusted_bridge_health_authorized=$false
  worker_workspace_write=$false
  worker_key_config_read_denied=$false
  worker_receipt_read_denied=$false
  worker_bridge_source_write_denied=$false
  worker_browser_profile_canary_list_denied=$false
  worker_bridge_http_unauthorized=$false
  worker_unsigned_send_unauthorized=$false
  worker_process_memory_read_denied=$false
  worker_process_probe_executed=$false
  worker_process_vm_read_opened=$null
  worker_process_probe_error=$null
  process_dacl_applied_after_launch=$false
  process_dacl_applied_before_key_load=$false
  process_created_suspended=$false
  process_created_with_explicit_security_descriptor=$false
  pre_resume_worker_vm_read_denied=$false
  pre_resume_worker_identity_nonadmin=$false
  pre_resume_probe_executed=$false
  service_survived_worker_probe=$false
  verdict='INCONCLUSIVE'
  error_class=$null
}
try {
  $exe=(Get-Command node.exe -ErrorAction Stop).Source
  $tcp=[System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,0)
  $tcp.Start()
  $port=([Net.IPEndPoint]$tcp.LocalEndpoint).Port
  $tcp.Stop()
  New-Item -ItemType Directory -Path $trusted,$worker -Force | Out-Null

  function Create-WorkerIdentity([string]$name) {
    $plain='Aa1!'+[guid]::NewGuid().ToString('N')+'Zz2!'
    $secret=ConvertTo-SecureString $plain -AsPlainText -Force
    $null=New-LocalUser -Name $name -Password $secret -AccountNeverExpires -ErrorAction Stop
    $credential=New-Object System.Management.Automation.PSCredential(($env:COMPUTERNAME+'\'+$name),$secret)
    $plain=$null
    return $credential
  }
  $trustedCred=Create-WorkerIdentity $trustedName
  $trustedCreated=$true
  $workerCred=Create-WorkerIdentity $workerName
  $workerCreated=$true
  $trustedSid=(Get-LocalUser -Name $trustedName).SID.Value
  $workerSid=(Get-LocalUser -Name $workerName).SID.Value
  foreach($sid in @($trustedSid,$workerSid)) {
    & icacls.exe $root '/grant' ('*'+$sid+':(RX)') | Out-Null
    if($LASTEXITCODE -ne 0){throw 'root_traversal_acl'}
  }
  & icacls.exe $trusted '/inheritance:r' '/grant:r' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' ('*'+$trustedSid+':(OI)(CI)M') | Out-Null
  if($LASTEXITCODE -ne 0){throw 'trusted_acl'}
  & icacls.exe $worker '/inheritance:r' '/grant:r' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' ('*'+$workerSid+':(OI)(CI)M') | Out-Null
  if($LASTEXITCODE -ne 0){throw 'worker_acl'}

  # Copy the actual public WAS bridge source/dependencies AFTER applying ACLs.
  $repoRoot=(Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
  Copy-Item -LiteralPath (Join-Path $repoRoot 'bridge') -Destination (Join-Path $trusted 'bridge') -Recurse -ErrorAction Stop
  New-Item -ItemType Directory -Path (Join-Path $trusted 'receipts'),(Join-Path $trusted 'browser-profile-canary') -Force | Out-Null
  Set-Content -LiteralPath (Join-Path $trusted 'receipts\existing-receipt.json') -Value '{"dummy":true}'
  Set-Content -LiteralPath (Join-Path $trusted 'browser-profile-canary\dummy.txt') -Value 'NOT_A_REAL_PROFILE'
  $configuration=[ordered]@{
    node_executable=$exe
    port=$port
    bridge_token=('synthetic-only-'+[guid]::NewGuid().ToString('N')+[guid]::NewGuid().ToString('N'))
    signer=('synthetic-only-'+[guid]::NewGuid().ToString('N')+[guid]::NewGuid().ToString('N'))
  }
  $configText=ConvertTo-Json -InputObject $configuration -Depth 4
  Set-Content -LiteralPath (Join-Path $trusted 'synthetic-config.json') -Value $configText -Encoding UTF8
  Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'cr01-rb01-live-bridge-worker-probe.ps1') -Destination (Join-Path $worker 'probe.ps1')
  Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'cr01-rb01-pre-resume-probe.ps1') -Destination (Join-Path $worker 'pre-resume-probe.ps1')

  # This test uses the actual CreateProcessAsUserW *process-creation*
  # SECURITY_ATTRIBUTES and CREATE_SUSPENDED. Do not spawn a process that
  # reads its synthetic key before access rights have been checked.
  Add-Type -Path (Join-Path $PSScriptRoot 'cr01-rb01-createprotected-ci.cs') -ErrorAction Stop
  $launchEnv=@(
    ('SystemRoot='+$env:SystemRoot),('WINDIR='+$env:WINDIR),
    ('Path='+$env:Path),('TEMP='+$trusted),('TMP='+$trusted),
    'WEB_SWARM_BIND=127.0.0.1',('WEB_SWARM_PORT='+$port),
    'WEB_SWARM_SEND_MODE=relay_required',
    ('WEB_SWARM_RELAY_PERMIT_KEY='+[string]$configuration.signer),
    ('WEB_SWARM_BRIDGE_TOKEN='+[string]$configuration.bridge_token),
    ('WEB_SWARM_RELAY_RECEIPTS_DIR='+(Join-Path $trusted 'receipts')),
    ('WEB_SWARM_BRIDGE_TOKEN_FILE='+(Join-Path $trusted 'bridge-token-file'))
  )
  $nodePid=[RB01CreateProtected]::LaunchSuspended(
    $trustedName,$trustedCred.GetNetworkCredential().Password,
    $exe,(Join-Path $trusted 'bridge\server.js'),
    (Join-Path $trusted 'bridge'),$trustedSid,[string[]]$launchEnv)
  if($nodePid -le 0){throw 'creation_time_process_dacl_not_installed'}
  $report.process_created_suspended=$true
  $report.process_created_with_explicit_security_descriptor=$true
  $report.process_dacl_applied_before_key_load=$true

  # Negative probe runs in a separately authenticated STANDARD Windows
  # worker while Node has not executed its first instruction.
  $preResult=Join-Path $worker 'pre-resume-result.json'
  $preArgs=@('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',
    ('"'+(Join-Path $worker 'pre-resume-probe.ps1')+'"'),
    '-TargetPid',"$nodePid",'-ResultPath',('"'+$preResult+'"'))
  $pre=Start-Process -FilePath "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -Credential $workerCred -ArgumentList $preArgs -WorkingDirectory $worker -RedirectStandardOutput (Join-Path $worker 'pre-resume-out.log') -RedirectStandardError (Join-Path $worker 'pre-resume-err.log') -Wait -PassThru -ErrorAction Stop
  if(-not (Test-Path -LiteralPath $preResult)){throw 'creation_time_probe_missing'}
  $preObserved=Get-Content -LiteralPath $preResult -Raw|ConvertFrom-Json
  $report.pre_resume_probe_executed=$true
  $report.pre_resume_worker_identity_nonadmin=[bool]$preObserved.worker_nonadmin
  $report.pre_resume_worker_vm_read_denied=([bool]$preObserved.process_vm_read_denied -and $pre.ExitCode -eq 0)
  if(-not $report.pre_resume_worker_vm_read_denied){throw 'creation_time_process_access_denied_fail'}
  [RB01CreateProtected]::Resume($nodePid)

  $url='http://127.0.0.1:'+ $port
  for($n=0;$n -lt 70;$n++) {
    try {
      $ready=Invoke-WebRequest -Uri ($url+'/health') -UseBasicParsing -Headers @{'x-was-token'=$configuration.bridge_token} -TimeoutSec 2 -ErrorAction Stop
      if($ready.StatusCode -eq 200) {
        $payload=$ready.Content|ConvertFrom-Json
        if($payload.sendPolicy -eq 'relay_required') {$report.trusted_bridge_health_authorized=$true;break}
      }
    } catch {}
    Start-Sleep -Milliseconds 300
  }
  if(-not $report.trusted_bridge_health_authorized){throw 'trusted_bridge_not_ready'}

  $nodes=Get-CimInstance Win32_Process -Filter ("ProcessId="+$nodePid)
  if(@($nodes).Count -ne 1){throw 'trusted_node_process_missing'}
  $owner=Invoke-CimMethod -InputObject (@($nodes)[0]) -MethodName GetOwner -ErrorAction Stop
  $report.trusted_identity_nonadmin=($owner.User -ieq $trustedName)
  $report.actual_bridge_process_running=($nodePid -gt 0 -and $report.trusted_identity_nonadmin)
  if(-not $report.actual_bridge_process_running){throw 'wrong_service_process_owner'}

  # No post-startup DACL patch is used here. The same full worker test
  # probes the already-protected real Node process after it loads secrets.
  $workerResult=Join-Path $worker 'worker-result.json'
  $args=@('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',
    ('"'+(Join-Path $worker 'probe.ps1')+'"'),'-TrustedDir',('"'+$trusted+'"'),
    '-WorkerDir',('"'+$worker+'"'),'-BridgeUrl',('"'+$url+'"'),
    '-BridgePid',"$nodePid",'-ResultPath',('"'+$workerResult+'"'))
  $process=Start-Process -FilePath "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -Credential $workerCred -ArgumentList $args -WorkingDirectory $worker -RedirectStandardOutput (Join-Path $worker 'stdout.log') -RedirectStandardError (Join-Path $worker 'stderr.log') -Wait -PassThru -ErrorAction Stop
  if($process.ExitCode -ne 0){throw ('worker_exit_'+$process.ExitCode)}
  if(-not (Test-Path -LiteralPath $workerResult)){throw 'worker_probe_missing'}
  $p=Get-Content -LiteralPath $workerResult -Raw | ConvertFrom-Json
  $report.worker_identity_nonadmin=($p.nonadmin -and $p.worker_identity.EndsWith(('\'+$workerName),[StringComparison]::OrdinalIgnoreCase))
  $report.worker_workspace_write=[bool]$p.workspace_write
  $report.worker_key_config_read_denied=-not [bool]$p.private_config_read
  $report.worker_receipt_read_denied=-not [bool]$p.receipt_read
  $report.worker_bridge_source_write_denied=-not [bool]$p.service_source_write
  $report.worker_browser_profile_canary_list_denied=-not [bool]$p.private_browser_profile_list
  $report.worker_bridge_http_unauthorized=($p.unsigned_health_http_status -eq 401)
  $report.worker_unsigned_send_unauthorized=($p.unsigned_send_http_status -eq 401)
  $report.worker_process_probe_executed=[bool]$p.process_vm_read_probe_executed
  $report.worker_process_vm_read_opened=$p.process_vm_read_opened
  $report.worker_process_probe_error=$p.process_probe_error
  $report.worker_process_memory_read_denied=($p.process_vm_read_probe_executed -and $p.process_vm_read_opened -eq $false)

  $after=Invoke-WebRequest -Uri ($url+'/health') -UseBasicParsing -Headers @{'x-was-token'=$configuration.bridge_token} -TimeoutSec 5 -ErrorAction Stop
  $report.service_survived_worker_probe=($after.StatusCode -eq 200)
  $checks=@(
    $report.actual_bridge_process_running, $report.trusted_identity_nonadmin,
    $report.worker_identity_nonadmin, $report.trusted_bridge_health_authorized,
    $report.worker_workspace_write, $report.worker_key_config_read_denied,
    $report.worker_receipt_read_denied, $report.worker_bridge_source_write_denied,
    $report.worker_browser_profile_canary_list_denied,
    $report.worker_bridge_http_unauthorized, $report.worker_unsigned_send_unauthorized,
    $report.worker_process_memory_read_denied, $report.service_survived_worker_probe,
    $report.process_created_suspended,
    $report.process_created_with_explicit_security_descriptor,
    $report.process_dacl_applied_before_key_load,
    $report.pre_resume_probe_executed,
    $report.pre_resume_worker_identity_nonadmin,
    $report.pre_resume_worker_vm_read_denied
  )
  if(($checks | Where-Object { -not $_ }).Count -eq 0) {
    $report.verdict='PASS_CREATION_TIME_PROCESS_BOUNDARY_ONLY'
  } else {
    $report.verdict='FAIL_CREATION_TIME_PROCESS_BOUNDARY'
  }
} catch {
  $report.verdict='INCONCLUSIVE_OR_FAIL'
  $report.error_class=$_.Exception.GetType().Name
  $report.error_stage=$_.Exception.Message
} finally {
  if($nodePid -gt 0) {
    try {[RB01CreateProtected]::StopSuspended($nodePid)} catch {}
    try {Stop-Process -Id $nodePid -Force -ErrorAction Stop} catch {}
  }
  if($trustedLauncher) {try {Stop-Process -Id $trustedLauncher.Id -Force -ErrorAction Stop} catch {}}
  if($trustedCreated) {try {Remove-LocalUser -Name $trustedName -ErrorAction Stop} catch {$report.cleanup_trusted_user_failed=$true}}
  if($workerCreated) {try {Remove-LocalUser -Name $workerName -ErrorAction Stop} catch {$report.cleanup_worker_user_failed=$true}}
  try {Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction Stop} catch {$report.cleanup_files_failed=$true}
  $report.completed_utc=[DateTime]::UtcNow.ToString('o')
  $report|ConvertTo-Json -Depth 7|Set-Content -LiteralPath $EvidencePath -Encoding UTF8
  Write-Output ($report|ConvertTo-Json -Depth 7)
}
if($report.verdict -eq 'PASS_CREATION_TIME_PROCESS_BOUNDARY_ONLY'){exit 0}
exit 1
