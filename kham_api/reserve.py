"""寬宏送單：讀選位頁 → 挑票種/座位 + 解驗證碼 → POST ADD_SHOPPING_CAR（驗證碼錯自動重試）。

送單封包（逐字對齊 UTK0205.min.js 的 addShoppingCart，2026-09-17 重抓）：
  POST UTK0205_.aspx  (application/x-www-form-urlencoded; charset=UTF-8)
  - 欄位順序、編碼都照官方：**只有 LOGIN_PWD / SEATS / _INFO 有 encodeURIComponent**，
    其餘（包含 DOC_MEMO 那串 JSON，裡面有 `+`）是原樣串進去的。伺服器是照瀏覽器的送法
    寫的，逐字相同最安全，不要「好心」幫它全部 encode
  - LOGIN_PWD = encodeURIComponent("㎞" + base64(UTF-8 密碼))；已登入時密碼欄是空的 → 只剩 "㎞"
  - _INFO = {"loadtime": 頁面給的載入時間, "timestamp": 按下送出的 ms}，官方新加的欄位
  - 官方按下送出後會 setTimeout(maxWaitTime) 才真的 POST（目前是 0），這裡照做
  回應是一段 JS（官方 globalEval）：成功含 location（跳購物車）；驗證碼錯含「驗證碼」。

驗證碼：/pic.aspx，選位頁 TYPE=UTK0205（登入頁 UTK1306、不劃位的張數頁 UTK0202），大小寫不敏感。
session 只認最後一張，所以要「抓圖 → 辨識 → 送單」一條龍，中間不能再抓別張 ——
這也是選位頁改成 fetch、不整頁導航的原因之一：真的打開選位頁，頁面自己的 jquery.captcha
會非同步抓一張，跟我們的搶先後，輸了就驗證碼錯。
"""
import time
import json
import base64
import asyncio
import re
import urllib.parse

from kham_api import BASE, parsing, captcha
from kham_api.browser_session import page_fetch, utk02_url


async def _get_captcha_bytes(tab, referrer: str):
    """抓一張新驗證碼（成為 session 當前預期值）→ 回 image bytes。"""
    js = (
        "(async()=>{try{"
        "const r=await fetch('/pic.aspx?TYPE=UTK0205&ts='+Date.now(),"
        f"{{credentials:'include',referrer:{json.dumps(utk02_url(referrer))}}});"
        "const b=await r.blob();"
        "return await new Promise(res=>{const f=new FileReader();f.onload=()=>res(f.result);f.readAsDataURL(b);});"
        "}catch(e){return 'ERR:'+e;}})()"
    )
    data = await read_js_await(tab, js)
    if data and str(data).startswith("data:image"):
        return base64.b64decode(str(data).split(",", 1)[1])
    return None


# add_to_cart 回傳的原因碼。除了 NO_TYPE（設定問題）以外都可以重試，
# 但 UNKNOWN 要由呼叫端限制連續次數，免得對著「您已購買過此場次」這種訊息無限鬼打牆。
REASON_OK = "ok"                    # 搶到了
REASON_EMPTY = "empty_page"         # 選位頁沒座位資料（頁面還沒好 / 被導走）
REASON_NO_TYPE = "no_type"          # 沒有符合條件的票種 —— 重試無用
REASON_NO_SEAT = "no_seat"          # 這區空位剛好被買光
REASON_NOT_ENOUGH = "not_enough"    # 剩餘不足指定張數（REQUIRE_FULL_AMOUNT）
REASON_CAPTCHA = "captcha"          # 驗證碼一直錯 / POST 一直失敗
REASON_UNKNOWN = "unknown"          # 伺服器回了沒見過的訊息


async def read_js_await(tab, expr):
    try:
        return await tab.evaluate(expr, await_promise=True, return_by_value=True)
    except Exception as e:
        print(f"[JS] await 讀取失敗: {e!r}")
        return None


def _enc(s: str) -> str:
    """= JS encodeURIComponent（比 quote 多保留 !~*'()）。"""
    return urllib.parse.quote(s, safe="!~*'()")


def _login_pwd(pwd: str) -> str:
    return _enc("㎞" + base64.b64encode(pwd.encode("utf-8")).decode("ascii"))


def build_body(fields: dict, chk: str, seats_json: str, info_json: str) -> str:
    """bot 一定是已登入狀態，密碼欄是空的（頁內登入那條路不走）。"""
    f = fields.get
    return (
        f"PERFORMANCE_ID={f('PERFORMANCE_ID', '')}"
        f"&PRODUCT_ID={f('PRODUCT_ID', '')}"
        f"&PRODUCT_CATEGORY_ID={f('PRODUCT_CATEGORY_ID', '')}"
        f"&GROUP_ID={f('GROUP_ID', '')}"
        f"&PLACE_ID={f('PLACE_ID', '')}"
        f"&PERFORMANCE_PRICE_AREA_ID={f('PRICE_AREA_ID', '')}"
        f"&LOGIN_ID={f('LOGIN_ID', '')}"
        f"&LOGIN_PWD={_login_pwd('')}"
        f"&ACTIVITY_GROUP_ID={f('ACTIVITY_GROUP_ID', '')}"
        f"&ACTIVITY_GROUP_ITEM_ID={f('ACTIVITY_GROUP_ITEM_ID', '')}"
        f"&QUANTITY_LIMIT={f('QUANTITY_LIMIT', '')}"
        f"&NOT_MIX={f('NOT_MIX', '')}"
        f"&CLOSE_3D={f('CLOSE_3D', '')}"
        f"&isMarketAmerica={f('isMarketAmerica', '')}"
        f"&NO_SELL_ON_WEBSITE={f('NO_SELL_ON_WEBSITE', '')}"
        f"&IS_NAME_BASED={f('IS_NAME_BASED', '')}"
        f"&CHK_VERIFY={chk}"
        f"&DOC_MEMO={f('DOC_MEMO', '')}"
        f"&action=ADD_SHOPPING_CAR&SEATS={_enc(seats_json)}"
        f"&_INFO={_enc(info_json)}"
        f"&sender=jquery"
    )


def _redirect_target(js_text: str) -> str | None:
    m = re.search(r"location(?:\.href)?\s*=\s*['\"]([^'\"]+)['\"]", js_text)
    if not m:
        return None
    return urllib.parse.urljoin(f"{BASE}/application/UTK02/UTK0205_.aspx", m.group(1))


async def add_to_cart(tab, seat_url: str, seat_html: str, *, amount: int, keyword: str,
                      exclude: str, require_full: bool = False,
                      max_retry: int = 6) -> tuple[str | None, str]:
    """拿 fetch 回來的選位頁 HTML：挑票種 + 挑座位 + 解驗證碼 + POST。驗證碼錯換一張重試。

    回 (購物車 URL, 原因)。原因是給清票迴圈判斷要不要重來的，值見 REASON_*：
    `no_type` 是設定問題，重試無用；`unknown` 是伺服器回了沒見過的訊息 —— **照樣可以重試**，
    由呼叫端限制連續幾次就放棄（早期版本直接中止，開賣瞬間萬一回「尚未開賣」就當場收工）。"""
    state = parsing.parse_seat_page(seat_html)
    if not state["seats"]:
        print("[RESERVE] 選位頁沒有座位資料（頁面還沒好或被導走）")
        return None, REASON_EMPTY

    types = state["types"]
    ttype = parsing.select_ticket_type(types, keyword=keyword, exclude=exclude)
    if not ttype:
        print(f"[RESERVE] 沒有符合條件的票種（types={[t['name'] for t in types]}）")
        return None, REASON_NO_TYPE

    limit = int(state["fields"].get("QUANTITY_LIMIT") or 0)
    if limit and amount > limit:
        print(f"[RESERVE] 張數 {amount} 超過本場上限 {limit}，改買 {limit} 張")
        amount = limit

    picked, how = parsing.pick_seats(state["seats"], amount, exclude=exclude)
    if not picked:
        print("[RESERVE] 這區已經沒有空位了")
        return None, REASON_NO_SEAT
    if len(picked) < amount and require_full:
        print(f"[RESERVE] 只剩 {len(picked)} 個空位 < {amount} 張，REQUIRE_FULL_AMOUNT 開著所以不買")
        return None, REASON_NOT_ENOUGH
    print(f"[RESERVE] 票種={ttype['name']}({ttype['price']}) 選 {len(picked)} 位【{how}】: "
          + ", ".join(s["label"] for s in picked))

    seats_json = parsing.build_add_cart_seats(picked, ttype["type_id"], ttype["z"])
    wait_ms = state["max_wait_ms"]
    if wait_ms:
        print(f"[RESERVE] 頁面設了 maxWaitTime={wait_ms}ms，送單前會照官方等這麼久")
    if not state["loadtime"]:
        print("[RESERVE] ⚠️ 頁面沒有 _info.loadtime，_INFO 只能送空字串（官方格式可能又改了）")

    for attempt in range(1, max_retry + 1):
        cap = await _get_captcha_bytes(tab, seat_url)
        chk = captcha.recognize(cap) if cap else ""
        if len(chk) != 4:
            # 長度不對一定是錯的，換一張就好，不值得浪費一發 POST
            print(f"[RESERVE] #{attempt} 驗證碼抓取/辨識失敗（{chk!r}），換一張")
            continue
        info_json = parsing.build_info(state["loadtime"], int(time.time() * 1000))
        if wait_ms:
            await asyncio.sleep(wait_ms / 1000)
        body = build_body(state["fields"], chk, seats_json, info_json)
        res = await page_fetch(tab, "UTK0205_.aspx", method="POST", body=body, referrer=seat_url,
                               headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                        "X-Requested-With": "XMLHttpRequest"})
        if not res.get("ok"):
            print(f"[RESERVE] #{attempt} POST 失敗: {res.get('error')}")
            continue
        text = res.get("text") or ""
        if "location" in text:
            cart = _redirect_target(text) or f"{BASE}/application/UTK02/UTK0206_.aspx"
            print(f"[RESERVE] ✅ 加入購物車成功（驗證碼={chk}，第 {attempt} 次）→ {cart}")
            return cart, REASON_OK
        if "驗證碼" in text:
            print(f"[RESERVE] #{attempt} 驗證碼錯（{chk}），換一張重試")
            continue
        # 座位被搶走 / 尚未開賣 / 買太多張……這些回應格式都還沒實測過，整段印出來留證據
        print(f"[RESERVE] #{attempt} 未成功，回應: {json.dumps(text[:300], ensure_ascii=False)}")
        return None, REASON_UNKNOWN

    print("[RESERVE] 重試用盡仍未加入購物車")
    return None, REASON_CAPTCHA
