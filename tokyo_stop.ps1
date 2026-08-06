# 關掉遠大東京機，並收乾淨 SSH 通道。

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "vps.config.ps1")

function Say($m) { Write-Host "==> $m" -ForegroundColor Cyan }

if (Test-Path $TokyoTunnelPidFile) {
    Say "收掉 SSH 通道"
    $old = Get-Content $TokyoTunnelPidFile -ErrorAction SilentlyContinue
    if ($old) { Stop-Process -Id $old -Force -ErrorAction SilentlyContinue }
    Remove-Item $TokyoTunnelPidFile -ErrorAction SilentlyContinue
}
Remove-Item $TokyoIpFile -ErrorAction SilentlyContinue

Push-Location $PSScriptRoot
try {
    Say "關機（AWS ap-northeast-1）"
    python aws_tokyo.py stop
} finally { Pop-Location }

Write-Host ""
Write-Host "  已關機。磁碟還在，下次 start 資料都還在。" -ForegroundColor Green
