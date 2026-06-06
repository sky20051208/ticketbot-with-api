"""Step 2：下單 — 加入 KKTIX queue 候位（透過瀏覽器 fetch）。

✅ join_queue 已用真實抓包驗證：
   POST https://queue.kktix.com/queue/{slug}?authenticity_token={URL-encoded CSRF}
   Content-Type: text/plain（body 是 JSON）
   body: {"tickets":[{"id":<int>,"quantity":<n>,"invitationCodes":[...],
                      "use_qualification_id":null}],
          "currency":"TWD","recaptcha":{},"agreeTerm":true}
   回 200: {"token":"<JWT>"}
   走瀏覽器 fetch：queue.kktix.com 是 kktix.com 的跨子網域，但 KKTIX 有開 CORS +
   credentials，fetch 會自動帶上 cf_clearance + 登入 cookie。

⚠️ redeem_queue（token → order 頁）後續封包還沒驗證，先 best-effort；拿不到就回 None，
   由 __main__ 把瀏覽器導到報名頁讓使用者完成（此時已在 queue 內）。
"""
import json
import urllib.parse

from kktix_api import BASE
from kktix_api.register import OpenResult
from kktix_api.browser_session import page_fetch
import config

QUEUE_BASE = "https://queue.kktix.com"


def _build_body(result: OpenResult) -> dict:
    codes = [config.PRESALE_CODE.strip()] if (config.PRESALE_CODE or "").strip() else []
    return {
        "tickets": [{
            "id": int(result.ticket["ticket_id"]),
            "quantity": result.ticket["amount"],
            "invitationCodes": codes,
            "use_qualification_id": None,
        }],
        "currency": "TWD",
        "recaptcha": {},
        "agreeTerm": True,
    }


async def join_queue(tab, slug: str, result: OpenResult) -> str | None:
    """加入候位佇列，回 token（JWT）。已用真實抓包驗證。"""
    if not result.csrf_token:
        print("[RESERVE] 沒抓到 CSRF token，無法送候位")
        return None

    token_q = urllib.parse.quote(result.csrf_token, safe="")
    url = f"{QUEUE_BASE}/queue/{slug}?authenticity_token={token_q}"
    body = json.dumps(_build_body(result))
    print(f"[RESERVE] 候位 body: {body}")
    res = await page_fetch(tab, url, method="POST", body=body,
                           headers={"Content-Type": "text/plain",
                                    "Accept": "application/json, text/plain, */*"})
    if not res.get("ok"):
        print(f"[RESERVE] 候位 fetch 失敗: {res.get('error')}")
        return None
    if res.get("status") != 200:
        print(f"[RESERVE] 候位 HTTP {res.get('status')}: {(res.get('text') or '')[:300]}")
        return None
    try:
        token = json.loads(res["text"]).get("token")
    except Exception:
        token = None
    if token:
        print(f"[RESERVE] ✅ 已進入候位佇列，token: {token[:32]}...")
        return token
    print(f"[RESERVE] ❌ 候位回應無 token: {(res.get('text') or '')[:300]}")
    return None


async def redeem_queue(tab, slug: str, token: str) -> str | None:
    """⚠️ best-effort：拿候位 token 試輪詢取 order 頁。後續封包未驗證，先試 + 印回應。"""
    import re
    candidates = [
        f"{QUEUE_BASE}/queue/{slug}/status?token={urllib.parse.quote(token, safe='')}",
        f"{QUEUE_BASE}/queue/{slug}?token={urllib.parse.quote(token, safe='')}",
    ]
    order_re = re.compile(r"/events/[^/]+/registrations/(\d+)")
    for url in candidates:
        res = await page_fetch(tab, url, headers={"Accept": "application/json, text/plain, */*"})
        if not res.get("ok"):
            continue
        text = res.get("text") or ""
        m = order_re.search(text)
        if m:
            order_url = f"{BASE}/events/{slug}/registrations/{m.group(1)}"
            print(f"[RESERVE] ✅ 取得 order 頁: {order_url}")
            return order_url
        print(f"[RESERVE] redeem {url} → HTTP {res.get('status')}: {text[:200]}")
    return None


async def reserve_ticket(tab, slug: str, result: OpenResult) -> str | None:
    """完整下單：加入候位 → 試取 order 頁。回 order URL 或 None（fallback 瀏覽器交棒）。"""
    token = await join_queue(tab, slug, result)
    if not token:
        return None
    return await redeem_queue(tab, slug, token)
