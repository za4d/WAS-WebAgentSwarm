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
$taskName='was_rb01_'+$id
$taskRegistered=$false
$trustedCreated=$false
$workerCreated=$false
$report=[ordered]@{
  case='RB01-DEPLOYMENT-RIGHTS-STRICT-BRIDGE-01'
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
  system_privileged_launch_task_used=$false
  system_launcher_identity_verified=$false
  pre_resume_worker_vm_read_denied=$false
  pre_resume_worker_identity_nonadmin=$false
  pre_resume_probe_executed=$false
  pre_resume_rights=$null
  post_resume_rights=$null
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
  & icacls.exe $root '/inheritance:r' '/grant:r' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
  if($LASTEXITCODE -ne 0){throw 'root_acl'}
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

  # A normal elevated GitHub runner lacks SeAssignPrimaryToken/
  # SeIncreaseQuota (CreateProcessAsUserW returns ERROR_PRIVILEGE_NOT_HELD
  # 1314). A disposable scheduled task running as LocalSystem provides the
  # proper Windows service-like privilege context. Node itself is launched
  # under the *separate standard* trusted account, never SYSTEM.
  # All account credentials in this file are random synthetic CI values.
  $systemInput=[ordered]@{
    username=$trustedName
    temporary_account_credential=$trustedCred.GetNetworkCredential().Password
    node_executable=$exe
    port=$port
    signer=[string]$configuration.signer
    bridge_token=[string]$configuration.bridge_token
    trusted_sid=$trustedSid
  }
  $systemInput|ConvertTo-Json -Depth 4|Set-Content -LiteralPath (Join-Path $trusted 'system-test-identity.json') -Encoding UTF8
  # Protect the privileged task action and its compiler input.
  $launcher=Join-Path $root 'launcher'
  New-Item -ItemType Directory -Path $launcher -Force|Out-Null
  & icacls.exe $launcher '/inheritance:r' '/grant:r' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
  if($LASTEXITCODE -ne 0){throw 'launcher_acl'}
  Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'cr01-rb01-system-task-launch-ci.ps1') -Destination $launcher
  Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'cr01-rb01-createprotected-ci.cs') -Destination $launcher
  $systemScript=Join-Path $launcher 'cr01-rb01-system-task-launch-ci.ps1'
  $probePath=Join-Path $worker 'rights-probe.ps1'
  Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'cr01-rb01-deployment-rights-probe.ps1') -Destination $probePath
  function Invoke-RightsProbe([string]$phase) {
    $proc=Get-Process -Id $nodePid -ErrorAction Stop
    $threads=@($proc.Threads|ForEach-Object{$_.Id})
    if($threads.Count -eq 0){throw 'no_target_threads'}
    $out=Join-Path $worker ($phase+'-rights.json')
    $args=@('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',
      ('"'+$probePath+'"'),'-TargetPid',"$nodePid",'-ThreadIds',($threads -join ','),
      '-ExpectedSid',$workerSid,'-ResultPath',('"'+$out+'"'),'-LauncherCanary',('"'+$systemScript+'"'))
    $p=Start-Process -FilePath "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -Credential $workerCred -ArgumentList $args -WorkingDirectory $worker -Wait -PassThru -ErrorAction Stop
    if(-not (Test-Path -LiteralPath $out)){throw 'rights_probe_missing'}
    $res=Get-Content -LiteralPath $out -Raw|ConvertFrom-Json
    $res|Add-Member -NotePropertyName exit_code -NotePropertyValue $p.ExitCode
    return $res
  }
  $taskArgs='-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'+$systemScript+'" -TrustedDir "'+$trusted+'"'
  $action=New-ScheduledTaskAction -Execute "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument $taskArgs
  $principal=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
  $settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 3)
  Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Force -ErrorAction Stop|Out-Null
  $taskRegistered=$true
  Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
  $systemState=Join-Path $trusted 'system-launch-status.json'
  $systemStateReady=$false
  for($n=0;$n -lt 150;$n++){
    if(Test-Path -LiteralPath $systemState){$systemStateReady=$true;break}
    Start-Sleep -Milliseconds 300
  }
  if(-not $systemStateReady){throw 'system_task_missing_creation_time_result'}
  $stage=Get-Content -LiteralPath $systemState -Raw|ConvertFrom-Json
  if($stage.stage -ne 'SUSPENDED_PROTECTED_AT_CREATION'){
    throw ('system_task_creation_failed_'+[string]$stage.error)
  }
  $nodePid=[int]$stage.target_pid
  if($nodePid -le 0){throw 'creation_time_process_pid_missing'}
  $report.system_privileged_launch_task_used=$true
  $report.system_launcher_identity_verified=([string]$stage.launcher_identity -ieq 'NT AUTHORITY\SYSTEM')
  $report.process_created_suspended=$true
  $report.process_created_with_explicit_security_descriptor=[bool]$stage.process_dacl_at_creation
  $report.process_dacl_applied_before_key_load=[bool]$stage.thread_dacl_at_creation
  if(-not $report.system_launcher_identity_verified){throw 'system_launcher_identity_not_verified'}

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
  $report.pre_resume_rights=Invoke-RightsProbe 'pre'
  if(-not $report.pre_resume_rights.pass -or $report.pre_resume_rights.exit_code -ne 0){throw 'pre_resume_rights_not_denied'}
  # Only the privileged creator holds the process's suspended thread handle.
  # Allow it to resume after the independent worker probe was denied.
  Set-Content -LiteralPath (Join-Path $trusted 'resume-after-worker-check') -Value 'PROBE_DENIED'
  $resumeStatus=Join-Path $trusted 'system-resumed'
  $didResume=$false
  for($n=0;$n -lt 80;$n++){
    if(Test-Path -LiteralPath $resumeStatus){$didResume=$true;break}
    Start-Sleep -Milliseconds 250
  }
  if(-not $didResume){throw 'system_launcher_did_not_resume_protected_process'}

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

  $report.post_resume_rights=Invoke-RightsProbe 'post'
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
    $report.system_privileged_launch_task_used,
    $report.system_launcher_identity_verified,
    $report.pre_resume_probe_executed,
    $report.pre_resume_worker_identity_nonadmin,
    $report.pre_resume_worker_vm_read_denied,
    ($report.pre_resume_rights.pass -and $report.pre_resume_rights.exit_code -eq 0),
    ($report.post_resume_rights.pass -and $report.post_resume_rights.exit_code -eq 0)
  )
  if(($checks | Where-Object { -not $_ }).Count -eq 0) {
    $report.verdict='PASS_DEPLOYMENT_RIGHTS_ONLY'
  } else {
    $report.verdict='FAIL_DEPLOYMENT_RIGHTS'
  }
} catch {
  $report.verdict='INCONCLUSIVE_OR_FAIL'
  $report.error_class=$_.Exception.GetType().Name
  $report.error_stage=$_.Exception.Message
} finally {
  if($taskRegistered){
    try {Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue} catch {}
    try {Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop} catch {$report.cleanup_task_failed=$true}
  }
  if($nodePid -gt 0) {try {Stop-Process -Id $nodePid -Force -ErrorAction Stop} catch {}}
  if($trustedLauncher) {try {Stop-Process -Id $trustedLauncher.Id -Force -ErrorAction Stop} catch {}}
  if($trustedCreated) {try {Remove-LocalUser -Name $trustedName -ErrorAction Stop} catch {$report.cleanup_trusted_user_failed=$true}}
  if($workerCreated) {try {Remove-LocalUser -Name $workerName -ErrorAction Stop} catch {$report.cleanup_worker_user_failed=$true}}
  try {Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction Stop} catch {$report.cleanup_files_failed=$true}
  $report.completed_utc=[DateTime]::UtcNow.ToString('o')
  $report|ConvertTo-Json -Depth 7|Set-Content -LiteralPath $EvidencePath -Encoding UTF8
  Write-Output ($report|ConvertTo-Json -Depth 7)
}
if($report.verdict -eq 'PASS_DEPLOYMENT_RIGHTS_ONLY'){exit 0}
exit 1
