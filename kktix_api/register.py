"""Step 1：抓 registrations/new（透過瀏覽器 fetch）、偵測開賣、依 config 選票。

改走瀏覽器 fetch（page_fetch）而非 curl_cffi：瀏覽器自動帶 cf_clearance + 登入 session，
不會 403、不用解 cookie。registrations/new 跟瀏覽器同源，fetch 最快。
"""
import time
import asyncio
from dataclasses import dataclass

import config
from kktix_api import parsing
from kktix_api.session import registration_url
from kktix_api.browser_session import page_fetch


@dataclass
class OpenResult:
    ticket: dict
    html: str
    csrf_token: str
    url: str


def _select_from_html(html: str) -> dict | None:
    units = parsing.parse_registration_ticket_units(html)
    return parsing.select_ticket(
        units,
        keyword=config.AREA_KEYWORD,
        exclude=config.EXCLUDE_AREA_KEYWORD,
        mode=config.AREA_AUTO_SELECT_MODE,
        amount=int(config.TICKET_AMOUNT or 1),
    )


async def fetch_once(tab, slug: str, verbose: bool = True) -> OpenResult | None:
    url = registration_url(slug)
    res = await page_fetch(tab, url)
    if not res.get("ok"):
        if verbose:
            print(f"[REGISTER] fetch 失敗: {res.get('error')}")
        return None
    if res.get("status") != 200:
        if verbose:
            print(f"[REGISTER] HTTP {res.get('status')}")
        return None

    html = res.get("text", "")
    if parsing.detect_challenge(html):
        if verbose:
            print("[REGISTER] 撞到安全驗證頁（理論上 fetch 不該遇到，檢查登入）")
        return None
    if not parsing.is_registration_open(html):
        if verbose:
            print("[REGISTER] 尚未開賣（無可選票種）")
        return None

    ticket = _select_from_html(html)
    if not ticket:
        if verbose:
            print("[REGISTER] 已開賣但沒有符合 config 條件的票")
        return None

    print(f"[REGISTER] 選中票種: id={ticket['ticket_id']} "
          f"{ticket.get('name','')} {ticket.get('price','')} x{ticket['amount']}")
    return OpenResult(ticket=ticket, html=html,
                      csrf_token=parsing.extract_csrf_token(html), url=url)


async def _diagnose(tab, slug: str):
    """一次性診斷：確認票況資料在 fetch 到的 HTML 裡，還是只在 register_info JSON / 渲染後 DOM。"""
    from kktix_api import BASE
    print("[DIAG] === 票況來源診斷（只跑一次）===")
    # 1) fetch registrations/new raw HTML
    res = await page_fetch(tab, registration_url(slug))
    html = res.get("text", "") if res.get("ok") else ""
    ticket_marker = 'id="ticket_'
    n_tickets = html.count(ticket_marker)
    has_app = "registrationsNewApp" in html
    print(f"[DIAG] registrations/new fetch ok={res.get('ok')} status={res.get('status')} "
          f"len={len(html)} 有registrationsNewApp={has_app} ticket_數={n_tickets}")
    # 2) register_info JSON（Angular 自己打的票況 API）
    ri = await page_fetch(tab, f"{BASE}/g/events/{slug}/register_info",
                          headers={"Accept": "application/json, text/plain, */*"})
    if ri.get("ok"):
        body = ri.get("text", "")
        print(f"[DIAG] register_info status={ri.get('status')} len={len(body)} 前600字:\n{body[:600]}")
    else:
        print(f"[DIAG] register_info 失敗: {ri.get('error')}")
    # 3) 渲染後 DOM 的 ticket 數（對照 raw HTML）
    try:
        dom_tickets = await tab.evaluate(
            "document.querySelectorAll('[id^=\"ticket_\"]').length", return_by_value=True)
        print(f"[DIAG] 目前分頁渲染後 DOM 的 ticket 元素數: {dom_tickets}")
    except Exception as e:
        print(f"[DIAG] 讀 DOM ticket 數失敗: {e!r}")
    print("[DIAG] === 診斷結束 ===")


async def poll_until_open(tab, slug: str,
                          max_duration: float = 60.0, interval: float = 0.2) -> OpenResult | None:
    """高頻 fetch 報名頁，抓到可選票立刻回傳。"""
    await _diagnose(tab, slug)
    deadline = time.monotonic() + max_duration
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        # 只在前幾次印詳細狀態，避免洗版
        result = await fetch_once(tab, slug, verbose=(attempt <= 3 or attempt % 20 == 0))
        if result:
            print(f"[POLL] #{attempt} 偵測到開賣 + 選到票")
            return result
        await asyncio.sleep(interval)
    print(f"[POLL] 超時 {max_duration}s（{attempt} 次）未抓到可選票")
    return None
