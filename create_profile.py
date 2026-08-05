"""建立一個專用的 Chrome user-data-dir 給某個帳號登入用。

用法:
    python create_profile.py --name 帳號名                       # 預設 --platform tixcraft
    python create_profile.py --name 帳號名 --platform kktix
    python create_profile.py --name 帳號名 --platform ticketplus
    python create_profile.py --name 帳號名 --fixed-ip     # 同 name 每次拿同一個 IP
    python create_profile.py --name 帳號名 --proxy http://user:pass@host:port
    python create_profile.py --name 帳號名 --url https://xxx/login   # 自訂登入頁，蓋掉 --platform

--platform 決定兩件事：**開哪個登入頁**，以及 **profile 存到哪個子資料夾**
（`chrome_profiles/<平台>/<帳號名>/`，GUI 下拉會依當前平台只列該平台的 profile）。
可選值見下面 PLATFORM_LOGIN。同一個帳號名要在多個平台都能搶，就分別各跑一次
（cookie 按網域分開存，互不干擾）：

    python create_profile.py --name 小明 --platform tixcraft
    python create_profile.py --name 小明 --platform ticketplus

TicketPlus 沒有獨立登入頁（登入是全站共用的 dialog），會開首頁，請按右上角登入。

重點：這裡用「一般 Chrome」開（subprocess 直接啟動，不經 Selenium）。
Google 會擋 Selenium 控制的瀏覽器登入（"這個瀏覽器或應用程式可能有安全疑慮"），
一般 Chrome 沒有自動化旗標，Google 登入才正常。

帶 --proxy 時走 localhost bridge 自動補 auth header（同 browser_login 機制）。
意義：登入時就用 proxy IP，cookie 跟「之後 bot 搶票走的 IP」綁同源，server 不會
看到「同一 cookie 從兩個 IP 出現」的可疑訊號。

流程:
    1. 在 chrome_profiles/<平台>/<帳號名>/ 建一份獨立的 Chrome profile
    2. 開一般 Chrome 視窗（含 proxy 設定，如有），自動跳到該平台的登入頁
    3. 你手動登入（Google / FB 登入也 OK）
    4. 關閉 Chrome 視窗 → 回 terminal 按 Enter

之後 GUI 下拉選單（切到對應平台時）就會看到這個帳號名，bot 用它開 Chrome 即為已登入狀態。
GUI 是在載入 / INIT 時掃 chrome_profiles/ 的，新建完要重整網頁才會出現。

在美東 VPS 上建 profile
-----------------------
**一定要在 VPS 上建，不能在本機建好再複製過去** —— 拓元的 `eps_sid` 綁發放時的出口 IP，
本機登入拿到的 cookie 到了 VPS 就作廢。

Chrome 會開在 VPS 的虛擬螢幕 :99 上，所以要先連上遠端桌面才看得到、才能點：

    1. 面板按「開機並打開 War-Room」，再按「遠端桌面」（Moonlight）
    2. 在遠端桌面裡右鍵 → 開一個終端機（xfce4-terminal），或從本機 ssh 進去
    3. cd ~/ticketbot && DISPLAY=:99 ~/venv/bin/python create_profile.py --name 帳號名
       （從 ssh 跑就一定要帶 DISPLAY=:99，不然 Chrome 找不到螢幕會直接 exit）
    4. Chrome 會出現在遠端桌面上 → 在那邊完成登入 → 關掉視窗 → 回終端機按 Enter
    5. 重整 War-Room 網頁，卡片的 chrome_profile 下拉就會出現這個名字

VPS 的 config.ENABLE_PROXY_POOL 是 False，所以不會走 CliProxy —— 這是對的：
登入用的出口 IP 就是搶票用的出口 IP，兩邊同源 eps_sid 才不會失效。
"""
import os
import re
import sys
import random
import string
import hashlib
import argparse
import subprocess

import requests

import config
from browser_login import setup_proxy_bridge, platform_chrome_flags

PROFILES_DIR = os.path.join(config.BASE_DIR, "chrome_profiles")

# 各平台登入頁。同一個 profile 可以多個平台都登（cookie 按網域分開存），
# 想讓一個帳號資料夾同時能搶拓元 + KKTIX，就分別用 --platform 各跑一次登入。
PLATFORM_LOGIN = {
    "tixcraft": "https://tixcraft.com/login",
    "kktix": "https://kktix.com/users/sign_in",
    # TicketPlus 沒有獨立登入頁（登入是全站共用的 dialog），開首頁按右上角登入
    "ticketplus": "https://ticketplus.com.tw/",
}

# 純首頁（proxy 暖機用）。剛換一個新代理 IP 就直衝 Facebook/Google OAuth，對這些平台的
# 風控來說是「陌生 IP 第一個動作就是登入」的可疑模式，容易被 reCAPTCHA 卡關；先讓這個 IP
# 逛一下普通頁面再登入，risk 分數會低很多。只在有用 proxy 時才這樣做，直連不需要暖機。
PLATFORM_HOME = {
    "tixcraft": "https://tixcraft.com/",
    "kktix": "https://kktix.com/",
    "ticketplus": "https://ticketplus.com.tw/",
}

# Chrome 常見安裝位置。美東 VPS 也要能建 profile —— 那台的登入態必須在那台上取得，
# 因為 eps_sid 綁發放時的出口 IP，拿本機登入的 cookie 上去用會直接作廢。
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
]


def find_chrome() -> str:
    for p in CHROME_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise FileNotFoundError(
        "找不到 Chrome — 請把你的 Chrome 路徑加到 create_profile.py 的 CHROME_CANDIDATES"
    )


def _make_sid(name: str, fixed: bool) -> str:
    """組 CliProxy 的 sid —— **sid 決定出口 IP**（同 sid = 同 IP，換 sid = 換 IP）。

    預設每跑一次就加一段亂數 → **每次建 profile 都是全新 IP**。這是想要的行為：
    同一個帳號重登通常是因為上次那個 IP 被風控卡住，沿用等於再撞一次牆。
    `--fixed-ip` 才會回到「同 name 拿同 IP」（想在同一個 IP 上重登時用）。

    **坑（2026-07-26 實測）：sid 只能是英數，不能有連字號**。CliProxy 的帳號字串
    `...-region-TW-sid-{sid}-t-90` 是拿 `-` 當 key/value 分隔，sid 裡再出現 `-` 整串就
    解析壞掉 → 靜默退回帳號預設 IP（實測三組不同 sid 全拿到同一個 IP，看起來像換了其實沒換）。
    中文名字也不能直接用（Basic auth 只吃 ASCII），一律先正規化，正規化後空掉就取 md5。"""
    slug = re.sub(r"[^a-zA-Z0-9]", "", name).lower()[:12]
    if not slug:
        slug = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    if fixed:
        return f"p{slug}"
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"p{slug}{rand}"


def _auto_proxy_url(sid: str) -> str:
    """ENABLE_PROXY_POOL=True 時自動從 config 建 CliProxy URL。

    注意：bot 跑時 proxy_pool 用 'acc{ACC_ID}' 當 sid，跟這裡的 'p-*' 不同 →
    登入時 IP 跟搶票 IP 不會 byte-for-byte 相同，但同 CliProxy 帳號、同台灣節點，
    risk 等級可接受。要完全一致請手動 --proxy 指定。"""
    if not config.ENABLE_PROXY_POOL:
        return ""
    user = config.CLIPROXY_USERNAME_TEMPLATE.format(acc_id=sid)
    return f"http://{user}:{config.CLIPROXY_PASSWORD}@{config.CLIPROXY_HOST}:{config.CLIPROXY_PORT}"


def _show_exit_ip(proxy_url: str) -> str:
    """打一次 ipify 確認這條 proxy 真的換了出口 IP（沒回報就無從確認「每次不同」）。
    失敗只印訊息不擋流程 —— 查 IP 掛掉不代表 proxy 不能用。"""
    try:
        res = requests.get("https://api.ipify.org?format=json",
                           proxies={"http": proxy_url, "https": proxy_url}, timeout=10)
        ip = res.json().get("ip", "")
        print(f"[PROXY] 本次出口 IP: {ip}")
        return ip
    except Exception as e:
        print(f"[PROXY] 查出口 IP 失敗（不影響登入）: {type(e).__name__}: {e}")
        return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True,
                        help="帳號名（會變成 chrome_profiles/ 底下的資料夾名）")
    parser.add_argument("--proxy", default="",
                        help='手動指定 proxy URL；省略時 config.ENABLE_PROXY_POOL=True 會自動從 CliProxy 模板建（用 name 當 sid）')
    parser.add_argument("--platform", default="tixcraft", choices=list(PLATFORM_LOGIN),
                        help="要登入哪個平台（決定開哪個登入頁）；同一 profile 可分別跑多次各平台都登")
    parser.add_argument("--url", default="",
                        help="自訂登入起始 URL（覆蓋 --platform）")
    parser.add_argument("--fixed-ip", action="store_true",
                        help="同一個 --name 每次都拿同一個 proxy IP（預設每次跑都換新 IP）")
    args = parser.parse_args()

    login_url = args.url or PLATFORM_LOGIN[args.platform]

    # 按平台分子資料夾：chrome_profiles/<平台>/<名字>/（GUI 下拉依平台只列該平台的）
    profile_dir = os.path.join(PROFILES_DIR, args.platform, args.name)
    os.makedirs(profile_dir, exist_ok=True)
    chrome = find_chrome()
    print(f"[PROFILE] 資料夾: {profile_dir}")
    print(f"[PROFILE] Chrome: {chrome}")

    # cmdline 參數組裝；有 proxy 就先起 localhost bridge 再插 --proxy-server
    cmd = [
        chrome,
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--lang=zh-TW",  # 語系跟 proxy 的台灣 IP 一致，避免 timezone/locale 跟地理位置對不上
        "--disable-blink-features=AutomationControlled",  # 保險旗標；一般 Chrome 本來就沒有這個標記
    ]
    # Linux（VPS）需要 --no-sandbox 之類的旗標，不加的話 Chrome 直接 exit。
    # 跟搶票時開的 Chrome 共用同一份定義，避免兩邊歪掉。
    cmd += platform_chrome_flags()
    # --proxy 明確指定優先；否則 ENABLE_PROXY_POOL=True 自動從 config 建
    if args.proxy:
        proxy_url = args.proxy
    else:
        sid = _make_sid(args.name, args.fixed_ip)
        proxy_url = _auto_proxy_url(sid)
        if proxy_url:
            print(f"[PROXY] sid={sid}" + ("（--fixed-ip：同 name 固定同 IP）" if args.fixed_ip
                                          else "（每次執行都換新 IP）"))
    proxy_applied = False
    if proxy_url:
        local_port = setup_proxy_bridge(proxy_url)
        if local_port is not None:
            cmd.append(f"--proxy-server=http://127.0.0.1:{local_port}")
            cmd.append("--webrtc-ip-handling-policy=disable_non_proxied_udp")
            proxy_applied = True
            _show_exit_ip(proxy_url)

    # 有用 proxy 時先開首頁暖機，不直衝登入頁（見 PLATFORM_HOME 說明）；
    # 直連沒有「陌生 IP」問題，維持原本直接開登入頁的行為。
    start_url = PLATFORM_HOME[args.platform] if proxy_applied else login_url
    cmd.append(start_url)

    # 用一般 Chrome 開（非 Selenium）→ Google 登入不會被擋
    proc = subprocess.Popen(cmd)

    print("=" * 56)
    if proxy_applied:
        print("  1. 視窗開的是首頁（用 proxy 時先暖機，降低卡 reCAPTCHA 機率）")
        print("     逛個一兩頁、等個 10~20 秒，再自己點連結進登入頁")
        print(f"     登入頁: {login_url}")
        print("  2. 在該頁完成登入（Google/Facebook 登入都可以）")
        print("     若還是跳出 reCAPTCHA，手動勾選/選圖完成即可，屬正常驗證，")
        print("     不代表失敗；只有「怎麼解都卡住」才是這組代理 IP 風評不好，")
        print("     直接同一行指令重跑一次就會換一個新 IP")
        print("  3. 登入完成後，關閉那個 Chrome 視窗")
        print("  4. 回到這裡按 Enter")
    else:
        print(f"  1. 在開啟的 Chrome 視窗登入 {args.platform}（Google 登入也 OK）")
        print(f"     登入頁: {login_url}")
        print("  2. 登入完成後，關閉那個 Chrome 視窗")
        print("  3. 回到這裡按 Enter")
    print("=" * 56)
    input()

    # 確保 Chrome 真的關了 — profile 資料夾不能被鎖著，bot 才能用
    if proc.poll() is None:
        try:
            if sys.platform == "win32":
                # Windows 要 /T 連子進程一起收；Chrome 的分頁是獨立 process
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=5)
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass

    print(f"[PROFILE] 登入狀態已存進 {profile_dir}")
    print(f"[PROFILE] 之後 GUI 下拉選單選 '{args.name}' 即可")


if __name__ == "__main__":
    main()
