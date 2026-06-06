"""curl_cffi session 建立 + 共用 header + 暖機/keep-alive。"""
import re
import threading

from curl_cffi import requests as cf_requests

import config
import proxy_pool
from tixcraftapi import BASE

# /game/ 頁出現場次按鈕的特徵（用於 keep_alive_loop 偵測「提前開賣」）
_GAME_BTN_RE = re.compile(r'data-href=["\'][^"\']*?/ticket/area/')


def build_session(cookie_str: str) -> cf_requests.Session:
    session = cf_requests.Session(impersonate="chrome124")
    for item in cookie_str.split("; "):
        if "=" in item:
            k, v = item.split("=", 1)
            session.cookies.set(k.strip(), v.strip(), domain="tixcraft.com")
    proxies = proxy_pool.as_dict(config.CURRENT_PROXY)
    if proxies:
        session.proxies = proxies
        print(f"[SESSION] 走代理: {config.CURRENT_PROXY}")
    return session


def build_headers(referer: str = "") -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": BASE,
        "Referer": referer or BASE,
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    }


def warmup_session(session: cf_requests.Session, slug: str = ""):
    """打一次 /activity/game/{slug} 建立 TLS/H2 連線，省下 T-0 第一個請求的握手時間。
    沒 slug fallback 打首頁。打真正的 game endpoint 讓 server 端 routing handler 也 warm。"""
    target = f"{BASE}/activity/game/{slug}" if slug else f"{BASE}/"
    try:
        session.get(target, headers=build_headers(), timeout=5)
        print(f"[WARMUP] 連線已暖機 ({target})")
    except Exception as e:
        print(f"[WARMUP] 失敗（不致命）: {e}")


def keep_alive_loop(session: cf_requests.Session, stop_event: threading.Event,
                    slug: str = "", interval: float = 30.0):
    """背景 thread：定期 ping /activity/game/{slug} 維持 TLS connection + 該 endpoint warm，
    順便看 /game/ HTML 狀態：
      - 「即將開賣 / coming soon」→ 尚未開放
      - 有場次按鈕 → !!! 提前開賣 !!!（log 大聲警告）
      - 其他 → 內容狀態未知
    T-0 之前必須 stop_event.set() 停止，避免跟主流程搶 session。"""
    target = f"{BASE}/activity/game/{slug}" if slug else f"{BASE}/"
    headers = build_headers()
    while not stop_event.wait(interval):
        try:
            res = session.get(target, headers=headers, timeout=5)
            if not slug or res.status_code != 200:
                continue
            html = res.text
            if "即將開賣" in html or "coming soon" in html.lower():
                print("[WATCH] /game/ 尚未開放 (即將開賣)")
            elif _GAME_BTN_RE.search(html):
                print("[WATCH] !!! /game/ 已出現場次按鈕，疑似提前開賣 !!!")
            else:
                print(f"[WATCH] /game/ 200 OK ({len(res.content):,}B) 內容無場次")
        except Exception:
            pass
