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


def _fetch_base_info_curl(slug: str) -> tuple[bool, list[dict]]:
    """用 curl_cffi 直接抓 base_info（公開端點、免登入、不經瀏覽器）。回 (ok, catalog)。

    ★ 為什麼不走瀏覽器 page_fetch：base_info 是靜態公開資料，實測 curl_cffi 仿 Chrome TLS
      免 cookie 就 200。走瀏覽器 fetch 反而會踩「登入完成瞬間頁面還在跳轉、execution context
      被銷毀」→ evaluate 丟例外（RTT 印 FAILED）→ 整場 catalog 空、永遠選不到票（2026-07 實測，
      就是這個害的）。curl 直抓徹底避開這個時序坑，也不吃倒數期間的 session 狀態。
      ok=True 表示 HTTP 200（catalog 可能為 []＝該場真的沒在賣的票種，例如已售完）。"""
    import config
    import proxy_pool
    from curl_cffi import requests as cf
    url = f"{BASE}/g/events/{slug}/base_info"
    try:
        proxies = proxy_pool.as_dict(config.CURRENT_PROXY)
        res = cf.get(url, impersonate="chrome124", proxies=proxies or None, timeout=10)
        if res.status_code == 200:
            return True, parsing.parse_base_info(res.text)
        print(f"[REGISTER] base_info(curl) HTTP {res.status_code}")
    except Exception as e:
        print(f"[REGISTER] base_info(curl) 失敗: {e!r}")
    return False, []


async def fetch_catalog(tab, slug: str, retries: int = 3, delay: float = 0.3) -> list[dict]:
    """抓 base_info 票種目錄。優先 curl_cffi 直抓（公開、穩），失敗才退瀏覽器 fetch 重試。"""
    ok, cat = _fetch_base_info_curl(slug)
    if ok:
        return cat  # HTTP 200 就信任（空 = 該場真的沒在賣的票種，不用再瀏覽器重試）

    print("[REGISTER] base_info(curl) 未取得，改用瀏覽器 fetch 重試")
    for i in range(retries):
        res = await page_fetch(tab, f"{BASE}/g/events/{slug}/base_info",
                               headers={"Accept": "application/json, text/plain, */*"})
        if res.get("ok") and res.get("status") == 200:
            return parsing.parse_base_info(res.get("text", ""))
        detail = res.get("error") or f"HTTP {res.get('status')}: {(res.get('text') or '')[:120]}"
        if i < retries - 1:
            print(f"[REGISTER] base_info(browser) 第 {i+1}/{retries} 次失敗（{detail}），{delay}s 後重試")
            await asyncio.sleep(delay)
        else:
            print(f"[REGISTER] base_info 都抓不到（{detail}）→ 退 register_info fallback（buy-any）")
    return []


async def fetch_catalog_and_csrf(tab, slug: str) -> tuple[list[dict], str]:
    """預抓 base_info 票種目錄 + registrations/new csrf token（兩者皆靜態）。

    ★ 在倒數（TimeWatcher）之前呼叫，T-0 一到 poll_until_open 只剩最快的 register_info 輪詢，
      不必在開賣瞬間才抓這兩支慢的（省開賣當下 ~340ms）。"""
    catalog = await fetch_catalog(tab, slug)

    csrf = ""
    reg = await page_fetch(tab, registration_url(slug))
    if reg.get("ok"):
        csrf = parsing.extract_csrf_token(reg.get("text", ""))

    if catalog:
        print("[REGISTER] 票種目錄: "
              + ", ".join(f"{c['ticket_id']}={c['name']}({c['price']})" for c in catalog))
    else:
        print("[REGISTER] ⚠️ 暫無 base_info 票種目錄（開賣後會自動補抓；届時退 register_info fallback）")
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


async def refetch_csrf(tab, slug: str) -> str:
    """重抓 registrations/new 的 csrf token（送單失敗時 csrf 可能已過期，回頭重搶前刷一次）。"""
    reg = await page_fetch(tab, registration_url(slug))
    return parsing.extract_csrf_token(reg.get("text", "")) if reg.get("ok") else ""


async def fetch_register_info(tab, slug: str) -> tuple[dict | None, int | None]:
    """抓 + 解析 register_info，回 (info, http_status)。
    info = parse 結果（非 200 或網路失敗時 None）；status 讓 poll 端分辨 403（被擋 → 退避），
    status=None 代表網路層失敗。"""
    res = await page_fetch(tab, f"{BASE}/g/events/{slug}/register_info",
                           headers={"Accept": "application/json, text/plain, */*"})
    status = res.get("status") if res.get("ok") else None
    if res.get("ok") and status == 200:
        return parsing.parse_register_info(res.get("text", "")), 200
    return None, status


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
                          max_duration: float = 60.0, fast_interval: float = 0.1,
                          clear_cooldown: float = 5.0,
                          blocked_cooldown: float = 8.0) -> OpenResult | None:
    """輪詢 register_info 找可買票，一有符合 config 條件的票立刻回傳 OpenResult。

    冷卻節奏借鑑拓元 FSM 清票（見 tixcraftapi/runner.py）：
      - **尚未開賣**（等 T-0）→ `fast_interval`（0.1s）全速，抓開賣瞬間（golden，不冷卻）
      - **有可買票** → 立刻選中送單（golden，永遠不冷卻，這是第一拍黃金時間）
      - **開賣但沒可買票**（售完/被搶走＝清票等回流）→ `clear_cooldown`（5s，對齊拓元 AREA 重入）
      - **撞 403 / 429**（被 rate-limit / Cloudflare 擋）→ `blocked_cooldown`（8s，對齊拓元
        BLOCKED_COOLDOWN；被擋還用 0.1s 狂打只會更慘，要退避給 server/IP 喘息）
      - register_info 網路失敗 → fast_interval 重試
    「有票就搶」永遠 golden；只有「沒東西可搶」或「被擋」才冷卻——跟拓元「第一拍 0ms、
    被踢回才 5s、403 才 8s」同一套哲學。"""
    deadline = time.monotonic() + max_duration
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        info, status = await fetch_register_info(tab, slug)
        verbose = attempt <= 3 or attempt % 20 == 0

        if status in (403, 429):
            print(f"[REGISTER] register_info 撞 {status}（被擋），退避 {blocked_cooldown:.0f}s（#{attempt}）")
            wait = blocked_cooldown
        elif info is None:
            if verbose:
                print(f"[REGISTER] register_info 讀取失敗（status={status}），重試")
            wait = fast_interval
        elif not info["open"]:
            if verbose:
                print(f"[REGISTER] 尚未開賣（status={info['register_status'] or '?'}）")
            wait = fast_interval
        else:
            ticket = _select(catalog, info)
            if ticket:
                print(f"[POLL] #{attempt} 開賣！選中 id={ticket['ticket_id']} "
                      f"{ticket.get('name', '')} {ticket.get('price', '')} x{ticket['amount']}")
                return OpenResult(ticket=ticket, html="", csrf_token=csrf,
                                  url=registration_url(slug))
            # 開賣但沒可買票（售完/被搶走）= 清票等回流 → 冷卻
            wait = clear_cooldown
            if verbose:
                print(f"[REGISTER] 已開賣、目前無可買目標票（售完/被搶走），"
                      f"清票中…{clear_cooldown:.0f}s 檢查一次等回流（#{attempt}）")
        await asyncio.sleep(wait)

    # 回 None 不是「放棄」——外層 grab_loop 會立刻再叫一次，等同無限清票（見 __main__.grab_loop）
    print(f"[POLL] 本輪 {max_duration:.0f}s（{attempt} 次）無可買票，回 grab_loop 繼續清票…")
    return None
