# CR01/RB01 actual-bridge negative probe. Starts as a separate local,
# NONADMIN coding-worker account on a disposable GitHub-hosted Windows runner.
# Do not call this script with live credentials, browsers or owner profile.
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$TrustedDir,
  [Parameter(Mandatory=$true)][string]$WorkerDir,
  [Parameter(Mandatory=$true)][string]$BridgeUrl,
  [Parameter(Mandatory=$true)][int]$BridgePid,
  [Parameter(Mandatory=$true)][string]$ResultPath
)
$ErrorActionPreference='Stop'
function Allowed([scriptblock]$block) {
  try {& $block | Out-Null;return $true} catch {return $false}
}
function Status([scriptblock]$block) {
  try {$r=& $block;return [int]$r.StatusCode}
  catch {
    if ($_.Exception.Response) {return [int]$_.Exception.Response.StatusCode}
    return -1
  }
}
$current=[Security.Principal.WindowsIdentity]::GetCurrent()
$principal=New-Object Security.Principal.WindowsPrincipal($current)
$observed=[ordered]@{
  worker_identity=$current.Name
  nonadmin=(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))
  workspace_write=(Allowed {Set-Content -LiteralPath (Join-Path $WorkerDir 'canary.txt') -Value 'worker-ok' -ErrorAction Stop})
  private_config_read=(Allowed {Get-Content -LiteralPath (Join-Path $TrustedDir 'synthetic-config.json') -ErrorAction Stop})
  receipt_read=(Allowed {Get-Content -LiteralPath (Join-Path $TrustedDir 'receipts\existing-receipt.json') -ErrorAction Stop})
  service_source_write=(Allowed {Set-Content -LiteralPath (Join-Path $TrustedDir 'bridge\server.js') -Value 'malicious' -ErrorAction Stop})
  private_browser_profile_list=(Allowed {Get-ChildItem -LiteralPath (Join-Path $TrustedDir 'browser-profile-canary') -ErrorAction Stop})
  unsigned_health_http_status=(Status {Invoke-WebRequest -UseBasicParsing -Uri ($BridgeUrl+'/health') -TimeoutSec 4 -ErrorAction Stop})
  unsigned_send_http_status=(Status {Invoke-WebRequest -UseBasicParsing -Method Post -Uri ($BridgeUrl+'/send') -Body '{}' -ContentType 'application/json' -TimeoutSec 4 -ErrorAction Stop})
  process_vm_read_probe_executed=$false
  process_vm_read_opened=$null
}
try {
  Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class RB01ProcessProbe {
 [DllImport("kernel32.dll",SetLastError=true)]
 public static extern IntPtr OpenProcess(uint rights,bool inherit,int pid);
 [DllImport("kernel32.dll",SetLastError=true)]
 public static extern bool CloseHandle(IntPtr handle);
}
'@ -ErrorAction Stop
  $observed.process_vm_read_probe_executed=$true
  $handle=[RB01ProcessProbe]::OpenProcess(0x0010,$false,$BridgePid)
  $observed.process_vm_read_opened=($handle -ne [IntPtr]::Zero)
  if ($handle -ne [IntPtr]::Zero) {[RB01ProcessProbe]::CloseHandle($handle)|Out-Null}
} catch {
  $observed.process_probe_error=$_.Exception.GetType().Name
}
$json=ConvertTo-Json -InputObject $observed -Depth 5
[IO.File]::WriteAllText($ResultPath,$json,[Text.Encoding]::UTF8)
Write-Output $json
