# 一鍵啟動美東搶票 VPS：開機 → 等 SSH → 開通道 → 打開 War-Room。
#
# VM 上的 Xvfb / webgui / noVNC 都是 systemd 服務（setup_vps.sh 裝的），開機自動起，
# 所以這支只負責「把機器叫醒 + 把埠接過來」。
#
# 用法：對著檔案按右鍵 →「用 PowerShell 執行」，或在終端機打 .\vps_start.ps1
#
# 前置（一次性）：
#   1. winget install Oracle.OCI-CLI
#   2. oci setup config     （產 API 金鑰，公鑰貼到 OCI Console → 你的使用者 → API Keys）
#   3. 編輯 vps.config.ps1 填入 InstanceOcid

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "vps.config.ps1")

function Say($msg)  { Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "!!  $msg" -ForegroundColor Yellow }

# TCP 連得上就算通。不用 Test-NetConnection —— 它在埠不通時要等很久。
function Wait-Port($TargetHost, $Port, $TimeoutSec) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $client = New-Object System.Net.Sockets.TcpClient
            $async = $client.BeginConnect($TargetHost, $Port, $null, $null)
            $ok = $async.AsyncWaitHandle.WaitOne(3000)
            if ($ok -and $client.Connected) { $client.Close(); return $true }
            $client.Close()
        } catch { }
        Start-Sleep -Seconds 3
        Write-Host "." -NoNewline
    }
    return $false
}

if ($VpsInstanceOcid -like "*請貼上*") {
    Warn "還沒設定 InstanceOcid —— 先編輯 vps.config.ps1"
    exit 1
}
if (-not (Get-Command oci -ErrorAction SilentlyContinue)) {
    Warn "找不到 oci CLI。先跑： winget install Oracle.OCI-CLI  然後  oci setup config"
    exit 1
}

# 不用 oci 的 --query：那是 JMESPath，`data."lifecycle-state"` 裡的雙引號在 PowerShell
# 的參數解析下會被吃掉，oci 收到的變成非法的 data.lifecycle-state。整包 JSON 拉回來自己解
# 最穩，也不用跟引號搏鬥。
function Get-VpsState {
    $json = & oci compute instance get --instance-id $VpsInstanceOcid | ConvertFrom-Json
    return $json.data.'lifecycle-state'
}

# --- 1. 開機（已經在跑就跳過）---
Say "查詢執行個體狀態"
$state = Get-VpsState
Write-Host "    目前：$state"

if ($state -ne "RUNNING") {
    Say "送出開機指令"
    # 不用 --wait-for-state：它會靜靜卡住，看起來像當掉。自己輪詢才顯示得出進度。
    & oci compute instance action --instance-id $VpsInstanceOcid --action START | Out-Null
    if ($LASTEXITCODE -ne 0) { Warn "開機失敗"; exit 1 }

    Say "等待進入 RUNNING（約 40 秒）"
    $deadline = (Get-Date).AddSeconds(300)
    while ((Get-Date) -lt $deadline) {
        $state = Get-VpsState
        if ($state -eq "RUNNING") { break }
        Write-Host "." -NoNewline
        Start-Sleep -Seconds 5
    }
    Write-Host ""
    if ($state -ne "RUNNING") { Warn "開機超時，目前狀態 $state"; exit 1 }
}

# --- 2. 取公網 IP ---
# ephemeral IP 每次重開都會換號碼，所以一定要現查，不能寫死在設定檔裡
Say "取得公網 IP"
$vnic = & oci compute instance list-vnics --instance-id $VpsInstanceOcid | ConvertFrom-Json
$ip = $vnic.data[0].'public-ip'
if (-not $ip) { Warn "拿不到公網 IP"; exit 1 }
Write-Host "    $ip"

# --- 3. 等 SSH ---
Say "等待 SSH（機器開機後服務還要幾十秒才會就緒）"
if (-not (Wait-Port $ip 22 $VpsBootTimeoutSec)) {
    Write-Host ""
    Warn "$VpsBootTimeoutSec 秒內 SSH 沒起來，去 Console 看看主控台輸出"
    exit 1
}
Write-Host ""

# --- 4. 收掉舊通道再開新的 ---
if (Test-Path $VpsTunnelPidFile) {
    $oldPid = Get-Content $VpsTunnelPidFile -ErrorAction SilentlyContinue
    if ($oldPid) { Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue }
    Remove-Item $VpsTunnelPidFile -ErrorAction SilentlyContinue
}

Say "建立 SSH 通道（webgui $VpsGuiLocalPort / noVNC $VpsVncLocalPort）"
# accept-new：IP 每次都換，不預先接受主機金鑰的話 ssh 會停在互動提示等輸入
$sshArgs = @(
    "-N",
    "-i", "`"$VpsKeyPath`"",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ServerAliveInterval=30",
    "-L", "$($VpsGuiLocalPort):127.0.0.1:$VpsGuiRemotePort",
    "-L", "$($VpsVncLocalPort):127.0.0.1:$VpsVncRemotePort",
    "$VpsUser@$ip"
)
$tunnel = Start-Process ssh -ArgumentList $sshArgs -PassThru -WindowStyle Minimized
$tunnel.Id | Set-Content $VpsTunnelPidFile
Start-Sleep -Seconds 3

# --- 5. 等 webgui 真的回應（systemd 起服務也要幾秒）---
Say "等待 War-Room 就緒"
if (-not (Wait-Port "127.0.0.1" $VpsGuiLocalPort 90)) {
    Write-Host ""
    Warn "通道通了但 webgui 沒回應。上去看 systemctl status tixcraft-webgui："
    Write-Host "    ssh -i $VpsKeyPath $VpsUser@$ip"
    exit 1
}
Write-Host ""

Start-Process "http://localhost:$VpsGuiLocalPort"

Write-Host ""
Write-Host "  美東 War-Room : http://localhost:$VpsGuiLocalPort  （拓元）" -ForegroundColor Green
Write-Host "  遠端畫面      : http://localhost:$VpsVncLocalPort/vnc.html  （換帳號重登 / 看結帳頁時用）"
Write-Host "  SSH      : ssh -i $VpsKeyPath $VpsUser@$ip"
Write-Host ""
Write-Host "  用完請執行 .\vps_stop.ps1 關機 —— 忘記關是每月 76 美金 vs 3 美金的差別" -ForegroundColor Yellow
