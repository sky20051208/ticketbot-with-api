# 用 Moonlight 連上遠大東京機的桌面（H.264 串流）。
#
# 預設 30fps 不是 60：這台是 m7i-flex.large，只有 2 核，實測 60fps 編碼會吃掉
# 約一半的機器（拓元那台 4 核只吃 21%）。平常登入帳號、看結帳頁 30fps 綽綽有餘，
# 而且把 CPU 留給搶票。真的想要 60fps 就改 vps.config.ps1 的 $TokyoStreamFps。

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "vps.config.ps1")

function Warn($m) { Write-Host "!!  $m" -ForegroundColor Yellow }

$exe = "C:\Program Files\Moonlight Game Streaming\Moonlight.exe"
if (-not (Test-Path $exe)) {
    Warn "找不到 Moonlight。先跑一次："
    Write-Host "      winget install --id MoonlightGameStreamingProject.Moonlight -e"
    Read-Host "按 Enter 關閉"; exit 1
}

# 公網 IP 每次開機都會換。tokyo_start.ps1 會寫在檔案裡，沒有才回頭問 AWS。
$ip = $null
if (Test-Path $TokyoIpFile) { $ip = (Get-Content $TokyoIpFile -ErrorAction SilentlyContinue | Select-Object -First 1) }
if (-not $ip) {
    Write-Host "==> 查詢東京機 IP" -ForegroundColor Cyan
    Push-Location $PSScriptRoot
    try { $ip = (python aws_tokyo.py ip).Trim() } finally { Pop-Location }
}
if (-not $ip) { Warn "拿不到 IP，機器可能沒開"; Read-Host "按 Enter 關閉"; exit 1 }

Write-Host "==> 連線到 $ip（1440x900 @ ${TokyoStreamFps}fps）" -ForegroundColor Cyan
Start-Process $exe -ArgumentList @(
    "stream", $ip, "Desktop",
    "--resolution", "1440x900",
    "--fps", "$TokyoStreamFps",
    "--bitrate", "$TokyoStreamBitrateKbps",
    "--video-codec", "H.264",
    "--display-mode", "windowed")

Write-Host ""
Write-Host "  串流中。Moonlight 快捷鍵：" -ForegroundColor Green
Write-Host "    Ctrl+Alt+Shift+S   顯示 / 隱藏效能資訊"
Write-Host "    Ctrl+Alt+Shift+Q   結束串流"
