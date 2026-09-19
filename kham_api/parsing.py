"""寬宏 HTML/JS 解析（純函式）。「寬宏頁面長什麼樣」的知識集中這檔。

涵蓋：
  - 任何頁：logged_in
  - 選日期頁 UTK0201_00：product_id_from / parse_performance_links / select_performance
  - 票區頁 UTK0204 / UTK0201_000：parse_areas / select_area / seat_page_url
  - 選位頁 UTK0205：parse_seat_page（hidden 欄位 + 票種 + seatStr + _info）
                    select_ticket_type / pick_seats / build_add_cart_seats / build_info

座位資料來源：選位頁 inline script 的 `seatStr`（UTK0205.min.js 的 page_init 用它建 `seats`）：
  以 tab 分組，每組 `方向:狀態:x,y,K,座位名.x,y,K,座位名...`
  - 方向 = 舞台在哪一側（上/下/左/右，空字串 = 上）。**左/右時 x 軸才是「排」**（小巨蛋側區實測）
  - 狀態 = statAry 的 index：0 空位 / 1 已售出 / 2 已選 / 3 booked / 4 在購物車。只有 0 能買
  - x,y = 格子座標（x 欄 / y 列）；走道 = 格子上沒有座位的空欄
**座位號不能拿來判斷相鄰**：寬宏是台灣標準編號，中間 1/2 號、往兩側單雙各自遞增
（同排相鄰的兩個位子號碼差 2，走道兩邊還可能是 2 跟 1），只能看格子座標。
"""
import re
import json
import html as _html


def _clean(x: str) -> str:
    """去 tag、解 HTML entity（票區名常帶 &nbsp;）、壓空白。"""
    text = _html.unescape(re.sub(r"<[^>]+>", " ", x or ""))
    return re.sub(r"\s+", " ", text).strip()


def _keyword_groups(keyword: str) -> list[list[str]]:
    """跟拓元同一套語法：`;` 分隔 = 優先順序（OR），`+` 分隔 = 同時要含（AND）。"""
    groups = []
    for kw in (keyword or "").split(";"):
        subs = [s.strip().casefold() for s in kw.split("+") if s.strip()]
        if subs:
            groups.append(subs)
    return groups


def _excludes(exclude: str) -> list[str]:
    return [e.strip() for e in (exclude or "").split(";") if e.strip()]


def logged_in(html: str) -> bool | None:
    """每頁頁首都有 `var _ul = '0'`（未登入）；選位頁的 addShoppingCart 就是看它決定要不要
    逼你當場填帳密。只有明確是 '0' 才算沒登入（登入後的值沒實測過，寧可放行也不要誤判中止）。
    不是完整頁面、找不到這個變數回 None。"""
    m = re.search(r"\bvar\s+_ul\s*=\s*'([^']*)'", html or "")
    if not m:
        return None
    return m.group(1) != "0"


def product_id_from(slug: str) -> str:
    """ACTIVITY_SLUG 可以填 PRODUCT_ID 本身，也可以直接貼活動網址（取 PRODUCT_ID= 那段）。
    貼到沒有 PRODUCT_ID 的網址（例如套票頁 UTK0201_040.aspx?AGID=…）回空字串，
    讓呼叫端擋下來 —— 直接把整串網址當代碼送出去只會得到看不懂的錯誤。"""
    m = re.search(r"PRODUCT_ID=([A-Za-z0-9]+)", slug or "")
    if m:
        return m.group(1).upper()
    slug = (slug or "").strip()
    return "" if ("/" in slug or "?" in slug) else slug.upper()


# 選日期頁「立即訂購」會連去的頁 → 種類。
#   UTK0204 / UTK0201_000 都是「選票區」頁，表格一樣（後者多了自行選位 / 電腦配位分頁，
#   預設自行選位，點票區一樣跳 UTK0205）
#   UTK0202 是不劃位的「輸入張數」頁（音樂節、自由入場），另一套送單流程，還沒做
_PERF_PAGES = {"UTK0204_": "area", "UTK0201_000": "area", "UTK0202_": "quantity"}


def parse_performance_links(html: str) -> list[dict]:
    """UTK0201_00 選日期頁：每個『立即訂購』→ 票區頁 / 輸入張數頁。
    回 [{performance_id, product_id, url, kind, label(該列文字，含日期/票價)}]，去重保序。
    kind：area = 選票區（支援）/ quantity = 不劃位輸入張數（不支援）。"""
    out, seen = [], set()
    row_re = re.compile(
        r"<tr[^>]*>((?:(?!</tr>).)*?(UTK0204_|UTK0201_000|UTK0202_)\.aspx\?PERFORMANCE_ID=([A-Z0-9]+)"
        r"&(?:amp;)?PRODUCT_ID=([A-Z0-9]+)(?:(?!</tr>).)*?)</tr>", re.S)
    for m in row_re.finditer(html):
        page, pid, prod = m.group(2), m.group(3), m.group(4)
        if pid in seen:
            continue
        seen.add(pid)
        out.append({"performance_id": pid, "product_id": prod,
                    "url": f"{page}.aspx?PERFORMANCE_ID={pid}&PRODUCT_ID={prod}",
                    "kind": _PERF_PAGES[page],
                    "label": _clean(m.group(1))[:80]})
    return out


def select_performance(perfs: list[dict], date_keyword: str, exclude: str = "") -> dict | None:
    """依 DATE_KEYWORD 選場次（命中該列文字優先，否則第一個）。
    exclude 也套在場次上：同一天常另開「【輪椅場】」，日期關鍵字會兩場都命中。"""
    for ek in _excludes(exclude):
        perfs = [p for p in perfs if ek not in p["label"]]
    if not perfs:
        return None
    kw = (date_keyword or "").strip()
    if kw:
        matched = [p for p in perfs if kw in p["label"]]
        if matched:
            return matched[0]
    return perfs[0]


def parse_areas(html: str) -> list[dict]:
    """UTK0204 票區頁：<tr class="status_tr[ Soldout]" rel="a12 a13" id="{AREA_ID}"> → 票區清單。
    回 [{area_id, name, price, avail(空位數), sold_out, groups}]。

    groups 來自 rel（`aN` → N），就是座位圖 `Send('0205', 場次, 票區, N)` 的 GROUP_ID。
    一個票區可能對應多個 group，但實測帶哪個拿到的座位資料都一樣（亂填才回空的）。"""
    out = []
    for m in re.finditer(r'<tr class="status_tr([^"]*)"([^>]*)>(.*?)</tr>', html, re.S):
        cls, attrs, body = m.group(1), m.group(2), m.group(3)
        id_m = re.search(r'\bid="([A-Z0-9]+)"', attrs)
        if not id_m:
            continue
        rel_m = re.search(r'\brel="([^"]*)"', attrs)
        groups = [g[1:] for g in (rel_m.group(1).split() if rel_m else []) if g.startswith("a")]
        tds = [_clean(t) for t in re.findall(r"<td[^>]*>(.*?)</td>", body, re.S)]
        name = tds[1] if len(tds) > 1 else ""
        price = tds[2] if len(tds) > 2 else ""
        avail_raw = tds[3] if len(tds) > 3 else ""
        sold_out = ("Soldout" in cls or "售完" in avail_raw
                    or avail_raw.strip() in ("0", ""))
        avail = 0 if sold_out else int(re.sub(r"\D", "", avail_raw) or 0)
        out.append({"area_id": id_m.group(1), "name": name, "price": price,
                    "avail": avail, "sold_out": sold_out, "groups": groups})
    return out


def select_area(areas: list[dict], *, keyword: str, exclude: str,
                amount: int = 1, strict: bool = False) -> dict | None:
    """依 config 選票區。

    - exclude（`;` 分隔）硬排除，比對票區名與票價
    - keyword 依優先序找第一組有命中的；同一組裡剩餘 >= amount 的優先
      （剩 1 張的區送 2 張只會被打回，白燒一輪驗證碼）
    - 全沒命中：strict=True 回 None（嚴格清票），否則退回有票的第一區
    """
    pool = [a for a in areas if not a["sold_out"] and a["avail"] > 0]
    for ek in _excludes(exclude):
        pool = [a for a in pool if ek not in a["name"] and ek not in a["price"]]
    if not pool:
        return None

    def _best(cands):
        enough = [a for a in cands if a["avail"] >= amount]
        return (enough or cands)[0]

    for subs in _keyword_groups(keyword):
        matched = [a for a in pool
                   if all(s in a["name"].casefold() or s in a["price"].casefold() for s in subs)]
        if matched:
            return _best(matched)
    if strict and _keyword_groups(keyword):
        return None
    return _best(pool)


def seat_page_url(performance_id: str, area: dict) -> str:
    """組選位頁網址（對齊票區頁的 Send()），不用等座位圖 .map 非同步載完再去點。"""
    group = area["groups"][0] if area.get("groups") else "0"
    return (f"UTK0205_.aspx?PERFORMANCE_ID={performance_id}"
            f"&GROUP_ID={group}&PERFORMANCE_PRICE_AREA_ID={area['area_id']}")


def parse_hidden_inputs(html: str, names: list[str]) -> dict:
    """抓指定 id 的 <input> value（送單 POST 用，已解 HTML entity）。找不到回空字串。"""
    out = {}
    for name in names:
        m = re.search(rf'<input[^>]*id="{re.escape(name)}"[^>]*>', html)
        val = ""
        if m:
            v = re.search(r'value="([^"]*)"', m.group(0))
            val = _html.unescape(v.group(1)) if v else ""
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
    """挑票種：exclude 硬排除（預設的「身障;身心;輪椅」會把身障票/陪同票刷掉）；
    keyword 有任一組命中票種名就用它，否則回第一個（寬宏第一個一律是原價）。"""
    pool = list(types)
    for ek in _excludes(exclude):
        pool = [t for t in pool if ek not in t["name"] and ek not in t["z"]]
    if not pool:
        return None
    for subs in _keyword_groups(keyword):
        matched = [t for t in pool if all(s in t["z"].casefold() for s in subs)]
        if matched:
            return matched[0]
    return pool[0]


SEAT_FIELD_IDS = [
    "PERFORMANCE_ID", "PRODUCT_ID", "PRODUCT_CATEGORY_ID", "GROUP_ID", "PLACE_ID",
    "PRICE_AREA_ID", "LOGIN_ID", "ACTIVITY_GROUP_ID", "ACTIVITY_GROUP_ITEM_ID",
    "QUANTITY_LIMIT", "NOT_MIX", "CLOSE_3D", "isMarketAmerica", "NO_SELL_ON_WEBSITE",
    "IS_NAME_BASED", "DOC_MEMO",
]


def parse_seat_str(seat_str: str) -> list[dict]:
    """seatStr → [{x, y, status, dir, label}]（格式見檔頭）。壞掉的格子直接跳過。"""
    out = []
    for grp in (seat_str or "").split("\t"):
        parts = grp.split(":", 2)
        if len(parts) != 3:
            continue
        direction, status, cells = parts[0] or "上", parts[1], parts[2]
        for cell in cells.split("."):
            f = cell.split(",")
            if len(f) < 4 or not f[0].isdigit() or not f[1].isdigit():
                continue
            out.append({"x": int(f[0]), "y": int(f[1]), "status": status,
                        "dir": direction, "label": f[3]})
    return out


def parse_seat_page(html: str) -> dict:
    """UTK0205 選位頁 → 送單需要的全部東西。
    回 {fields, types, seats, loadtime, max_wait_ms}。

    - loadtime：`var _info = {'loadtime':'...'}`，送單時要原樣放進 `_INFO`
    - max_wait_ms：`maxWaitTime = N`，官方 addShoppingCart 會 setTimeout 這麼久才 POST
      （平常是 0，推測是熱門場伺服器端拿來節流的開關）"""
    ss = re.search(r"\bseatStr\s*=\s*'([^']*)'", html)
    lt = re.search(r"_info\s*=\s*\{\s*'loadtime'\s*:\s*'([^']*)'", html)
    mw = re.search(r"\bmaxWaitTime\s*=\s*(\d+)", html)
    return {
        "fields": parse_hidden_inputs(html, SEAT_FIELD_IDS),
        "types": parse_ticket_types(html),
        "seats": parse_seat_str(ss.group(1) if ss else ""),
        "loadtime": lt.group(1) if lt else "",
        "max_wait_ms": int(mw.group(1)) if mw else 0,
    }


# 方向 → (沿著排走的座標, 前後排的座標, 舞台在前後座標的小端=+1 / 大端=-1)
_AXES = {"上": ("x", "y", 1), "下": ("x", "y", -1), "左": ("y", "x", 1), "右": ("y", "x", -1)}
# 「同排」最多容忍幾格空隙：走道實測 2 格寬（Tera、小巨蛋平面特1區），再多 1 格容一個已售位子。
# 超過就跟散位沒兩樣，不該因為同排就壓過前面幾排的好位子
_MAX_ROW_GAP = 3


def pick_seats(seats: list[dict], amount: int, *, exclude: str = "") -> tuple[list[dict], str]:
    """從 parse_seat_str 的結果挑 amount 個空位。回 (座位們, 挑法)。

    三段式，前一段挑得到就不看下一段：
      1. 連號   同一排、格子相鄰（中間沒有走道）的 N 個
      2. 同排   同一排、中間最多隔 _MAX_ROW_GAP 格（走道 / 已售位子）
      3. 散位   不管排，各自挑最好的
    同一段裡比「排」（離舞台近的優先）→ 跨度 →「離中線多遠」（同排靠中間的優先）。
    排跟中線都用格子座標算：排數字/座位號在各場館的編法不一致，格子不會騙人。
    座位數不夠 amount 就能給幾個給幾個（要不要買不足張數由呼叫端決定）。
    """
    amount = max(1, amount)
    excludes = _excludes(exclude)
    best = None  # (key, seats, 挑法)

    for direction in dict.fromkeys(s["dir"] for s in seats):  # 保序：平手時結果要固定
        along, depth, sign = _AXES.get(direction, _AXES["上"])
        group = [s for s in seats if s["dir"] == direction]
        # 中線 / 前排基準用「整區所有座位」算，不能只看空位，不然會被剩下的位子帶偏
        a_vals = [s[along] for s in group]
        d_vals = [s[depth] for s in group]
        center = (min(a_vals) + max(a_vals)) / 2
        front = min(d_vals) if sign > 0 else max(d_vals)

        def row_rank(s):
            return (s[depth] - front) * sign

        def off_center(run):
            return abs((run[0][along] + run[-1][along]) / 2 - center)

        free = [s for s in group
                if s["status"] == "0" and s["label"]
                and not any(ek in s["label"] for ek in excludes)]
        rows: dict[int, list[dict]] = {}
        for s in free:
            rows.setdefault(s[depth], []).append(s)

        cands = []
        for line in rows.values():
            line.sort(key=lambda s: s[along])
            rank = row_rank(line[0])
            for i in range(len(line) - amount + 1):
                win = line[i:i + amount]
                gap = (win[-1][along] - win[0][along]) - (amount - 1)
                if gap > _MAX_ROW_GAP:
                    continue
                cands.append(((0 if gap == 0 else 1, rank, gap, off_center(win), win[0][along]), win))
        # 散位永遠算一份，靠 key 第一位自然排在連號/同排後面
        scattered = sorted(free, key=lambda s: (row_rank(s), off_center([s]), s[along]))[:amount]
        if scattered:
            cands.append(((2, row_rank(scattered[0]), 0, 0.0, 0), scattered))

        for key, win in cands:
            if best is None or key < best[0]:
                best = (key, win, ("連號", "同排", "散位")[key[0]])

    if best is None:
        return [], ""
    return list(best[1]), best[2]


def build_add_cart_seats(picked_seats: list[dict], type_id: str, type_z: str) -> str:
    """組 addShoppingCart 的 SEATS 參數（未 URL-encode 的 JSON 字串）。
    對齊 JS：每格 {TYPE_ID, TYPE_NAME=Z前半, PRICE=Z的$後, SEAT=座位名}。
    JSON.stringify 不加空白，這裡也要壓掉，送出去才會跟瀏覽器逐字相同。"""
    name, price = _type_name_price(type_z)
    arr = [{"TYPE_ID": type_id, "TYPE_NAME": name, "PRICE": price, "SEAT": s["label"]}
           for s in picked_seats]
    return json.dumps(arr, ensure_ascii=False, separators=(",", ":"))


def build_info(loadtime: str, timestamp_ms: int) -> str:
    """組 `_INFO`：官方是 `_info.timestamp = Date.now()` 後 JSON.stringify(_info)。"""
    return json.dumps({"loadtime": loadtime, "timestamp": timestamp_ms},
                      ensure_ascii=False, separators=(",", ":"))
