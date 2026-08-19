"""curl_cffi session 建立 + 共用 header + 暖機/keep-alive/連線池預熱。

**連線是 thread-local 的**（2026-07-26 實測，整包延遲的主因）：curl_cffi 的 Session
預設 `use_thread_local_curl=True`，也就是「每個 thread 各自一份 curl handle = 各自一套
連線池」。所以
  - 背景 keep-alive thread 暖的是**它自己**那條連線，主執行緒（T-0 真正打第一發的那條）
    照樣被 proxy/LB 的 idle timeout 收掉 → 第一發還是要重跑 TLS 握手
  - 每次 `with ThreadPoolExecutor(...)` 都是全新 thread = 全新連線，並行抓驗證碼那發
    永遠在付握手成本
實測（走 CliProxy）：重用連線 ~360ms、新建連線 760~3500ms，差 2~10 倍。
對策：並行請求一律丟 `WarmPool` 這組**常駐** worker，倒數期間持續 ping，讓主執行緒 +
每條 worker 的連線全部保持熱。
"""
import base64
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests as cf_requests

import config
import proxy_pool
from tixcraftapi import BASE
from tixcraftapi.parsing import game_not_open, has_area_button


# cookie `_csrf` 的內層長相：urldecode 後是 `a:2:{i:0;s:5:"_csrf";i:1;s:32:"<token>";}`
_CSRF_IN_COOKIE = re.compile(r's:5:"_csrf";i:1;s:\d+:"([^"]+)";')


def csrf_from_cookie(session: cf_requests.Session) -> str | None:
    """從 cookie 直接算出一顆可用的表單 `_csrf`，**不用 GET 那一頁**。拿不到回 None。

    Yii2 的 CSRF 是 masked token：`maskToken = base64url(mask + (mask XOR token))`，
    每次 render 值都不同，但 server 驗證時把 POST 來的和 cookie 裡的**都 unmask 再比對**，
    所以只要 token 一樣，mask 是誰做的無所謂。而 cookie 那顆被 cookie-validation 包成
    「64 字 HMAC + urlencode(PHP serialize)」，真 token 就寫在 `s:32:"…"` 裡面。

    2026-08-16 對 26_joji 實測三組（驗證碼一律故意打錯，不會成單）：
      - 亂造 token          → 301 踢回首頁
      - GET 頁面拿的 _csrf  → 200，58589 bytes
      - 本函式算的 _csrf    → 200，58589 bytes（跟上一行 byte 數完全相同）
    """
    raw = session.cookies.get("_csrf")
    if not raw:
        return None
    m = _CSRF_IN_COOKIE.search(urllib.parse.unquote(raw))
    if not m:
        return None
    token = m.group(1).encode()
    mask = os.urandom(len(token))
    masked = bytes(a ^ b for a, b in zip(mask, token))
    # Yii2 的 base64UrlEncode 只換 +/ 不去 padding，要照做
    return base64.b64encode(mask + masked).decode().replace("+", "-").replace("/", "_")


def build_session(cookie_str: str, local_ip: str = "") -> cf_requests.Session:
    """`local_ip` 非空時所有請求從該本機 IP 出去（curl 的 CURLOPT_INTERFACE）。

    多開隔離用：一台機器掛多顆公網 IP，每個 instance 綁一顆，取代 proxy 換 IP。
    **Chrome 必須綁同一顆**（見 [bind_proxy.py](bind_proxy.py)）—— eps_sid 綁發放時的
    出口 IP，兩邊不一致的話瀏覽器過完挑戰拿到的 cookie 一交給 curl_cffi 就作廢。
    """
    kw = {"interface": local_ip} if local_ip else {}
    session = cf_requests.Session(impersonate="chrome124", **kw)
    if local_ip:
        print(f"[SESSION] 綁定來源 IP: {local_ip}")
    for item in cookie_str.split("; "):
        if "=" in item:
            k, v = item.split("=", 1)
            session.cookies.set(k.strip(), v.strip(), domain="tixcraft.com")
    proxies = proxy_pool.as_dict(config.CURRENT_PROXY)
    if proxies:
        session.proxies = proxies
        print(f"[SESSION] 走代理: {proxy_pool.redact(config.CURRENT_PROXY)}")
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


def warmup_session(session: cf_requests.Session, slug: str = "", shots: int = 2):
    """打 /activity/game/{slug}：第 1 發建 TLS/H2 連線（含握手成本），
    第 2 發量純 RTT — 這個數字就是這條 session（含 proxy）的延遲基準，
    T-0 的 [POLL] / [PERF] 數字比它高很多就代表是 server 端塞車而不是線路問題。
    沒 slug fallback 打首頁。打真正的 game endpoint 讓 server 端 routing handler 也 warm。

    **`shots=1` 的用途**：啟動序列對同一個 game URL 打太多發會撞 eps 限流（約 3 req/s）。
    後面 `__main__.prefetch_area_urls` 本來就要再打一發同樣的頁面，讓它兼任「第 2 發」
    並回報 RTT 基準即可，這裡就只留建連線那一發。
    """
    target = f"{BASE}/activity/game/{slug}" if slug else f"{BASE}/"
    try:
        t0 = time.monotonic()
        session.get(target, headers=build_headers(), timeout=5)
        first_ms = (time.monotonic() - t0) * 1000
        if shots < 2:
            print(f"[WARMUP] 連線已建立 ({target}) | 第1發 {first_ms:.0f}ms（含握手）"
                  f"— 純 RTT 基準由稍後的 [PREOPEN] 那發量，不重複打")
            return
        t1 = time.monotonic()
        session.get(target, headers=build_headers(), timeout=5)
        second_ms = (time.monotonic() - t1) * 1000
        print(f"[WARMUP] 連線已暖機 ({target}) | 第1發 {first_ms:.0f}ms（含握手）/ 第2發 {second_ms:.0f}ms（純 RTT 基準）")
    except Exception as e:
        print(f"[WARMUP] 失敗（不致命）: {e}")


class WarmPool:
    """固定 size 條常駐 worker，每條各自握著一份「熱」的 curl 連線。

    搶票期間所有需要並行的請求都丟這裡（不要再 `with ThreadPoolExecutor(...)` 現開），
    理由見本檔頂端說明：新 thread = 新連線 = 多付一次 TLS 握手。

    生命週期跟 process 一樣長（daemon thread，不用特別收）。
    """

    # 預熱/續命用的目標：**故意挑靜態檔**。worker 要的只是「同一個 host 的熱 TLS 連線」，
    # 而動態頁每發都要吃 eps 的請求配額（同一 URL 約 3 req/s 就開始 403），啟動瞬間
    # size 條 worker 一起打 game 頁，加上 warmup 和 prefetch，實測 0.6 秒內 5 發同 URL
    # 直接把後面的 AREA 打成 403。靜態檔走 nginx 直出（實測 TTFB 128ms，動態頁 300ms+），
    # 不經 PHP、不算動態配額，建連線的效果完全一樣。
    WARM_TARGET = f"{BASE}/js/common.js?v=20221013"

    def __init__(self, session: cf_requests.Session, slug: str = "", size: int = 2):
        self.session = session
        self.size = size
        self.target = self.WARM_TARGET
        self.headers = build_headers()
        self._ex = ThreadPoolExecutor(max_workers=size, thread_name_prefix="warm")

    def submit(self, fn, *args, **kwargs):
        return self._ex.submit(fn, *args, **kwargs)

    def warm(self, timeout: float = 8.0, quiet: bool = False):
        """讓「每一條」worker 各打一發，把連線建起來 / 續命。

        用 Barrier 把先領到工作的 worker 卡住，逼 executor 把後面的工作派給還沒動的
        worker —— 不然 N 個工作可能被同一條 thread 依序吃光，其他 worker 的連線還是冷的。"""
        barrier = threading.Barrier(self.size)

        def _one():
            try:
                barrier.wait(timeout=timeout)   # 等所有 worker 都就位再一起打
            except threading.BrokenBarrierError:
                pass
            t0 = time.monotonic()
            try:
                self.session.get(self.target, headers=self.headers, timeout=timeout)
                return (time.monotonic() - t0) * 1000
            except Exception:
                return -1.0

        futures = [self._ex.submit(_one) for _ in range(self.size)]
        rtts = []
        for f in futures:
            try:
                rtts.append(f.result(timeout=timeout + 2))
            except Exception:
                rtts.append(-1.0)
        if not quiet:
            shown = " / ".join("失敗" if ms < 0 else f"{ms:.0f}ms" for ms in rtts)
            print(f"[POOL] {self.size} 條並行連線已預熱: {shown}")
        return rtts


def keep_alive_loop(session: cf_requests.Session, stop_event: threading.Event,
                    slug: str = "", interval: float = 15.0,
                    pool: "WarmPool | None" = None):
    """背景 thread：定期 ping /activity/game/{slug} 維持 TLS connection + 該 endpoint warm。
    間隔 15s（原 30s）：proxy / LB 的 idle timeout 常見 30-60s，30s 一次太貼邊，
    T-0 第一發可能踩到剛被收掉的連線要重握手 — 這正是 session 延遲的主要來源之一。
    每次 ping 帶 RTT log，開賣前就能看出線路延遲有沒有異常。
    順便看 /game/ HTML 狀態：
      - 「即將開賣 / coming soon」→ 尚未開放
      - 有場次按鈕 → !!! 提前開賣 !!!（log 大聲警告）
      - 其他 → 內容狀態未知
    T-0 之前必須 stop_event.set() 停止，避免跟主流程搶 session。

    給 pool 時每輪順便 `pool.warm()` 讓 worker 的連線一起續命（這條 thread 自己的 ping
    只能暖到自己那份 handle，暖不到別條 —— 見本檔頂端說明）。"""
    target = f"{BASE}/activity/game/{slug}" if slug else f"{BASE}/"
    headers = build_headers()
    while not stop_event.wait(interval):
        if pool is not None:
            pool.warm(quiet=True)
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
