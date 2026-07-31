"""寬宏送單：讀選位頁狀態 → 挑空位 + 解驗證碼 → POST addShoppingCart（驗證碼錯自動重試）。

送單封包（逆自頁面 addShoppingCart JS，真實驗證待 live）：
  POST UTK0205_.aspx  (application/x-www-form-urlencoded)
  一堆 hidden 欄位 + PERFORMANCE_PRICE_AREA_ID(=#PRICE_AREA_ID) + CHK_VERIFY(驗證碼)
  + action=ADD_SHOPPING_CAR + SEATS=<JSON> + sender=jquery
  成功回應含 "location"（跳購物車）；驗證碼錯回應含「驗證碼」。

⚠️ LOGIN_ID/LOGIN_PWD：登入後靠 session cookie，這裡先送空字串；若 live 發現需要再補
   頁面的 ㎞+base64 編碼。
"""
import urllib.parse

import config
from kham_api import BASE, parsing, captcha
from kham_api.browser_session import page_fetch, read_js

# addShoppingCart 會讀的 hidden 欄位（id → 直接讀 value）
_FIELD_IDS = [
    "PERFORMANCE_ID", "PRODUCT_ID", "PRODUCT_CATEGORY_ID", "GROUP_ID", "PLACE_ID",
    "PRICE_AREA_ID", "LOGIN_ID", "ACTIVITY_GROUP_ID", "ACTIVITY_GROUP_ITEM_ID",
    "QUANTITY_LIMIT", "NOT_MIX", "CLOSE_3D", "isMarketAmerica", "NO_SELL_ON_WEBSITE",
    "IS_NAME_BASED", "DOC_MEMO",
]


async def read_seat_page(tab) -> dict | None:
    """一次 evaluate 讀選位頁：hidden 欄位值 + seats 物件 + 票種(setType onclick)。"""
    fields_js = ",".join(
        f'{f}:((document.getElementById("{f}")||{{}}).value||"")' for f in _FIELD_IDS)
    js = (
        "JSON.stringify({fields:{" + fields_js + "},"
        "seats:(typeof seats!=='undefined'?seats:{}),"
        "types:[...document.querySelectorAll('[onclick^=\"setType\"]')]"
        ".map(b=>b.getAttribute('onclick'))})"
    )
    raw = await read_js(tab, js)
    if not raw:
        return None
    import json
    try:
        return json.loads(raw)
    except Exception:
        return None


async def _get_captcha_bytes(tab):
    """抓一張新驗證碼（成為 session 當前預期值）→ 回 image bytes。"""
    js = (
        "(async()=>{try{"
        "const r=await fetch('/pic.aspx?TYPE=UTK0205&ts='+Date.now()+Math.random(),{credentials:'include'});"
        "const b=await r.blob();"
        "return await new Promise(res=>{const f=new FileReader();f.onload=()=>res(f.result);f.readAsDataURL(b);});"
        "}catch(e){return 'ERR:'+e;}})()"
    )
    data = await read_js_await(tab, js)
    if data and str(data).startswith("data:image"):
        import base64
        return base64.b64decode(str(data).split(",", 1)[1])
    return None


async def read_js_await(tab, expr):
    try:
        return await tab.evaluate(expr, await_promise=True, return_by_value=True)
    except Exception as e:
        print(f"[JS] await 讀取失敗: {e!r}")
        return None


def _build_body(fields: dict, chk: str, seats_json: str) -> str:
    q = urllib.parse.quote
    parts = {
        "PERFORMANCE_ID": fields.get("PERFORMANCE_ID", ""),
        "PRODUCT_ID": fields.get("PRODUCT_ID", ""),
        "PRODUCT_CATEGORY_ID": fields.get("PRODUCT_CATEGORY_ID", ""),
        "GROUP_ID": fields.get("GROUP_ID", ""),
        "PLACE_ID": fields.get("PLACE_ID", ""),
        "PERFORMANCE_PRICE_AREA_ID": fields.get("PRICE_AREA_ID", ""),
        "LOGIN_ID": fields.get("LOGIN_ID", ""),
        "LOGIN_PWD": "",
        "ACTIVITY_GROUP_ID": fields.get("ACTIVITY_GROUP_ID", ""),
        "ACTIVITY_GROUP_ITEM_ID": fields.get("ACTIVITY_GROUP_ITEM_ID", ""),
        "QUANTITY_LIMIT": fields.get("QUANTITY_LIMIT", ""),
        "NOT_MIX": fields.get("NOT_MIX", ""),
        "CLOSE_3D": fields.get("CLOSE_3D", ""),
        "isMarketAmerica": fields.get("isMarketAmerica", ""),
        "NO_SELL_ON_WEBSITE": fields.get("NO_SELL_ON_WEBSITE", ""),
        "IS_NAME_BASED": fields.get("IS_NAME_BASED", ""),
        "CHK_VERIFY": chk,
        "DOC_MEMO": fields.get("DOC_MEMO", ""),
        "action": "ADD_SHOPPING_CAR",
        "SEATS": seats_json,
        "sender": "jquery",
    }
    return "&".join(f"{k}={q(str(v))}" for k, v in parts.items())


async def add_to_cart(tab, *, amount: int, area_keyword: str, exclude: str,
                      max_retry: int = 6) -> str | None:
    """在選位頁：挑空位 + 解驗證碼 + POST。驗證碼錯換一張重試。成功回購物車 URL。"""
    state = await read_seat_page(tab)
    if not state:
        print("[RESERVE] 讀不到選位頁狀態")
        return None

    types = parsing.parse_ticket_types(
        "".join(f'<b onclick="{o}"></b>' for o in state.get("types", [])))
    ttype = parsing.select_ticket_type(types, keyword=area_keyword, exclude=exclude)
    if not ttype:
        print(f"[RESERVE] 沒有符合條件的票種（types={[t['name'] for t in types]}）")
        return None

    picked = parsing.pick_available_seats(state.get("seats", {}), amount)
    if not picked:
        print("[RESERVE] 無可選空位")
        return None
    print(f"[RESERVE] 票種={ttype['name']}({ttype['price']}) 選 {len(picked)} 位: "
          + ", ".join(s.get("I", "") for s in picked))

    seats_json = parsing.build_add_cart_seats(picked, ttype["type_id"], ttype["z"])

    for attempt in range(1, max_retry + 1):
        cap = await _get_captcha_bytes(tab)
        chk = captcha.recognize(cap) if cap else ""
        if not chk:
            print(f"[RESERVE] #{attempt} 驗證碼抓取/辨識失敗，重試")
            continue
        body = _build_body(state["fields"], chk, seats_json)
        res = await page_fetch(tab, "UTK0205_.aspx", method="POST", body=body,
                               headers={"Content-Type": "application/x-www-form-urlencoded",
                                        "X-Requested-With": "XMLHttpRequest"})
        text = (res.get("text") or "") if res.get("ok") else ""
        if not res.get("ok"):
            print(f"[RESERVE] #{attempt} POST 失敗: {res.get('error')}")
            continue
        if "location" in text:
            print(f"[RESERVE] ✅ 加入購物車成功（驗證碼={chk}，第 {attempt} 次）")
            return f"{BASE}/application/UTK02/UTK0206_.aspx"
        if "驗證碼" in text:
            print(f"[RESERVE] #{attempt} 驗證碼錯（{chk}），換一張重試")
            continue
        print(f"[RESERVE] #{attempt} 未成功，回應: {text[:150]}")
        return None

    print("[RESERVE] 重試用盡仍未加入購物車")
    return None
