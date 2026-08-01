# 關掉美東搶票 VPS：收通道 → 關機。
#
# **這支比 vps_start.ps1 更重要**：機器掛著跑一整個月是 $76，用完就關是 $3
# （停機只收開機磁碟的錢，環境和資料全部保留，下次開機兩分鐘回來）。
#
# 用法：.\vps_stop.ps1
#       .\vps_stop.ps1 -TunnelOnly    只收通道、機器繼續跑

param([switch]$TunnelOnly)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "vps.config.ps1")

function Say($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "!!  $msg" -ForegroundColor Yellow }

# --- 1. 收通道 ---
Say "關閉 SSH 通道"
if (Test-Path $VpsTunnelPidFile) {
    $tunnelPid = Get-Content $VpsTunnelPidFile -ErrorAction SilentlyContinue
    if ($tunnelPid) {
        Stop-Process -Id $tunnelPid -Force -ErrorAction SilentlyContinue
        Write-Host "    已關閉 (PID $tunnelPid)"
    }
    Remove-Item $VpsTunnelPidFile -ErrorAction SilentlyContinue
} else {
    Write-Host "    沒有記錄中的通道"
}

if ($TunnelOnly) {
    Write-Host "`n  機器仍在執行中（-TunnelOnly）。真的要關機請不帶參數重跑。" -ForegroundColor Yellow
    exit 0
}

# --- 2. 關機 ---
if (-not (Get-Command oci -ErrorAction SilentlyContinue)) {
    Warn "找不到 oci CLI，請自己去 Console 把 $VpsInstanceOcid 停掉"
    exit 1
}

Say "關機中（SOFTSTOP，會讓 OS 正常關閉）"
# SOFTSTOP = 送 ACPI 關機訊號，讓 systemd 服務正常收尾；STOP 是直接斷電
& oci compute instance action --instance-id $VpsInstanceOcid --action SOFTSTOP --wait-for-state STOPPED | Out-Null

if ($LASTEXITCODE -ne 0) {
    Warn "關機指令失敗 —— 請去 Console 手動確認機器狀態，別讓它空轉計費"
    exit 1
}

Write-Host ""
Write-Host "  已關機。運算費用停止，只剩開機磁碟約 1.3 美金/月。" -ForegroundColor Green
Write-Host "  下次開工：.\vps_start.ps1"
