# 用 Moonlight 連上美東 VPS 的桌面（H.264 串流）。
#
# 為什麼不是 VNC：VNC/Tight 是「每張畫面各自壓縮」的靜態圖編碼，實測捲一次頁面要
# 131KB，台灣↔Ashburn 這條路只有 2.1MB/s，換算下來 16fps 就是天花板 —— 那個
# 一格一格的頓挫感就是這樣來的。H.264 會做影格間預測，同一段捲動 60fps 只吃 3.9Mbps。
#
# vps_vnc.bat 留著沒刪：Sunshine 走 UDP 直連，要對外開埠；VNC 走 SSH 通道，
# 在只有 22 埠通得出去的網路（公司 / 飯店）它是唯一還能用的路。

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "vps.config.ps1")

function Warn($m) { Write-Host "!!  $m" -ForegroundColor Yellow }

$exe = "C:\Program Files\Moonlight Game Streaming\Moonlight.exe"
if (-not (Test-Path $exe)) {
    Warn "找不到 Moonlight。先跑一次："
    Write-Host "      winget install --id MoonlightGameStreamingProject.Moonlight -e"
    Write-Host ""
    Read-Host "按 Enter 關閉"
    exit 1
}

# 公網 IP 是 ephemeral 的，每次開機都會換號碼。vps_start.ps1 開機時會把它寫下來，
# 這裡優先讀那份；沒有（例如沒走面板直接點這支）才回頭問 OCI。
$ip = $null
if (Test-Path $VpsIpFile) { $ip = (Get-Content $VpsIpFile -ErrorAction SilentlyContinue | Select-Object -First 1) }
if (-not $ip) {
    if (-not (Get-Command oci -ErrorAction SilentlyContinue)) {
        Warn "沒有 $VpsIpFile 也沒有 oci CLI，查不到 VPS 的 IP。先用面板開機。"
        Read-Host "按 Enter 關閉"; exit 1
    }
    Write-Host "==> 查詢 VPS 公網 IP" -ForegroundColor Cyan
    $vnic = & oci compute instance list-vnics --instance-id $VpsInstanceOcid | ConvertFrom-Json
    $ip = $vnic.data[0].'public-ip'
}
if (-not $ip) { Warn "拿不到公網 IP，機器可能沒開"; Read-Host "按 Enter 關閉"; exit 1 }

Write-Host "==> 連線到 $ip（1440x900 @ ${VpsStreamFps}fps）" -ForegroundColor Cyan

# 解析度跟 VM 的 X 螢幕一模一樣，讓伺服器端不用縮放（縮放要多花 CPU 又糊）。
# bitrate 是上限不是固定值 —— 實測捲動只用到 3.9Mbps，留這麼多是給突發畫面用的，
# 但仍壓在路徑上限 16.8Mbps 之下，不會自己把線路塞爆。
$args = @(
    "stream", $ip, "Desktop",
    "--resolution", "1440x900",
    "--fps", "$VpsStreamFps",
    "--bitrate", "$VpsStreamBitrateKbps",
    "--video-codec", "H.264",
    "--display-mode", "windowed"
)
Start-Process $exe -ArgumentList $args

Write-Host ""
Write-Host "  串流中。Moonlight 快捷鍵：" -ForegroundColor Green
Write-Host "    Ctrl+Alt+Shift+S   顯示 / 隱藏效能資訊（fps、延遲、丟包）"
Write-Host "    Ctrl+Alt+Shift+Q   結束串流"
