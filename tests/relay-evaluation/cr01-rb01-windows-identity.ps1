# CR01/RB01: DISPOSABLE Windows identity/ACL feasibility experiment.
# Run only on an ephemeral GitHub-hosted Windows runner, NOT the owner's PC.
# Synthetic canary files only; no real HMAC key, Chrome profile, service,
# network sender, credentials from existing accounts, or browser effects.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$EvidencePath)
$ErrorActionPreference = 'Stop'
$id = [guid]::NewGuid().ToString('N').Substring(0,10)
$user = 'wasrb01_' + $id
$root = Join-Path $env:ProgramData ('WAS_RB01_' + $id)
$trusted = Join-Path $root 'trusted'
$workspace = Join-Path $root 'worker'
$out = Join-Path $workspace 'worker-result.json'
$probe = Join-Path $workspace 'probe.ps1'
$report = [ordered]@{
    case = 'CR01-RB01-WINDOWS-IDENTITY-01'
    provenance = 'REAL WINDOWS ACL + STANDARD ACCOUNT ON EPHEMERAL GITHUB VM'
    actual_owner_machine_tested = $false
    trusted_service_installed = $false
    same_principal_can_read_dummy_key = $false
    child_worker_identity_confirmed = $false
    worker_nonadmin = $false
    worker_can_write_disposable_workspace = $false
    worker_key_access_denied = $false
    worker_receipt_read_denied = $false
    worker_config_write_denied = $false
    worker_receipt_delete_denied = $false
    worker_directory_listing_denied = $false
    verdict = 'INCONCLUSIVE'
    error_type = $null
}
$created = $false
try {
    New-Item -ItemType Directory -Path $trusted -Force | Out-Null
    New-Item -ItemType Directory -Path $workspace -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $trusted 'dummy-key.txt') -Value 'SYNTHETIC_ONLY'
    Set-Content -LiteralPath (Join-Path $trusted 'bridge-config.json') -Value '{"mode":"relay_required"}'
    Set-Content -LiteralPath (Join-Path $trusted 'receipt.json') -Value '{"attempt":"synthetic"}'
    $report.same_principal_can_read_dummy_key =
        ((Get-Content -LiteralPath (Join-Path $trusted 'dummy-key.txt') -Raw).Trim() -eq 'SYNTHETIC_ONLY')
    $pass = 'Aa1!' + [guid]::NewGuid().ToString('N') + 'Bz2!'
    $sec = ConvertTo-SecureString $pass -AsPlainText -Force
    $null = New-LocalUser -Name $user -Password $sec -AccountNeverExpires -ErrorAction Stop
    $created = $true
    $sid = (Get-LocalUser -Name $user).SID.Value
    $creds = New-Object System.Management.Automation.PSCredential(($env:COMPUTERNAME + '\' + $user), $sec)
    $pass = $null
    & icacls.exe $root '/grant' ('*' + $sid + ':(RX)') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'root-traverse-acl' }
    & icacls.exe $workspace '/grant' ('*' + $sid + ':(OI)(CI)M') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'workspace-acl' }
    & icacls.exe $trusted '/inheritance:r' '/grant:r' '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'trusted-acl' }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'cr01-rb01-worker-probe.ps1') -Destination $probe
    $arguments = @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',
        ('"' + $probe + '"'),'-ProtectedDir',('"' + $trusted + '"'),
        '-WorkspaceDir',('"' + $workspace + '"'),
        '-ResultPath',('"' + $out + '"'))
    $process = Start-Process -FilePath "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" -ArgumentList $arguments -WorkingDirectory $workspace -Credential $creds -RedirectStandardOutput (Join-Path $workspace 'out.log') -RedirectStandardError (Join-Path $workspace 'err.log') -Wait -PassThru -ErrorAction Stop
    if ($process.ExitCode -ne 0) { throw ('worker-exit-' + $process.ExitCode) }
    if (-not (Test-Path -LiteralPath $out)) { throw 'no-worker-result' }
    $p = Get-Content -LiteralPath $out -Raw | ConvertFrom-Json
    $report.child_worker_identity_confirmed = $p.identity.EndsWith(('\' + $user),[StringComparison]::OrdinalIgnoreCase)
    $report.worker_nonadmin = -not [bool]$p.administrator
    $report.worker_can_write_disposable_workspace = [bool]$p.own_workspace_write
    $report.worker_key_access_denied = -not [bool]$p.read_dummy_key
    $report.worker_receipt_read_denied = -not [bool]$p.read_receipt
    $report.worker_config_write_denied = -not [bool]$p.overwrite_bridge_config
    $report.worker_receipt_delete_denied = -not [bool]$p.remove_receipt
    $report.worker_directory_listing_denied = -not [bool]$p.list_protected_dir
    $checks = @(
        $report.same_principal_can_read_dummy_key,
        $report.child_worker_identity_confirmed,
        $report.worker_nonadmin,
        $report.worker_can_write_disposable_workspace,
        $report.worker_key_access_denied,
        $report.worker_receipt_read_denied,
        $report.worker_config_write_denied,
        $report.worker_receipt_delete_denied,
        $report.worker_directory_listing_denied
    )
    if (($checks | Where-Object { -not $_ }).Count -eq 0) {
        $report.verdict = 'PASS_FEASIBILITY_ONLY'
    } else {
        $report.verdict = 'FAIL_ACCESS_CONTRACT'
    }
} catch {
    $report.verdict = 'INCONCLUSIVE_HARNESS_OR_PRIVILEGES'
    $report.error_type = $_.Exception.GetType().Name
} finally {
    if ($created) {
        try { Remove-LocalUser -Name $user -ErrorAction Stop } catch {
            $report.cleanup_user_failed = $true
        }
    }
    try { Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction Stop } catch {
        $report.cleanup_directory_failed = $true
    }
    $report.completed_utc = [DateTime]::UtcNow.ToString('o')
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $EvidencePath -Encoding UTF8
    Write-Output ($report | ConvertTo-Json -Depth 5)
}
if ($report.verdict -eq 'PASS_FEASIBILITY_ONLY') { exit 0 }
exit 1
