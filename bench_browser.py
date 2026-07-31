"""eps 挑戰通關測試 —— 在這台機器上開真瀏覽器，能不能拿到有效的 eps_sid。

bench_eps.py 只證明「這個 IP 沒被 403 黑名單擋」（401 = 待挑戰，是正常訪客待遇）。
挑戰要瀏覽器跑 JS 才過得了，過了會發一顆綁**當前 IP** 的 eps_sid。

這支把整條驗證做完：
    1. Selenium 開 Chrome 進拓元 -> 等挑戰跑完
    2. 判斷是否還停在挑戰頁
    3. 把 cookie 掏出來餵給 curl_cffi（= 你 bot 真正的請求路徑）再打一次

curl_cffi 那步回 200 就代表整條鏈在這台機器上成立，可以開始搬。

用 Selenium 不用 nodriver，是因為拓元正式流程本來就走 Selenium（browser_login.py）。

用法（VPS，無螢幕環境用 Xvfb 當虛擬顯示器）：
    ~/venv/bin/pip install selenium
    xvfb-run -a ~/venv/bin/python bench_browser.py
本機（有螢幕）直接跑：
    python bench_browser.py
"""
import argparse
import sys
import time

from curl_cffi import requests as cf_requests
from selenium import webdriver

BASE = "https://tixcraft.com"
PROBE = f"{BASE}/activity"
# 挑戰頁的特徵：eps 自己的資產路徑 + 擋人頁文案
CHALLENGE_MARKERS = ["/epsf/asset", "Identity Verified", "Browsing Activity Has Been Paused"]
WAIT_FIRST = 8      # 給挑戰跑 JS 的時間
WAIT_RETRY = 10     # 第一次還沒過就再等這麼久


def looks_like_challenge(html: str) -> str:
    for m in CHALLENGE_MARKERS:
        if m in html:
            return m
    return ""


def build_driver(proxy: str):
    opts = webdriver.ChromeOptions()
    # 無螢幕機器上 Chrome 的沙箱常常起不來；VPS 是單用途機器，關掉沒差
    for arg in ("--no-sandbox", "--disable-dev-shm-usage", "--no-first-run",
                "--no-default-browser-check", "--disable-notifications",
                "--window-size=1280,900"):
        opts.add_argument(arg)
    if proxy:
        # Chrome cmdline 不吃 inline auth，靠 tixcraftapi.proxy_bridge 起 localhost
        # forwarder 補 Proxy-Authorization（跟 browser_login 走同一套）
        from tixcraftapi.proxy_bridge import LocalProxyBridge
        port = LocalProxyBridge(proxy).start()
        opts.add_argument(f"--proxy-server=http://127.0.0.1:{port}")
        opts.add_argument("--webrtc-ip-handling-policy=disable_non_proxied_udp")
        print(f"[0/3] proxy bridge 127.0.0.1:{port} -> {proxy.split('@')[-1]}")
    return webdriver.Chrome(options=opts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="", help="例：http://user:pass@host:3010")
    proxy = ap.parse_args().proxy

    print("=" * 74)
    print("[1/3] 啟動 Chrome …")
    driver = build_driver(proxy)
    try:
        print(f"[2/3] 開 {PROBE} …")
        driver.get(PROBE)
        time.sleep(WAIT_FIRST)

        html, title = driver.page_source, driver.title
        marker = looks_like_challenge(html)
        print("-" * 74)
        print(f"  頁面標題  : {title}")
        print(f"  HTML 長度 : {len(html)}B")
        if marker:
            print(f"  [!!] 還停在挑戰頁（命中 {marker!r}）—— 再等 {WAIT_RETRY} 秒重看")
            time.sleep(WAIT_RETRY)
            html, title = driver.page_source, driver.title
            marker = looks_like_challenge(html)
            print(f"  重看：標題={title} 長度={len(html)}B 挑戰特徵={marker or '無'}")

        jar = {c["name"]: c["value"] for c in driver.get_cookies()}
        print(f"  拓元 cookie : {len(jar)} 顆 -> {', '.join(sorted(jar)) or '(空)'}")
        print(f"  eps_sid     : {'有' if 'eps_sid' in jar else '沒有 ← 挑戰沒過'}")
    finally:
        driver.quit()

    print("-" * 74)
    print("[3/3] 拿這組 cookie 走 curl_cffi（= bot 真正的請求路徑）再打一次 …")
    cookie_str = "; ".join(f"{k}={v}" for k, v in jar.items())
    session = cf_requests.Session(impersonate="chrome124")
    if proxy:
        # 瀏覽器跟 curl_cffi 一定要同一個出口 IP，不然 eps_sid 立刻作廢
        session.proxies = {"http": proxy, "https": proxy}

    ok = False
    for label, path in [("活動列表", "/activity"), ("活動頁", "/activity/game/26_joji")]:
        res = session.get(BASE + path, headers={"Cookie": cookie_str}, timeout=20,
                          allow_redirects=False)
        hit = looks_like_challenge(res.text or "")
        good = res.status_code == 200 and not hit
        ok = ok or good
        print(f"  {'[OK]' if good else '[!!]'} {label:8s} HTTP {res.status_code} "
              f"({len(res.text or '')}B){'  挑戰特徵=' + hit if hit else ''}")

    print("=" * 74)
    if ok:
        print("結論：這台機器過得了 eps -> 搬遷可行，下一步部署整包 bot")
    else:
        print("結論：過不了。依序試：")
        print("  1. 加 --proxy 走住宅出口")
        print("  2. 遠端桌面手動看那頁長怎樣（可能是 CAPTCHA 要人點）")
        print("  3. 放棄這個機房")
    print("=" * 74)


if __name__ == "__main__":
    sys.exit(main())
