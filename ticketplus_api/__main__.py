"""遠大 TicketPlus API 模式 entry point — `python -m ticketplus_api --config <path>` 啟動。

流程：
  取 token（貼 cookie 或開瀏覽器登入）→ 抓靜態目錄（場次/票區/票種）→ 倒數
  → 高頻打票況 API 等開賣 → 挑到有票的票區就 enqueue + reserve → 導向訂單頁結帳。

只做串接，搶票步驟在 ticketplus_api 其他檔（catalog / parsing / reserve / session）。

config 對應：
  ACTIVITY_SLUG        活動網址 /activity/ 後面那串（整條網址貼進來也吃）
  DATE_KEYWORD         挑場次
  AREA_KEYWORD / AREA_AUTO_SELECT_MODE / EXCLUDE_AREA_KEYWORD   挑票區、挑票種
  TICKET_AMOUNT        張數（超過官方每單上限會自動夾到上限）
  PRESALE_CODE         有序號的活動填這裡（送單時當 serialNumber）
  COOKIE / COOKIE_SOURCE  token 來源，見下面 _resolve_token
"""
import sys
import time
import json
import asyncio
import argparse

import nodriver as uc

import config
import proxy_pool
from timeWatcher import TimeWatcher, corrected_now
from tixcraftapi import alerts
from LineBot import line_push

from ticketplus_api import catalog, crypto, parsing, reserve as tp_reserve
from ticketplus_api.session import (build_session, describe_token, extract_token,
                                    make_keepalive, token_remaining, warmup_queue,
                                    warmup_session)
from ticketplus_api import browser_session as tp_browser

# 清票冷卻（對齊拓元 FSM，見 tixcraftapi/runner.py）：
#   有票就搶永遠不冷卻（第一拍黃金時間）；開賣但售完/送單被搶走 = 5s（拓元 AREA 重入）；
#   被流量管制/擋（HTTP 429/403 或 errCode 110）= 8s（拓元 BLOCKED，給 server/IP 喘息）。
# 無總時間上限，跑到搶到 / 致命錯誤 / 使用者 STOP。
CLEAR_COOLDOWN = 5.0
BLOCKED_COOLDOWN = 8.0


def load_config_override():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default=None)
    args, _ = parser.parse_known_args()
    if args.config:
        with open(args.config, "r", encoding="utf-8") as f:
            for key, val in json.load(f).items():
                if hasattr(config, key):
                    setattr(config, key, val)
        print(f"[CONFIG] 已載入: {args.config}")
    else:
        print("[CONFIG] 使用預設 config.py")


def _install_log_timestamp():
    import builtins
    _orig = builtins.print
    SKIP = ("[TIMER]",)

    def _stamped(*args, **kwargs):
        if args and isinstance(args[0], str):
            head = args[0].lstrip()
            if head.startswith("[") and not any(head.startswith(p) for p in SKIP):
                # 用對時後的時間，不要用 time.localtime() —— 本機時鐘平常就偏幾百毫秒
                # （2026-08-14 實測這台偏 282ms），而開賣前後的分析全靠這些時間戳，
                # 沒校正的話整份 log 讀起來會比實際早，看不出真正的延遲。
                args = (f"[{corrected_now():%H:%M:%S.%f}"[:-3] + f"] {args[0]}", *args[1:])
        _orig(*args, **kwargs)

    builtins.print = _stamped


def parse_event_id(slug: str) -> str:
    """吃 `ac64…218e` 或整條 `https://ticketplus.com.tw/activity/ac64…218e`。"""
    slug = (slug or "").strip().rstrip("/")
    if "/activity/" in slug:
        slug = slug.split("/activity/", 1)[1].split("/")[0].split("?")[0]
    return slug


async def _resolve_token(event_id: str):
    """回 (token, browser, tab)。browser/tab 只有 userdata 模式才有。

    COOKIE_SOURCE="string"   → 從 config.COOKIE 挖 access_token（整串 cookie / user
                               cookie 的值 / 直接貼 JWT 都吃）
    COOKIE_SOURCE="userdata" → 開 Chrome profile 讓使用者登入，讀 `user` cookie
    """
    if config.COOKIE_SOURCE == "userdata":
        browser, tab, token = await tp_browser.open_and_login(
            config.CHROME_USER_DATA_DIR, event_id, proxy_url=config.CURRENT_PROXY)
        return token, browser, tab

    token = extract_token(config.COOKIE)
    if token:
        remaining = token_remaining(token)
        if remaining is not None and remaining < tp_browser.MIN_TOKEN_LIFE:
            # 貼的 cookie 裡通常連 refresh_token 一起貼進來了，先自動換一張再說
            token = await tp_browser.refresh_via_cookie(None, config.COOKIE) or token
        print(f"[TOKEN] 已從 COOKIE 取得 access_token（{len(token)} 字，{describe_token(token)}）")
    else:
        print("[TOKEN] COOKIE 裡找不到 access_token —— "
              "請在 DevTools → Application → Cookies → ticketplus.com.tw 複製 `user` 這個 cookie")
    return token, None, None


def poll_and_grab(session, plan: dict, token: str, poller,
                  opened_at: float | None = None) -> dict:
    """無限清票直到搶到 / 致命錯誤 / 使用者 STOP。冷卻節奏借鑑拓元 FSM：
      - **未開賣**（沒有任何票種 onsale，等 T-0）→ 全速輪詢（golden，不冷卻）
      - **有可買票** → 立刻送單（golden，永不冷卻，第一拍黃金時間）
      - **開賣但目標票售完/被搶走** → 5s 清票冷卻等回流（per 票種各自冷卻，同輪仍會馬上
        去搶別的票區，只有失敗的那個票種等 5s）
      - **被流量管制/擋**（HTTP 429/403 或 errCode 110）→ 8s 退避（給 server/IP 喘息）

    開賣前的偵測交給 `catalog.InfoPoller`（在 main_async 建好，倒數期間就把連線暖起來）：
    RETRY_INTERVAL 夠小時它會併行送、不等上一發回來，週期才真的等於 interval 而不是
    RTT+interval。翻 onsale 之後改回依序 —— 清票是 5s 一輪，併行只是多打對方。

    回 {"orderId": …} 或 {"error": …, "fatal": True}。"""
    poller.warm()      # 走過倒數的話已經暖好了，這裡是 no-op
    try:
        return _grab_loop(session, poller, plan, token, plan["amount"], opened_at)
    finally:
        poller.close()


def _grab_loop(session, poller, plan: dict, token: str, amount: int,
               opened_at: float | None = None) -> dict:
    """`session` 是**主執行緒**那條（queue 網域已暖），送單一定要用它 ——
    偵測走 poller 的 worker 連線，但 enqueue/reserve 不能借那些 thread。

    `opened_at` = 倒數結束（≈ 開賣）那一刻的 monotonic。**偵測耗時要從這裡算起**，
    不能從本函式進來才起算 —— 2026-08-14 就是因為從這裡起算，把 T-0 前那 2.2 秒的
    排隊連線重建整個藏起來，log 印「開搶後 0.1s 偵測到票」，實際上是開賣後 1.9 秒。
    """
    started = opened_at if opened_at is not None else time.monotonic()
    cooldown: dict[str, float] = {}   # per 票種：售完/被搶走後的清票冷卻
    attempt = 0
    log = _PollLog()

    while True:
        attempt += 1
        result, blocked, rtt = poller.next()
        if blocked:
            log.flush()
            print(f"[POLL] #{attempt} 被流量管制/擋，退避 {BLOCKED_COOLDOWN:.0f}s（對齊拓元 BLOCKED）")
            poller.pause(BLOCKED_COOLDOWN)
            continue
        product_infos, area_infos = parsing.index_infos(result)

        now = time.monotonic()
        available = {pid: info for pid, info in product_infos.items()
                     if cooldown.get(pid, 0) <= now}
        target = parsing.pick_target(plan["targets"], available, area_infos,
                                     amount, exclude=config.EXCLUDE_AREA_KEYWORD,
                                     balanced=plan.get("balanced", False),
                                     matched_keys=plan.get("matched_keys"),
                                     target=plan.get("target_price"))
        if not target:
            open_now = parsing.sale_open(product_infos)
            if open_now:
                # 開賣後就沒有搶毫秒的意義了，改回依序，別再對 origin 灌 20 req/s
                poller.pipelined = False
            states = {i.get("status") for i in product_infos.values()} or {"?"}
            phase = f"清票中…{CLEAR_COOLDOWN:.0f}s 等回流" if open_now else "全速等開賣"
            log.poll(attempt, phase, states, rtt)
            if open_now:
                poller.pause(CLEAR_COOLDOWN)
            continue

        area, product, count = target
        info = product_infos.get(product["productId"], {})
        seat = bool(plan["session"].get("ticketArea") and info.get("seatAssignment"))
        # 熱路徑核心：能用開賣前組好的就直接用（無劃位 + 張數沒被 purchaseLimit 夾過），
        # 「挑到票 → 送出」中間零組裝。只有有劃位（seat 要看即時 seatAssignment）或
        # 張數被夾（count < amount）這兩種才臨時補組 —— 兩種都罕見，也還是微秒級。
        pre = plan.get("prebuilt", {}).get(product["productId"])
        if pre is not None and not seat and count == amount:
            payload = pre
        else:
            payload = tp_reserve.build_payload(product["productId"], count,
                                               seat_assignment=seat,
                                               serial_number=config.PRESALE_CODE)
        detect_s = time.monotonic() - started
        label = parsing.target_label(area, product)
        log.flush()
        print(f"[GRAB] 目標: {label} ${product.get('price')} × {count} 張"
              f"{'（系統配位）' if seat else ''}"
              f" — **開賣後 {detect_s:.2f}s** 才送出（含暖機等所有前置，"
              f"第 {attempt} 輪，票況 RTT {rtt:.0f}ms）")
        outcome = tp_reserve.grab(session, payload, token)
        if outcome.get("orderId"):
            outcome["seat_assignment"] = seat   # 決定搶到後要導去哪一頁
            outcome["detect_s"] = detect_s
            outcome["total_s"] = time.monotonic() - started
            outcome["bought"] = f"{label} ${product.get('price')} × {count} 張"
            return outcome
        if outcome.get("fatal"):
            print(f"[GRAB] 中止: {outcome.get('error')}")
            return outcome
        # 送單失敗（售完/被搶走）→ 該票種 5s 清票冷卻，同輪馬上去搶別的票區
        print(f"[GRAB] 失敗: {outcome.get('error')} → 該票種冷卻 {CLEAR_COOLDOWN:.0f}s，換下一個票區")
        poller.pipelined = False
        cooldown[product["productId"]] = time.monotonic() + CLEAR_COOLDOWN


class _PollLog:
    """輪詢的 log 節流。

    併行模式下一秒有 20 筆讀數，原本「每一發都印」會把畫面沖爛、也拖慢主迴圈。
    但當初刻意不隱藏是為了**看得到 pending 翻 onsale 的那一瞬間** —— 所以規則是：
    狀態一有變化就立刻印（那是唯一真正重要的一行），沒變化的話每 0.5s 印一筆摘要。
    """

    _QUIET = 0.5

    def __init__(self):
        self._states = None
        self._last = 0.0
        self._skipped = 0

    def poll(self, attempt: int, phase: str, states: set, rtt: float):
        key = "/".join(sorted(map(str, states)))
        now = time.monotonic()
        if key == self._states and now - self._last < self._QUIET:
            self._skipped += 1
            return
        changed = self._states is not None and key != self._states
        tail = f"，前 {self._skipped} 筆同狀態" if self._skipped else ""
        print(f"[POLL] #{attempt} {'狀態變了 → ' if changed else ''}尚無可買票種"
              f"（{phase}，狀態: {key}，RTT {rtt:.0f}ms{tail}）")
        self._states, self._last, self._skipped = key, now, 0

    def flush(self):
        if self._skipped:
            print(f"[POLL] （前 {self._skipped} 筆同狀態省略）")
            self._skipped = 0


def build_plan(session, event_id: str) -> dict | None:
    """抓靜態目錄 + 依 config 決定「要搶哪一場、票區優先序」。回 None = 資料不足。"""
    data = catalog.fetch_catalog(session, event_id)
    sess = parsing.select_session(data["sessions"], config.DATE_KEYWORD)
    if not sess:
        print("[PLAN] 抓不到場次，檢查 ACTIVITY_SLUG 是否為活動網址上那串 id")
        return None
    print(f"[PLAN] 場次: {sess.get('name')} {sess.get('date')} {sess.get('time')} "
          f"@ {sess.get('location')}")

    session_id = sess.get("sessionId")
    areas = parsing.areas_of_session(data["ticketAreas"], session_id)
    # 票種一定要先用 sessionId 篩掉別場的（多場次活動的 products.json 是全部混在一起的）
    products = [p for p in data["products"]
                if p.get("sessionId") == session_id and not p.get("hidden")]
    if not products:
        print("[PLAN] 這個場次底下沒有票種")
        return None

    # config.py 是 gitignored 的，舊機器（例如剛 git pull 的 VPS）上不會有這個欄位 ——
    # 用 getattr 給預設值，不然整支 bot 會在 build_plan 就 AttributeError 起不來。
    # **但還是要記得補進那台的 config.py**：`load_config_override` 是用 hasattr 過濾的，
    # 欄位不存在的話 GUI 上選的清票模式會被靜默忽略。
    strict = str(getattr(config, "CLEAR_MODE", "寬鬆") or "寬鬆").strip() == "嚴格"
    targets = parsing.rank_targets(products, areas,
                                   keyword=config.AREA_KEYWORD,
                                   exclude=config.EXCLUDE_AREA_KEYWORD,
                                   strategy=config.AREA_AUTO_SELECT_MODE,
                                   strict=strict)
    if not targets:
        print("[PLAN] 排除後沒有可搶的票種"
              + ("（嚴格清票 + 關鍵字沒命中）" if strict else ""))
        return None
    print(f"[PLAN] 清票模式: {'嚴格（只買關鍵字命中的）' if strict else '寬鬆（志願順序 × 剩餘張數取平衡）'}")
    kind = "有劃位" if areas else "無劃位"
    order = " > ".join(parsing.target_label(a, p) for a, p in targets)
    print(f"[PLAN] {kind}活動，優先序: {order}")

    # 關鍵字全沒中時要靠「目標價位」算期望值（使用者習慣把價位打在最後一順位）。
    # 兩個都是靜態資料，開賣前算好就行，不要放進每秒 20 次的熱路徑。
    units = areas if areas else products
    price_target = parsing.target_price(config.AREA_KEYWORD, units)
    matched_keys = parsing.keyword_hit_keys(products, areas,
                                            keyword=config.AREA_KEYWORD,
                                            exclude=config.EXCLUDE_AREA_KEYWORD)
    if price_target and not strict:
        print(f"[PLAN] 目標價位 {price_target:.0f}（從關鍵字最後一組認出）—— "
              f"關鍵字沒中的票區改用「離這個價位多近」算期望值")

    _warn_if_serial_required(session, session_id, targets)
    _warn_if_lottery(session, event_id)

    # **開賣前就把每個票種的送單 payload 組好**（放進 plan，熱路徑直接拿）。
    # 開賣前是靜態的：productId（S3 目錄就有）+ serialNumber（config）+ 張數。
    # 唯一開賣後才知道的是「有劃位活動的 seat 旗標（seatAssignment）」和「purchaseLimit
    # 夾出來的實際張數」—— 那兩種情況熱路徑會自己補組（見 _grab_loop），其餘直接用這份。
    # 目的是把熱路徑的「挑到票 → 送出」中間工作壓到零，而不是省 CPU（組 dict 本來就微秒）。
    amount = int(config.TICKET_AMOUNT or 1)
    has_seat = bool(sess.get("ticketArea"))
    prebuilt = {}
    if not has_seat:      # 無劃位活動：payload 全靜態，開賣前就能定案
        for _area, product in targets:
            pid = product["productId"]
            prebuilt[pid] = tp_reserve.build_payload(
                pid, amount, seat_assignment=False,
                serial_number=config.PRESALE_CODE)

    # **「要 poll 什麼」跟「要買什麼」要分開**：偵測一律打整場所有 id，只有下單才看
    # targets。原因是 `catalog._fresh_params` 靠「把 id 排列順序打亂」來繞 CloudFront
    # 快取，而變體數是 id 數的階乘 —— 嚴格清票把候選縮到 1~2 個票區時排列組合只剩 2 種，
    # 直接繞不過快取、偵測慢約 1 秒，剛好把併行偵測省下來的都賠回去。
    # 多帶 id 不用多送請求（本來就是一發查全部），純賺。
    return {"session": sess, "session_id": session_id, "targets": targets,
            "amount": amount, "balanced": not strict,
            "matched_keys": matched_keys, "target_price": price_target,
            "prebuilt": prebuilt,
            "poll_products": [p["productId"] for p in products],
            "poll_areas": [a["ticketAreaId"] for a in areas]}


def _warn_if_lottery(session, event_id: str):
    """「登記抽選」活動要先講清楚 —— 那種活動搶快沒有意義。

    TicketPlus 有兩種完全不同的活動：一般搶票（先搶先贏）跟**登記抽選**
    （`campaign.isLottery`，前端整套 UI 換成「立即登記 / 排隊登記中 / 您的登記已完成」）。
    抽選是登記完之後隨機抽，官方公告原文寫「將根據登記訂單進行隨機抽選」——
    所以倒數 0.4 秒、併行偵測、每秒 20 發，對中籤率一點幫助都沒有。

    照樣讓它跑（登記還是得登記，而且登記本身也有名額/時間限制），只是要讓使用者知道
    「沒搶到不是 bot 慢」，也不用為了這種活動去多開 IP。
    """
    info = catalog.get_event_info(session, crypto.decrypt_id(event_id))
    if not info.get("isLottery"):
        return
    print("[PLAN] ⚠ 這是**登記抽選**活動（isLottery=True），不是先搶先贏 —— "
          "官方是「根據登記訂單隨機抽選」")
    print("[PLAN]   照常幫你送出登記，但**搶快不會提高中籤率**，不用為這場多開 IP 或調快間隔")


def _warn_if_serial_required(session, session_id: str, targets):
    """開賣前檢查這場要不要「專屬代碼」（會員碼 / 加購序號），要而沒填就大聲喊。

    **兩個信號都要看，只看一個會漏**（2026-08-08 用 NCT WISH 那場驗證出來的）：
      場次層 `transactionValidType`  —— 主辦一設定就有，**最早出現**
      票種層 `serialKey`             —— 常常晚一步才補上（NCT WISH 開賣前 2 天，
                                        場次層已是 sk00000466，71 個票種卻全是 null）

    前端要兩個都成立才渲染輸入框，但對 bot 來說「其中一個有」就該提醒 —— 寧可多喊一次，
    也不要在 T-0 才被 errCode 124 打回（實測不填不會馬上擋，會讓你白排完一輪隊）。
    """
    reasons = []

    info = catalog.get_session_info(session, crypto.decrypt_id(session_id))
    if info.get("transactionValidType"):
        reasons.append(f"場次設定 transactionValidType={info['transactionValidType']}")

    infos, _ = catalog.get_infos(session, [p["productId"] for _, p in targets], log=False)
    named = [p for _, p in targets
             if any(i.get("id") == p["productId"] and i.get("serialKey")
                    for i in (infos.get("product") or []))]
    if named:
        # 票種名常常整場都叫「全票」，直接串起來會印出 70 個「全票」把整行洗掉 ——
        # 這是開賣前最後一眼要看的警告，不能被雜訊蓋住。去重 + 只列前幾個。
        uniq = list(dict.fromkeys(str(p.get("name")) for p in named))
        shown = "、".join(uniq[:3]) + ("…" if len(uniq) > 3 else "")
        reasons.append(f"{len(named)} 個票種綁序號（{shown}）")

    if not reasons:
        return
    desc = info.get("SNDescription") or {}
    hint = ""
    for val in desc.values() if isinstance(desc, dict) else []:
        tw = (val or {}).get("tw") if isinstance(val, dict) else None
        if tw and tw.get("title"):
            hint = f"（官方說明：{tw['title']}）"
            break

    print(f"[PLAN] ⚠ 這場需要專屬代碼{hint} —— {' / '.join(reasons)}")
    if config.PRESALE_CODE:
        print(f"[PLAN] ⚠ 將帶 PRESALE_CODE 送出（填錯會被 errCode 124 中止）")
    else:
        print("[PLAN] ⚠ 但 PRESALE_CODE 是空的！不填不會馬上被擋，"
              "而是排完隊才失敗 —— 請先去卡片填上會員碼／序號")


async def _refresh_token_if_stale(token: str, tab) -> str:
    """開搶前確認手上這顆 token 還活著；快過期就回瀏覽器重讀一次。

    倒數可能跑很久（使用者常常提早一兩小時就掛著），而 TicketPlus 的 token 只活
    60 分鐘 —— 光在登入時檢查有效期不夠，倒數期間它照樣會死。

    **健康就完全不動**：T-0 的每一毫秒都不該花在無謂的 CDP 呼叫上，只有快過期時才
    去讀。使用者若在倒數期間重新登入過，`user` cookie 會是新的，這裡就接得到。
    """
    remaining = token_remaining(token)
    if remaining is None or remaining >= tp_browser.MIN_TOKEN_LIFE:
        return token

    print(f"[TOKEN] token {describe_token(token)}，開搶前先換一張")
    # 有瀏覽器就用瀏覽器裡的 cookie（使用者可能中途自己重登過，那顆最新）；
    # 手貼模式就用 config.COOKIE 裡一起貼進來的 refresh_token。
    raw = await tp_browser.read_user_cookie(tab) if tab is not None else config.COOKIE
    fresh_in_browser = extract_token(raw)
    if fresh_in_browser and (token_remaining(fresh_in_browser) or 0) >= tp_browser.MIN_TOKEN_LIFE:
        print(f"[TOKEN] 瀏覽器裡已經是新的了（{describe_token(fresh_in_browser)}）")
        return fresh_in_browser

    fresh = await tp_browser.refresh_via_cookie(tab, raw)
    if fresh:
        return fresh

    print(f"[TOKEN] ⚠ 換發失敗，手上還是過期的 token —— 送單很可能被 errCode 103 打回。"
          f"{'請在瀏覽器先登出再登入' if tab is not None else '請重貼一份新的 COOKIE'}")
    return token


async def main_async():
    event_id = parse_event_id(config.ACTIVITY_SLUG)
    print(f"[INIT] 活動 id: {event_id}")

    token, browser, tab = await _resolve_token(event_id)
    if not token:
        print("[ERROR] 沒有 access_token，無法送單")
        return

    session = build_session(token)
    plan = build_plan(session, event_id)
    if not plan:
        return
    warmup_session(session)

    # 偵測打整場的 id（見 build_plan 對 poll_products 的說明），不是只打要買的那幾個
    product_ids = plan["poll_products"]
    area_ids = plan["poll_areas"]
    poller = catalog.InfoPoller(session, product_ids, area_ids,
                                float(config.RETRY_INTERVAL or 0.3))
    print(f"[POLL] {poller.describe()}")

    if config.ENABLE_TIME_WATCHER:
        watcher = TimeWatcher(config.TARGET_START_TIME, config.TIME_WATCH_URL, lead_seconds=0.4)
        print(f"[TIMER] 目標時間: {config.TARGET_START_TIME}")
        ping = make_keepalive(session, product_ids)

        def _tick(remaining: float):
            ping(remaining)             # 主執行緒的連線
            poller.keepalive(remaining)  # 併行偵測那幾條 worker 的連線

        await watcher.wait_for_open_async(on_tick=_tick)
    else:
        print("[TIMER] 定時啟動已關閉，直接開搶")
    # 倒數結束的這一刻 ≈ 開賣。之後每一步（換 token、暖連線、偵測）都算在延遲裡，
    # 所以要在這裡取時間，不能等進了 poll_and_grab 才取。
    opened_at = time.monotonic()

    token = await _refresh_token_if_stale(token, tab)
    # queue 網域要在 T-0 是熱的，但**倒數期間 make_keepalive 已經一直在暖它**
    # （log 的「[KEEPALIVE] 排隊連線續命」就是）。有倒數還在這裡再暖一發，是站在關鍵
    # 路徑上白花約 156ms（2026-08-17 實測 log 抓到，那發直接把偵測往後推了半個 detect_s）。
    # 這 156ms 就算連線真的冷掉了，也只會轉嫁到第一發 enqueue 的建連上（那是 queue_s，
    # 不是 detect_s），不會疊加 —— 所以砍掉永遠不會更差。只有「關掉定時啟動、直接開搶」
    # 那條沒走過 keepalive 的路才需要它。
    if not config.ENABLE_TIME_WATCHER:
        warmup_queue(session)
    outcome = poll_and_grab(session, plan, token, poller, opened_at)

    if outcome.get("orderId"):
        alerts.play_checkout()
        seat = bool(outcome.get("seat_assignment"))
        url = tp_browser.checkout_url(event_id, plan["session_id"], seat)
        print(f"[SUCCESS] 🎉 搶到 {outcome.get('bought', '')} orderId={outcome['orderId']}")
        for line in tp_reserve.summarize(outcome.get("raw") or {}):
            print(f"[SUCCESS] {line}")
        print(f"[SUCCESS] 開搶 → 到手共 {outcome.get('total_s', 0):.1f}s"
              f"（偵測 {outcome.get('detect_s', 0):.1f}s"
              f" / 排隊 {outcome.get('queue_s', 0):.1f}s"
              f" / 送單 {outcome.get('reserve_s', 0):.1f}s）")
        print(f"[SUCCESS] 結帳頁: {url}")
        if config.ENABLE_LINE_NOTIFY and config.LINE_USER_ID:
            line_push.notify_grabbed(config.LINE_USER_ID, slug=event_id,
                                     amount=str(plan["amount"]), fee=config.TICKET_FEE,
                                     platform="TICKETPLUS")
        if tab is not None:
            await tp_browser.goto_checkout(tab, event_id, plan["session_id"], seat)
            # 導到結帳頁後截圖推給客人證明搶到（截圖失敗不影響——票已到手）
            if config.ENABLE_LINE_NOTIFY and config.LINE_USER_ID:
                await tp_browser.wait_checkout_ready(tab)  # 等 SPA render 出訂單再截
                await line_push.notify_checkout_from_tab(config.LINE_USER_ID, tab)
    else:
        print(f"[FAIL] {outcome.get('error')}")

    if browser is not None:
        print("[DONE] 瀏覽器保持開啟 — 完成後關閉本程式（GUI: STOP）")
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass


def main():
    sys.stdout.reconfigure(line_buffering=True)
    _install_log_timestamp()
    load_config_override()
    if config.ENABLE_PROXY_POOL:
        proxy_pool.acquire()
    uc.loop().run_until_complete(main_async())


if __name__ == "__main__":
    main()
