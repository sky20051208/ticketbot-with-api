"""KKTIX 瀏覽器層（nodriver）：登入 + 在頁面內用 fetch 打 KKTIX。

為什麼用「瀏覽器 fetch」而不是把 cookie 掏出來餵 curl_cffi：
  這台機器上把 httpOnly cookie 掏出來行不通 —— CDP getCookies/getAllCookies 卡死，
  Network 事件不派發到 handler，Chrome 148 的 cookie 檔又有 App-Bound 加密、讀檔也解不開。
  唯一實測穩定的 CDP 路徑是 Runtime（tab.evaluate）。所以改成：保留登入好的瀏覽器，
  用 tab.evaluate(fetch(...)) 在頁面同源環境打 KKTIX —— 瀏覽器自動帶上 cf_clearance +
  登入 session（credentials:include），不用解 cookie、不會 403、CSRF 也天然帶過。

對外提供：
  open_and_login(...) -> (browser, tab)   開 nodriver、等登入、回瀏覽器與分頁（全程保持開啟）
  page_fetch(tab, url, ...) -> dict        在頁面內 fetch，回 {ok,status,url,text} 或 {ok:False,error}
"""
import json
import time
import asyncio

import nodriver as uc

import browser_login as _tx  # 只借用 setup_proxy_bridge（domain 無關）
import config
from kktix_api import BASE

SIGN_IN_URL = f"{BASE}/users/sign_in"


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


async def _logged_in(tab) -> bool:
    try:
        return bool(await tab.evaluate(
            "!!(document.querySelector('a[href*=\"sign_out\"]')"
            "||document.querySelector('a[href*=\"/dashboard\"]')"
            "||document.querySelector('.navbar .dropdown-toggle'))",
            return_by_value=True))
    except Exception:
        return False


async def open_and_login(user_data_dir: str, proxy_url: str = "", timeout: int = 600):
    """開 nodriver（用 profile）、等使用者過人機驗證 + 登入。回 (browser, tab, ok)。
    瀏覽器全程保持開啟（後面 fetch / 結帳都靠它）。"""
    browser = await uc.start(headless=False, user_data_dir=user_data_dir or None,
                             browser_args=_browser_args(proxy_url))
    print(f"[LOGIN] nodriver Chrome 已開: {user_data_dir or '(臨時 profile)'}")
    tab = await browser.get(SIGN_IN_URL)
    print(f"[LOGIN] 請在視窗完成人機驗證 + 登入（最多 {timeout}s）...")

    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            url = await tab.evaluate("window.location.href", return_by_value=True)
        except Exception:
            await asyncio.sleep(1.0)
            continue
        if url and "kktix" in url and "/sign_in" not in url and await _logged_in(tab):
            print(f"[LOGIN] 登入完成: {url}")
            return browser, tab, True
        if url != last:
            print(f"[LOGIN] 等待中... 目前: {url}")
            last = url
        await asyncio.sleep(1.0)

    print(f"[LOGIN] 超時 {timeout}s 未偵測到登入")
    return browser, tab, False


async def page_fetch(tab, url: str, method: str = "GET",
                     body: str | None = None, headers: dict | None = None,
                     timeout: float = 20.0) -> dict:
    """在頁面同源環境用 fetch 打 url。credentials:include → 自動帶 cookie（含 httpOnly）。
    回 {ok:True, status, url, text} 或 {ok:False, error}。

    注意：tab 必須停在 kktix.com 頁面（同源），fetch 才會帶對 cookie，所以偵測期間別把
    tab 導航走。打 queue.kktix.com 是跨子網域，但 KKTIX 有開 CORS + credentials，照樣 OK。
    """
    opts = {"method": method, "headers": headers or {}, "credentials": "include"}
    if body is not None:
        opts["body"] = body
    js = (
        "(async()=>{try{"
        "const _a=performance.now();"
        f"const r=await fetch({json.dumps(url)},{json.dumps(opts)});"
        "const _b=performance.now();"
        "const t=await r.text();"
        "const _c=performance.now();"
        "return JSON.stringify({ok:true,status:r.status,url:r.url,text:t,"
        "_ttfb_ms:Math.round(_b-_a),_net_ms:Math.round(_c-_a)});"
        "}catch(e){return JSON.stringify({ok:false,error:String(e)});}})()"
    )
    label = url.split("//", 1)[-1][:60]
    t0 = time.perf_counter()
    try:
        raw = await asyncio.wait_for(
            tab.evaluate(js, await_promise=True, return_by_value=True), timeout=timeout)
    except Exception as e:
        wall = round((time.perf_counter() - t0) * 1000)
        print(f"[RTT] {method} {label}  wall={wall}ms  FAILED")
        return {"ok": False, "error": f"evaluate 失敗: {e!r}"}
    wall = round((time.perf_counter() - t0) * 1000)
    try:
        res = json.loads(raw)
    except Exception:
        print(f"[RTT] {method} {label}  wall={wall}ms  非JSON")
        return {"ok": False, "error": "回傳非 JSON", "raw": str(raw)[:200]}
    # net=純網路(TTFB) / body=含讀完 body / wall=Python端含 CDP evaluate 開銷
    print(f"[RTT] {method} {label}  net={res.get('_ttfb_ms', '?')}ms "
          f"body={res.get('_net_ms', '?')}ms wall={wall}ms  HTTP {res.get('status', '-')}")
    return res
