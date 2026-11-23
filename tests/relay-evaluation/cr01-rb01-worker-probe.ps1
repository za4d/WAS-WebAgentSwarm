# Synthetic RB-01 worker access probe. Runs only under a fresh STANDARD local user
# created by cr01-rb01-windows-identity.ps1 on an ephemeral CI host.
# No real HMAC keys, browser data, user paths or production credentials.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ProtectedDir,
    [Parameter(Mandatory=$true)][string]$WorkspaceDir,
    [Parameter(Mandatory=$true)][string]$ResultPath
)
$ErrorActionPreference = 'Stop'
function Probe([scriptblock]$action) {
    try {
        & $action | Out-Null
        return $true
    } catch {
        return $false
    }
}
$current = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($current)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$results = [ordered]@{
    identity = $current.Name
    administrator = $isAdmin
    own_workspace_write = (Probe { Set-Content -LiteralPath (Join-Path $WorkspaceDir 'worker-canary.txt') -Value 'WORKER_WRITE_OK' -ErrorAction Stop })
    read_dummy_key = (Probe { Get-Content -LiteralPath (Join-Path $ProtectedDir 'dummy-key.txt') -ErrorAction Stop })
    read_receipt = (Probe { Get-Content -LiteralPath (Join-Path $ProtectedDir 'receipt.json') -ErrorAction Stop })
    overwrite_bridge_config = (Probe { Set-Content -LiteralPath (Join-Path $ProtectedDir 'bridge-config.json') -Value '{}' -ErrorAction Stop })
    remove_receipt = (Probe { Remove-Item -LiteralPath (Join-Path $ProtectedDir 'receipt.json') -Force -ErrorAction Stop })
    list_protected_dir = (Probe { Get-ChildItem -LiteralPath $ProtectedDir -ErrorAction Stop })
}
$json = ConvertTo-Json -InputObject $results -Compress -Depth 4
[IO.File]::WriteAllText($ResultPath, $json, [Text.Encoding]::UTF8)
