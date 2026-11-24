# Disposable Windows CI only. Request access handles, then close them.
# No memory read/write, thread mutation, credentials or browser effect.
param([int]$TargetPid,[string]$ThreadIds,[string]$ExpectedSid,[string]$ResultPath,[string]$LauncherCanary)
$ErrorActionPreference='Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class RB01Rights {
 [DllImport("kernel32.dll",SetLastError=true)] public static extern IntPtr OpenProcess(uint rights,bool inherit,int pid);
 [DllImport("kernel32.dll",SetLastError=true)] public static extern IntPtr OpenThread(uint rights,bool inherit,int tid);
 [DllImport("kernel32.dll")] public static extern int GetCurrentThreadId();
 [DllImport("kernel32.dll",SetLastError=true)] public static extern bool CloseHandle(IntPtr h);
}
'@
function Test-Right([string]$kind,[int]$target,[string]$name,[uint32]$mask) {
  if($kind -eq 'process') {$h=[RB01Rights]::OpenProcess($mask,$false,$target)}
  else {$h=[RB01Rights]::OpenThread($mask,$false,$target)}
  $err=[Runtime.InteropServices.Marshal]::GetLastWin32Error()
  $opened=$h -ne [IntPtr]::Zero
  if($opened){[RB01Rights]::CloseHandle($h)|Out-Null;$err=0}
  [pscustomobject]@{kind=$kind;target=$target;right=$name;opened=$opened;win32_error=$err}
}
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
$principal=[Security.Principal.WindowsPrincipal]::new($identity)
$procRights=[ordered]@{VM_READ=0x10;VM_WRITE=0x20;VM_OPERATION=0x8;DUP_HANDLE=0x40;CREATE_THREAD=0x2;TERMINATE=0x1;SET_INFORMATION=0x200;WRITE_DAC=0x40000;WRITE_OWNER=0x80000}
$threadRights=[ordered]@{GET_CONTEXT=0x8;SET_CONTEXT=0x10;SUSPEND_RESUME=0x2;TERMINATE=0x1;SET_INFORMATION=0x20;WRITE_DAC=0x40000;WRITE_OWNER=0x80000}
$checks=@()
foreach($n in $procRights.Keys){$checks+=Test-Right 'process' $TargetPid $n $procRights[$n]}
$tids=@($ThreadIds.Split(',')|ForEach-Object{[int]$_})
foreach($tid in $tids){foreach($n in $threadRights.Keys){$checks+=Test-Right 'thread' $tid $n $threadRights[$n]}}
$controlProcess=Test-Right 'process' $PID 'SELF_VM_READ' 0x10
$controlThread=Test-Right 'thread' ([RB01Rights]::GetCurrentThreadId()) 'SELF_GET_CONTEXT' 0x8
$canaryDenied=$false
try {
  $f=[IO.File]::Open($LauncherCanary,[IO.FileMode]::Open,[IO.FileAccess]::Write,[IO.FileShare]::ReadWrite)
  $f.Dispose()
} catch [UnauthorizedAccessException] { $canaryDenied=$true }
$wrong=@($checks|Where-Object{$_.opened -or $_.win32_error -ne 5})
$result=[ordered]@{
  exact_worker_sid=($identity.User.Value -eq $ExpectedSid)
  worker_nonadmin=(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))
  self_process_control=$controlProcess.opened
  self_thread_control=$controlThread.opened
  launcher_canary_write_denied=$canaryDenied
  count=$checks.Count
  target_threads=$tids.Count
  all_denied_error5=($checks.Count -gt 0 -and $wrong.Count -eq 0)
  unexpected=$wrong
  observations=$checks
}
$result.pass=($result.exact_worker_sid -and $result.worker_nonadmin -and $result.self_process_control -and $result.self_thread_control -and $result.launcher_canary_write_denied -and $result.all_denied_error5)
[IO.File]::WriteAllText($ResultPath,($result|ConvertTo-Json -Depth 6),[Text.Encoding]::UTF8)
if($result.pass){exit 0}
exit 1
