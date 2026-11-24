# Disposable Windows CI probe: run as separate NONADMIN worker while
# restricted Node process is CREATE_SUSPENDED, before loading any secrets.
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][int]$TargetPid,
  [Parameter(Mandatory=$true)][string]$ResultPath
)
$ErrorActionPreference="Stop"
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class RB01PreResumeProbe {
 [DllImport("kernel32.dll",SetLastError=true)]
 public static extern IntPtr OpenProcess(uint desired,bool inherit,int pid);
 [DllImport("kernel32.dll",SetLastError=true)]
 public static extern bool CloseHandle(IntPtr handle);
}
'@ -ErrorAction Stop
$user=[Security.Principal.WindowsIdentity]::GetCurrent()
$p=New-Object Security.Principal.WindowsPrincipal($user)
$handle=[RB01PreResumeProbe]::OpenProcess([uint32]0x0010,$false,$TargetPid)
$granted=($handle -ne [IntPtr]::Zero)
if($granted){[RB01PreResumeProbe]::CloseHandle($handle)|Out-Null}
$report=[ordered]@{
  probe="RB01_PRE_RESUME_VM_READ"
  worker_identity=$user.Name
  worker_nonadmin=(-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))
  process_vm_read_granted=$granted
  process_vm_read_denied=(-not $granted)
}
[IO.File]::WriteAllText($ResultPath,(ConvertTo-Json -InputObject $report -Compress),[Text.Encoding]::UTF8)
Write-Output (ConvertTo-Json -InputObject $report -Compress)
if($report.worker_nonadmin -and $report.process_vm_read_denied){exit 0}
exit 1
