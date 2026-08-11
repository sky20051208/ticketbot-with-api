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
from timeWatcher import TimeWatcher
from tixcraftapi import alerts
from LineBot import line_push

from ticketplus_api import catalog, crypto, parsing, reserve as tp_reserve
from ticketplus_api.session import (build_session, describe_token, extract_token,
                                    make_keepalive, token_remaining, warmup_session)
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
                now = time.time()
                ts = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int((now % 1) * 1000):03d}"
                args = (f"[{ts}] {args[0]}", *args[1:])
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


def poll_and_grab(session, plan: dict, token: str) -> dict:
    """無限清票直到搶到 / 致命錯誤 / 使用者 STOP。冷卻節奏借鑑拓元 FSM：
      - **未開賣**（沒有任何票種 onsale，等 T-0）→ 全速輪詢（golden，不冷卻）
      - **有可買票** → 立刻送單（golden，永不冷卻，第一拍黃金時間）
      - **開賣但目標票售完/被搶走** → 5s 清票冷卻等回流（per 票種各自冷卻，同輪仍會馬上
        去搶別的票區，只有失敗的那個票種等 5s）
      - **被流量管制/擋**（HTTP 429/403 或 errCode 110）→ 8s 退避（給 server/IP 喘息）
    回 {"orderId": …} 或 {"error": …, "fatal": True}。"""
    product_ids = [p["productId"] for _, p in plan["targets"]]
    area_ids = list(dict.fromkeys(a["ticketAreaId"] for a, _ in plan["targets"] if a))
    amount = plan["amount"]
    fast_interval = float(config.RETRY_INTERVAL or 0.3)

    started = time.monotonic()
    cooldown: dict[str, float] = {}   # per 票種：售完/被搶走後的清票冷卻
    attempt = 0

    while True:
        attempt += 1
        result, blocked = catalog.get_infos(session, product_ids, area_ids)
        if blocked:
            print(f"[POLL] #{attempt} 被流量管制/擋，退避 {BLOCKED_COOLDOWN:.0f}s（對齊拓元 BLOCKED）")
            time.sleep(BLOCKED_COOLDOWN)
            continue
        product_infos, area_infos = parsing.index_infos(result)

        now = time.monotonic()
        available = {pid: info for pid, info in product_infos.items()
                     if cooldown.get(pid, 0) <= now}
        target = parsing.pick_target(plan["targets"], available, area_infos,
                                     amount, exclude=config.EXCLUDE_AREA_KEYWORD)
        if not target:
            open_now = parsing.sale_open(product_infos)
            # 每一發都印出來（不隱藏）——開賣翻 onsale 的瞬間才看得到
            states = {i.get("status") for i in product_infos.values()} or {"?"}
            phase = f"清票中…{CLEAR_COOLDOWN:.0f}s 等回流" if open_now else "全速等開賣"
            print(f"[POLL] #{attempt} 尚無可買票種（{phase}，狀態: {'/'.join(sorted(map(str, states)))}）")
            # 開賣但沒票 → 5s 清票；還沒開賣 → 全速等 T-0（golden，抓開賣瞬間）
            time.sleep(CLEAR_COOLDOWN if open_now else fast_interval)
            continue

        area, product, count = target
        info = product_infos.get(product["productId"], {})
        seat = bool(plan["session"].get("ticketArea") and info.get("seatAssignment"))
        detect_s = time.monotonic() - started
        label = parsing.target_label(area, product)
        print(f"[GRAB] 目標: {label} ${product.get('price')} × {count} 張"
              f"{'（系統配位）' if seat else ''}"
              f" — 開搶後 {detect_s:.1f}s 偵測到票（第 {attempt} 輪）")

        payload = tp_reserve.build_payload(product["productId"], count,
                                           seat_assignment=seat,
                                           serial_number=config.PRESALE_CODE)
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
        cooldown[product["productId"]] = time.monotonic() + CLEAR_COOLDOWN


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

    targets = parsing.rank_targets(products, areas,
                                   keyword=config.AREA_KEYWORD,
                                   exclude=config.EXCLUDE_AREA_KEYWORD,
                                   strategy=config.AREA_AUTO_SELECT_MODE)
    if not targets:
        print("[PLAN] 排除後沒有可搶的票種")
        return None
    kind = "有劃位" if areas else "無劃位"
    order = " > ".join(parsing.target_label(a, p) for a, p in targets)
    print(f"[PLAN] {kind}活動，優先序: {order}")

    _warn_if_serial_required(session, session_id, targets)
    return {"session": sess, "session_id": session_id, "targets": targets,
            "amount": int(config.TICKET_AMOUNT or 1)}


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

    if config.ENABLE_TIME_WATCHER:
        watcher = TimeWatcher(config.TARGET_START_TIME, config.TIME_WATCH_URL, lead_seconds=0.4)
        print(f"[TIMER] 目標時間: {config.TARGET_START_TIME}")
        product_ids = [p["productId"] for _, p in plan["targets"]]
        await watcher.wait_for_open_async(on_tick=make_keepalive(session, product_ids))
    else:
        print("[TIMER] 定時啟動已關閉，直接開搶")

    token = await _refresh_token_if_stale(token, tab)
    outcome = poll_and_grab(session, plan, token)

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
