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

function Wait-Stopped($TimeoutSec) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $state = ""
    while ((Get-Date) -lt $deadline) {
        $json = & oci compute instance get --instance-id $VpsInstanceOcid | ConvertFrom-Json
        $state = $json.data.'lifecycle-state'
        if ($state -eq "STOPPED") { return $state }
        Write-Host "." -NoNewline
        Start-Sleep -Seconds 5
    }
    return $state
}

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

Say "送出關機指令（SOFTSTOP，讓 OS 正常關閉）"
# SOFTSTOP = 送 ACPI 訊號讓 systemd 服務正常收尾；STOP 是直接斷電。
# 不用 --wait-for-state：它會靜靜卡住好幾分鐘，看起來像當掉。自己輪詢才能顯示進度。
& oci compute instance action --instance-id $VpsInstanceOcid --action SOFTSTOP | Out-Null
if ($LASTEXITCODE -ne 0) {
    Warn "關機指令失敗 —— 請去 Console 手動確認機器狀態，別讓它空轉計費"
    exit 1
}

Say "等待關機完成（ACPI 關機通常 1~3 分鐘）"
# 不用 oci 的 --query：那是 JMESPath，`data."lifecycle-state"` 裡的雙引號在 PowerShell
# 的參數解析下會被吃掉，oci 收到的變成非法的 data.lifecycle-state。整包 JSON 自己解最穩。
$state = Wait-Stopped -TimeoutSec 180
Write-Host ""

# SOFTSTOP 卡住通常是 OS 端有服務不肯結束（Chrome / webgui / xrdp），systemd 每個服務
# 等 90 秒才放棄，疊起來輕鬆破五分鐘；OCI 要 15 分鐘才自己強制斷電。與其讓使用者乾等
# 或忘記關（那才是真正燒錢的情況），三分鐘後直接強制斷電。ext4 有 journal，不會壞資料。
if ($state -ne "STOPPED") {
    Warn "SOFTSTOP 超過 3 分鐘還沒完成（目前 $state），試著改用強制斷電"
    # 前一個動作還在進行時 OCI 會回 "currently being modified" 拒絕第二個動作。
    # 這不是錯誤 —— SOFTSTOP 本身有 15 分鐘上限，超時 OCI 自己會斷電，繼續等就好。
    & oci compute instance action --instance-id $VpsInstanceOcid --action STOP 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    OCI 還在處理前一個動作，繼續等它自己完成（上限 15 分鐘）"
    }
    $state = Wait-Stopped -TimeoutSec 780
    Write-Host ""
}

if ($state -ne "STOPPED") {
    Warn "等了 16 分鐘狀態還是 $state —— 請去 Console 手動確認，別讓它空轉計費"
    exit 1
}

Write-Host ""
Write-Host "  已關機。運算費用停止，只剩開機磁碟約 1.3 美金/月。" -ForegroundColor Green
Write-Host "  下次開工：.\vps_start.ps1"
