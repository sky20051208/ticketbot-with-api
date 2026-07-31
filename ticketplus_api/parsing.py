"""純資料挑選邏輯 —— 零 HTTP、零 config，「TicketPlus 的資料長什麼樣」的知識全集中這檔。

資料形狀（來自 [catalog.py](ticketplus_api/catalog.py)）：
  session      {sessionId(加密), name, date, time, location, ticketArea: bool}
  ticketArea   {ticketAreaId: "a000008654", sessionId(加密), name, price, sortedIndex}
  product      {productId: "p000015682", ticketAreaId, sessionId(加密), name, price}
  即時票況     product/ticketArea 各多出 status / count / purchaseLimit / productLimit …
               （注意即時票況那邊 ticketArea 的 id 欄位叫 `id` 不叫 `ticketAreaId`）

「有沒有票」的判斷跟前端一致：status 要 onsale，而 count 只有在對應的 *Limit 旗標
為 true 時才算數（旗標 false = 不限量，count 會是 0 或 999999 都不代表售完）。
"""
import random


def select_session(sessions: list[dict], date_keyword: str = "") -> dict | None:
    """依 DATE_KEYWORD 挑場次（比對日期 / 時間 / 名稱 / 場地）。空關鍵字 = 第一場。"""
    visible = [s for s in sessions if not s.get("hidden")]
    visible.sort(key=lambda s: s.get("sortedIndex") or 0)
    if not visible:
        return None
    kw = (date_keyword or "").strip()
    if not kw:
        return visible[0]
    for s in visible:
        haystack = " ".join(str(s.get(k, "")) for k in ("date", "time", "name", "location"))
        if kw in haystack:
            return s
    print(f"[PARSE] DATE_KEYWORD '{kw}' 無匹配，改用第一場")
    return visible[0]


def areas_of_session(ticket_areas: list[dict], session_id: str) -> list[dict]:
    """該場次的票區，依 sortedIndex（票價由高到低就是官方的排序）排好。"""
    areas = [a for a in ticket_areas
             if a.get("sessionId") == session_id and not a.get("hidden")]
    areas.sort(key=lambda a: a.get("sortedIndex") or 0)
    return areas


def products_of_area(products: list[dict], area_id: str) -> list[dict]:
    """該票區底下的票種（全票 / 身障票 / 陪同票…），依 sortedIndex 排好。"""
    items = [p for p in products
             if p.get("ticketAreaId") == area_id and not p.get("hidden")]
    items.sort(key=lambda p: p.get("sortedIndex") or 0)
    return items


def _rank(items: list[dict], keyword: str, exclude: str, strategy: str,
          unit: str) -> list[dict]:
    """把 items 排成「想要的順序」（不是只回一個 —— 首選沒票就換下一個）。
    票區跟票種共用這套：兩者都有 name / price 可以比對。

    keyword 語法跟拓元那邊一致：`;` 分隔 = OR 依序嘗試，`+` 分隔 = AND 同時要含。
    strategy: 關鍵字優先 / 由上而下 / 由下而上 / 隨機。
    """
    pool = list(items)
    if exclude:
        excludes = [e.strip() for e in exclude.split(";") if e.strip()]
        before = len(pool)
        pool = [i for i in pool
                if not any(e in str(i.get("name", "")) for e in excludes)]
        if len(pool) < before:
            print(f"[PARSE] 排除 {before - len(pool)} 個{unit}（排除詞: {', '.join(excludes[:3])}…）")
    if not pool:
        return []

    if strategy == "由下而上":
        return list(reversed(pool))
    if strategy == "隨機":
        random.shuffle(pool)
        return pool
    if strategy == "關鍵字優先" and keyword:
        ranked, rest = [], list(pool)
        for kw in [k.strip() for k in keyword.split(";") if k.strip()]:
            subs = [s.strip() for s in kw.split("+") if s.strip()]
            if not subs:
                continue
            hit = [i for i in rest
                   if all(s in f"{i.get('name', '')} {i.get('price', '')}" for s in subs)]
            if hit:
                print(f"[PARSE] 關鍵字 '{' AND '.join(subs)}' 命中 {len(hit)} 個{unit}")
            ranked += hit
            rest = [i for i in rest if i not in hit]
        if not ranked:
            print(f"[PARSE] 關鍵字 '{keyword}' 全無匹配，改用預設順序")
        return ranked + rest
    return pool


def rank_areas(areas: list[dict], keyword: str = "", exclude: str = "",
               strategy: str = "") -> list[dict]:
    """票區優先序。"""
    return _rank(areas, keyword, exclude, strategy, "票區")


def rank_products(products: list[dict], keyword: str = "", exclude: str = "",
                  strategy: str = "") -> list[dict]:
    """票種優先序（無票區活動用，關鍵字直接比對票種名稱/票價）。"""
    return _rank(products, keyword, exclude, strategy, "票種")


def rank_targets(products: list[dict], areas: list[dict], keyword: str = "",
                 exclude: str = "", strategy: str = "") -> list[tuple[dict | None, dict]]:
    """回「候選票種」的扁平優先序 [(票區 or None, 票種), …]。

    TicketPlus 有兩種活動，**票種掛的位置完全不同**（2026-07-29 踩到）：
      有劃位 `session.ticketArea=True`  → ticketAreas.json 有內容，票種帶 ticketAreaId，
                                          關鍵字比對票區名 → 再展開該區底下的票種
      無劃位 `session.ticketArea=False` → **ticketAreas.json 是空陣列**，票種身上根本
                                          沒有 ticketAreaId 欄位，關鍵字直接比對票種名

    所以不能假設「票種一定掛在票區底下」，否則無劃位的活動會被判成沒票。
    """
    if areas:
        ranked = rank_areas(areas, keyword, exclude, strategy)
        return [(area, product)
                for area in ranked
                for product in products_of_area(products, area["ticketAreaId"])]
    return [(None, p) for p in rank_products(products, keyword, exclude, strategy)]


def target_label(area: dict | None, product: dict) -> str:
    """(票區, 票種) → log 用的一行標籤。"""
    name = str(product.get("name", ""))
    if area:
        return f"{area.get('name')}/{name}"
    return name


def _limited_out(item: dict, limit_key: str) -> bool:
    """有開數量限制且剩餘為 0 → 售完。"""
    return bool(item.get(limit_key)) and not (item.get("count") or 0)


def area_ready(info: dict) -> bool:
    """即時票況裡這個票區可不可以買。"""
    return info.get("status") == "onsale" and not _limited_out(info, "ticketAreaLimit")


def product_ready(info: dict) -> bool:
    """即時票況裡這個票種可不可以買。"""
    return info.get("status") == "onsale" and not _limited_out(info, "productLimit")


def sale_open(product_infos: dict) -> bool:
    """這場是否已開賣（即時票況裡有任何票種 status=onsale）。用來分辨清票節奏：
      有 onsale → 開賣了（我的目標票可能剛售完/被搶走）→ 走清票冷卻等回流
      全無 onsale → 還沒開賣 → 全速輪詢等 T-0（第一拍黃金時間不冷卻）"""
    return any((i or {}).get("status") == "onsale" for i in product_infos.values())


def index_infos(result: dict) -> tuple[dict, dict]:
    """即時票況 → ({productId: info}, {ticketAreaId: info})。
    坑：ticketArea 的 id 欄位在這支 API 叫 `id`，跟 S3 目錄的 `ticketAreaId` 不同名。"""
    products = {p.get("id"): p for p in (result.get("product") or [])}
    areas = {a.get("id"): a for a in (result.get("ticketArea") or [])}
    return products, areas


def pick_target(targets: list[tuple[dict | None, dict]],
                product_infos: dict, area_infos: dict,
                amount: int, exclude: str = "") -> tuple[dict | None, dict, int] | None:
    """從 `rank_targets` 的優先序裡挑第一個「真的買得到」的票種。

    回 (area or None, product, count) 或 None。count 會依 purchaseLimit 夾到上限
    （超買必被打回，寧可少買也不要整發作廢）。
    """
    excludes = [e.strip() for e in (exclude or "").split(";") if e.strip()]
    for area, product in targets:
        if area is not None:
            area_info = area_infos.get(area.get("ticketAreaId"))
            if area_info is not None and not area_ready(area_info):
                continue
        if any(e in str(product.get("name", "")) for e in excludes):
            continue
        info = product_infos.get(product.get("productId"))
        if not info or not product_ready(info):
            continue
        limit = info.get("purchaseLimit") or 0
        count = min(amount, limit) if limit else amount
        if limit and count < amount:
            print(f"[PARSE] {target_label(area, product)} 每單上限 {limit} 張，"
                  f"{amount} → {count}")
        if count <= 0:
            continue
        return area, product, count
    return None
