"""TicketPlus 瀏覽器層（nodriver）：只做兩件事 —— 登入拿 token、搶到票後跳結帳頁。

跟 KKTIX / 寬宏不同，這裡**不用瀏覽器打 API**：TicketPlus 的登入態是 JS 寫的
`user` cookie（非 httpOnly），`document.cookie` 就讀得到，掏出 access_token 之後整條
搶票鏈路都走 curl_cffi 純封包，比 CDP evaluate 少一層開銷。

TicketPlus 沒有 /login 路由（登入是全站共用的 dialog），所以直接開活動頁等使用者按
右上角登入；判斷登入完成的依據是 `user` cookie 出現，比 DOM 特徵可靠。
"""
import asyncio
import json
import time
from urllib.parse import quote, unquote

import nodriver as uc

import browser_login as _tx   # 只借用 setup_proxy_bridge（跟站台無關）
import config
from ticketplus_api import BASE
from ticketplus_api.session import (build_session, describe_token, extract_refresh_token,
                                    extract_token, refresh_access_token, token_remaining)

READ_USER_COOKIE = "document.cookie.split('; ').find(c=>c.startsWith('user='))||''"

# token 至少要還有這麼久才收：撐得過「建 plan + 倒數 + 送單」。
# TicketPlus 的 token 只活 60 分鐘，profile 裡上次登入留下的那顆通常早就死了。
MIN_TOKEN_LIFE = 120.0


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


async def read_user_cookie(tab) -> str:
    """讀原始的 `user=` cookie 字串（access_token 和 refresh_token 都在裡面）。"""
    try:
        raw = await tab.evaluate(READ_USER_COOKIE, return_by_value=True)
    except Exception:
        return ""
    # nodriver: falsy 值可能回原始 RemoteObject，所以先確定是 str 再用
    return raw if isinstance(raw, str) else ""


async def read_token(tab) -> str:
    """從頁面的 `user` cookie 掏 access_token，沒登入回空字串。"""
    return extract_token(await read_user_cookie(tab))


async def write_user_cookie(tab, access_token: str, refresh_token: str) -> bool:
    """把換發到的新 token 寫回瀏覽器的 `user` cookie。

    **為什麼一定要寫回**：refresh_token 是輪替的（官方前端的 REFRESH_TOKEN mutation 會把
    它一起換掉）。我們在外面換一次，瀏覽器手上那顆就作廢了 —— 搶到票跳結帳頁時 SPA 會
    拿舊的去續命、失敗、直接登出，客人就付不了款。

    走 CDP 而不是 `document.cookie`：JS 讀不到 cookie 的 domain / path / expires，自己
    寫一顆很容易變成「同名但 domain 不同」的第二顆蓋在上面。CDP 可以先把原本那顆完整
    讀出來，照原樣的屬性寫回去。
    """
    from nodriver import cdp
    try:
        cookies = await tab.send(cdp.network.get_cookies([BASE]))
        target = next((c for c in cookies if c.name == "user"), None)
        if target is None:
            print("[TOKEN] 瀏覽器裡找不到 user cookie，跳過寫回")
            return False
        data = json.loads(unquote(target.value))
        data["access_token"] = access_token
        if refresh_token:
            data["refresh_token"] = refresh_token
        await tab.send(cdp.network.set_cookie(
            name="user",
            value=quote(json.dumps(data, separators=(",", ":")), safe=""),
            domain=target.domain, path=target.path,
            secure=target.secure, http_only=target.http_only,
        ))
        return True
    except Exception as e:
        print(f"[TOKEN] 寫回瀏覽器 cookie 失敗（不影響送單，但結帳頁可能要重新登入）: "
              f"{type(e).__name__}: {e}")
        return False


async def refresh_via_cookie(tab, raw_cookie: str) -> str:
    """用 cookie 裡的 refresh_token 換一張新的 access_token，並寫回瀏覽器。

    回新的 access_token；沒有 refresh_token 或連它也過期了就回空字串（那才真的要重登）。
    """
    refresh_token = extract_refresh_token(raw_cookie)
    if not refresh_token:
        return ""
    print("[TOKEN] access_token 已過期，改用 refresh_token 自動換發…")
    access, new_refresh = refresh_access_token(build_session(), refresh_token)
    if not access:
        return ""
    print(f"[TOKEN] 換發成功（{describe_token(access)}）")
    if tab is not None:
        await write_user_cookie(tab, access, new_refresh or refresh_token)
    return access


async def open_and_login(user_data_dir: str, event_id: str,
                         proxy_url: str = "", timeout: int = 600):
    """開 nodriver（用 profile）→ 活動頁 → 等使用者登入。回 (browser, tab, token)。"""
    browser = await uc.start(headless=False, user_data_dir=user_data_dir or None,
                             browser_args=_browser_args(proxy_url))
    print(f"[LOGIN] nodriver Chrome 已開: {user_data_dir or '(臨時 profile)'}")
    tab = await browser.get(activity_url(event_id))
    print(f"[LOGIN] 請在視窗右上角完成登入（最多 {timeout}s）...")

    deadline = time.monotonic() + timeout
    warned_stale = False
    tried_refresh = False
    while time.monotonic() < deadline:
        raw = await read_user_cookie(tab)
        token = extract_token(raw)
        if token:
            remaining = token_remaining(token)
            # 解不開（remaining is None）就照收 —— 可能是官方換了 token 格式，
            # 不該因為我們看不懂就把人擋在門外。
            if remaining is None or remaining >= MIN_TOKEN_LIFE:
                print(f"[LOGIN] 登入完成，已取得 access_token"
                      f"（{len(token)} 字，{describe_token(token)}）")
                return browser, tab, token
            # 過期了先別急著叫人重登 —— cookie 裡通常還有一顆 refresh_token，
            # 官方前端自己就是拿它自動續命的。換得到就完全不用麻煩使用者。
            if not tried_refresh:
                tried_refresh = True
                fresh = await refresh_via_cookie(tab, raw)
                if fresh:
                    return browser, tab, fresh
            if not warned_stale:
                warned_stale = True
                # 這裡最容易誤會：畫面上通常還顯示登入中的狀態（前端不會自己去驗 exp），
                # 所以一定要講明「先登出再登入」，不然使用者會以為已經登入而乾等。
                print(f"[LOGIN] ⚠ profile 裡的 token {describe_token(token)}，不能用。")
                print(f"[LOGIN]   TicketPlus 的 token 只活 60 分鐘，這是上次登入留下的。")
                print(f"[LOGIN]   請在視窗右上角**先登出、再登入一次**（畫面可能仍顯示已登入，那是假的）")
        await asyncio.sleep(1.0)

    print(f"[LOGIN] 超時 {timeout}s 未取得可用的 token")
    return browser, tab, ""


async def wait_checkout_ready(tab, timeout: float = 6.0):
    """等結帳確認頁把「伺服器端訂單」render 出來再回來（給截圖用）。

    /confirmSeat（或 /confirm）是 Vue SPA，init() 要先打 getUserCurrentReservedOrder 從
    伺服器重建訂單才會顯示座位/明細，太早截會是 loading 或空白。判斷：路由還停在 confirm
    （沒被 redirect 回 order）且 body 文字量足夠並連續兩次穩定。逾時就直接回（截圖照拍，
    不能因為等不到就不推）。"""
    deadline = time.monotonic() + timeout
    last_len = -1
    while time.monotonic() < deadline:
        try:
            href = await tab.evaluate("location.pathname", return_by_value=True)
            tlen = await tab.evaluate(
                "document.body ? document.body.innerText.length : 0", return_by_value=True)
        except Exception:
            await asyncio.sleep(0.3)
            continue
        tlen = tlen if isinstance(tlen, int) else 0
        on_confirm = isinstance(href, str) and "confirm" in href
        if on_confirm and tlen > 300 and tlen == last_len:
            return   # 內容穩定 → render 完
        last_len = tlen
        await asyncio.sleep(0.4)
    print("[FINALIZE] 等結帳頁 render 逾時，仍先截圖")


async def goto_checkout(tab, event_id: str, session_id: str, seat_assignment: bool):
    """把已登入的視窗導去結帳確認頁讓人（或客人）接手付款。"""
    url = checkout_url(event_id, session_id, seat_assignment)
    print(f"[FINALIZE] 導向結帳頁: {url}")
    try:
        await tab.get(url)
    except Exception as e:
        print(f"[FINALIZE] 導向失敗（票還在，手動開即可）: {e!r}")
    return url
