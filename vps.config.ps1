# 美東搶票 VPS 的連線設定 —— vps_start.ps1 / vps_stop.ps1 共用。
#
# 只有 InstanceOcid 要你填：
#   OCI Console → Compute → Instances → tixcraft-prod → General information → OCID → Copy
#
# 這裡沒有任何機密（OCID 不是憑證，要動它得有 API 金鑰），可以安心 commit。

$VpsInstanceOcid = "ocid1.instance.oc1.iad.anuwcljt76jdqbycacjhwpbb2z2sshgrrf3bwm2whbupmjtrywh4ffmkwlsa"

$VpsUser    = "ubuntu"
$VpsKeyPath = "$env:USERPROFILE\.ssh\ticket-ohio.pem"

# 埠對應：本機這一側刻意錯開，才能「本機 War-Room（KKTIX/寬宏/TicketPlus）」和
# 「美東 War-Room（拓元）」同時開著 —— 兩邊 webgui 都是 7860，不錯開會搶同一個本機埠。
#   http://localhost:7860  → 本機的 War-Room（這支腳本不碰）
#   http://localhost:7861  → 美東 VPS 的 War-Room
$VpsGuiLocalPort  = 7861
$VpsGuiRemotePort = 7860
$VpsVncLocalPort  = 6080   # noVNC，換帳號重登 / 看結帳頁時用
$VpsVncRemotePort = 6080

# 原生 VNC 埠。台灣↔Ashburn 來回 200ms，瀏覽器裡的 noVNC（JS 畫 canvas）在這種延遲下
# 特別鈍；用原生客戶端（TigerVNC 之類）接 5900 會順很多。noVNC 留著當「懶得裝軟體時」的
# 備案，兩條通道都開著不衝突。
$VpsVncRawLocalPort  = 5900
$VpsVncRawRemotePort = 5900

# Sunshine（H.264 串流，給 Moonlight 用）。
# VNC 是「每張畫面各自壓縮」的靜態圖編碼，實測捲一次頁面要 131KB，在台灣↔Ashburn
# 這條 2.1MB/s 的路徑上只能跑到 16fps。H.264 會做影格間預測，同樣的畫面成本掉一個
# 數量級，60fps 才有可能。代價是 Sunshine 走 UDP —— **沒辦法穿 SSH 通道**，必須開對外埠。
#
# 所以用 sync_stream_ip.ps1 把來源限制在「你家當下的對外 IP」，並由 vps_start.ps1
# 每次開機自動同步。搬家或 ISP 換 IP 都不用手動維護白名單。
$VpsSecurityListId = "ocid1.securitylist.oc1.iad.aaaaaaaasls6mnzr2yfvjd2vbc3w5ntchtenkrvrckwjq5gph5bey2q64hwa"
# 埠表（每個元素是一段 min,max）。**48010/TCP 是 RTSP 交握**，漏掉的話配對得成、
# 串流卻會停在 "Connection timed out after 10 seconds (TCP port 48010)"（實際踩過）。
# 47990 是網頁管理 UI，**故意不開對外** —— 它走 SSH 通道就好，沒理由曝在公網。
#
# 用具名物件而不是 @(min,max) 巢狀陣列：PowerShell 會把**單元素**的巢狀陣列自動攤平，
# `@(@(47998,48010))` 會變成 `@(47998,48010)`，foreach 就跑出兩條規則、max 還是空的。
$VpsStreamTcpRanges = @(
    [pscustomobject]@{ min = 47984; max = 47989 }   # HTTPS 47984 / HTTP 47989
    [pscustomobject]@{ min = 48010; max = 48010 }   # RTSP
)
$VpsStreamUdpRanges = @(
    [pscustomobject]@{ min = 47998; max = 48010 }   # 影音 / 控制 / 音訊 / 麥克風
)
# Sunshine 管理介面（配對輸入 PIN 用）。只聽本機，走 SSH 通道進來。
$VpsSunshineLocalPort  = 47990
$VpsSunshineRemotePort = 47990

# 開機後等 SSH 起來的上限（OCI 冷開機實測約 60~90 秒）
$VpsBootTimeoutSec = 240

# 串流參數。實測 1440x900：30fps=2.3Mbps/單核41%，60fps=3.9Mbps/單核82%（共 4 核）。
# 路徑上限 16.8Mbps，60fps 只佔 23%，所以直接給 60。
$VpsStreamFps         = 60
$VpsStreamBitrateKbps = 15000   # 上限不是固定值；壓在路徑上限之下避免自己塞爆線路

# ── 遠大 TicketPlus 的東京機（AWS ap-northeast-1）─────────────────────────
# 跟拓元那台是兩碼事：tixcraft 的 origin 在美東、TicketPlus 的在 AWS 東京，
# 剛好相反，不能共用一台。實測東京→遠大 queue 只有 2.1ms（台灣 66ms）。
#
# EC2 的開關機 / 安全群組同步都在 aws_tokyo.py（boto3），這裡只管通道和埠。
# 本機這側的埠全部錯開，三個 War-Room 才能同時開著：
#   7860 本機 / 7861 拓元美東 / 7862 遠大東京
$TokyoGuiLocalPort  = 7862
$TokyoGuiRemotePort = 7860
$TokyoVncRawLocalPort  = 5901   # 原生 VNC（備援，只需要 22 埠）
$TokyoVncRawRemotePort = 5900
$TokyoSunshineLocalPort  = 47991  # Sunshine 管理介面（配對輸入 PIN）
$TokyoSunshineRemotePort = 47990
$TokyoKeyPath = "$env:USERPROFILE\.ssh\ticketplus-tokyo.pem"
$TokyoUser = "ubuntu"
# 這台只有 2 核，60fps 編碼會吃掉約一半的機器（拓元那台 4 核只吃 21%）。
# 平常看畫面 30fps 就夠，搶票時本來就不該盯著串流。
$TokyoStreamFps = 30
$TokyoStreamBitrateKbps = 15000
$TokyoTunnelPidFile = Join-Path $env:TEMP "ticketplus_tokyo_tunnel.pid"
$TokyoIpFile = Join-Path $env:TEMP "ticketplus_tokyo_ip.txt"

# 通道 process 的 PID 記在這，stop 腳本用它收乾淨
$VpsTunnelPidFile = Join-Path $env:TEMP "tixcraft_vps_tunnel.pid"
# 開機後把當下的公網 IP 寫在這，vps_moonlight.ps1 直接讀，省一次 OCI 查詢
$VpsIpFile = Join-Path $env:TEMP "tixcraft_vps_ip.txt"
