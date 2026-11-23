# RB-01 synthetic strict Node bridge launcher, invoked only as disposable
# trusted STANDARD account by GitHub-hosted Windows CI. Private config file,
# not argv or source, holds one-use synthetic token + signer. No real data.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$TrustedDir)
$ErrorActionPreference='Stop'
$cfg=Get-Content -LiteralPath (Join-Path $TrustedDir 'synthetic-config.json') -Raw | ConvertFrom-Json
$env:WEB_SWARM_SEND_MODE='relay_required'
$env:WEB_SWARM_RELAY_PERMIT_KEY=[string]$cfg.signer
$env:WEB_SWARM_BRIDGE_TOKEN=[string]$cfg.bridge_token
$env:WEB_SWARM_BIND='127.0.0.1'
$env:WEB_SWARM_PORT=[string]$cfg.port
$env:WEB_SWARM_RELAY_RECEIPTS_DIR=(Join-Path $TrustedDir 'receipts')
$env:WEB_SWARM_BRIDGE_TOKEN_FILE=(Join-Path $TrustedDir 'bridge-token-file')
$bridge=Join-Path $TrustedDir 'bridge'
Set-Location -LiteralPath $bridge
& ([string]$cfg.node_executable) (Join-Path $bridge 'server.js')
if ($LASTEXITCODE -ne 0) {exit $LASTEXITCODE}
