"""KKTIX HTML 解析：報名頁票區、challenge 偵測、CSRF token、依 config 選票。

票區/challenge 的解析邏輯參考 wooooody98/ticket-bot-public 的 kktix_parser.py
（那份是對真實 KKTIX DOM 寫的，所以 regex 可信），這裡只留 API 模式會用到的部分，
並補上「依 config 挑票」與「抓 CSRF token」。
"""
import re
import json
from html import unescape

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean_text(value: str) -> str:
    text = unescape(_TAG_RE.sub(" ", value or ""))
    return _WS_RE.sub(" ", text).strip()


def _search(pattern: str, html: str, flags: int = 0) -> str:
    m = re.search(pattern, html, flags)
    return m.group(1) if m else ""


def detect_challenge(html: str) -> bool:
    """偵測 KKTIX 的 Cloudflare 安全驗證頁（此時 cookie 失效或被擋）。"""
    n = html.lower()
    return (
        "just a moment" in n
        or "enable javascript and cookies to continue" in n
        or "正在執行安全驗證" in html
        or "window._cf_chl_opt" in n
    )


def extract_csrf_token(html: str) -> str:
    """Rails 的 <meta name="csrf-token" content="..."> — 下單 POST 要帶。"""
    return _search(r'<meta name="csrf-token" content="([^"]+)"', html)


def parse_registration_ticket_units(html: str) -> list[dict]:
    """解析報名頁的票種區塊。每個 <... id="ticket_{id}"> 是一個票種。"""
    ticket_matches = list(re.finditer(r'id="ticket_(\d+)"', html))
    if not ticket_matches:
        return []

    units = []
    list_end = html.find('</div>\n<div class="platform-fee-remark-wrapper-ticket')

    for index, match in enumerate(ticket_matches):
        start = match.start()
        if index + 1 < len(ticket_matches):
            end = ticket_matches[index + 1].start()
        else:
            end = list_end if list_end > start else len(html)

        block = html[start:end]
        name = _clean_text(
            _search(r'<span class="ticket-name[^"]*">\s*([^<]+?)\s*(?:<!--|<div)', block, re.DOTALL)
        )
        label = _clean_text(_search(r'<div class="small[^"]*">(.*?)</div>', block, re.DOTALL))
        price = _clean_text(_search(r'(TWD\$[0-9,]+)', block))

        status = "available"
        if "Sold Out" in block or "已售完" in block:
            status = "sold_out"
        elif "Temporarily Unavailable" in block or "暫無票" in block:
            status = "temporarily_unavailable"
        elif "Need invitation code" in block or "邀請碼" in block:
            status = "invitation_required"

        units.append({
            "ticket_id": match.group(1),
            "name": name,
            "label": label,
            "price": price,
            "status": status,
            # selectable：DOM 有 Angular 的 +1 按鈕 → 真的能加票
            "selectable": 'ng-click="quantityBtnClick(1)"' in block
                          or 'class="btn-default plus"' in block,
        })
    return units


def is_registration_open(html: str) -> bool:
    """報名頁是否已開賣：有任何「可選」票種就算開了。"""
    return any(u["status"] == "available" and u["selectable"]
              for u in parse_registration_ticket_units(html))


def _summarize(unit: dict) -> str:
    parts = [unit.get("name", ""), unit.get("label", ""), unit.get("price", "")]
    return " / ".join(p for p in parts if p)


def select_ticket(units: list[dict], *, keyword: str, exclude: str,
                  mode: str, amount: int) -> dict | None:
    """依 config 從票區清單挑一個票種。

    mode 對齊 config.AREA_AUTO_SELECT_MODE：
      關鍵字優先 / 由上而下 / 由下而上 / 隨機
    keyword = AREA_KEYWORD（票種名稱或價格關鍵字，空 = 不限）
    exclude = EXCLUDE_AREA_KEYWORD（分號分隔）
    """
    import random

    pool = [u for u in units if u["status"] == "available" and u["selectable"]]
    if not pool:
        return None

    exclude_list = [e.strip() for e in (exclude or "").split(";") if e.strip()]
    if exclude_list:
        pool = [u for u in pool
                if not any(ek in _summarize(u) for ek in exclude_list)]
    if not pool:
        return None

    kw = (keyword or "").strip()
    if kw:
        kw_l = kw.casefold()
        matched = [u for u in pool if kw_l in _summarize(u).casefold()]
        # 關鍵字是「偏好」不是「硬篩選」：命中就縮到命中的，沒命中就退回「全部有票的」照 mode 挑。
        # 這樣「隨機／由上而下／由下而上」搭配關鍵字時，關鍵字沒命中也不會整個放棄搶票
        # （例：選隨機但關鍵字欄留了字，仍會隨機抓一張有票的）。硬性排除請用 EXCLUDE_AREA_KEYWORD。
        if matched:
            pool = matched

    if not pool:
        return None

    if mode == "由下而上":
        pool = list(reversed(pool))
    elif mode == "隨機":
        random.shuffle(pool)
    # 由上而下 / 關鍵字優先 → 維持原順序

    chosen = dict(pool[0])
    chosen["amount"] = max(1, amount)
    return chosen


# ---------------------------------------------------------------------------
# 純封包（API）版票種來源 —— KKTIX 報名頁票種是 Angular 前端渲染，raw HTML 沒有；
# 票名/票價在 base_info API、票況在 register_info API。以下把兩者解成 select_ticket 能吃的 units，
# 完全不需要瀏覽器渲染。
# ---------------------------------------------------------------------------

def _price_str(price: dict) -> str:
    """{"cents":328000,"currency":"TWD"} → "TWD$3,280"（對齊渲染 DOM 的顯示格式）。"""
    cents = price.get("cents")
    if not isinstance(cents, int):
        return ""
    return f'{price.get("currency", "TWD")}${cents // 100:,}'


def parse_base_info(json_text: str) -> list[dict]:
    """base_info API → 靜態票種目錄（含票名/票價，開場前抓一次即可）。
    每項: ticket_id, name, price(顯示字串), min_to_buy, max_to_buy, need_invitation_code, position。

    來源合併 eventData.tickets（開賣中的票種）+ stop_selling_tickets（已停售/售完的票種）。
    為什麼要含 stop_selling_tickets：售完活動的票種會全部移到這裡、eventData.tickets 變空；
    等回流票時票一釋出 register_info 會顯示 in_stock，此時 catalog 有它的名字，AREA_KEYWORD
    才比對得到（清票主力場景）。以 id 去重，eventData.tickets（正在賣的）優先。"""
    try:
        data = json.loads(json_text)
    except Exception:
        return []
    ed = data.get("eventData") or {}
    out, seen = [], set()
    for t in (ed.get("tickets") or []) + (ed.get("stop_selling_tickets") or []):
        tid = str(t.get("id"))
        if tid in seen:
            continue
        seen.add(tid)
        out.append({
            "ticket_id": tid,
            "name": t.get("name", "") or "",
            "price": _price_str(t.get("price") or {}),
            "min_to_buy": t.get("min_to_buy", 1),
            "max_to_buy": t.get("max_to_buy"),
            "need_invitation_code": bool(t.get("need_invitation_code")),
            "position": t.get("position", 0),
        })
    return out


def parse_register_info(json_text: str) -> dict:
    """register_info API → 動態票況。
    回 {register_status, open(bool), in_stock_ids(set[str]), sections(dict id→stock_level),
        tickets(原始票種 list)}。

    tickets 原封不動保留，供 base_info 抓失敗時的 fallback（register_info 的票種身上有時
    也帶 name/price，沒有的話至少有 id + in_stock，仍可 buy-any 搶）。"""
    empty = {"register_status": "", "open": False, "in_stock_ids": set(),
             "sections": {}, "tickets": []}
    try:
        data = json.loads(json_text)
    except Exception:
        return empty
    status = data.get("register_status", "") or ""
    raw_tickets = data.get("tickets") or []
    in_stock = {str(t.get("id")) for t in raw_tickets if t.get("in_stock")}
    sections = {str(s.get("id")): s.get("stock_level")
                for s in (data.get("sections") or [])}
    return {
        "register_status": status,
        # ★ 開賣判定「只認 register_status == IN_STOCK」，絕不能看 in_stock 旗標！
        #   坑（實測 tanya26 尚未開賣）：KKTIX 在 COMING_SOON（開賣前）就把每個票種的
        #   in_stock 標成 true（只是 upper_bound=0）。若拿 in_stock 當開賣訊號，會在開賣前
        #   就誤判 open、狂送 join_queue → 回 403 EVENT_NOT_YET_START（就是「第一拍 not yet」）。
        #   register_status 才是權威：COMING_SOON=未開 / IN_STOCK=開賣 / SOLD_OUT=售完 /
        #   REGISTRATION_CLOSED、CLOSED=已關。
        "open": status == "IN_STOCK",
        "in_stock_ids": in_stock,
        "sections": sections,
        "tickets": raw_tickets,
    }


def units_from_register_info(reg_info: dict) -> list[dict]:
    """base_info 票種目錄抓不到時的 fallback：直接用 register_info 自己的票種清單建 units。
    register_info 的票種通常只有 id + in_stock（不一定有 name/price）—— 有就帶上，
    沒有 name 就用 '票種{id}' 佔位。keyword 比對會受限（沒名字比不到），但 buy-any 照搶。"""
    units = []
    for t in reg_info.get("tickets") or []:
        tid = str(t.get("id"))
        in_stock = bool(t.get("in_stock"))
        name = t.get("name") or f"票種{tid}"
        price = _price_str(t.get("price")) if isinstance(t.get("price"), dict) else (t.get("price") or "")
        units.append({
            "ticket_id": tid,
            "name": name,
            "label": "",
            "price": price if isinstance(price, str) else "",
            "status": "available" if in_stock else "sold_out",
            "selectable": in_stock,
        })
    return units


def parse_redeem_to_param(json_text: str) -> str:
    """候位 redeem 回應（GET queue/token/{token}）→ to_param（報名 id，如 "157460846-3724ee..."）。
    沒有就回空字串。"""
    try:
        return json.loads(json_text).get("to_param", "") or ""
    except Exception:
        return ""


# redeem/送單 message 分類：售完類 = 軟失敗（繼續清票等回流票）；其餘（資格不符等）= 硬失敗
# （重試無用，該停）。字串來自真實 alert：「票券已全部售出」「目前沒有可以購買的票券。」
# vs「非 KKTIX 身心障礙身份認證會員，不可選購票種…」。
_SOLDOUT_MSG_MARKERS = ("售出", "售完", "沒有可以購買", "沒有可購買", "已無票", "無可購買", "已售完")


def redeem_message_is_soldout(msg: str) -> bool:
    """message 是否屬「售完/沒票」類（→ 該繼續清票，不是硬失敗）。"""
    return any(m in (msg or "") for m in _SOLDOUT_MSG_MARKERS)


def parse_redeem_result(json_text: str) -> dict:
    """redeem 回應完整語意（逆向前端 registrations/new bundle 的 waitForRegistration）：
      - to_param 有值 → **成功**（接著 confirm_booking / 導訂單頁）
      - message 有值 → **硬失敗**，前端就是 alert 這句（例：「你沒有身障資格」、售完），
                        要立刻中止這張，別再空等
      - 兩者皆無     → **還在排隊**，繼續 poll
    坑（2026-07 真實封包驗證）：join_queue 不檢查資格/庫存，送身障席沒資格照樣回 200+token；
    真正的驗證在這支 redeem，靠 message 才看得出來。"""
    try:
        d = json.loads(json_text)
    except Exception:
        return {"to_param": "", "message": ""}
    return {"to_param": d.get("to_param") or "", "message": d.get("message") or ""}


def merge_availability(catalog: list[dict], reg_info: dict) -> list[dict]:
    """base_info 票種目錄 + register_info 票況 → select_ticket 能吃的 units。
    有票(in_stock) → status=available & selectable=True；否則 sold_out。

    catalog 空（base_info 抓失敗）時退回用 register_info 自己的票種清單，不會整場搶不到。"""
    if not catalog:
        return units_from_register_info(reg_info)
    ids = reg_info.get("in_stock_ids") or set()
    units = []
    for c in catalog:
        in_stock = c["ticket_id"] in ids
        units.append({
            "ticket_id": c["ticket_id"],
            "name": c["name"],
            "label": "",
            "price": c["price"],
            "status": "available" if in_stock else "sold_out",
            "selectable": in_stock,
        })
    return units
