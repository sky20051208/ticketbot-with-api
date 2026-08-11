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
from ticketplus_api import BASE, CONFIG_API, QUEUE_API, USER_API

# 多開時每個 instance 要長得不一樣。**只換 IP 不夠** —— 20~30 個客人的帳號如果
# TLS 指紋、User-Agent、Sec-Ch-Ua 全部一模一樣，同一毫秒下單同一場活動，那本身就是
# 比 IP 更強的關聯訊號（2026-08-09 檢查發現當時所有 instance 只有 token 不同）。
#
# 三個原則，做錯比不做更糟：
#   1. **指紋和 header 必須內部一致**：TLS 說 Chrome 146、UA 卻寫 142、Sec-Ch-Ua 又寫
#      別的版本 —— 這種矛盾組合比「大家都一樣」更容易被挑出來
#   2. **同一個 ACC_ID 永遠拿同一組**：帳號的瀏覽器指紋每次執行都在變也是異常，
#      所以用 ACC_ID 取模，不用亂數
#   3. **`Sec-Ch-Ua` 只有 Chromium 送**：Firefox / Safari 根本沒有這組 header。
#      用 safari 指紋卻照送 Sec-Ch-Ua 等於自己舉手，所以 header 要跟著家族換
#
# **只換 Chrome 版本沒有用**（2026-08-09 實測）：chrome136/142/145/146 的 JA4 完全相同
# （`t13d1516h2_8daaf6152771_d8a2da3f94cd`），因為 Chrome 這幾版的 TLS ClientHello 沒變。
# 要讓 TLS 層真的分開，必須跨瀏覽器家族。
_PROFILES = [
    # (impersonate, 家族, UA)
    ("chrome146", "chromium",
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"),
    ("firefox147", "firefox",
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0"),
    ("safari184", "safari",
     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/18.4 Safari/605.1.15"),
    ("chrome145", "chromium",
     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"),
    ("firefox144", "firefox",
     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:144.0) Gecko/20100101 Firefox/144.0"),
    ("chrome131", "chromium",
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    ("safari260", "safari",
     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/26.0 Safari/605.1.15"),
    ("chrome142", "chromium",
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"),
]


def _supported() -> list[tuple[str, str, str]]:
    """只留這台機器的 curl_cffi 真的認得的指紋。

    **不同機器的 curl_cffi 版本不一樣**：東京機是 0.16（有 chrome145/146、firefox147），
    開發機是 0.14（沒有）。清單裡塞了不支援的目標，`Session(impersonate=...)` 會直接
    拋 ImpersonateError 讓整個 bot 起不來 —— 2026-08-10 在本機踩到。
    所以啟動時先過濾，寧可指紋種類少一點也不能炸掉。
    """
    global _SUPPORTED_CACHE
    if _SUPPORTED_CACHE is not None:
        return _SUPPORTED_CACHE
    try:
        import typing
        from curl_cffi.requests.impersonate import BrowserTypeLiteral
        names = set(typing.get_args(BrowserTypeLiteral))
    except Exception:
        try:
            from curl_cffi.requests import BrowserType
            names = {b.value for b in BrowserType}
        except Exception:
            names = set()
    usable = [p for p in _PROFILES if not names or p[0] in names] or [_PROFILES[-1]]
    dropped = len(_PROFILES) - len(usable)
    if dropped:
        print(f"[SESSION] 這台的 curl_cffi 不支援其中 {dropped} 種指紋，"
              f"可用 {len(usable)} 種（多開的區隔度會下降，升級 curl_cffi 可恢復）")
    _SUPPORTED_CACHE = usable
    return usable


_SUPPORTED_CACHE = None


def browser_profile() -> tuple[str, str, str]:
    """這個 instance 該用哪一組瀏覽器指紋。回 (impersonate, 家族, User-Agent)。

    **每次都重讀 `config.ACC_ID`**，不要在 import 時算好 —— GUI 是用
    `load_config_override()` 在啟動後才把 ACC_ID 蓋進來的。
    """
    pool = _supported()
    return pool[int(config.ACC_ID or 0) % len(pool)]


def _client_hints(family: str, ua: str) -> dict:
    """Chromium 專屬的 Sec-Ch-* header。非 Chromium 回空 dict（它們不送這組）。"""
    if family != "chromium":
        return {}
    import re
    ver = re.search(r"Chrome/(\d+)", ua).group(1)
    return {
        "Sec-Ch-Ua": f'"Chromium";v="{ver}", "Google Chrome";v="{ver}", '
                     f'"Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"' if "Macintosh" in ua else '"Windows"',
    }


def parse_user_cookie(cookie_str: str) -> dict:
    """把 `user` cookie 解成 dict，解不出來回空 dict。吃得下：
      - 整串 cookie（`...; user=%7B%22access_token%22...%7D; ...`）
      - 只有 user cookie 的值（JSON，url-encoded 或原樣）
    """
    raw = (cookie_str or "").strip()
    if not raw:
        return {}
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
        if isinstance(data, dict):
            return data
    return {}


def extract_token(cookie_str: str) -> str:
    """挖出 access_token，挖不到回空字串。除了 `user` cookie，也吃「直接貼 JWT」。"""
    raw = (cookie_str or "").strip()
    if raw.startswith("eyJ"):
        return raw.split(";")[0].strip()
    return str(parse_user_cookie(cookie_str).get("access_token") or "")


def extract_refresh_token(cookie_str: str) -> str:
    """挖出 refresh_token。

    `user` cookie 裡除了 access_token 還有一顆 refresh_token，官方前端就是拿它去打
    `/user/api/v1/refreshToken` 自動續命（access_token 剩不到 5 分鐘就換一張）。
    有了它，profile 裡的舊 cookie 就不必然要手動重登。
    """
    return str(parse_user_cookie(cookie_str).get("refresh_token") or "")


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


def refresh_access_token(session: cf_requests.Session, refresh_token: str) -> tuple[str, str]:
    """用 refresh_token 換一張新的 access_token。回 (access_token, refresh_token)，失敗回 ("", "")。

    這不是旁門左道 —— 官方前端自己就在用：它把 token 到期時間存在 cookie 裡，發現剩不到
    5 分鐘（`-3e5` 毫秒）就先打這支換一張再繼續。我們只是照做。

    **Authorization 帶的是 refresh_token，不是 access_token**（前端原始碼就是這樣寫的）。
    回應長這樣：`{"errCode":"00","userInfo":{"access_token":…,"refresh_token":…}}`。

    **refresh_token 是輪替的**（前端的 REFRESH_TOKEN mutation 會把它一起換掉），所以換完
    一定要把新的兩顆寫回瀏覽器 cookie，不然瀏覽器手上那顆舊的就作廢了 —— 搶到票要跳
    結帳頁時會被登出，客人就付不了款。寫回的部分在 browser_session.write_user_cookie()。
    """
    if not refresh_token:
        return "", ""
    try:
        res = session.post(f"{USER_API}/refreshToken", json={},
                           headers=build_headers(refresh_token), timeout=10)
        data = res.json()
    except Exception as e:
        print(f"[TOKEN] 換發 access_token 失敗: {type(e).__name__}: {e}")
        return "", ""
    if str(data.get("errCode")) != "00":
        print(f"[TOKEN] 換發被拒 errCode={data.get('errCode')} {data.get('errMsg', '')}"
              f" —— refresh_token 也過期了，只能重新登入")
        return "", ""
    info = data.get("userInfo") or {}
    return str(info.get("access_token") or ""), str(info.get("refresh_token") or "")


def build_session(token: str = "") -> cf_requests.Session:
    """建 session。token 只影響預設 header，隨時可以用 auth_headers() 覆蓋。"""
    imp, family, _ua = browser_profile()
    session = cf_requests.Session(impersonate=imp)
    proxies = proxy_pool.as_dict(config.CURRENT_PROXY)
    if proxies:
        session.proxies = proxies
        print(f"[SESSION] 走代理: {proxy_pool.redact(config.CURRENT_PROXY)}")
    # 印出來才看得出多開時每個 instance 真的長不一樣
    print(f"[SESSION] acc={config.ACC_ID} impersonate={imp}（{family}）"
          f" token={'有' if token else '無'}")
    return session


def build_headers(token: str = "") -> dict:
    """XHR header（TicketPlus 全部 API 都是 CORS XHR，不是頁面導覽）。"""
    _imp, family, ua = browser_profile()
    headers = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": f"{BASE}/",
        # Sec-Fetch-* 三個瀏覽器都送，Sec-Ch-Ua 只有 Chromium 送（見 _client_hints）
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }
    headers.update(_client_hints(family, ua))
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

    **queue 網域也要暖**（2026-08-12 改；原本刻意不暖）。舊的理由是「開賣前碰排隊系統
    不像正常使用者」，但實測推翻了那個顧慮：未開賣時打 enqueue 拿到的是官方定義好的
    `errCode 137 + waitSecond 10`，跟開賣後排隊**完全同一個回應** —— 對方系統本來就
    預期有人會提早來排，這是正常行為。

    **但省下的比想像中少，別高估**。2026-08-06 從台灣量到 175ms；搬到東京後實測
    冷連線只剩 **10ms**、熱連線 5ms —— 也就是**暖機只省約 5ms**。

    先前看到「第 1 發 enqueue 110ms、第 2 發起 11ms」，我一度以為那 99ms 是握手，
    **是錯的**：同一個網域單純 GET 的冷連線只要 10ms。那 99ms 是**伺服器端處理
    第一次 enqueue** 的成本（建立排隊項目），暖機省不掉，只能認了。

    那還做嗎？做，因為零成本零風險。用一發**單純的 GET**（不是 enqueue）把 TCP+TLS
    建起來 —— 只要同一個 host 連線池就會重用，不必真的去排隊，footprint 最小。

    最後 stop_before 秒停手，不讓網路 IO 影響倒數精度。"""
    url = f"{CONFIG_API}/get?productId={','.join(product_ids)}&"
    headers = build_headers()
    state = {"last": 0.0, "queue_warmed": False}

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

        # queue 網域暖一次就夠 —— 之後每輪的票況 ping 會讓整個連線池保持活著。
        # 放在票況 ping 之後，確保偵測那條連線優先。
        if not state["queue_warmed"]:
            state["queue_warmed"] = True
            warmup_queue(session)

    return _tick


def warmup_queue(session: cf_requests.Session):
    """把 queue 網域的 TCP+TLS 先建起來（實測省約 5ms：冷 10ms → 熱 5ms）。

    刻意打**根路徑的 GET**而不是真的 enqueue：連線池是 per-host 的，握完手就達到目的，
    不必在對方的排隊系統多留一次抽籤紀錄。回 404 是正常的（那個路徑本來就沒有 handler）。
    """
    t0 = time.monotonic()
    try:
        res = session.get(f"{QUEUE_API}/", headers=build_headers(), timeout=5)
        print(f"[WARMUP] 排隊網域已暖機 HTTP {res.status_code} "
              f"（{(time.monotonic() - t0) * 1000:.0f}ms）")
    except Exception as e:
        print(f"[WARMUP] 排隊網域暖機失敗（不致命）: {type(e).__name__}")
