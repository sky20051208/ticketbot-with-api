"""Step 2：下單 — 完整候位 → redeem → confirm_booking（透過瀏覽器 fetch，全部真實抓包驗證）。

真實流程（2026-07 攔封包驗證）：
  1) join_queue     POST queue.kktix.com/queue/{slug}?authenticity_token={URL-encoded CSRF}
                    Content-Type text/plain, body:
                    {"tickets":[{"id":<int>,"quantity":<n>,"invitationCodes":[...],
                                 "use_qualification_id":null}],
                     "currency":"TWD","recaptcha":{},"agreeTerm":true}
                    → 200 {"token":"<JWT>"}
  2) redeem_queue   GET queue.kktix.com/queue/token/{token}
                    → 200 {"to_param":"<報名id，如 157460846-3724ee...>"}
  3) confirm_booking PATCH kktix.com/g/events/{slug}/registrations/{to_param}/confirm_booking
                    body {} + X-CSRF-Token header
                    → 200 {state:pending, booking_state:confirmed, ...}  ← 真的佔到位
  4) 回 order URL kktix.com/events/{slug}/registrations/{to_param}#/booking
                    由 __main__ 導瀏覽器過去，讓客人填資料 + 付款

全部走瀏覽器 fetch：queue.kktix.com 是 kktix.com 的跨子網域，但 KKTIX 有開 CORS + credentials，
fetch 自動帶 cf_clearance + 登入 cookie；confirm_booking 是同源 PATCH，帶 X-CSRF-Token。
"""
import time
import json
import asyncio
import urllib.parse

from kktix_api import BASE, parsing
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


async def redeem_queue(tab, slug: str, token: str,
                       max_wait: float = 120.0, interval: float = 0.2) -> tuple[str | None, bool]:
    """拿候位 token 輪詢兌換 report id（to_param）。回 (to_param, fatal)。

    候位 token 不是拿到就能兌換 —— 要一直 GET queue/token/{token}，還沒輪到會回
    {"result":"not_found"}，輪到才回 {"to_param":"..."}。T-0 大排隊時可能要等一陣子。

    redeem 語意（逆向 waitForRegistration + 真實 alert 字串驗證）：
      - to_param 有值            → 成功
      - message 且屬售完類        → 軟失敗（回 None, fatal=False）→ 清票迴圈繼續等回流
      - message 且非售完（資格等） → 硬失敗（回 None, fatal=True）→ 重試無用，該停
      - 逾時                      → 軟失敗（None, False）"""
    url = f"{QUEUE_BASE}/queue/token/{token}"
    deadline = time.monotonic() + max_wait
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        res = await page_fetch(tab, url, headers={"Accept": "application/json, text/plain, */*"})
        if res.get("ok") and res.get("status") == 200:
            r = parsing.parse_redeem_result(res.get("text", ""))
            if r["to_param"]:
                print(f"[RESERVE] ✅ redeem 取得報名 id: {r['to_param']}（第 {attempt} 次）")
                return r["to_param"], False
            if r["message"]:
                if parsing.redeem_message_is_soldout(r["message"]):
                    print(f"[RESERVE] 售完（繼續清票）：{r['message']}")
                    return None, False
                print(f"[RESERVE] ❌ 硬失敗（停止清票，重試無用）：{r['message']}")
                return None, True
            if attempt <= 3 or attempt % 20 == 0:
                print(f"[RESERVE] 候位中，尚未輪到...（{(res.get('text') or '')[:80]}）")
        elif attempt <= 3:
            print(f"[RESERVE] redeem HTTP {res.get('status')}，重試")
        await asyncio.sleep(interval)
    print(f"[RESERVE] redeem 超時 {max_wait}s（{attempt} 次）未輪到")
    return None, False


async def confirm_booking(tab, slug: str, to_param: str, csrf_token: str) -> bool:
    """PATCH confirm_booking 真的佔位。回 True/False（失敗不致命，仍可導頁讓客人手動確認）。"""
    url = f"{BASE}/g/events/{slug}/registrations/{to_param}/confirm_booking"
    res = await page_fetch(tab, url, method="PATCH", body="{}",
                           headers={"Content-Type": "application/json",
                                    "Accept": "application/json, text/plain, */*",
                                    "X-CSRF-Token": csrf_token})
    if not res.get("ok") or res.get("status") != 200:
        print(f"[RESERVE] confirm_booking 失敗 HTTP {res.get('status')}: {(res.get('text') or '')[:200]}")
        return False
    try:
        state = json.loads(res["text"]).get("booking_state")
    except Exception:
        state = None
    print(f"[RESERVE] ✅ confirm_booking OK（booking_state={state}）")
    return True


async def reserve_ticket(tab, slug: str, result: OpenResult) -> tuple[str | None, bool]:
    """完整下單：候位 → redeem → confirm_booking。回 (order_url, fatal)。
    fatal=True 表示硬失敗（如資格不符）重試無用，清票迴圈該停；
    fatal=False + order_url None = 軟失敗（售完 / 逾時 / 被搶走），該繼續清票。"""
    token = await join_queue(tab, slug, result)
    if not token:
        return None, False  # join_queue 失敗（售完 403 / csrf 過期）→ 軟，繼續清票
    to_param, fatal = await redeem_queue(tab, slug, token)
    if not to_param:
        return None, fatal
    await confirm_booking(tab, slug, to_param, result.csrf_token)  # 失敗不致命
    order_url = f"{BASE}/events/{slug}/registrations/{to_param}#/booking"
    print(f"[RESERVE] ✅ order 頁: {order_url}")
    return order_url, False
