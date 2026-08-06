# 一鍵啟動遠大東京機：開機 → 同步安全群組 → 開通道 → 打開 War-Room。
#
# EC2 的部分全在 aws_tokyo.py（boto3），這裡只負責通道和開瀏覽器 —— 邏輯不要兩邊各寫一份。
#
# 為什麼遠大要另外一台：TicketPlus 的 origin 在 AWS 東京，實測東京→queue 只有 2.1ms，
# 台灣是 66ms。拓元那台在美東是因為 tixcraft 的 origin 在美東，兩邊剛好相反。

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "vps.config.ps1")

function Say($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "!!  $m" -ForegroundColor Yellow }

function Wait-Port($TargetHost, $Port, $TimeoutSec) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $c = New-Object System.Net.Sockets.TcpClient
            $a = $c.BeginConnect($TargetHost, $Port, $null, $null)
            if ($a.AsyncWaitHandle.WaitOne(3000) -and $c.Connected) { $c.Close(); return $true }
            $c.Close()
        } catch { }
        Start-Sleep -Seconds 3
        Write-Host "." -NoNewline
    }
    return $false
}

Push-Location $PSScriptRoot
try {
    # aws_tokyo.py start 會一併把安全群組的來源同步成你當下的對外 IP
    Say "開機（AWS ap-northeast-1）"
    python aws_tokyo.py start
    if ($LASTEXITCODE -ne 0) { Warn "開機失敗"; exit 1 }

    $ip = (python aws_tokyo.py ip).Trim()
    if (-not $ip) { Warn "拿不到公網 IP"; exit 1 }
    Set-Content -Path $TokyoIpFile -Value $ip -Encoding ascii
} finally { Pop-Location }

Say "等待 SSH"
if (-not (Wait-Port $ip 22 240)) { Write-Host ""; Warn "SSH 沒起來"; exit 1 }
Write-Host ""

# 收掉舊通道再開新的
if (Test-Path $TokyoTunnelPidFile) {
    $old = Get-Content $TokyoTunnelPidFile -ErrorAction SilentlyContinue
    if ($old) { Stop-Process -Id $old -Force -ErrorAction SilentlyContinue }
    Remove-Item $TokyoTunnelPidFile -ErrorAction SilentlyContinue
}

Say "建立 SSH 通道（War-Room $TokyoGuiLocalPort / VNC $TokyoVncRawLocalPort / Sunshine UI $TokyoSunshineLocalPort）"
$sshArgs = @(
    "-N", "-i", "`"$TokyoKeyPath`"",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ServerAliveInterval=30",
    "-L", "$($TokyoGuiLocalPort):127.0.0.1:$TokyoGuiRemotePort",
    "-L", "$($TokyoVncRawLocalPort):127.0.0.1:$TokyoVncRawRemotePort",
    "-L", "$($TokyoSunshineLocalPort):127.0.0.1:$TokyoSunshineRemotePort",
    "$TokyoUser@$ip"
)
$t = Start-Process ssh -ArgumentList $sshArgs -PassThru -WindowStyle Minimized
$t.Id | Set-Content $TokyoTunnelPidFile
Start-Sleep -Seconds 3

Say "等待 War-Room 就緒"
if (-not (Wait-Port "127.0.0.1" $TokyoGuiLocalPort 90)) {
    Write-Host ""
    Warn "通道通了但 webgui 沒回應。上去看 systemctl status tixcraft-webgui："
    Write-Host "    ssh -i $TokyoKeyPath $TokyoUser@$ip"
    exit 1
}
Write-Host ""
Start-Process "http://localhost:$TokyoGuiLocalPort"

Write-Host ""
Write-Host "  遠大 War-Room : http://localhost:$TokyoGuiLocalPort" -ForegroundColor Green
Write-Host "  遠端桌面      : 面板的「遠端桌面」鈕，或 tokyo_moonlight.bat"
Write-Host "  遠端桌面(備援): VNC 客戶端連 localhost:$TokyoVncRawLocalPort"
Write-Host "  Sunshine 設定 : https://localhost:$TokyoSunshineLocalPort"
Write-Host "  SSH           : ssh -i $TokyoKeyPath $TokyoUser@$ip"
Write-Host ""
Write-Host "  用完請關機 —— m7i-flex.large 開著是一天 2 美金" -ForegroundColor Yellow
