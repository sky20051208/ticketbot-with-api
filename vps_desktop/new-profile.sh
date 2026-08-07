#!/usr/bin/env bash
# 在 VPS 桌面上點一下就能建 Chrome profile —— 問平台、問帳號名，然後開一個終端機
# 跑 create_profile.py。
#
# 為什麼要有這支：profile **一定要在 VPS 上建**，不能本機建好複製過去（拓元的
# eps_sid 綁發放時的出口 IP，換 IP 就作廢）。而 create_profile.py 是互動式的
# （開 Chrome → 你手動登入 → 回終端機按 Enter），沒有終端機就走不完流程。
#
# 入口有三個，都指到這裡：工作列最左邊的啟動鈕、桌面空白處右鍵、~/.local/share/applications。

set -u
REPO="$HOME/ticketbot"
VENV="$HOME/venv"
export DISPLAY="${DISPLAY:-:99}"      # 從 ssh 叫的話沒有 DISPLAY，Chrome 會直接 exit

die() { zenity --error --title="建立 Profile" --text="$1" --width=320 2>/dev/null; exit 1; }

[ -f "$REPO/create_profile.py" ] || die "找不到 $REPO/create_profile.py"
[ -x "$VENV/bin/python" ]        || die "找不到 $VENV/bin/python"

PLATFORM=$(zenity --list --title="建立 Chrome Profile" \
    --text="要登入哪個平台？\n（同一個帳號要在多個平台搶，就分別各建一次）" \
    --column="平台" --column="說明" \
    tixcraft "拓元 tixcraft" \
    kktix "KKTIX" \
    ticketplus "遠大 TicketPlus" \
    --height=260 --width=400 2>/dev/null) || exit 0
[ -n "$PLATFORM" ] || exit 0

NAME=$(zenity --entry --title="建立 Chrome Profile" \
    --text="帳號名稱\n（會變成資料夾名，也是 War-Room 下拉選單顯示的名字）" \
    --width=400 2>/dev/null) || exit 0
NAME=$(echo "$NAME" | xargs)          # 去頭尾空白；資料夾名有空白後面會很難處理
[ -n "$NAME" ] || exit 0

if [ -d "$REPO/chrome_profiles/$PLATFORM/$NAME" ]; then
    zenity --question --title="已經存在" --width=380 \
        --text="chrome_profiles/$PLATFORM/$NAME 已經有了。\n\n繼續的話會開啟現有的 profile（可以拿來重新登入 / 換帳號），\n原本的 cookie 會被你在瀏覽器裡的操作蓋掉。要繼續嗎？" \
        2>/dev/null || exit 0
fi

# 把要跑的東西寫成暫存腳本再交給終端機，不要塞成 `xfce4-terminal -e "bash -c '...'"`：
# 那種寫法有三層引號，帳號名只要有空白或引號就會炸開。
RUN=$(mktemp /tmp/new-profile.XXXXXX.sh)
cat > "$RUN" <<EOF
#!/usr/bin/env bash
cd "$REPO" || exit 1
DISPLAY=$DISPLAY "$VENV/bin/python" create_profile.py --platform "$PLATFORM" --name "$NAME"
echo
echo "----------------------------------------------------------"
echo "  完成後記得回 War-Room 網頁按重整，下拉選單才會出現「$NAME」"
echo "----------------------------------------------------------"
read -r -p "按 Enter 關閉這個視窗 "
rm -f "$RUN"
EOF
chmod +x "$RUN"

exec xfce4-terminal --title="建立 Profile：$PLATFORM / $NAME" --geometry=100x30 -x "$RUN"
