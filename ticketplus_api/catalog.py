"""活動目錄 + 即時票況（CONFIG_API，唯讀且**不需要登入**）。

兩層資料，用途完全不同：
  1. static S3 目錄（`getS3?path=event/<加密eventId>/*.json`）—— 場次 / 票區 / 票種的
     「靜態結構」，開賣前就抓好，跑一次就夠：sessions / ticketAreas / products
  2. `/get?productId=..&ticketAreaId=..` —— 「即時票況」：status(onsale/soldout/…)、
     count(剩餘)、purchaseLimit、saleStart。**開賣偵測就是高頻打這一支**（實測 ~160ms）

本檔只負責拿資料，挑哪一個交給 [parsing.py](ticketplus_api/parsing.py)。
"""
import time

from curl_cffi import requests as cf_requests

from ticketplus_api import CONFIG_API
from ticketplus_api.session import build_headers

_S3 = f"{CONFIG_API}/getS3"


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


def get_infos(session: cf_requests.Session, product_ids: list[str] | None = None,
              ticket_area_ids: list[str] | None = None,
              timeout: float = 10.0, log: bool = True) -> tuple[dict, bool]:
    """即時票況。id 一律用**明文**（p000…/a000…）。回 (result, blocked)。

    前端就是把 id 用逗號串起來當 query string，一次可以查整場所有票種 —— 所以偵測開賣
    只要一發請求，不用每個票區各打一次。

    blocked=True 代表被流量管制/擋（HTTP 429/403 或 errCode 110 流量管制）→ 呼叫端該退避，
    對齊拓元撞 403 的 BLOCKED cooldown（被擋還用 0.3s 狂打只會更慘）。
    """
    params = []
    if product_ids:
        params.append("productId=" + ",".join(product_ids))
    if ticket_area_ids:
        params.append("ticketAreaId=" + ",".join(ticket_area_ids))
    url = f"{CONFIG_API}/get?{'&'.join(params)}&"

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
