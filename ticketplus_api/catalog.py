"""活動目錄 + 即時票況（CONFIG_API，唯讀且**不需要登入**）。

兩層資料，用途完全不同：
  1. static S3 目錄（`getS3?path=event/<加密eventId>/*.json`）—— 場次 / 票區 / 票種的
     「靜態結構」，開賣前就抓好，跑一次就夠：sessions / ticketAreas / products
  2. `/get?productId=..&ticketAreaId=..` —— 「即時票況」：status(onsale/soldout/…)、
     count(剩餘)、purchaseLimit、saleStart。**開賣偵測就是高頻打這一支**（實測 ~160ms）

本檔只負責拿資料，挑哪一個交給 [parsing.py](ticketplus_api/parsing.py)。
"""
import math
import random
import time

from curl_cffi import requests as cf_requests

from ticketplus_api import CONFIG_API
from ticketplus_api.session import build_headers

_S3 = f"{CONFIG_API}/getS3"

# 變體數低於這個就沒有繞過快取的意義：TTL 約 1.5s、輪詢 0.3s 代表一個 TTL 內會打 5 發，
# 變體太少就會在快取還沒過期時就轉回同一個 key。留 4 倍餘裕。
_MIN_VARIANTS = 20
_warned_variants = False


def _get_json(session: cf_requests.Session, url: str, timeout: float = 10.0) -> dict:
    res = session.get(url, headers=build_headers(), timeout=timeout)
    if res.status_code != 200:
        print(f"[API] HTTP {res.status_code} {url[:80]}")
        return {}
    try:
        return res.json()
    except Exception:
        print(f"[API] 回傳非 JSON: {res.text[:120]}")
        return {}


def get_session_info(session: cf_requests.Session, session_id: str) -> dict:
    """查單一場次的即時設定。session_id 要**明文**（s000002171），加密的先用
    `crypto.decrypt_id()` 轉。

    開賣前檢查用（不在熱路徑上，所以不做 `_fresh_params` 那套繞快取）。要看的是
    `transactionValidType` —— 它非空就代表這個場次開了「專屬代碼」驗證，而且**它比票種的
    `serialKey` 更早出現**：2026-08-08 看 NCT WISH（s000002171）時場次層已經是
    `sk00000466`，71 個票種的 serialKey 卻還全是 null（主辦設定還沒補完）。
    只看 serialKey 會漏掉這種還沒設定完的場次。
    """
    data = _get_json(session, f"{CONFIG_API}/get?sessionId={session_id}&")
    if data.get("errCode") not in ("00", None):
        print(f"[INFO] 場次設定查詢 errCode={data.get('errCode')}")
        return {}
    sessions = (data.get("result") or {}).get("session") or []
    return sessions[0] if sessions else {}


def fetch_catalog(session: cf_requests.Session, event_id: str) -> dict:
    """抓活動的靜態目錄。event_id 用**網址上那串加密 id**。
    回 {"sessions": [...], "ticketAreas": [...], "products": [...]}，抓不到的鍵給空 list。"""
    catalog = {}
    for key, filename in (("sessions", "sessions.json"),
                          ("ticketAreas", "ticketAreas.json"),
                          ("products", "products.json")):
        data = _get_json(session, f"{_S3}?path=event/{event_id}/{filename}")
        if data.get("errCode"):
            print(f"[CATALOG] {filename} 取得失敗: {data.get('errCode')} {data.get('errDetail', '')}")
        catalog[key] = data.get(key) or []
    print(f"[CATALOG] 場次 {len(catalog['sessions'])} / 票區 {len(catalog['ticketAreas'])} "
          f"/ 票種 {len(catalog['products'])}")
    return catalog


def _fresh_params(product_ids: list[str] | None,
                  ticket_area_ids: list[str] | None) -> list[str]:
    """組 `/get` 的 query string，並且**每次都換一個 CloudFront cache key**。

    這支 API 被 CloudFront 快取，TTL 約 1.5 秒（實測 `X-Cache` 由 Hit 轉 Miss 的週期）。
    照原樣打的話 3ms 就回來，但拿到的是最多 1.5 秒前的舊票況 —— 偵測延遲直接被綁死在
    ~900ms，**而且再怎麼縮短輪詢間隔都沒用，只是重複讀同一份快取**。

    繞法是把 id 的順序打亂：cache key 是照參數字串算的，`p1,p2` 和 `p2,p1` 是兩個不同
    的 key，但回應內容完全一樣。實測連打 8 發全是 Miss，每發都真的回東京 origin。
    安全性也確認過：`parsing.index_infos()` 是用 `id` 建字典的（`{p["id"]: p}`），
    完全不看順序，所以打亂請求順序不影響任何下游判斷。

    試過但不能用的兩招：
      - 加 `&_=<亂數>`：cache key 只認白名單參數（productId / ticketAreaId），未知的
        直接忽略 → 還是 Hit
      - 重複同一個 id 當變體：行為不一致，重複 2 次回 errCode 102、3 次才又正常，
        不能拿來當機制

    代價是每一發都打到 origin 而不是吃 CDN。單一 IP 實測到 12 req/s 都沒被節流
    （p50 平平的 170ms 沒劣化），但**多開時總速率是 N 倍**，間隔要自己乘回去。

    票種和票區都只有 1~2 個的活動排列數不夠，這時就退回原本的行為 —— 不會更糟，
    只是沒有變好。
    """
    global _warned_variants
    products = list(product_ids or [])
    areas = list(ticket_area_ids or [])

    variants = math.factorial(len(products)) * math.factorial(len(areas))
    if variants < _MIN_VARIANTS and not _warned_variants:
        _warned_variants = True
        print(f"[INFO] 這場只有 {len(products)} 票種 / {len(areas)} 票區，"
              f"排列組合只有 {variants} 種，繞不過 CDN 快取（偵測會慢約 1 秒）")

    params = []
    if products:
        params.append("productId=" + ",".join(random.sample(products, len(products))))
    if areas:
        params.append("ticketAreaId=" + ",".join(random.sample(areas, len(areas))))
    return params


def get_infos(session: cf_requests.Session, product_ids: list[str] | None = None,
              ticket_area_ids: list[str] | None = None,
              timeout: float = 10.0, log: bool = True) -> tuple[dict, bool]:
    """即時票況。id 一律用**明文**（p000…/a000…）。回 (result, blocked)。

    前端就是把 id 用逗號串起來當 query string，一次可以查整場所有票種 —— 所以偵測開賣
    只要一發請求，不用每個票區各打一次。

    blocked=True 代表被流量管制/擋（HTTP 429/403 或 errCode 110 流量管制）→ 呼叫端該退避，
    對齊拓元撞 403 的 BLOCKED cooldown（被擋還用 0.3s 狂打只會更慘）。
    """
    url = f"{CONFIG_API}/get?{'&'.join(_fresh_params(product_ids, ticket_area_ids))}&"

    t0 = time.perf_counter()
    try:
        res = session.get(url, headers=build_headers(), timeout=timeout)
    except Exception as e:
        print(f"[INFO] 票況請求失敗: {type(e).__name__}")
        return {}, False
    rtt = (time.perf_counter() - t0) * 1000
    if res.status_code in (403, 429):
        print(f"[INFO] HTTP {res.status_code}（流量管制/被擋）")
        return {}, True
    if res.status_code != 200:
        print(f"[INFO] HTTP {res.status_code} {url[:80]}")
        return {}, False
    try:
        data = res.json()
    except Exception:
        print(f"[INFO] 回傳非 JSON: {res.text[:120]}")
        return {}, False
    code = data.get("errCode")
    if code not in ("00", None):
        blocked = str(code) == "110"      # 110 = 流量管制
        print(f"[INFO] errCode={code} {data.get('errMsg', '')}")
        return {}, blocked
    if log:
        print(f"[INFO] 票況 RTT {rtt:.0f}ms")
    return data.get("result") or {}, False
