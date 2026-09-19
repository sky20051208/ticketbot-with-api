"""寬宏瀏覽器層（nodriver）：登入 + 在頁面內用 fetch 打 kham。

跟 kktix_api.browser_session 同一套設計：保留登入好的瀏覽器，用 tab.evaluate(fetch(...)) 在
同源環境打 kham.com.tw（自動帶 cookie、TLS 指紋就是真 Chrome）。搶票主線全部走 fetch，
分頁只在開頭停到活動頁、結尾跳購物車 —— 不整頁導航，省掉圖片 / JS / 座位圖 .map 的載入，
也避免選位頁自己的 jquery.captcha 去抓一張驗證碼，把我們手上那張蓋掉。

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
from kham_api import BASE, parsing

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
    """頁內抓一次登入頁，用寬宏自己的登入旗標 `var _ul` 判斷（'0' = 未登入，
    選位頁的 addShoppingCart 就是拿它決定要不要逼你當場填帳密）。

    **兩個坑都踩過，所以才這樣寫**：
    1. nodriver 的 `tab.evaluate` 在值 falsy 時回原始 RemoteObject，`bool()` 恆真 ——
       舊寫法 `bool(await tab.evaluate("!!document.querySelector(...)"))` **永遠回 True**，
       沒登入也判定登入完成（2026-09-19 被騙過）。
    2. evaluate 跑在隔離環境，**讀不到頁面的 JS 全域**（`typeof _ul` 是 undefined），
       所以不能直接讀旗標，要抓 HTML 自己解。
    使用者跑去 Google / FB 登入時分頁不在 kham 網域，fetch 會被 CORS 擋 → 當成還沒登入。"""
    res = await page_fetch(tab, LOGIN_URL, log=False)
    return parsing.logged_in(res.get("text") or "") is True


async def open_and_login(user_data_dir: str, proxy_url: str = "", timeout: int = 600):
    """開 nodriver（用 profile）、等使用者過人機驗證 + 登入。回 (browser, tab, ok)。"""
    browser = await uc.start(headless=False, user_data_dir=user_data_dir or None,
                             browser_args=_browser_args(proxy_url))
    print(f"[LOGIN] nodriver Chrome 已開: {user_data_dir or '(臨時 profile)'}")
    tab = await browser.get(LOGIN_URL)
    print(f"[LOGIN] 請在視窗完成登入（帳號=身分證字號 + 密碼 + 驗證碼，最多 {timeout}s）...")
    print("[LOGIN] 寬宏的登入態是 session cookie，profile 存不住 —— 每次啟動都要重登一次")

    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            url = await tab.evaluate("window.location.href", return_by_value=True)
        except Exception:
            await asyncio.sleep(1.0)
            continue
        # 不看停在哪一頁（寬宏登入後可能就留在登入頁），只看登入旗標
        if url and "kham" in url and await _logged_in(tab):
            print(f"[LOGIN] 登入完成: {url}")
            return browser, tab, True
        if url != last:
            print(f"[LOGIN] 等待中... 目前: {url}")
            last = url
        await asyncio.sleep(2.0)  # 每輪要打一發登入頁，別問太密

    print(f"[LOGIN] 超時 {timeout}s 未偵測到登入")
    return browser, tab, False


def utk02_url(url: str) -> str:
    """UTK02 底下的相對網址（parsing 產的都是）→ 絕對網址。已是絕對就原樣回。
    fetch 的相對網址是相對「分頁目前所在頁」解析的，分頁不一定停在 UTK02，所以一律轉絕對。"""
    return url if url.startswith("http") else f"{BASE}/application/UTK02/{url.lstrip('/')}"


async def goto(tab, url: str, settle: float = 0.6):
    """導航到 url 並等頁面穩定。url 可相對（相對 UTK02）或絕對。"""
    full = utk02_url(url)
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
                     timeout: float = 20.0, referrer: str = "", log: bool = True) -> dict:
    """在頁面同源環境 fetch。回 {ok,status,url(轉址後),text} 或 {ok:False,error}。

    分頁必須停在 kham.com.tw 上（同源才帶得到 cookie）。
    referrer：要假裝從哪一頁發的（官方的 ajax 都是從選位頁發，Referer 就是選位頁）。
    log=False 給高頻輪詢用，失敗照樣會印。"""
    url = utk02_url(url)
    opts = {"method": method, "headers": headers or {}, "credentials": "include"}
    if referrer:
        opts["referrer"] = utk02_url(referrer)
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
    label = url.split("/application/", 1)[-1][:55]
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
    res["wall_ms"] = wall
    if log or not res.get("ok"):
        print(f"[RTT] {method} {label}  net={res.get('_ttfb_ms','?')}ms "
              f"body={res.get('_net_ms','?')}ms wall={wall}ms  HTTP {res.get('status','-')}")
    return res
