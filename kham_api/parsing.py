"""寬宏 HTML/JS 解析（純函式）。「寬宏頁面長什麼樣」的知識集中這檔。

目前涵蓋選位頁 UTK0205 的部分（搶票核心）：
  - parse_hidden_inputs：送單 POST 要帶的一堆 hidden 欄位
  - parse_ticket_types：從 setType('id','原價-NT$3,280') 抓票種
  - pick_available_seats：從 seats 物件（瀏覽器 evaluate 讀來）挑 S==0 的空位
  - build_add_cart_seats：組 addShoppingCart 的 SEATS 參數（JSON 字串）

座位資料來源：選位頁的 JS 全域 `seats` 物件（key=p{欄}{列}，每格 {S:狀態,I:座位id,T:方向,...}），
bot 直接在瀏覽器 `tab.evaluate("JSON.stringify(seats)")` 讀，不硬解 HTML（座位是 JS 動態填的）。
狀態 S：0=空位可選 / 其他=已售或不可選（對齊頁面 statAry）。
"""
import re
import json


def _clean(x: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x or "")).strip()


def parse_performance_links(html: str) -> list[dict]:
    """UTK0201_00 選日期頁：每個『立即訂購』→ UTK0204?PERFORMANCE_ID=...。
    回 [{performance_id, product_id, url, label(該列文字，含日期/票價)}]，去重保序。"""
    out, seen = [], set()
    row_re = re.compile(
        r"<tr[^>]*>((?:(?!</tr>).)*?UTK0204_\.aspx\?PERFORMANCE_ID=([A-Z0-9]+)"
        r"&(?:amp;)?PRODUCT_ID=([A-Z0-9]+)(?:(?!</tr>).)*?)</tr>", re.S)
    for m in row_re.finditer(html):
        pid, prod = m.group(2), m.group(3)
        if pid in seen:
            continue
        seen.add(pid)
        out.append({"performance_id": pid, "product_id": prod,
                    "url": f"UTK0204_.aspx?PERFORMANCE_ID={pid}&PRODUCT_ID={prod}",
                    "label": _clean(m.group(1))[:80]})
    return out


def select_performance(perfs: list[dict], date_keyword: str) -> dict | None:
    """依 DATE_KEYWORD 選場次（命中該列文字優先，否則第一個）。"""
    if not perfs:
        return None
    kw = (date_keyword or "").strip()
    if kw:
        matched = [p for p in perfs if kw in p["label"]]
        if matched:
            return matched[0]
    return perfs[0]


def parse_areas(html: str) -> list[dict]:
    """UTK0204 票區頁：<tr class="status_tr" id="{AREA_ID}"> → 票區清單。
    回 [{area_id, name, price, avail(空位數), sold_out}]。"""
    out = []
    for m in re.finditer(r'<tr class="status_tr"[^>]*id="([A-Z0-9]+)"[^>]*>(.*?)</tr>', html, re.S):
        area_id, body = m.group(1), m.group(2)
        tds = [_clean(t) for t in re.findall(r"<td[^>]*>(.*?)</td>", body, re.S)]
        name = tds[1] if len(tds) > 1 else ""
        price = tds[2] if len(tds) > 2 else ""
        avail_raw = tds[3] if len(tds) > 3 else ""
        sold_out = ("售完" in avail_raw) or avail_raw.strip() in ("0", "")
        avail = 0 if sold_out else int(re.sub(r"\D", "", avail_raw) or 0)
        out.append({"area_id": area_id, "name": name, "price": price,
                    "avail": avail, "sold_out": sold_out})
    return out


def select_area(areas: list[dict], *, keyword: str, exclude: str) -> dict | None:
    """依 config 選票區：只留有空位的；exclude 硬排除；keyword 命中優先。"""
    pool = [a for a in areas if not a["sold_out"] and a["avail"] > 0]
    for ek in [e.strip() for e in (exclude or "").split(";") if e.strip()]:
        pool = [a for a in pool if ek not in a["name"] and ek not in a["price"]]
    if not pool:
        return None
    kw = (keyword or "").strip().casefold()
    if kw:
        matched = [a for a in pool if kw in a["name"].casefold() or kw in a["price"].casefold()]
        if matched:
            return matched[0]
    return pool[0]


def parse_hidden_inputs(html: str, names: list[str]) -> dict:
    """抓指定 id 的 <input> value（送單 POST 用）。找不到回空字串。"""
    out = {}
    for name in names:
        m = re.search(rf'<input[^>]*id="{re.escape(name)}"[^>]*>', html)
        val = ""
        if m:
            v = re.search(r'value="([^"]*)"', m.group(0))
            val = v.group(1) if v else ""
        out[name] = val
    return out


def _type_name_price(type_z: str) -> tuple[str, str]:
    """'原價-NT$3,280' → ('原價', '3,280')（對齊 addShoppingCart 的 Z.split('-')[0] / Z.split('$')[1]）。"""
    name = type_z.split("-")[0] if type_z else ""
    price = type_z.split("$")[1] if "$" in type_z else ""
    return name, price


def parse_ticket_types(html: str) -> list[dict]:
    """從 setType('P17IVGYH','原價-NT$3,280') 抓票種。回 [{type_id, z, name, price}]。"""
    out = []
    seen = set()
    for m in re.finditer(r"setType\('([^']+)','([^']+)'\)", html):
        tid, z = m.group(1), m.group(2)
        if tid in seen:
            continue
        seen.add(tid)
        name, price = _type_name_price(z)
        out.append({"type_id": tid, "z": z, "name": name, "price": price})
    return out


def select_ticket_type(types: list[dict], *, keyword: str, exclude: str) -> dict | None:
    """依 config 挑票種：exclude 分號分隔硬排除；keyword 命中優先，否則回第一個。"""
    pool = list(types)
    for ek in [e.strip() for e in (exclude or "").split(";") if e.strip()]:
        pool = [t for t in pool if ek not in t["name"] and ek not in t["z"]]
    if not pool:
        return None
    kw = (keyword or "").strip().casefold()
    if kw:
        matched = [t for t in pool if kw in t["name"].casefold() or kw in t["z"].casefold()]
        if matched:
            return matched[0]
    return pool[0]


def pick_available_seats(seats: dict, amount: int) -> list[dict]:
    """seats: {key: {"S":"0","I":"1樓特A區-10排-11號",...}}（瀏覽器 evaluate 讀來的）。
    S=="0"(字串) 且有座位 id = 空位可選。回最多 amount 個，保持原順序。
    ⚠️ 寬宏 S 是字串 "0"，不是整數；用 str() 正規化相容兩者。"""
    avail = [s for s in seats.values()
             if isinstance(s, dict) and str(s.get("S")) == "0" and s.get("I")]
    return avail[:max(1, amount)]


def build_add_cart_seats(picked_seats: list[dict], type_id: str, type_z: str) -> str:
    """組 addShoppingCart 的 SEATS 參數（未 URL-encode 的 JSON 字串）。
    對齊 JS：每格 {TYPE_ID, TYPE_NAME=Z前半, PRICE=Z的$後, SEAT=座位id}。"""
    name, price = _type_name_price(type_z)
    arr = [{"TYPE_ID": type_id, "TYPE_NAME": name, "PRICE": price, "SEAT": s["I"]}
           for s in picked_seats]
    return json.dumps(arr, ensure_ascii=False)
