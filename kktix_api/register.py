"""Step 1（純封包版）：偵測 KKTIX 開賣 + 依 config 選票。

KKTIX 報名頁票種是 Angular 前端渲染，raw HTML / 高頻 fetch 頁面都拿不到票種 —— 所以改走 KKTIX
自己的 JSON API（跟 Angular 前端打的同一組），完全不需渲染：
  - base_info     /g/events/{slug}/base_info     靜態票種目錄（票名/票價/id/張數限制），開場前抓一次
  - register_info /g/events/{slug}/register_info  動態票況（register_status + 每個 id 的 in_stock），高頻輪詢
  - csrf 從 registrations/new raw HTML 的 <meta name="csrf-token"> 抓（下單 join_queue 要用）
全部透過 browser_session.page_fetch 在已登入頁面同源打，自動帶 cf_clearance + 登入 session，不會 403。

⚠️ slug 必須是「場次」slug（如 cb2818b8），不是「活動」slug（如 y9abe2f0，那頁是選場次頁）。
"""
import time
import asyncio
from dataclasses import dataclass

import config
from kktix_api import BASE, parsing
from kktix_api.session import registration_url
from kktix_api.browser_session import page_fetch


@dataclass
class OpenResult:
    ticket: dict
    html: str
    csrf_token: str
    url: str


async def _get_json(tab, url: str) -> str | None:
    res = await page_fetch(tab, url, headers={"Accept": "application/json, text/plain, */*"})
    if not res.get("ok") or res.get("status") != 200:
        return None
    return res.get("text", "")


async def fetch_catalog_and_csrf(tab, slug: str) -> tuple[list[dict], str]:
    """預抓 base_info 票種目錄 + registrations/new csrf token（兩者皆靜態）。

    ★ 在倒數（TimeWatcher）之前呼叫，T-0 一到 poll_until_open 只剩最快的 register_info 輪詢，
      不必在開賣瞬間才抓這兩支慢的（省開賣當下 ~340ms）。"""
    catalog = []
    bi = await _get_json(tab, f"{BASE}/g/events/{slug}/base_info")
    if bi:
        catalog = parsing.parse_base_info(bi)

    csrf = ""
    reg = await page_fetch(tab, registration_url(slug))
    if reg.get("ok"):
        csrf = parsing.extract_csrf_token(reg.get("text", ""))

    if catalog:
        print("[REGISTER] 票種目錄: "
              + ", ".join(f"{c['ticket_id']}={c['name']}({c['price']})" for c in catalog))
    else:
        print("[REGISTER] ⚠️ 抓不到 base_info 票種目錄（確認 slug 是『場次』slug，非活動 slug）")
    if not csrf:
        print("[REGISTER] ⚠️ 沒抓到 csrf token —— 下單會失敗，檢查登入 / slug")
    return catalog, csrf


async def keep_alive_refresh(tab, slug: str, holder: dict, interval: float = 120.0):
    """倒數期間背景跑：定期重抓 registrations/new → 更新 holder['csrf'] + 維持 session 不過期。

    csrf 是綁 session 的，太早啟動、等很久後原本那顆可能隨 session 失效；這裡定期換新，
    順便當 keep-alive（GET 一下讓 KKTIX session 保持活著）。被 cancel（T-0 到）時安靜結束。"""
    try:
        while True:
            await asyncio.sleep(interval)
            reg = await page_fetch(tab, registration_url(slug))
            if reg.get("ok"):
                token = parsing.extract_csrf_token(reg.get("text", ""))
                if token:
                    holder["csrf"] = token
                    print("[KEEPALIVE] csrf 已更新 + session 保活")
                else:
                    print("[KEEPALIVE] ⚠️ 重抓未拿到 csrf（session 可能已失效，檢查登入）")
            else:
                print("[KEEPALIVE] ⚠️ keep-alive 請求失敗")
    except asyncio.CancelledError:
        pass


async def fetch_register_info(tab, slug: str) -> dict | None:
    """抓 + 解析 register_info，回 parsing.parse_register_info 的 dict，失敗回 None。"""
    body = await _get_json(tab, f"{BASE}/g/events/{slug}/register_info")
    return parsing.parse_register_info(body) if body is not None else None


def _select(catalog: list[dict], reg_info: dict) -> dict | None:
    units = parsing.merge_availability(catalog, reg_info)
    return parsing.select_ticket(
        units,
        keyword=config.AREA_KEYWORD,
        exclude=config.EXCLUDE_AREA_KEYWORD,
        mode=config.AREA_AUTO_SELECT_MODE,
        amount=int(config.TICKET_AMOUNT or 1),
    )


async def poll_until_open(tab, slug: str, catalog: list[dict], csrf: str,
                          max_duration: float = 60.0, interval: float = 0.1) -> OpenResult | None:
    """高頻輪詢 register_info 偵測開賣，一有符合 config 條件的票立刻回傳 OpenResult。
    catalog / csrf 由 fetch_catalog_and_csrf 事先（倒數前）抓好傳入，這裡只跑最快的 register_info。
    interval 預設 0.1s（實際頻率被 RTT ~130ms 綁住，等效約每 0.24s 一次）。"""
    deadline = time.monotonic() + max_duration
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        info = await fetch_register_info(tab, slug)
        verbose = attempt <= 3 or attempt % 20 == 0
        if info is None:
            if verbose:
                print("[REGISTER] register_info 讀取失敗")
        elif not info["open"]:
            if verbose:
                print(f"[REGISTER] 尚未開賣（status={info['register_status'] or '?'}）")
        else:
            ticket = _select(catalog, info)
            if ticket:
                print(f"[POLL] #{attempt} 開賣！選中 id={ticket['ticket_id']} "
                      f"{ticket.get('name', '')} {ticket.get('price', '')} x{ticket['amount']}")
                return OpenResult(ticket=ticket, html="", csrf_token=csrf,
                                  url=registration_url(slug))
            if verbose:
                print("[REGISTER] 已開賣但無符合 config 條件的票（目標票區可能售完）")
        await asyncio.sleep(interval)

    print(f"[POLL] 超時 {max_duration}s（{attempt} 次）未搶到符合條件的票")
    return None
