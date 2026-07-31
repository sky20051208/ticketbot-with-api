"""TicketPlus 瀏覽器層（nodriver）：只做兩件事 —— 登入拿 token、搶到票後跳結帳頁。

跟 KKTIX / 寬宏不同，這裡**不用瀏覽器打 API**：TicketPlus 的登入態是 JS 寫的
`user` cookie（非 httpOnly），`document.cookie` 就讀得到，掏出 access_token 之後整條
搶票鏈路都走 curl_cffi 純封包，比 CDP evaluate 少一層開銷。

TicketPlus 沒有 /login 路由（登入是全站共用的 dialog），所以直接開活動頁等使用者按
右上角登入；判斷登入完成的依據是 `user` cookie 出現，比 DOM 特徵可靠。
"""
import asyncio
import time

import nodriver as uc

import browser_login as _tx   # 只借用 setup_proxy_bridge（跟站台無關）
import config
from ticketplus_api import BASE
from ticketplus_api.session import extract_token

READ_USER_COOKIE = "document.cookie.split('; ').find(c=>c.startsWith('user='))||''"


def activity_url(event_id: str) -> str:
    return f"{BASE}/activity/{event_id}"


def order_url(event_id: str, session_id: str) -> str:
    """Order1 = 選票數那頁（我們是繞過它直接送單的，只當 fallback 用）。"""
    return f"{BASE}/order/{event_id}/{session_id}"


def checkout_url(event_id: str, session_id: str, seat_assignment: bool) -> str:
    """搶到票後該落在哪一頁 —— 跟前端 `nextStep` 自己的路由判斷一致：
      有劃位（我們一律送 finalizedSeats=True 系統配位）→ ConfirmSt `/confirmSeat`
      無劃位                                          → Order2   `/confirm`

    直接開這兩頁是安全的：它們 `init()` 會先打 `getUserCurrentReservedOrder`，有保留中的
    訂單就 `setReservedData()` 純從伺服器重建狀態（不依賴搶票當下的前端狀態）；沒訂單才
    自己 `$router.push` 退回 Order1。所以「搶到 → 開確認頁」跟「沒搶到 → 退回選票頁」
    都由官方前端自己處理，我們不用判斷。

    兩個 id 都要用**加密形式**（S3 目錄給的就是加密的，直接丟）。
    """
    page = "confirmSeat" if seat_assignment else "confirm"
    return f"{BASE}/{page}/{event_id}/{session_id}"


def _browser_args(proxy_url):
    args = ["--no-first-run", "--no-default-browser-check", "--disable-notifications"]
    if config.WINDOW_W > 0 and config.WINDOW_H > 0:
        args.append(f"--window-size={config.WINDOW_W},{config.WINDOW_H}")
    if config.WINDOW_X >= 0 and config.WINDOW_Y >= 0:
        args.append(f"--window-position={config.WINDOW_X},{config.WINDOW_Y}")
    if proxy_url:
        port = _tx.setup_proxy_bridge(proxy_url)
        if port:
            args.append(f"--proxy-server=http://127.0.0.1:{port}")
            args.append("--webrtc-ip-handling-policy=disable_non_proxied_udp")
    return args


async def read_token(tab) -> str:
    """從頁面的 `user` cookie 掏 access_token，沒登入回空字串。"""
    try:
        raw = await tab.evaluate(READ_USER_COOKIE, return_by_value=True)
    except Exception:
        return ""
    # nodriver: falsy 值可能回原始 RemoteObject，所以先確定是 str 再用
    return extract_token(raw) if isinstance(raw, str) else ""


async def open_and_login(user_data_dir: str, event_id: str,
                         proxy_url: str = "", timeout: int = 600):
    """開 nodriver（用 profile）→ 活動頁 → 等使用者登入。回 (browser, tab, token)。"""
    browser = await uc.start(headless=False, user_data_dir=user_data_dir or None,
                             browser_args=_browser_args(proxy_url))
    print(f"[LOGIN] nodriver Chrome 已開: {user_data_dir or '(臨時 profile)'}")
    tab = await browser.get(activity_url(event_id))
    print(f"[LOGIN] 請在視窗右上角完成登入（最多 {timeout}s）...")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        token = await read_token(tab)
        if token:
            print(f"[LOGIN] 登入完成，已取得 access_token（{len(token)} 字）")
            return browser, tab, token
        await asyncio.sleep(1.0)

    print(f"[LOGIN] 超時 {timeout}s 未取得 token")
    return browser, tab, ""


async def goto_checkout(tab, event_id: str, session_id: str, seat_assignment: bool):
    """把已登入的視窗導去結帳確認頁讓人（或客人）接手付款。"""
    url = checkout_url(event_id, session_id, seat_assignment)
    print(f"[FINALIZE] 導向結帳頁: {url}")
    try:
        await tab.get(url)
    except Exception as e:
        print(f"[FINALIZE] 導向失敗（票還在，手動開即可）: {e!r}")
    return url
