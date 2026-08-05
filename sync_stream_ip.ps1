# 把 Sunshine 串流埠的來源限制同步成「你家當下的對外 IP」。
#
# 為什麼需要這支：Sunshine 走 UDP，穿不了 SSH 通道，所以那幾個埠一定得對外開。
# 那台機器上有所有登入中的購票帳號和 cookie，不該讓服務直接曝在公網上，
# 所以來源限制在單一 IP。而家用 IP 本來就會變（搬家、ISP 重撥），
# 手動維護白名單遲早會忘 —— 由 vps_start.ps1 每次開機自動叫這支就沒這問題。
#
# 用法：
#   .\sync_stream_ip.ps1            同步成目前的對外 IP
#   .\sync_stream_ip.ps1 -Remove    把規則整個移掉（不打算再用串流時）

param([switch]$Remove)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "vps.config.ps1")

function Say($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "!!  $m" -ForegroundColor Yellow }

# 認得出「哪幾條規則是這支腳本管的」。改動只碰帶這個標記的，使用者自己加的規則不動。
$MARK = "sunshine-stream (auto)"

# OCI 讀回來是 kebab-case，寫回去要 camelCase —— 不轉的話 API 會把整個
# tcp-options 當成未知欄位丟掉，規則就變成「整個協定全開」。
function ConvertTo-ApiRule($r) {
    $o = [ordered]@{ protocol = $r.protocol; source = $r.source }
    if ($null -ne $r.'source-type')  { $o.sourceType  = $r.'source-type' }
    if ($null -ne $r.'is-stateless') { $o.isStateless = [bool]$r.'is-stateless' }
    if ($r.description)              { $o.description = $r.description }
    foreach ($p in @(@('tcp-options', 'tcpOptions'), @('udp-options', 'udpOptions'))) {
        $src = $r.($p[0])
        if ($null -eq $src) { continue }
        $opt = [ordered]@{}
        foreach ($rng in @(@('destination-port-range', 'destinationPortRange'),
                           @('source-port-range', 'sourcePortRange'))) {
            $v = $src.($rng[0])
            if ($null -ne $v) { $opt[$rng[1]] = [ordered]@{ min = $v.min; max = $v.max } }
        }
        $o[$p[1]] = $opt
    }
    if ($null -ne $r.'icmp-options') {
        $o.icmpOptions = [ordered]@{ type = $r.'icmp-options'.type }
        if ($null -ne $r.'icmp-options'.code) { $o.icmpOptions.code = $r.'icmp-options'.code }
    }
    return $o
}

function New-PortRule($proto, $range, $cidr) {
    [ordered]@{
        protocol    = $proto          # 6 = TCP, 17 = UDP
        source      = $cidr
        sourceType  = "CIDR_BLOCK"
        isStateless = $false
        description = $MARK
        ($(if ($proto -eq "6") { "tcpOptions" } else { "udpOptions" })) = [ordered]@{
            destinationPortRange = [ordered]@{ min = $range.min; max = $range.max }
        }
    }
}

if (-not (Get-Command oci -ErrorAction SilentlyContinue)) {
    Warn "找不到 oci CLI"; exit 1
}

$cidr = $null
if (-not $Remove) {
    Say "查詢目前的對外 IP"
    try   { $myIp = (Invoke-RestMethod "https://api.ipify.org" -TimeoutSec 15).ToString().Trim() }
    catch { Warn "查不到對外 IP：$($_.Exception.Message)"; exit 1 }
    if ($myIp -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { Warn "回傳的不像 IP：$myIp"; exit 1 }
    $cidr = "$myIp/32"
    Write-Host "    $cidr"
}

$data     = & oci network security-list get --security-list-id $VpsSecurityListId | ConvertFrom-Json
$existing = @($data.data.'ingress-security-rules')
$mine     = @($existing | Where-Object { $_.description -eq $MARK })
$others   = @($existing | Where-Object { $_.description -ne $MARK })
$wantCount = $VpsStreamTcpRanges.Count + $VpsStreamUdpRanges.Count

# 已經是對的就不要白打一次 API（開機流程每次都會叫這支）
if (-not $Remove -and $mine.Count -eq $wantCount -and
    (@($mine | Where-Object { $_.source -eq $cidr }).Count -eq $wantCount)) {
    Say "規則已經是 $cidr，不用改"
    exit 0
}
if ($Remove -and $mine.Count -eq 0) {
    Say "本來就沒有串流規則"
    exit 0
}

$rules = @($others | ForEach-Object { ConvertTo-ApiRule $_ })
if (-not $Remove) {
    foreach ($r in $VpsStreamTcpRanges) { $rules += New-PortRule "6"  $r $cidr }
    foreach ($r in $VpsStreamUdpRanges) { $rules += New-PortRule "17" $r $cidr }
}

# 走檔案不走命令列字串：這串 JSON 有巢狀引號，交給 PowerShell 的原生指令參數解析
# 一定會被拆壞。oci CLI 認得 file:// 前綴。
$tmp = Join-Path $env:TEMP "oci_ingress_rules.json"
# 一定要 -Depth，預設只序列化 2 層，tcpOptions 底下的 port range 會變成型別名字串。
# 寫檔一定要用「不含 BOM」的 UTF-8：PS 5.1 的 `Set-Content -Encoding utf8` 會寫 BOM，
# oci CLI 的 JSON parser 直接噴 "must be in JSON format"（訊息完全看不出是 BOM 害的）。
[IO.File]::WriteAllText($tmp, (ConvertTo-Json @($rules) -Depth 10), [Text.UTF8Encoding]::new($false))

Say $(if ($Remove) { "移除串流規則" } else { "更新串流規則來源為 $cidr" })
& oci network security-list update --security-list-id $VpsSecurityListId `
    --ingress-security-rules "file://$($tmp -replace '\\', '/')" --force | Out-Null
$ok = ($LASTEXITCODE -eq 0)
Remove-Item $tmp -ErrorAction SilentlyContinue
if (-not $ok) { Warn "更新失敗"; exit 1 }

$desc = (($VpsStreamTcpRanges | ForEach-Object { "TCP $($_.min)-$($_.max)" }) +
         ($VpsStreamUdpRanges | ForEach-Object { "UDP $($_.min)-$($_.max)" })) -join " / "
Write-Host "    完成：$desc" -ForegroundColor Green
