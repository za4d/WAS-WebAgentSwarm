# Synthetic RB-01 **post-launch** Windows PROCESS object DACL comparison.
# Only use on disposable GitHub CI VM, exact Node PID and synthetic test key.
# Post-launch ACL changes have a pre-hardening race; NOT a deployment fix.
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][int]$TargetPid,
  [Parameter(Mandatory=$true)][string]$TrustedSid
)
$ErrorActionPreference='Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class RB01ProcessDacl {
 [DllImport("kernel32.dll",SetLastError=true)]
 public static extern IntPtr OpenProcess(uint access,bool inherit,int pid);
 [DllImport("kernel32.dll",SetLastError=true)]
 public static extern bool CloseHandle(IntPtr h);
 [DllImport("advapi32.dll",SetLastError=true)]
 [return:MarshalAs(UnmanagedType.Bool)]
 public static extern bool SetKernelObjectSecurity(IntPtr h,uint info,byte[] sd);
}
'@ -ErrorAction Stop
# Give process access to trusted user + SYSTEM + Administrators only.
# Do not grant BUILTIN\\Users, Authenticated Users or Everyone.
$sddl='D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;'+$TrustedSid+')'
$descriptor=New-Object System.Security.AccessControl.RawSecurityDescriptor($sddl)
$buf=New-Object byte[] $descriptor.BinaryLength
$descriptor.GetBinaryForm($buf,0)
$WRITE_DAC=[uint32]0x00040000
$handle=[RB01ProcessDacl]::OpenProcess($WRITE_DAC,$false,$TargetPid)
if($handle -eq [IntPtr]::Zero){throw 'Cannot open target process for WRITE_DAC'}
try {
  $success=[RB01ProcessDacl]::SetKernelObjectSecurity($handle,[uint32]4,$buf)
  if(-not $success) {
    $errorCode=[Runtime.InteropServices.Marshal]::GetLastWin32Error()
    throw ('SetKernelObjectSecurity failed error code '+$errorCode)
  }
} finally {
  [RB01ProcessDacl]::CloseHandle($handle)|Out-Null
}
Write-Output 'RB01_SYNTHETIC_PROCESS_DACL_APPLIED'
