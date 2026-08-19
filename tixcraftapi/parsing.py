"""HTML / JS 解析（純 regex，不打網路）。

「拓元頁面長什麼樣」的知識全部集中在這檔 — 拓元改版時只改這裡。
game / verify / area / submit / session(keep-alive) 共用。
"""
import json
import re

# /game/ 頁出現場次按鈕的特徵（已開賣）
_GAME_BTN_RE = re.compile(r'data-href=["\'][^"\']*?/ticket/area/')


# ---------- game 頁 ----------

def game_not_open(html: str) -> bool:
    """game 頁還在「即將開賣」狀態（尚未放票）。"""
    return "即將開賣" in html or "coming soon" in html.lower()


def has_area_button(html: str) -> bool:
    """game 頁已出現場次按鈕（開賣特徵，keep-alive 提前開賣偵測用）。"""
    return bool(_GAME_BTN_RE.search(html))


def parse_game_keys(html: str, date_keyword: str = "") -> list[str]:
    """從 game 頁抽場次 id（`data-key`）。**開賣前就抓得到**（那時還沒有 data-href），
    拿來預先組出 area URL `/ticket/area/{slug}/{key}`，T-0 就能直接 poll 選區頁。

    **`data-key` 在 `<tr>` 標籤自己身上**（`<tr class="gridc fcTxt" data-key="22381">`），
    不在列的內容裡 —— 所以要連標籤的屬性一起抓。2026-08-16 之前這裡是在列內容找、
    永遠落空，然後退回「全頁掃 data-key」，結果把**已截止的購票專區**也撈進來當 poll
    目標（26_joji 實測：22381 正在賣，22382/22383 是已截止的 MyVideo / 台灣大哥大專區，
    打過去一律 301）。那條全頁 fallback 因此移除 —— 它產出的是錯的資料，而且
    date_keyword 對它完全無效。

    排除「截止」的列：那種場次 poll 它只是白白吃掉 eps 的請求配額（實測同一 URL
    超過約 3 req/s 就開始 403）。
    """
    keys: list[str] = []
    for m in re.finditer(r'<tr([^>]*class="gridc[^"]*"[^>]*)>(.*?)</tr>', html, re.DOTALL):
        attrs, row_html = m.group(1), m.group(2)
        km = re.search(r'data-key=["\']([^"\']+)["\']', attrs)
        if not km:
            continue
        if date_keyword and date_keyword not in row_html:
            continue
        if "截止" in row_html:
            continue
        if km.group(1) not in keys:
            keys.append(km.group(1))
    return keys


def parse_game_area_url(html: str, date_keyword: str = "") -> str | None:
    """從 game page HTML 抽 area URL；找不到回 None。"""
    rows = re.findall(
        r'<tr[^>]*class="gridc[^"]*"[^>]*>(.*?)</tr>',
        html, re.DOTALL
    )
    for row_html in rows:
        href_m = re.search(r'data-href=["\']([^"\']+)["\']', row_html)
        if not href_m:
            continue
        if date_keyword and date_keyword not in row_html:
            continue
        return href_m.group(1)

    fallback = re.search(r'data-href=["\']([^"\']*ticket/area/[^"\']+)["\']', html)
    if fallback:
        return fallback.group(1)
    return None


# ---------- 表單 hidden 欄位（verify 頁 / ticket 頁共用） ----------

def parse_hidden_inputs(html: str) -> dict:
    """抽出所有 <input type="hidden"> 的 name → value。"""
    fields: dict[str, str] = {}
    for m in re.finditer(r'<input[^>]+type=["\']hidden["\'][^>]*>', html):
        tag = m.group(0)
        name_m = re.search(r'name=["\']([^"\']+)["\']', tag)
        val_m = re.search(r'value=["\']([^"\']*)["\']', tag)
        if name_m:
            fields[name_m.group(1)] = val_m.group(1) if val_m else ""
    return fields


def parse_ticket_form(html: str) -> dict:
    """ticket 頁表單：只留 _csrf 和 TicketForm 系列欄位。"""
    return {k: v for k, v in parse_hidden_inputs(html).items()
            if "_csrf" in k or "TicketForm" in k}


def find_ticket_codes(payload: dict) -> list[str]:
    codes = []
    for k in payload:
        m = re.search(r'TicketForm\[(?:ticketPrice|priceSize)\]\[(\w+)\]', k)
        if m and m.group(1) not in codes:
            codes.append(m.group(1))
    return codes


# ---------- area 頁 ----------

def parse_area_availables(html: str) -> list[tuple[str, str, str]] | None:
    """從 area 頁抽可購買區域 [(area_id, 區名文字, ticket_url), ...]。

    areaUrlList 可能是 dict JS literal、逐筆 areaUrlList[id]=url，或根本不存在。
    已售完的區域沒有 <a id="21567_22"> 標籤。
    回 None = 頁面沒有 areaUrlList（未開賣/全售完/解析失敗）；回 [] = 有列表但沒可買的 <a>。
    """
    url_list_m = re.search(r'areaUrlList\s*=\s*(\{.*?\})', html, re.DOTALL)
    if url_list_m:
        try:
            area_urls = json.loads(url_list_m.group(1))
        except json.JSONDecodeError:
            return None
    else:
        assigns = re.findall(
            r'areaUrlList\[(["\']?)([^\]"\']+)\1\]\s*=\s*["\']([^"\']+)["\']', html
        )
        if not assigns:
            return None
        area_urls = {key: val for _, key, val in assigns}

    available = []
    for m in re.finditer(
        r'<a\s+id=["\'](\d+_\d+)["\'][^>]*>(.*?)</a>',
        html, re.DOTALL
    ):
        area_id = m.group(1)
        area_text = re.sub(r'<[^>]+>', '', m.group(2)).strip()
        if area_id in area_urls:
            available.append((area_id, area_text, area_urls[area_id]))
    return available
