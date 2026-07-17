"""建立一個專用的 Chrome user-data-dir 給某個帳號登入用。

用法:
    python create_profile.py --name 帳號名
    python create_profile.py --name 帳號名 --proxy http://user:pass@host:port

重點：這裡用「一般 Chrome」開（subprocess 直接啟動，不經 Selenium）。
Google 會擋 Selenium 控制的瀏覽器登入（"這個瀏覽器或應用程式可能有安全疑慮"），
一般 Chrome 沒有自動化旗標，Google 登入才正常。

帶 --proxy 時走 localhost bridge 自動補 auth header（同 browser_login 機制）。
意義：登入時就用 proxy IP，cookie 跟「之後 bot 搶票走的 IP」綁同源，server 不會
看到「同一 cookie 從兩個 IP 出現」的可疑訊號。

流程:
    1. 在 chrome_profiles/帳號名/ 建一份獨立的 Chrome profile
    2. 開一般 Chrome 視窗（含 proxy 設定，如有），自動跳到 Tixcraft 登入頁
    3. 你手動登入 Tixcraft（Google 登入也 OK）
    4. 關閉 Chrome 視窗 → 回 terminal 按 Enter

之後 GUI 下拉選單就會看到這個帳號名，bot 用它開 Chrome 即為已登入狀態。
"""
import os
import argparse
import subprocess

import config
from browser_login import setup_proxy_bridge

PROFILES_DIR = os.path.join(config.BASE_DIR, "chrome_profiles")

# 各平台登入頁。同一個 profile 可以多個平台都登（cookie 按網域分開存），
# 想讓一個帳號資料夾同時能搶拓元 + KKTIX，就分別用 --platform 各跑一次登入。
PLATFORM_LOGIN = {
    "tixcraft": "https://tixcraft.com/login",
    "kktix": "https://kktix.com/users/sign_in",
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

# Windows 上 Chrome 常見安裝位置
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def find_chrome() -> str:
    for p in CHROME_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise FileNotFoundError(
        "找不到 chrome.exe — 請把你的 Chrome 路徑加到 create_profile.py 的 CHROME_CANDIDATES"
    )


def _auto_proxy_url(name: str) -> str:
    """ENABLE_PROXY_POOL=True 時自動從 config 建 CliProxy URL；
    sid 用 'p-{name}'（不跟 bot 跑時的 'acc{ACC_ID}' 撞），重複跑同 name 拿同 IP。

    注意：bot 跑時 proxy_pool 用 'acc{ACC_ID}' 當 sid，跟這裡的 'p-{name}' 不同 →
    登入時 IP 跟搶票 IP 不會 byte-for-byte 相同，但同 CliProxy 帳號、同台灣節點，
    risk 等級可接受。要完全一致請手動 --proxy 指定，或把 proxy_pool._build_cliproxy_url
    改成用 profile name 而非 ACC_ID。"""
    if not config.ENABLE_PROXY_POOL:
        return ""
    user = config.CLIPROXY_USERNAME_TEMPLATE.format(acc_id=f"p-{name}")
    return f"http://{user}:{config.CLIPROXY_PASSWORD}@{config.CLIPROXY_HOST}:{config.CLIPROXY_PORT}"


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
    # --proxy 明確指定優先；否則 ENABLE_PROXY_POOL=True 自動從 config 建
    proxy_url = args.proxy or _auto_proxy_url(args.name)
    proxy_applied = False
    if proxy_url:
        local_port = setup_proxy_bridge(proxy_url)
        if local_port is not None:
            cmd.append(f"--proxy-server=http://127.0.0.1:{local_port}")
            cmd.append("--webrtc-ip-handling-policy=disable_non_proxied_udp")
            proxy_applied = True

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
        print("     可以換個 --name 拿新的 sid 重試")
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
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=5)
        except Exception:
            pass

    print(f"[PROFILE] 登入狀態已存進 {profile_dir}")
    print(f"[PROFILE] 之後 GUI 下拉選單選 '{args.name}' 即可")


if __name__ == "__main__":
    main()
