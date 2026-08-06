"""curl_cffi session + Bearer token 解析（TicketPlus 版）。

TicketPlus 的登入態**不是一般 session cookie**，而是 JS 寫的一個名為 `user` 的 cookie，
內容是 URL-encoded JSON：{"access_token":"<JWT>", ...}。前端每支 API 都是自己從這個
cookie 取 access_token 塞 `authorization: Bearer <token>`，所以我們只要那串 token，
把它掛在 header 上就等於登入 —— cookie 本身反而不用帶。

（這也是「在 DevTools 找不到 cookie」的原因：要看 Application → Cookies → `user`，
  不是 Network 的 request cookie。）
"""
import base64
import json
import time
from urllib.parse import unquote

from curl_cffi import requests as cf_requests

import config
import proxy_pool
from ticketplus_api import BASE, CONFIG_API

_IMPERSONATE = "chrome142"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")


def extract_token(cookie_str: str) -> str:
    """從各種貼法裡挖出 access_token，挖不到回空字串。吃得下：
      - 整串 cookie（`...; user=%7B%22access_token%22...%7D; ...`）
      - 只有 user cookie 的值（JSON，url-encoded 或原樣）
      - 直接貼 JWT（eyJ... 開頭）
    """
    raw = (cookie_str or "").strip()
    if not raw:
        return ""
    if raw.startswith("eyJ"):
        return raw.split(";")[0].strip()

    blob = raw
    if "user=" in raw:
        blob = raw.split("user=", 1)[1]
        # cookie 值不含 `;`，但 JSON 裡可能有 `%7B...%7D`，切到第一個 `;` 就是完整值
        blob = blob.split(";", 1)[0]
    for candidate in (unquote(blob), blob):
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get("access_token"):
            return str(data["access_token"])
    return ""


def token_remaining(token: str) -> float | None:
    """access_token 還剩幾秒有效；不是 JWT 或解不開回 None（當作「不知道」，不擋流程）。

    **為什麼一定要有這個**（2026-08-06 實際在正式搶票時掛掉才補的）：userdata 模式是
    去讀 profile 裡的 `user` cookie，而那顆 cookie 上次登入留下來就一直躺在那。
    `open_and_login()` 看到有 token 就當成登入成功（log 上是「1 毫秒完成登入」），
    等到 T-0 送單才發現 **errCode 103 token 無效**，整場就沒了。

    TicketPlus 的 token 是 JWT，壽命只有 60 分鐘（實測 exp - iat = 3600）。payload 是
    base64url 的 JSON，直接讀 `exp` 就好 —— 我們只需要判斷「還能不能用」，不需要驗簽
    （簽章本來也只有官方驗得了）。
    """
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)      # base64url 慣例不帶 padding，要自己補
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None
    if not isinstance(exp, (int, float)):
        return None
    return float(exp) - time.time()


def describe_token(token: str) -> str:
    """給 log 用的一句話：還剩多久 / 過期多久。"""
    remaining = token_remaining(token)
    if remaining is None:
        return "有效期未知"
    if remaining < 0:
        return f"已過期 {-remaining / 60:.0f} 分鐘"
    return f"剩 {remaining / 60:.0f} 分鐘"


def build_session(token: str = "") -> cf_requests.Session:
    """建 session。token 只影響預設 header，隨時可以用 auth_headers() 覆蓋。"""
    session = cf_requests.Session(impersonate=_IMPERSONATE)
    proxies = proxy_pool.as_dict(config.CURRENT_PROXY)
    if proxies:
        session.proxies = proxies
        print(f"[SESSION] 走代理: {config.CURRENT_PROXY}")
    print(f"[SESSION] impersonate={_IMPERSONATE} token={'有' if token else '無'}")
    return session


def build_headers(token: str = "") -> dict:
    """XHR header（TicketPlus 全部 API 都是 CORS XHR，不是頁面導覽）。"""
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": f"{BASE}/",
        "Sec-Ch-Ua": '"Chromium";v="142", "Google Chrome";v="142", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def warmup_session(session: cf_requests.Session):
    """先跟兩個 API 網域各握一次手，省掉 T-0 第一發的 TLS 成本。
    curl_cffi 的連線池是 thread-local，所以這裡跟主線請求必須同一條 thread。"""
    url = f"{CONFIG_API}/get?productId=&"
    try:
        res = session.get(url, headers=build_headers(), timeout=5)
        print(f"[WARMUP] 票況 連線已暖機 HTTP {res.status_code}")
    except Exception as e:
        print(f"[WARMUP] 票況 暖機失敗（不致命）: {e}")


def make_keepalive(session: cf_requests.Session, product_ids: list[str],
                   interval: float = 20.0, stop_before: float = 15.0):
    """回傳給 TimeWatcher 每輪呼叫的 callback：在**主執行緒**上定期 ping 票況 API。

    為什麼非要主執行緒自己打：curl_cffi 的連線池是 thread-local，背景 thread 暖的是
    它自己那條，暖不到主執行緒 —— 而 T-0 第一發偵測正是主執行緒打的。

    **只暖票況網域，刻意不碰 queue.ticketplus.com.tw**（2026-07-29 決定）：搶票前對排隊
    系統發任何請求都跟「還沒要買票」的正常使用者行為不符，寧可讓第一發 enqueue 自己付
    TLS 握手（實測貴約 0.6s），也不要在對方的排隊系統留下多餘足跡。

    最後 stop_before 秒停手，不讓網路 IO 影響倒數精度。"""
    url = f"{CONFIG_API}/get?productId={','.join(product_ids)}&"
    headers = build_headers()
    state = {"last": 0.0}

    def _tick(remaining: float):
        if remaining <= stop_before:
            return
        now = time.monotonic()
        if now - state["last"] < interval:
            return
        state["last"] = now
        t0 = time.monotonic()
        try:
            session.get(url, headers=headers, timeout=5)
            print(f"[KEEPALIVE] 票況連線續命 RTT {(time.monotonic() - t0) * 1000:.0f}ms")
        except Exception as e:
            print(f"[KEEPALIVE] ping 失敗: {type(e).__name__}: {e}")

    return _tick
