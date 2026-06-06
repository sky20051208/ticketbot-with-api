"""KKTIX HTML 解析：報名頁票區、challenge 偵測、CSRF token、依 config 選票。

票區/challenge 的解析邏輯參考 wooooody98/ticket-bot-public 的 kktix_parser.py
（那份是對真實 KKTIX DOM 寫的，所以 regex 可信），這裡只留 API 模式會用到的部分，
並補上「依 config 挑票」與「抓 CSRF token」。
"""
import re
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
        # 關鍵字優先：找不到就退回全部；其他模式關鍵字當硬篩選
        if matched:
            pool = matched
        elif mode == "關鍵字優先":
            pass  # 找不到關鍵字票，下面照排序挑第一個有票的
        else:
            pool = matched  # 空 → 回 None

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
