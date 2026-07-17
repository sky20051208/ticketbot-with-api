"""curl_cffi session 建立 + 共用 header + 暖機/keep-alive。"""
import threading
import time

from curl_cffi import requests as cf_requests

import config
import proxy_pool
from tixcraftapi import BASE
from tixcraftapi.parsing import game_not_open, has_area_button


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
    """打兩次 /activity/game/{slug}：第 1 發建 TLS/H2 連線（含握手成本），
    第 2 發量純 RTT — 這個數字就是這條 session（含 proxy）的延遲基準，
    T-0 的 [POLL] / [PERF] 數字比它高很多就代表是 server 端塞車而不是線路問題。
    沒 slug fallback 打首頁。打真正的 game endpoint 讓 server 端 routing handler 也 warm。"""
    target = f"{BASE}/activity/game/{slug}" if slug else f"{BASE}/"
    try:
        t0 = time.monotonic()
        session.get(target, headers=build_headers(), timeout=5)
        first_ms = (time.monotonic() - t0) * 1000
        t1 = time.monotonic()
        session.get(target, headers=build_headers(), timeout=5)
        second_ms = (time.monotonic() - t1) * 1000
        print(f"[WARMUP] 連線已暖機 ({target}) | 第1發 {first_ms:.0f}ms（含握手）/ 第2發 {second_ms:.0f}ms（純 RTT 基準）")
    except Exception as e:
        print(f"[WARMUP] 失敗（不致命）: {e}")


def keep_alive_loop(session: cf_requests.Session, stop_event: threading.Event,
                    slug: str = "", interval: float = 15.0):
    """背景 thread：定期 ping /activity/game/{slug} 維持 TLS connection + 該 endpoint warm。
    間隔 15s（原 30s）：proxy / LB 的 idle timeout 常見 30-60s，30s 一次太貼邊，
    T-0 第一發可能踩到剛被收掉的連線要重握手 — 這正是 session 延遲的主要來源之一。
    每次 ping 帶 RTT log，開賣前就能看出線路延遲有沒有異常。
    順便看 /game/ HTML 狀態：
      - 「即將開賣 / coming soon」→ 尚未開放
      - 有場次按鈕 → !!! 提前開賣 !!!（log 大聲警告）
      - 其他 → 內容狀態未知
    T-0 之前必須 stop_event.set() 停止，避免跟主流程搶 session。"""
    target = f"{BASE}/activity/game/{slug}" if slug else f"{BASE}/"
    headers = build_headers()
    while not stop_event.wait(interval):
        try:
            t0 = time.monotonic()
            res = session.get(target, headers=headers, timeout=5)
            rtt = (time.monotonic() - t0) * 1000
            if not slug:
                continue
            if res.status_code != 200:
                print(f"[WATCH] ping HTTP {res.status_code} ({rtt:.0f}ms)")
                continue
            html = res.text
            if game_not_open(html):
                print(f"[WATCH] /game/ 尚未開放 (即將開賣) | RTT {rtt:.0f}ms")
            elif has_area_button(html):
                print(f"[WATCH] !!! /game/ 已出現場次按鈕，疑似提前開賣 !!! (RTT {rtt:.0f}ms)")
            else:
                print(f"[WATCH] /game/ 200 OK ({len(res.content):,}B) 內容無場次 | RTT {rtt:.0f}ms")
        except Exception as e:
            print(f"[WATCH] ping 失敗: {type(e).__name__}: {e}")
