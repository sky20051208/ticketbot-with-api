# 美東搶票 VPS 的連線設定 —— vps_start.ps1 / vps_stop.ps1 共用。
#
# 只有 InstanceOcid 要你填：
#   OCI Console → Compute → Instances → tixcraft-prod → General information → OCID → Copy
#
# 這裡沒有任何機密（OCID 不是憑證，要動它得有 API 金鑰），可以安心 commit。

$VpsInstanceOcid = "ocid1.instance.oc1.iad.請貼上你的完整OCID"

$VpsUser    = "ubuntu"
$VpsKeyPath = "$env:USERPROFILE\.ssh\ticket-ohio.pem"

# 本機要轉發的埠（左邊本機 = 右邊 VM，兩邊同號比較好記）
$VpsGuiPort = 7860      # War-Room webgui
$VpsVncPort = 6080      # noVNC，換帳號重登 / 看結帳頁時用

# 開機後等 SSH 起來的上限（OCI 冷開機實測約 60~90 秒）
$VpsBootTimeoutSec = 240

# 通道 process 的 PID 記在這，stop 腳本用它收乾淨
$VpsTunnelPidFile = Join-Path $env:TEMP "tixcraft_vps_tunnel.pid"
