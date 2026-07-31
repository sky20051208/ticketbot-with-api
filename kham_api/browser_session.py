"""寬宏瀏覽器層（nodriver）：登入 + 在頁面內用 fetch 打 kham + 讀 JS 狀態。

跟 kktix_api.browser_session 同一套設計：保留登入好的瀏覽器，用 tab.evaluate(fetch(...)) 在
同源環境打 kham.com.tw（自動帶 cookie），並用 evaluate 直接讀選位頁的 JS 全域 `seats` 物件。

對外提供：
  open_and_login(...) -> (browser, tab, ok)   開瀏覽器、等登入
  page_fetch(tab, url, ...) -> dict            在頁面內 fetch（含 RTT log）
  goto(tab, url)                               導航並等頁面載入
  read_js(tab, expr)                           evaluate 讀 JS 值
"""
import json
import time
import asyncio

import nodriver as uc

import browser_login as _tx  # 借用 setup_proxy_bridge（domain 無關）
import config
from kham_api import BASE

LOGIN_URL = f"{BASE}/application/utk13/utk1306_.aspx"


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
    """寬宏登入後頁面帶 doLogout 連結（未登入的登入頁沒有）。"""
    try:
        return bool(await tab.evaluate(
            "!!document.querySelector('[onclick*=\"doLogout\"]')", return_by_value=True))
    except Exception:
        return False


async def open_and_login(user_data_dir: str, proxy_url: str = "", timeout: int = 600):
    """開 nodriver（用 profile）、等使用者過人機驗證 + 登入。回 (browser, tab, ok)。"""
    browser = await uc.start(headless=False, user_data_dir=user_data_dir or None,
                             browser_args=_browser_args(proxy_url))
    print(f"[LOGIN] nodriver Chrome 已開: {user_data_dir or '(臨時 profile)'}")
    tab = await browser.get(LOGIN_URL)
    print(f"[LOGIN] 請在視窗完成登入（最多 {timeout}s）...")

    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            url = await tab.evaluate("window.location.href", return_by_value=True)
        except Exception:
            await asyncio.sleep(1.0)
            continue
        if url and "kham" in url and "utk1306" not in url.lower() and await _logged_in(tab):
            print(f"[LOGIN] 登入完成: {url}")
            return browser, tab, True
        if url != last:
            print(f"[LOGIN] 等待中... 目前: {url}")
            last = url
        await asyncio.sleep(1.0)

    print(f"[LOGIN] 超時 {timeout}s 未偵測到登入")
    return browser, tab, False


async def goto(tab, url: str, settle: float = 0.6):
    """導航到 url 並等頁面穩定。url 可相對（相對 BASE）或絕對。"""
    full = url if url.startswith("http") else f"{BASE}/application/UTK02/{url.lstrip('/')}"
    await tab.get(full)
    await asyncio.sleep(settle)
    return full


async def read_js(tab, expr: str):
    """evaluate 讀一個 JS 運算式的值（return_by_value）。失敗回 None。"""
    try:
        return await tab.evaluate(expr, return_by_value=True)
    except Exception as e:
        print(f"[JS] 讀取失敗 ({expr[:40]}...): {e!r}")
        return None


async def page_fetch(tab, url: str, method: str = "GET",
                     body: str | None = None, headers: dict | None = None,
                     timeout: float = 20.0) -> dict:
    """在頁面同源環境 fetch。回 {ok,status,url,text} 或 {ok:False,error}。含 RTT log。"""
    opts = {"method": method, "headers": headers or {}, "credentials": "include"}
    if body is not None:
        opts["body"] = body
    js = (
        "(async()=>{try{"
        "const _a=performance.now();"
        f"const r=await fetch({json.dumps(url)},{json.dumps(opts)});"
        "const _b=performance.now();"
        "const t=await r.text();"
        "return JSON.stringify({ok:true,status:r.status,url:r.url,text:t,"
        "_ttfb_ms:Math.round(_b-_a),_net_ms:Math.round(performance.now()-_a)});"
        "}catch(e){return JSON.stringify({ok:false,error:String(e)});}})()"
    )
    label = url.split("//", 1)[-1][:55] if "//" in url else url[:55]
    t0 = time.perf_counter()
    try:
        raw = await asyncio.wait_for(
            tab.evaluate(js, await_promise=True, return_by_value=True), timeout=timeout)
    except Exception as e:
        print(f"[RTT] {method} {label}  wall={round((time.perf_counter()-t0)*1000)}ms FAILED")
        return {"ok": False, "error": f"evaluate 失敗: {e!r}"}
    wall = round((time.perf_counter() - t0) * 1000)
    try:
        res = json.loads(raw)
    except Exception:
        return {"ok": False, "error": "回傳非 JSON", "raw": str(raw)[:200]}
    print(f"[RTT] {method} {label}  net={res.get('_ttfb_ms','?')}ms "
          f"body={res.get('_net_ms','?')}ms wall={wall}ms  HTTP {res.get('status','-')}")
    return res
