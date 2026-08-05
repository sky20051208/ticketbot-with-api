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

# 開機後等 SSH 起來的上限（OCI 冷開機實測約 60~90 秒）
$VpsBootTimeoutSec = 240

# 通道 process 的 PID 記在這，stop 腳本用它收乾淨
$VpsTunnelPidFile = Join-Path $env:TEMP "tixcraft_vps_tunnel.pid"
