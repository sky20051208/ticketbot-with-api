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
# 釘版本而不是抓 latest：串流參數（fec / preset / 埠）是對著這版調出來的
SUNSHINE_VER=2026.516.143833

log() { echo -e "\n\033[1;36m==> $*\033[0m"; }

log "系統套件"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3-pip python3-venv python3-dev build-essential \
    xvfb xserver-xorg-video-dummy xserver-xorg-input-libinput x11-xserver-utils \
    xinput chrony git curl unzip netcat-openbsd
# xserver-xorg-input-libinput 是 Moonlight 滑鼠鍵盤能不能用的關鍵，別以為只有顯示要驅動。
# 少了它 Xorg 對每個裝置都印「No input driver specified, ignoring this device」——
# udev 明明認得 Sunshine 建的虛擬滑鼠，Xorg 卻沒有驅動可以綁，游標完全不動。
#
# 特別難查的是 **VNC 的滑鼠還是好的**：x11vnc 走 XTEST 直接注入 X，不經過 evdev；
# Moonlight 走 uinput → evdev → X，斷的是中間那一段。所以「VNC 能動 = 輸入正常」
# 這個推論在這裡不成立（東京機實際踩到）。美東那台沒出過這問題，推測是 base image
# 裡本來就有這包（它有 lightdm，會把 xserver-xorg-input-* 一起拉進來），但沒實際查證過。
# xinput 純診斷用，`DISPLAY=:99 xinput list` 看得到 Mouse/Keyboard passthrough 就對了。

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
#
# 這支腳本兩台機器共用（拓元在 Oracle 美東、遠大在 AWS 東京），所以要先認機房：
# rotate_ips.py 走 OCI 的 Instance Principals，在 AWS 上必定失敗。光看檔案存不存在
# 不夠 —— 那是同一個 repo，兩邊都有。
IS_OCI=no
if [ "$(cat /sys/class/dmi/id/chassis_asset_tag 2>/dev/null)" = "OracleCloud.com" ]; then
    IS_OCI=yes
fi
if [ "$IS_OCI" != "yes" ] && [ -f "$REPO_DIR/sync_aws_ips.py" ]; then
    # AWS 也一樣：EC2 只把 IP 配給網卡，OS 不會自己掛，而 `ip addr add` 重開機就沒了。
    # 資料來源是 IMDSv2，機器上不用放金鑰。**次要私有 IP 要另外綁 Elastic IP 才有
    # 公網出口**（在自己電腦上跑 `python aws_tokyo.py multi-ip`）。
    # 用系統 python3 不是 venv —— 這支只用標準函式庫，而且要 root 跑。
    sudo tee /etc/systemd/system/aws-secondary-ips.service > /dev/null <<EOF
[Unit]
Description=Attach EC2 secondary private IPs to the NIC
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 $REPO_DIR/sync_aws_ips.py

[Install]
WantedBy=multi-user.target
EOF
    sudo systemctl daemon-reload
    sudo systemctl enable --now aws-secondary-ips.service || true
    systemctl is-active aws-secondary-ips.service || true
elif [ "$IS_OCI" != "yes" ]; then
    echo "  （不是 OCI 機器、也找不到 sync_aws_ips.py，跳過）"
elif [ -f "$HOME/rotate_ips.py" ] || [ -f "$REPO_DIR/rotate_ips.py" ]; then
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
# -config 一定要給**相對**檔名。Xorg 透過 setuid wrapper 提權執行時會擋掉絕對路徑
# （"With elevated privileges -config must specify a relative path"），它自己會去
# /etc/X11/ 底下找。
ExecStart=/usr/bin/Xorg :99 -config xorg-dummy.conf -nolisten tcp -novtswitch
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
# xfce4-terminal 不是可有可無的：tint2 啟動鈕、openbox 右鍵選單、以及 new-profile.sh
# 最後那個 exec 全都叫它。漏裝的話「建立 Chrome Profile」會走完兩個 zenity 對話框，
# 然後 exec 失敗、畫面上什麼都不會發生（東京機實際踩到）。
sudo apt-get install -y -qq x11vnc novnc websockify openbox tint2 zenity thunar \
    xfce4-terminal
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
# 這份 heredoc 沒有加引號，$HOME 要展開（launcher 的 .desktop 路徑必須是絕對路徑）。
# tint2rc 裡沒有其他 $ 或反引號，展開不會咬到別的東西。
cat > "$HOME/.config/tint2/tint2rc" <<EOF
# L=Launcher T=Taskbar S=Systray C=Clock。
# 有 Launcher 是因為 profile 一定要在這台機器上建（eps_sid 綁出口 IP），而
# create_profile.py 是互動式的 —— 沒有可以點的入口就得每次自己 ssh 進來打指令。
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

panel_items = LTSC
panel_position = bottom center horizontal
panel_size = 100% 32
panel_margin = 0 0
panel_padding = 4 2 4
panel_background_id = 1
panel_layer = top
panel_monitor = all
wm_menu = 1

launcher_icon_size = 22
launcher_padding = 6 2 6
launcher_background_id = 0
launcher_icon_theme = Adwaita
launcher_tooltip = 1
launcher_item_app = $HOME/.local/share/applications/tixcraft-new-profile.desktop
launcher_item_app = $HOME/.local/share/applications/tixcraft-profiles-folder.desktop
launcher_item_app = /usr/share/applications/xfce4-terminal.desktop

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

# 桌面入口：工作列的啟動鈕 + 桌面空白處右鍵選單，兩邊指到同一支腳本。
# openbox 不畫桌面圖示（那是檔案管理器的工作），所以這兩個就是這台機器的「桌面捷徑」。
chmod +x "$REPO_DIR/vps_desktop/new-profile.sh" 2>/dev/null || true
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/tixcraft-new-profile.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=建立 Chrome Profile
Comment=幫一個購票帳號建立獨立的 Chrome 登入 profile（一定要在這台機器上建）
Exec=$REPO_DIR/vps_desktop/new-profile.sh
Icon=contact-new
Terminal=false
Categories=Utility;
EOF
cat > "$HOME/.local/share/applications/tixcraft-profiles-folder.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Profile 資料夾
Comment=瀏覽 chrome_profiles/（刪掉某個帳號的登入態就是刪這裡的資料夾）
Exec=thunar $REPO_DIR/chrome_profiles
Icon=folder
Terminal=false
Categories=Utility;
EOF

mkdir -p "$HOME/.config/openbox"
cat > "$HOME/.config/openbox/menu.xml" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<openbox_menu xmlns="http://openbox.org/3.4/menu">
<menu id="root-menu" label="tixcraft">
  <item label="建立 Chrome Profile">
    <action name="Execute"><execute>$REPO_DIR/vps_desktop/new-profile.sh</execute></action>
  </item>
  <item label="Profile 資料夾">
    <action name="Execute"><execute>thunar $REPO_DIR/chrome_profiles</execute></action>
  </item>
  <item label="終端機">
    <action name="Execute"><execute>xfce4-terminal</execute></action>
  </item>
  <separator/>
  <item label="重新載入桌面設定">
    <action name="Reconfigure"/>
  </item>
</menu>
</openbox_menu>
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

# Sunshine：H.264 串流給 Moonlight 用。VNC 是「每張畫面各自壓縮」的靜態圖編碼，
# 實測捲一次頁面要 131KB，在台灣↔Ashburn 這條 2.1MB/s 的路徑上只能跑 16fps。
# H.264 做影格間預測，同一段捲動 60fps 只要 3.9Mbps —— 佔用率從 100% 掉到 23%。
if ! command -v sunshine > /dev/null; then
    curl -fsSL -o /tmp/sunshine.deb \
        "https://github.com/LizardByte/Sunshine/releases/download/v${SUNSHINE_VER}/sunshine-ubuntu-24.04-amd64.deb"
    sudo apt-get install -y -qq /tmp/sunshine.deb
    rm -f /tmp/sunshine.deb
fi
# Sunshine 自帶的 /usr/lib/udev/rules.d/60-sunshine.rules 已經把 /dev/uinput 開給
# input 群組，不要在 /etc 底下放同名檔案覆蓋它（會連 uhid、手把那幾條一起蓋掉）。
mkdir -p "$HOME/.config/sunshine"
cat > "$HOME/.config/sunshine/sunshine.conf" <<'EOF'
# 這台沒有 GPU，只能軟體編碼
encoder = software
# superfast 是「畫質 vs 跟不跟得上 fps」的折衷。實測 1440x900@60 吃掉單核 82%
# （總共有 4 核），再慢的 preset 會掉幀，ultrafast 畫質掉很多但省不到多少 CPU。
sw_preset = superfast
# 遠端桌面要的是低延遲不是壓縮率，關掉會累積延遲的前瞻 / B-frame
sw_tune = zerolatency

capture = x11

# 網頁管理介面只聽本機 —— 走 SSH 通道進來，不對外開埠（47990 也刻意不在防火牆放行清單裡）
origin_web_ui_allowed = pc

# 預設 20 不夠：實測 26 秒掉一張影格就觸發
# "Reference frame invalidation is not supported by this host"，
# 整個畫面要等下一張關鍵影格才能恢復，在 200ms 的線路上就是一次可見的停頓。
# 頻寬只用掉 23%，拿餘裕換穩定很划算。
fec_percentage = 40

min_log_level = info
EOF

sudo tee /etc/systemd/system/sunshine.service > /dev/null <<EOF
[Unit]
Description=Sunshine stream host (H.264 remote desktop for :99)
After=xorg-dummy.service
Requires=xorg-dummy.service

[Service]
User=$USER
Environment=DISPLAY=:99
SupplementaryGroups=input
ExecStart=/usr/bin/sunshine
Restart=on-failure
RestartSec=5
TimeoutStopSec=10
# T-0 時搶票進程一定要贏過編碼器 —— 使用者很可能一邊看畫面一邊搶票。
# Nice 管 CPU 排程優先序，CPUWeight 管 cgroup 之間的 CPU 分配比例（預設 100）。
Nice=10
CPUWeight=20

[Install]
WantedBy=multi-user.target
EOF

# 主機防火牆。OCI 的 Ubuntu 映像檔在 INPUT 尾端有一條 REJECT 兜底，串流埠要插在它前面。
# **48010/TCP 是 RTSP 交握**，漏掉的話配對得成、串流卻停在
# "Connection timed out after 10 seconds (TCP port 48010)"（實際踩過）。
# 來源限制交給 OCI Security List（sync_stream_ip.ps1 每次開機同步成你家當下的 IP）。
_fw_add() {
    sudo iptables -C INPUT "$@" 2>/dev/null && return 0
    local n; n=$(sudo iptables -L INPUT --line-numbers -n | awk '/REJECT/{print $1; exit}')
    sudo iptables -I INPUT "${n:-1}" "$@"
}
_fw_add -p tcp --dport 47984:47989 -j ACCEPT -m comment --comment sunshine-stream
_fw_add -p tcp --dport 48010       -j ACCEPT -m comment --comment sunshine-rtsp
_fw_add -p udp --dport 47998:48010 -j ACCEPT -m comment --comment sunshine-stream
sudo netfilter-persistent save > /dev/null 2>&1 || true

sudo systemctl daemon-reload
# xvfb 只留 unit 檔當回退，不進開機清單 —— 它跟 xorg-dummy 互為 Conflicts，兩個都
# enable 的話開機會互相把對方踢掉。
sudo systemctl disable xvfb.service 2>/dev/null || true
sudo systemctl stop xvfb.service 2>/dev/null || true
sudo systemctl enable xorg-dummy.service openbox.service tint2.service x11vnc.service novnc.service sunshine.service tixcraft-webgui.service
# 一定要 restart 不能只 enable --now —— 對已經在跑的服務，`--now` 不會重啟，
# 改寫的 unit 檔就靜靜地不生效（踩過一次：改了 websockify 的綁定位址卻沒套上）。
# 這支是維護腳本，跑的時候不該有搶票在進行。
sudo systemctl restart xorg-dummy.service
sleep 2
sudo systemctl restart openbox.service tint2.service x11vnc.service novnc.service sunshine.service tixcraft-webgui.service
sleep 3
systemctl is-active xorg-dummy openbox tint2 x11vnc novnc sunshine tixcraft-webgui | tr '\n' ' '; echo
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
