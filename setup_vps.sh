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
    xvfb xserver-xorg-video-dummy x11-xserver-utils \
    chrony git curl unzip netcat-openbsd

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

log "關掉 lightdm —— 它會攔截電源鍵，讓 SOFTSTOP 永遠關不掉機器"
# 裝 xfce4 時 lightdm 被當相依套件拉進來，它的 unity-greeter 會拿一個 block 模式的
# `handle-power-key` inhibitor。logind 收到 ACPI 電源鍵後交給 greeter 處理，而那是
# 無頭機上永遠沒人看的登入畫面 —— 訊號直接被吃掉，OCI 只能等滿 15 分鐘強制斷電。
# 實測：SOFTSTOP 花了 12 分鐘，上次開機的 journal 裡連一行關機紀錄都沒有。
# 這台的畫面走 Xvfb :99，不需要顯示管理器。
sudo systemctl disable --now lightdm 2>/dev/null || true

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
# 為什麼要虛擬螢幕：webgui spawn 的 Chrome 是 headful 的，沒有 X display 會直接 exit
# （Selenium 那端只看得到 "Chrome instance exited"）。VPS 沒有實體螢幕，用虛擬的。
#
# 主力是 Xorg + dummy driver，不是 Xvfb。理由有兩個，Xvfb 都做不到：
#   1. **RandR** —— 可以用 xrandr 隨時換解析度而不用重啟整個 display。VNC 每幀成本正比
#      於像素數，想換到低解析度換幀率時這是唯一乾淨的做法（x11vnc 的 -scale 不能用：
#      官方 README 寫明偵測到縮放會自動退回 ZRLE 無損編碼，等於把 JPEG 關掉，只會更慢）
#   2. **udev** —— Xvfb 不支援 udev 熱插拔，Sunshine 用 uinput 造出來的虛擬鍵鼠
#      X server 根本看不到，串流進去會變成「畫面會動但完全不能操作」
#
# xvfb.service 的 unit 檔仍然留著但不啟用，純粹當回退用：
#   sudo systemctl disable --now xorg-dummy && sudo systemctl enable --now xvfb
sudo tee /etc/systemd/system/xvfb.service > /dev/null <<EOF
[Unit]
Description=Xvfb virtual display :99 (1440x900) -- 備援，平常不啟用
After=network.target
Conflicts=xorg-dummy.service

[Service]
User=$USER
ExecStart=/usr/bin/Xvfb :99 -screen 0 1440x900x24
Restart=always
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF

# Virtual 給到 1920x1200，是 RandR 能配置的上限（framebuffer 一開始就要留夠大，之後
# xrandr 只能在這個範圍內加模式）。實際起始模式仍是 1440x900 —— 跟 Xvfb 時期一模一樣，
# 這樣 Chrome 的版面、視窗大小、LINE 成交截圖的寬度通通不受影響。
sudo tee /etc/X11/xorg-dummy.conf > /dev/null <<'EOF'
Section "Device"
    Identifier  "dummy"
    Driver      "dummy"
    VideoRam    256000
EndSection

Section "Monitor"
    Identifier  "dummy-monitor"
    HorizSync   5.0 - 1000.0
    VertRefresh 5.0 - 200.0
    Modeline "1440x900"  106.50 1440 1528 1672 1904  900 903 909 934 -hsync +vsync
    Modeline "1280x800"   83.50 1280 1352 1480 1680  800 803 809 831 -hsync +vsync
    Modeline "1152x720"   66.75 1152 1208 1328 1504  720 723 729 748 -hsync +vsync
    Modeline "1024x640"   52.00 1024 1072 1176 1328  640 643 649 666 -hsync +vsync
EndSection

Section "Screen"
    Identifier  "dummy-screen"
    Device      "dummy"
    Monitor     "dummy-monitor"
    DefaultDepth 24
    SubSection "Display"
        Depth     24
        Modes     "1440x900" "1280x800" "1152x720" "1024x640"
        Virtual   1920 1200
    EndSubSection
EndSection

Section "ServerLayout"
    Identifier  "dummy-layout"
    Screen      "dummy-screen"
EndSection
EOF

# Xorg 預設只讓「實體 console 上的登入者」啟動。這台是無頭機、由 systemd 拉起來，
# 不放寬的話會噴 "only console users are allowed to run the X server"。
sudo tee /etc/X11/Xwrapper.config > /dev/null <<'EOF'
allowed_users=anybody
needs_root_rights=yes
EOF

sudo tee /etc/systemd/system/xorg-dummy.service > /dev/null <<EOF
[Unit]
Description=Xorg dummy virtual display :99 (1440x900, RandR capable)
After=network.target systemd-udevd.service
Conflicts=xvfb.service

[Service]
User=$USER
ExecStart=/usr/bin/Xorg :99 -config /etc/X11/xorg-dummy.conf -nolisten tcp -novtswitch
Restart=always
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF

# Sunshine 靠 /dev/uinput 造虛擬鍵鼠。預設權限是 root only，且 ubuntu 不在 input 群組。
# 兩者都補上 Sunshine 才注入得了鍵盤滑鼠（改群組要重登入才生效，所以順便直接 chmod）。
sudo tee /etc/udev/rules.d/60-sunshine.rules > /dev/null <<'EOF'
KERNEL=="uinput", SUBSYSTEM=="misc", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"
EOF
sudo usermod -aG input "$USER"
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=misc || true

sudo tee /etc/systemd/system/tixcraft-webgui.service > /dev/null <<EOF
[Unit]
Description=Tixcraft War-Room webgui
After=xorg-dummy.service oci-secondary-ips.service
Requires=xorg-dummy.service

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
sudo apt-get install -y -qq x11vnc novnc websockify openbox tint2
# 視窗管理器：Xvfb 只負責畫，標題列 / 最小化 / 關閉鈕 / 拖曳縮放全是 WM 的事。
# 沒有 WM 的話遠端看到的 Chrome 是一塊沒有邊框、也動不了的畫面。openbox 約 2MB。
# 但 openbox 只有 WM 沒有工作列 —— 按了最小化的視窗會直接消失，而且沒有任何入口
# 叫得回來（實際踩到）。tint2 就是補那條入口。
sudo tee /etc/systemd/system/openbox.service > /dev/null <<EOF
[Unit]
Description=Openbox window manager on :99
After=xorg-dummy.service
Requires=xorg-dummy.service

[Service]
User=$USER
Environment=DISPLAY=:99
ExecStart=/usr/bin/openbox
Restart=always
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF

mkdir -p "$HOME/.config/tint2"
cat > "$HOME/.config/tint2/tint2rc" <<'EOF'
# 只留 Taskbar / Systray / Clock，不放啟動器：要開什麼都是 webgui 自己 spawn 的。
# 背景樣式要寫在被引用之前，id 由上而下從 1 開始編（id 0 是內建的全透明）。
rounded = 0
border_width = 0
background_color = #2b2f38 100
border_color = #000000 0

rounded = 2
border_width = 0
background_color = #454b58 100
border_color = #000000 0

rounded = 2
border_width = 0
background_color = #3a6eaf 100
border_color = #000000 0

panel_items = TSC
panel_position = bottom center horizontal
panel_size = 100% 32
panel_margin = 0 0
panel_padding = 4 2 4
panel_background_id = 1
panel_layer = top
panel_monitor = all
wm_menu = 1

taskbar_mode = single_desktop
taskbar_padding = 2 0 2
taskbar_background_id = 0

# 標題留寬一點：同時開多隻 Chrome 時要靠標題分辨是哪個帳號的視窗
task_maximum_size = 260 30
task_padding = 6 2 6
task_font = sans 10
task_font_color = #dddddd 100
task_background_id = 2
task_active_background_id = 3
task_icon = 1
task_text = 1
task_centered = 0

systray_padding = 4 2 4
systray_icon_size = 20

time1_format = %H:%M
time1_font = sans 10
clock_font_color = #dddddd 100
clock_padding = 6 0

mouse_middle = close
mouse_right = toggle_iconify
EOF

sudo tee /etc/systemd/system/tint2.service > /dev/null <<EOF
[Unit]
Description=tint2 taskbar on :99
After=openbox.service
Requires=openbox.service

[Service]
User=$USER
Environment=DISPLAY=:99
ExecStart=/usr/bin/tint2
Restart=always
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/x11vnc.service > /dev/null <<EOF
[Unit]
Description=x11vnc for display :99
After=xorg-dummy.service
Requires=xorg-dummy.service

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
# 一定要寫成 127.0.0.1:6080 —— websockify 只給埠號的話會綁 0.0.0.0，等於把無密碼的
# noVNC 暴露在公網介面上。目前 Security List 只放行 22 埠擋住了，但不該靠那個當唯一防線。
# 走 SSH 通道進來完全不受影響。
ExecStart=/usr/bin/websockify --web=/usr/share/novnc 127.0.0.1:6080 localhost:5900
Restart=always
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
# xvfb 只留 unit 檔當回退，不進開機清單 —— 它跟 xorg-dummy 互為 Conflicts，兩個都
# enable 的話開機會互相把對方踢掉。
sudo systemctl disable xvfb.service 2>/dev/null || true
sudo systemctl stop xvfb.service 2>/dev/null || true
sudo systemctl enable xorg-dummy.service openbox.service tint2.service x11vnc.service novnc.service tixcraft-webgui.service
# 一定要 restart 不能只 enable --now —— 對已經在跑的服務，`--now` 不會重啟，
# 改寫的 unit 檔就靜靜地不生效（踩過一次：改了 websockify 的綁定位址卻沒套上）。
# 這支是維護腳本，跑的時候不該有搶票在進行。
sudo systemctl restart xorg-dummy.service
sleep 2
sudo systemctl restart openbox.service tint2.service x11vnc.service novnc.service tixcraft-webgui.service
sleep 3
systemctl is-active xorg-dummy openbox tint2 x11vnc novnc tixcraft-webgui | tr '\n' ' '; echo
DISPLAY=:99 xrandr 2>/dev/null | head -3 || echo "  !! xrandr 讀不到 :99"

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
         DISPLAY=:99 google-chrome --no-sandbox --disable-dev-shm-usage \\
           --remote-debugging-port=9222 --user-data-dir=\$HOME/chrome-tixcraft \\
           --window-size=1440,900 https://tixcraft.com &
       本機：ssh -L 9222:127.0.0.1:9222 ubuntu@<IP>，然後瀏覽器開 http://localhost:9222
EOF
