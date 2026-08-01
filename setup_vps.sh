#!/usr/bin/env bash
# Oracle Cloud (Ubuntu 24.04) 一鍵環境安裝 —— 拓元搶票機的美東節點。
#
# 這台機器存在的理由：拓元 origin 在美東，eps 只放行特定 ASN。Oracle 的 AS31898 是
# 實測可用的（AWS/Vultr/DO/Hetzner/OVH 全被 403 封死），直連約 82~145ms，
# 對比家裡走 CliProxy 的 331ms 每發省 200ms 上下。詳見 bench_*.py 三支診斷工具。
#
# 用法：
#     scp setup_vps.sh ubuntu@<IP>:~/ && ssh ubuntu@<IP> 'bash setup_vps.sh'
#
# 冪等：重跑不會壞，已裝的會跳過。
set -euo pipefail

REPO_DIR="$HOME/ticketbot"
VENV="$HOME/venv"
SWAP_GB=4

log() { echo -e "\n\033[1;36m==> $*\033[0m"; }

log "系統套件"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3-pip python3-venv python3-dev build-essential \
    xvfb chrony git curl unzip netcat-openbsd

log "swap ${SWAP_GB}G（寫進 fstab，重開機不會消失）"
# 上一台機器就是栽在這：fallocate 建的 swap 沒進 fstab，重開機後 Chrome 直接 OOM
if ! grep -q '^/swapfile' /etc/fstab; then
    sudo swapoff /swapfile 2>/dev/null || true
    sudo rm -f /swapfile
    sudo fallocate -l ${SWAP_GB}G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap -q /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab > /dev/null
fi
free -h | grep -i swap

log "校時（chrony）—— T-0 高頻偵測的前提"
sudo systemctl enable --now chrony
chronyc tracking | head -3 || true

log "Google Chrome"
if ! command -v google-chrome > /dev/null; then
    # 從 /tmp 裝，不要從 $HOME —— apt 的 _apt 使用者讀不到家目錄，會 Permission denied
    cd /tmp
    curl -fsSLO https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    sudo apt-get install -y -qq /tmp/google-chrome-stable_current_amd64.deb
    rm -f /tmp/google-chrome-stable_current_amd64.deb
    cd "$HOME"
fi
google-chrome --version

log "Python 環境"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
if [ -f "$REPO_DIR/requirements.txt" ]; then
    "$VENV/bin/pip" install -q -r "$REPO_DIR/requirements.txt"
else
    echo "  （還沒放 repo，先裝最小集合；之後 rsync 完再跑一次這支腳本補齊）"
    "$VENV/bin/pip" install -q curl_cffi selenium onnxruntime numpy Pillow requests ntplib
fi
"$VENV/bin/pip" install -q oci        # rotate_ips.py 用（輪替公網 IP）

log "cloudflared（遠端開 webgui 用）"
if ! command -v cloudflared > /dev/null; then
    curl -fsSL -o /tmp/cloudflared.deb \
        https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
    sudo apt-get install -y -qq /tmp/cloudflared.deb
    rm -f /tmp/cloudflared.deb
fi
cloudflared --version

log "開機自動補掛 OCI 次要私有 IP"
# OCI 只把 IP 配給 VNIC，guest OS 不會自己掛上；`ip addr add` 又是暫時的。
# rotate_ips.py --sync-os 直接跟 OCI 對帳，Console 上新增 IP 之後不用改設定檔。
if [ -f "$HOME/rotate_ips.py" ] || [ -f "$REPO_DIR/rotate_ips.py" ]; then
    SYNC_PY=$([ -f "$REPO_DIR/rotate_ips.py" ] && echo "$REPO_DIR/rotate_ips.py" || echo "$HOME/rotate_ips.py")
    sudo tee /etc/systemd/system/oci-secondary-ips.service > /dev/null <<EOF
[Unit]
Description=Attach OCI secondary private IPs to the NIC
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$VENV/bin/python $SYNC_PY --sync-os

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now oci-secondary-ips.service || true
    systemctl is-active oci-secondary-ips.service || true
else
    echo "  （找不到 rotate_ips.py，跳過；放上來後重跑這支腳本）"
fi

log "常駐服務（開機自動起，本機腳本只要開機 + 開通道）"
# 為什麼要 Xvfb：webgui spawn 的 Chrome 是 headful 的，沒有 X display 會直接 exit
# （Selenium 那端只看得到 "Chrome instance exited"）。VPS 沒有實體螢幕，用虛擬的。
sudo tee /etc/systemd/system/xvfb.service > /dev/null <<EOF
[Unit]
Description=Xvfb virtual display :99
After=network.target

[Service]
User=$USER
ExecStart=/usr/bin/Xvfb :99 -screen 0 1920x1080x24
Restart=always
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/tixcraft-webgui.service > /dev/null <<EOF
[Unit]
Description=Tixcraft War-Room webgui
After=xvfb.service oci-secondary-ips.service
Requires=xvfb.service

[Service]
User=$USER
WorkingDirectory=$REPO_DIR
Environment=DISPLAY=:99
ExecStart=$VENV/bin/python run_webgui.py
Restart=on-failure
# webgui 會 spawn 搶票子進程和 Chrome，收得慢；10 秒沒結束就整個 cgroup SIGKILL，
# 不然關機會卡在 systemd 等它（實測 SOFTSTOP 卡超過 5 分鐘就是這個原因）
TimeoutStopSec=10
KillMode=mixed

[Install]
WantedBy=multi-user.target
EOF

# x11vnc + noVNC：需要「用眼睛看」時（換帳號重登、檢查結帳頁）從瀏覽器連 :6080。
# 只 bind localhost，一律走 SSH 通道進來，不對外開埠。
sudo apt-get install -y -qq x11vnc novnc websockify
sudo tee /etc/systemd/system/x11vnc.service > /dev/null <<EOF
[Unit]
Description=x11vnc for display :99
After=xvfb.service
Requires=xvfb.service

[Service]
User=$USER
ExecStart=/usr/bin/x11vnc -display :99 -localhost -nopw -forever -shared -quiet
Restart=always
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/novnc.service > /dev/null <<EOF
[Unit]
Description=noVNC web front-end for x11vnc
After=x11vnc.service
Requires=x11vnc.service

[Service]
User=$USER
ExecStart=/usr/bin/websockify --web=/usr/share/novnc 6080 localhost:5900
Restart=always
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now xvfb.service x11vnc.service novnc.service tixcraft-webgui.service
sleep 3
systemctl is-active xvfb x11vnc novnc tixcraft-webgui | tr '\n' ' '; echo

log "完成"
cat <<EOF

  venv        : $VENV/bin/python
  repo 放這裡 : $REPO_DIR   （rsync 上來之後重跑本腳本安裝 requirements）

  下一步：
    1. 把 repo 同步上來
    2. 跑診斷確認這台機器可用：
         $VENV/bin/python $REPO_DIR/bench_eps.py
         $VENV/bin/python $REPO_DIR/bench_vps.py
    3. 開 Chrome 測登入（無桌面，用 CDP 通道從本機操作）：
         Xvfb :99 -screen 0 1440x900x24 &
         DISPLAY=:99 google-chrome --no-sandbox --disable-dev-shm-usage \\
           --remote-debugging-port=9222 --user-data-dir=\$HOME/chrome-tixcraft \\
           --window-size=1440,900 https://tixcraft.com &
       本機：ssh -L 9222:127.0.0.1:9222 ubuntu@<IP>，然後瀏覽器開 http://localhost:9222
EOF
