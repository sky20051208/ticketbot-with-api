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

# ── 寬鬆清票的兩個旋鈕（見 score_target）────────────────────────────────
# 名次每往後一位，想要程度打幾折。0.85 → 第 5 志願剩 0.44、第 10 志願剩 0.20，
# 排到很後面的爛位置自然趨近 0（大巨蛋那種 158 個票區也不會亂跳）。
RANK_DECAY = 0.85
# 「剩幾張才算不稀缺」。機率分 = count/(count+HALF)，HALF=4 時剩 4 張剛好 0.5。
# 調小 → 更看重位置；調大 → 更看重數量。
SUPPLY_HALF = 4.0
# **同價位**內，官方排序（sortedIndex，通常就是離舞台遠近）每往後一位打的折。
# 刻意設得很弱：場館把這些區定成同一個價，本身就代表它們差不多 —— 這只是拿來破同分，
# 不該蓋過真實的張數差距。設 1.0 = 同價位完全不分先後（純看張數）。
TIER_DECAY = 0.97
# 超出目標價位的罰則指數。比目標便宜是線性折（位置差而已），比目標貴則是 (目標/價)^N ——
# 買貴了客人可能不認那個差額、變成自己吃掉，比坐差一點嚴重。
# **設成 1.0 就是「貴便宜一視同仁、只看偏離幅度」**。
PRICE_OVER_PENALTY = 3.0


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


def _keyword_groups(keyword: str) -> list[list[str]]:
    """關鍵字語法（跟拓元一致）：`;` 分隔 = OR 依序嘗試，`+` 分隔 = AND 同時要含。"""
    groups = []
    for kw in [k.strip() for k in (keyword or "").split(";") if k.strip()]:
        subs = [s.strip() for s in kw.split("+") if s.strip()]
        if subs:
            groups.append(subs)
    return groups


def _matches(item: dict, subs: list[str]) -> bool:
    haystack = f"{item.get('name', '')} {item.get('price', '')}"
    return all(s in haystack for s in subs)


def _price(item: dict) -> float | None:
    try:
        v = float(item.get("price"))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def target_price(keyword: str, units: list[dict]) -> float | None:
    """從 AREA_KEYWORD 認出「目標價位」—— 使用者的習慣是**把價位打在最後一順位**
    （例：`搖滾A;搖滾B;6800`）。回 None = 沒打價位，一切照舊。

    只認純數字的那一組（`綠517`、`G05` 這種帶字母的不算），有多組就取最後一組。
    還會拿這場真實的價格區間做健檢：離譜的數字（多半是把區號當價位）直接不採用，
    免得把「517 排」誤讀成 517 元。
    """
    nums = [float(s) for subs in _keyword_groups(keyword) for s in subs
            if s.isdigit() and 100 <= float(s) <= 100000]
    if not nums:
        return None
    guess = nums[-1]
    prices = [p for p in (_price(u) for u in units) if p]
    if not prices:
        return guess
    lo, hi = min(prices) * 0.5, max(prices) * 2
    if not lo <= guess <= hi:
        print(f"[PARSE] 關鍵字裡的 {guess:.0f} 不在這場的票價範圍 "
              f"({min(prices):.0f}~{max(prices):.0f})，**不當成目標價位**（當一般關鍵字用）")
        return None
    return guess


def price_affinity(price: float | None, target: float | None) -> float:
    """票價跟目標價位的契合度，0~1。目標價位本身 = 1.0。

    刻意不對稱（見 PRICE_OVER_PENALTY）：
        目標 6800 →  6500: 0.96   3800: 0.56   1800: 0.26
                     8800: 0.46  12000: 0.18   ← 同樣偏離幅度，貴的罰更重
    """
    if not target or not price:
        return 1.0
    ratio = price / target
    if ratio <= 1.0:
        return ratio                                   # 便宜：位置差而已，線性折
    return (1.0 / ratio) ** PRICE_OVER_PENALTY         # 超價：罰重


def keyword_hit_keys(products: list[dict], areas: list[dict], keyword: str = "",
                     exclude: str = "") -> set:
    """哪些「票區（或無劃位活動的票種）」是關鍵字命中的 → 回它們的 key。

    key 跟 `_area_ranks` 是同一套，所以 `pick_target` 能用它分辨
    「這個是使用者明講要的」還是「只是備胎」——兩者的想要程度算法不同。
    """
    units = areas if areas else products
    id_key = "ticketAreaId" if areas else "productId"
    excludes = [e.strip() for e in (exclude or "").split(";") if e.strip()]
    groups = _keyword_groups(keyword)
    if not groups:
        return set()
    return {u[id_key] for u in units
            if not any(e in str(u.get("name", "")) for e in excludes)
            and any(_matches(u, subs) for subs in groups)}


def desirability(rank_index: int, matched: bool, n_matched: int,
                 price: float | None, target: float | None,
                 tier_rank: int = 0) -> float:
    """「想要程度」。命中關鍵字的照使用者排的順序；**沒中的改用價位契合度**。

    為什麼要分開算：關鍵字全沒中時，`rest` 是照官方 `sortedIndex` 排的 —— 那個順序
    跟使用者的意圖沒有關係，名次衰減套上去只是在「隨便一個順序」上加權。價位不一樣，
    它是每個票區都有、而且連續的訊號，使用者打的價位就是他真正的目標。

    沒中的一律先退到「命中的那幾個後面」當同一階（`RANK_DECAY^n_matched`），
    再由價位契合度連續拉開差距。效果（2 個命中、目標價位 6800）：
        命中第 1 順位              1.00
        命中第 2 順位              0.85
        沒中但剛好 6800            0.72   ← 舊做法若它排第 50，只有 0.0003，等於永遠選不到
        沒中且 3800                0.40
        沒中且 12000               0.13
    沒打價位（target=None）時退回原本的 `RANK_DECAY^名次`，行為完全不變。

    `tier_rank` = **同價位內**的官方排序名次，用來破同分：同一個價位的票區價位契合度
    完全一樣，少了這一項的話「藍1A~藍1E」想要度會一模一樣，誰被選中就 100% 由張數決定，
    結果常常挑到同價位裡位置最差的那一區。用 TIER_DECAY 而不是 RANK_DECAY 是因為
    這是弱訊號（同價 = 場館認為它們差不多），不該蓋過真實的張數差距。
    """
    if matched or target is None:
        return RANK_DECAY ** rank_index
    return ((RANK_DECAY ** n_matched) * price_affinity(price, target)
            * (TIER_DECAY ** tier_rank))


def _rank(items: list[dict], keyword: str, exclude: str, strategy: str,
          unit: str, strict: bool = False) -> list[dict]:
    """把 items 排成「想要的順序」（不是只回一個 —— 首選沒票就換下一個）。
    票區跟票種共用這套：兩者都有 name / price 可以比對。

    strategy: 關鍵字優先 / 由上而下 / 由下而上 / 隨機。

    `strict=True`（嚴格清票）= **只留關鍵字命中的**，其餘直接丟掉，不當備胎。
    寬鬆模式則是命中的排前面、沒中的仍留在後面當備胎（原本的行為）。
    嚴格模式沒填關鍵字會退回寬鬆並警告 —— 沒有關鍵字的「嚴格」等於全部排除，
    那只會讓 bot 空轉整場，絕不是使用者要的。
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

    groups = _keyword_groups(keyword)
    if strict and not groups:
        print("[PARSE] ⚠ 嚴格清票沒填關鍵字（等於全部排除），自動改用寬鬆")
        strict = False

    if strict:
        # 嚴格：只留命中的，照 `;` 的先後排；沒中的整包丟掉，不當備胎
        ranked, rest = [], list(pool)
        for subs in groups:
            hit = [i for i in rest if _matches(i, subs)]
            if hit:
                print(f"[PARSE] 關鍵字 '{' AND '.join(subs)}' 命中 {len(hit)} 個{unit}")
            ranked += hit
            rest = [i for i in rest if i not in hit]
        if not ranked:
            print(f"[PARSE] ⚠ 嚴格清票：關鍵字 '{keyword}' 一個{unit}都沒命中 —— "
                  f"這樣會整場買不到，請檢查關鍵字")
        else:
            print(f"[PARSE] 嚴格清票：只買命中的 {len(ranked)} 個{unit}，"
                  f"其餘 {len(rest)} 個不當備胎")
        return ranked

    # ── 以下維持原本寬鬆的行為，一個字都不要動 ───────────────────────
    if strategy == "由下而上":
        return list(reversed(pool))
    if strategy == "隨機":
        random.shuffle(pool)
        return pool
    if strategy == "關鍵字優先" and groups:
        ranked, rest = [], list(pool)
        for subs in groups:
            hit = [i for i in rest if _matches(i, subs)]
            if hit:
                print(f"[PARSE] 關鍵字 '{' AND '.join(subs)}' 命中 {len(hit)} 個{unit}")
            ranked += hit
            rest = [i for i in rest if i not in hit]
        if not ranked:
            print(f"[PARSE] 關鍵字 '{keyword}' 全無匹配，改用預設順序")
        return ranked + rest
    return pool


def rank_areas(areas: list[dict], keyword: str = "", exclude: str = "",
               strategy: str = "", strict: bool = False) -> list[dict]:
    """票區優先序。"""
    return _rank(areas, keyword, exclude, strategy, "票區", strict)


def rank_products(products: list[dict], keyword: str = "", exclude: str = "",
                  strategy: str = "", strict: bool = False) -> list[dict]:
    """票種優先序（無票區活動用，關鍵字直接比對票種名稱/票價）。"""
    return _rank(products, keyword, exclude, strategy, "票種", strict)


def rank_targets(products: list[dict], areas: list[dict], keyword: str = "",
                 exclude: str = "", strategy: str = "",
                 strict: bool = False) -> list[tuple[dict | None, dict]]:
    """回「候選票種」的扁平優先序 [(票區 or None, 票種), …]。

    TicketPlus 有兩種活動，**票種掛的位置完全不同**（2026-07-29 踩到）：
      有劃位 `session.ticketArea=True`  → ticketAreas.json 有內容，票種帶 ticketAreaId，
                                          關鍵字比對票區名 → 再展開該區底下的票種
      無劃位 `session.ticketArea=False` → **ticketAreas.json 是空陣列**，票種身上根本
                                          沒有 ticketAreaId 欄位，關鍵字直接比對票種名

    所以不能假設「票種一定掛在票區底下」，否則無劃位的活動會被判成沒票。
    """
    if areas:
        ranked = rank_areas(areas, keyword, exclude, strategy, strict)
        return [(area, product)
                for area in ranked
                for product in products_of_area(products, area["ticketAreaId"])]
    return [(None, p) for p in rank_products(products, keyword, exclude, strategy, strict)]


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


def _supply(info: dict, limit_key: str) -> int | None:
    """這一層的「還剩幾張」。**沒開限量旗標 = 不限量**，回 None 代表不稀缺。

    旗標 false 時 count 可能是 0 也可能是 999999，兩個都不代表真實庫存
    （這也是 `_limited_out` 只在旗標為 true 時才判售完的原因）。
    """
    if not info.get(limit_key):
        return None
    return int(info.get("count") or 0)


def score_target(desire: float, product_info: dict,
                 area_info: dict | None) -> tuple[float, int | None]:
    """寬鬆清票的期待值分數 = **想要程度 × 搶到機率**。回 (分數, 可見庫存)。

    我們看得到的只有價格、區域、剩餘數量，所以兩項都只能從這裡推：

    想要程度 = `desirability()` 算好傳進來
        命中關鍵字 → 使用者排的名次；沒中 → 跟他打的目標價位有多接近。

    搶到機率 = `count / (count + SUPPLY_HALF)`
        對手人數未知，但**同一場同一刻對每個票區大致相同**，所以機率正比於庫存。
        用飽和曲線而不是線性：1 張變 8 張是天壤之別，50 張變 100 張沒差。
        剩 1 張→0.20、4 張→0.50、20 張→0.83、不限量→1.00。

    效果（SUPPLY_HALF=4）：
        第 1 志願剩 1 張   1.00×0.20 = 0.20
        第 2 志願剩 30 張  0.85×0.88 = 0.75  ← 選這個
        第 6 志願剩 100 張 0.44×0.96 = 0.42  ← 不會為了量大跳到爛位置
        第 1 志願剩 50 張  1.00×0.93 = 0.93  ← 首選有貨就是首選

    票區跟票種各有自己的限量旗標，**取兩者較小的**（真正卡住你的是比較緊的那個）。

    **這套在 T-0 跟清票時會自動切換重心，不需要分兩種邏輯**：T-0 時每區庫存都是幾百幾千，
    機率分全部逼近 1.0 → 名次說了算，等於照使用者排的志願走；清票時庫存只剩個位數，
    機率分才拉開差距 → 這時才真的在「首選 1 張」跟「次選 30 張」之間做取捨。
    SUPPLY_HALF 就是這個切換點，調大會讓它更早開始看數量。
    """
    supplies = [s for s in (_supply(product_info, "productLimit"),
                            _supply(area_info or {}, "ticketAreaLimit"))
                if s is not None]
    supply = min(supplies) if supplies else None
    abundance = 1.0 if supply is None else supply / (supply + SUPPLY_HALF)
    return desire * abundance, supply


def _buyable(area: dict | None, product: dict, product_infos: dict,
             area_infos: dict, amount: int,
             excludes: list[str]) -> tuple[dict, dict | None, int] | None:
    """這個 (票區, 票種) 現在買不買得到？回 (票種即時票況, 票區即時票況, 可買張數)。"""
    area_info = area_infos.get(area.get("ticketAreaId")) if area is not None else None
    if area_info is not None and not area_ready(area_info):
        return None
    if any(e in str(product.get("name", "")) for e in excludes):
        return None
    info = product_infos.get(product.get("productId"))
    if not info or not product_ready(info):
        return None
    limit = info.get("purchaseLimit") or 0
    count = min(amount, limit) if limit else amount
    if count <= 0:
        return None
    return info, area_info, count


def _tier_ranks(targets: list[tuple[dict | None, dict]]) -> dict:
    """{key: 同價位內的名次}。同價位的票區排序（官方 sortedIndex）就是位置好壞，
    但它是弱訊號 —— 場館把它們定成同一個價，本來就代表差不多。只拿來破同分。"""
    seen_per_price, ranks = {}, {}
    for area, product in targets:
        unit = area if area is not None else product
        key = area.get("ticketAreaId") if area is not None else product.get("productId")
        if key in ranks:
            continue
        price = _price(unit)
        ranks[key] = seen_per_price.get(price, 0)
        seen_per_price[price] = ranks[key] + 1
    return ranks


def _area_ranks(targets: list[tuple[dict | None, dict]]) -> dict:
    """{票區 or 票種 key: 名次}。**同一票區底下的票種共用同一個名次** ——
    不然一個票區掛 3 個票種（全票/身障/陪同）就會白白把下一個票區推後 3 名。"""
    ranks = {}
    for area, product in targets:
        key = area.get("ticketAreaId") if area is not None else product.get("productId")
        ranks.setdefault(key, len(ranks))
    return ranks


def pick_target(targets: list[tuple[dict | None, dict]],
                product_infos: dict, area_infos: dict,
                amount: int, exclude: str = "", balanced: bool = False,
                matched_keys: set | None = None,
                target: float | None = None) -> tuple[dict | None, dict, int] | None:
    """挑一個「真的買得到」的票種。回 (area or None, product, count) 或 None。

    `balanced=False`（嚴格清票，或使用者要照自己排的順序）→ **取優先序裡第一個買得到的**。
    `balanced=True`（寬鬆清票）→ 取 `score_target` 分數最高的，也就是在
    「最期待的位置」跟「最可能搶到」之間取平衡。

    `matched_keys` / `target`（目標價位）由 `build_plan` 算好傳進來，用途見
    `desirability()`：關鍵字全沒中時改用價位契合度，而不是在官方排序上硬套名次衰減。

    count 會依 purchaseLimit 夾到上限（超買必被打回，寧可少買也不要整發作廢）。
    """
    excludes = [e.strip() for e in (exclude or "").split(";") if e.strip()]
    matched_keys = matched_keys or set()

    if not balanced:
        for area, product in targets:
            got = _buyable(area, product, product_infos, area_infos, amount, excludes)
            if got is None:
                continue
            _info, _area_info, count = got
            if count < amount:
                print(f"[PARSE] {target_label(area, product)} 每單上限 "
                      f"{_info.get('purchaseLimit')} 張，{amount} → {count}")
            return area, product, count
        return None

    ranks = _area_ranks(targets)
    tiers = _tier_ranks(targets)
    n_matched = len(matched_keys)
    best = None
    for area, product in targets:
        got = _buyable(area, product, product_infos, area_infos, amount, excludes)
        if got is None:
            continue
        info, area_info, count = got
        key = area.get("ticketAreaId") if area is not None else product.get("productId")
        matched = key in matched_keys
        price = _price(area if area is not None else product)
        desire = desirability(ranks[key], matched, n_matched, price, target,
                              tiers[key])
        score, supply = score_target(desire, info, area_info)
        if best is None or score > best[0]:
            best = (score, ranks[key], matched, price, supply, area, product, count)

    if best is None:
        return None
    score, rank_index, matched, price, supply, area, product, count = best
    if count < amount:
        info = product_infos.get(product.get("productId")) or {}
        print(f"[PARSE] {target_label(area, product)} 每單上限 "
              f"{info.get('purchaseLimit')} 張，{amount} → {count}")
    if matched or target is None:
        why = f"第 {rank_index + 1} 志願"
    else:
        why = (f"關鍵字沒中，價位 {price:.0f} vs 目標 {target:.0f}"
               f"（契合度 {price_affinity(price, target):.2f}）")
    print(f"[PARSE] 寬鬆清票選擇: {target_label(area, product)}"
          f"（{why}，剩 {'不限量' if supply is None else f'{supply} 張'}"
          f"，分數 {score:.2f}）")
    return area, product, count
