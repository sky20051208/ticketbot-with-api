"""curl_cffi session 建立 + 共用 header + 暖機/keep-alive（KKTIX 版）。

跟 tixcraftapi.session 同一套設計，只是 domain / referer 換成 kktix.com。

⚠️ KKTIX 在 Cloudflare 後面，cf_clearance cookie 綁「IP + User-Agent + TLS 指紋」。
   userdata 模式登入時會抓真實瀏覽器 UA 存進 config.BROWSER_USER_AGENT，這裡據此送同一個
   UA + 對應的 Sec-Ch-Ua，並用最接近的 curl_cffi impersonate 目標對齊 TLS，避免 403。
"""
import re
import threading

from curl_cffi import requests as cf_requests

import config
import proxy_pool
from kktix_api import BASE

# curl_cffi 0.14 最高支援 chrome142；實際 Chrome 通常更新，用最接近的對齊 TLS 指紋
_IMPERSONATE = "chrome142"
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)


def _ua() -> str:
    """優先用登入時抓到的真實瀏覽器 UA（cf_clearance 就是綁這個）。"""
    return getattr(config, "BROWSER_USER_AGENT", "") or _DEFAULT_UA


def _chrome_major(ua: str) -> str:
    m = re.search(r"Chrome/(\d+)", ua)
    return m.group(1) if m else "142"


def build_session(cookie_str: str) -> cf_requests.Session:
    session = cf_requests.Session(impersonate=_IMPERSONATE)
    for item in cookie_str.split("; "):
        if "=" in item:
            k, v = item.split("=", 1)
            session.cookies.set(k.strip(), v.strip(), domain=".kktix.com")
    proxies = proxy_pool.as_dict(config.CURRENT_PROXY)
    if proxies:
        session.proxies = proxies
        print(f"[SESSION] 走代理: {proxy_pool.redact(config.CURRENT_PROXY)}")
    print(f"[SESSION] impersonate={_IMPERSONATE} UA={_ua()}")
    return session


def build_headers(referer: str = "") -> dict:
    """頁面導覽用 header（含 Sec-Fetch 導覽語意 + Upgrade-Insecure-Requests）。
    queue POST 之類非導覽請求由 caller 覆蓋 Sec-Fetch / Accept / Content-Type。"""
    ua = _ua()
    major = _chrome_major(ua)
    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Upgrade-Insecure-Requests": "1",
        "Referer": referer or BASE,
        "Sec-Ch-Ua": f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
    }


def registration_url(slug: str) -> str:
    """KKTIX 報名頁（Angular SPA）。slug = 活動 id，例如 'abcd1234'。"""
    return f"{BASE}/events/{slug}/registrations/new"


def warmup_session(session: cf_requests.Session, slug: str = ""):
    """打一次 registrations/new 建立 TLS/H2 連線，省下 T-0 第一個請求的握手時間。
    沒 slug fallback 打首頁。"""
    target = registration_url(slug) if slug else f"{BASE}/"
    try:
        session.get(target, headers=build_headers(), timeout=5)
        print(f"[WARMUP] 連線已暖機 ({target})")
    except Exception as e:
        print(f"[WARMUP] 失敗（不致命）: {e}")


def keep_alive_loop(session: cf_requests.Session, stop_event: threading.Event,
                    slug: str = "", interval: float = 30.0):
    """背景 thread：定期 ping registrations/new 維持 TLS connection + 該 endpoint warm。
    T-0 之前必須 stop_event.set() 停止，避免跟主流程搶 session。"""
    target = registration_url(slug) if slug else f"{BASE}/"
    headers = build_headers()
    while not stop_event.wait(interval):
        try:
            res = session.get(target, headers=headers, timeout=5)
            if res.status_code == 200:
                print(f"[WATCH] registrations/new 200 OK ({len(res.content):,}B)")
            else:
                print(f"[WATCH] registrations/new HTTP {res.status_code}")
        except Exception:
            pass
