"""寬宏 Kham API 模式 entry point — `python -m kham_api --config <path>` 啟動。

流程（非實名制、自行選位）：
  登入 → OCR 暖機 → 分頁停到活動頁 → 倒數（背景續命）
       → 頁內 fetch：選日期 → 輪詢票區直到有空位 → 選位頁挑空位 + 解碼 + 送單 → 分頁跳購物車。
沒搶到不會停，照三平台統一清票模型繼續等回流票（見 `_grab` 上面的常數）。
只做串接，搶票步驟在 kham_api 其他檔（parsing / browser_session / reserve / captcha）。
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
from LineBot import line_push

from kham_api import parsing, captcha
from kham_api.browser_session import open_and_login, goto, page_fetch
from kham_api import reserve as kham_reserve


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


async def _fetch_page(tab, url: str, log: bool = False) -> tuple[str, str]:
    """頁內 GET → (HTML, 轉址後的網址)。失敗回 ("", 網址)。"""
    res = await page_fetch(tab, url, log=log)
    if not res.get("ok") or res.get("status") != 200:
        return "", res.get("url", "")
    return res.get("text") or "", res.get("url") or ""


async def _keepalive(tab, url: str, interval: float = 30.0):
    """倒數期間背景跑：定期打活動頁，保住 ASP.NET session 跟 Chrome 到 Google 邊緣的連線，
    順便檢查登入有沒有掉（掉了要到 T-0 送單才會發現，那時已經來不及）。T-0 被 cancel。"""
    warned = False
    try:
        while True:
            await asyncio.sleep(interval)
            html, _ = await _fetch_page(tab, url)
            if not html:
                print("[KEEPALIVE] ⚠️ 活動頁打不到")
            elif parsing.logged_in(html) is False and not warned:
                print("[KEEPALIVE] ⚠️ 登入已失效！請在瀏覽器視窗重新登入，不然開賣時送不了單")
                warned = True
    except asyncio.CancelledError:
        pass


class _Throttle:
    """輪詢 log 節流：前 3 筆照印，之後每 N 筆印一筆。"""

    def __init__(self, every: int = 20):
        self.n, self.every = 0, every

    def __call__(self, msg: str):
        self.n += 1
        if self.n <= 3 or self.n % self.every == 0:
            print(f"{msg}（第 {self.n} 次）")


# 清票節奏（對齊拓元 FSM / KKTIX / 遠大，見三平台統一清票模型）：
#   有票 → 立刻送單，永不冷卻
#   剛開跑（含剛過 T-0）→ 全速輪詢抓開賣瞬間
#   開賣後目標售完 / 被搶走 → 5s 等回流
#   請求失敗 / 被擋 → 8s 退避
#   無總時間上限，跑到搶到 / 致命錯誤 / 使用者 GUI STOP
POLL_INTERVAL = 0.4
CLEAR_COOLDOWN = 5.0
BLOCKED_COOLDOWN = 8.0
FULL_SPEED_WINDOW = 60.0   # 開跑後這段時間一律全速（沒有 T-0 也可能是人工掐點開跑）
MAX_UNKNOWN_ROUNDS = 5     # 連續幾次收到看不懂的送單回應就放棄（怕對著同一句話無限重試）


async def _grab(tab, date_url: str, amount: int, perf: dict | None = None) -> dict:
    """全程頁內 fetch：選場次 → 輪詢票區 → 選位頁送單，沒搶到就無限清票等回流。
    回 {cart, seat_url, perf}；cart 為 None 代表停下來了（致命錯誤），seat_url 有值就開給人接手。

    perf：倒數前就選好的場次。場次網址開賣前後不會變，先給就等於 T-0 少打一發選日期頁
    （實測 ~200ms）。開賣前還沒出現「立即訂購」的話傳 None，這裡會自己輪詢等它出現。"""
    started = time.monotonic()
    seat_url = None
    wait_perf, wait_area = _Throttle(), _Throttle()
    clearing_announced = login_warned = False
    unknown_rounds = 0

    def _idle_wait() -> float:
        """沒票時要等多久：剛開跑全速搶開賣瞬間，之後轉 5s 清票（一直全速只是狂打對方）。"""
        nonlocal clearing_announced
        if time.monotonic() - started < FULL_SPEED_WINDOW:
            return POLL_INTERVAL
        if not clearing_announced:
            print(f"[GRAB] 轉入清票模式：每 {CLEAR_COOLDOWN:.0f}s 看一次有沒有回流票，"
                  "要停請按 GUI 的 STOP")
            clearing_announced = True
        return CLEAR_COOLDOWN

    while True:
        if perf is None:
            html, _ = await _fetch_page(tab, date_url)
            if not html:
                print(f"[NAV] 活動頁讀取失敗，{BLOCKED_COOLDOWN:.0f}s 後重試")
                await asyncio.sleep(BLOCKED_COOLDOWN)
                continue
            perf = parsing.select_performance(parsing.parse_performance_links(html),
                                              config.DATE_KEYWORD, config.EXCLUDE_AREA_KEYWORD)
            if perf is None:
                wait_perf("[NAV] 還沒有可訂購的場次")
                await asyncio.sleep(_idle_wait())
                continue
            print(f"[NAV] 選場次: {perf['performance_id']}  {perf['label']}")
            if perf["kind"] != "area":
                print("[NAV] ❌ 這場是不劃位（UTK0202 輸入張數），bot 還不支援，請手動購買")
                return {"cart": None, "seat_url": perf["url"], "perf": perf}

        html, _ = await _fetch_page(tab, perf["url"])
        if not html:
            print(f"[NAV] 票區頁讀取失敗，{BLOCKED_COOLDOWN:.0f}s 後重試")
            await asyncio.sleep(BLOCKED_COOLDOWN)
            continue
        if parsing.logged_in(html) is False and not login_warned:
            # 掛著清票好幾小時，session 中途掉了要馬上講 —— 不然要等到有票那一刻才發現。
            # 分頁就停在寬宏上，使用者直接在視窗重登，我們的 fetch 立刻就跟著有效
            print("[GRAB] ⚠️ 登入已失效！請在瀏覽器視窗重新登入（清票繼續跑，但有票也送不出去）")
            login_warned = True
        areas = parsing.parse_areas(html)
        area = parsing.select_area(areas, keyword=config.AREA_KEYWORD,
                                   exclude=config.EXCLUDE_AREA_KEYWORD, amount=amount,
                                   strict=config.CLEAR_MODE == "嚴格")
        if not area:
            wait_area(f"[NAV] 尚無可選票區（{len(areas)} 區，多為售完/未開）")
            await asyncio.sleep(_idle_wait())
            continue

        # 直接組選位頁網址、頁內 fetch：不用等座位圖 .map 載完去點，也不會讓頁面自己抓驗證碼
        seat_url = parsing.seat_page_url(perf["performance_id"], area)
        print(f"[NAV] 票區開放: {area['area_id']} {area['name']} 空位{area['avail']}")
        seat_html, final = await _fetch_page(tab, seat_url, log=True)
        if "UTK0205" not in final:
            print(f"[NAV] 選位頁被導去 {final or '(請求失敗)'}，{BLOCKED_COOLDOWN:.0f}s 後重試")
            await asyncio.sleep(BLOCKED_COOLDOWN)
            continue
        if parsing.logged_in(seat_html) is False:
            print("[NAV] ❌ 選位頁顯示未登入，送不了單。請在瀏覽器視窗重新登入")
            return {"cart": None, "seat_url": seat_url, "perf": perf}

        cart, reason = await kham_reserve.add_to_cart(
            tab, seat_url, seat_html, amount=amount, keyword=config.AREA_KEYWORD,
            exclude=config.EXCLUDE_AREA_KEYWORD, require_full=config.REQUIRE_FULL_AMOUNT)
        if cart or reason == kham_reserve.REASON_NO_TYPE:
            return {"cart": cart, "seat_url": seat_url, "perf": perf}

        if reason == kham_reserve.REASON_UNKNOWN:
            # 沒見過的回應照樣重試（開賣瞬間早了一點點回「尚未開賣」是最可能的一種），
            # 但一直是同樣結果就不要鬼打牆 —— 可能是「您已購買過此場次」那類講什麼都沒用的
            unknown_rounds += 1
            if unknown_rounds >= MAX_UNKNOWN_ROUNDS:
                print(f"[GRAB] ❌ 連續 {unknown_rounds} 次收到看不懂的回應（內容看上面），停止清票")
                return {"cart": None, "seat_url": seat_url, "perf": perf}
        else:
            unknown_rounds = 0
        # 位子在這一瞬間被搶走 / 買光 —— 回頭等回流
        print(f"[GRAB] 這輪沒拿到（{reason}），{CLEAR_COOLDOWN:.0f}s 後繼續清票")
        await asyncio.sleep(CLEAR_COOLDOWN)


async def main_async():
    product_id = parsing.product_id_from(config.ACTIVITY_SLUG)
    if not product_id:
        print("[ERROR] ACTIVITY SLUG 要填寬宏的 PRODUCT_ID（活動代碼，例 P1D3G65D），"
              "或整串活動頁網址 …/UTK0201_.aspx?PRODUCT_ID=xxx。"
              "套票頁（UTK0201_040.aspx?AGID=…）沒有 PRODUCT_ID，不支援")
        return
    if config.COOKIE_SOURCE != "userdata":
        print("[WARN] 寬宏只支援 chrome profile 登入，COOKIE 欄位不會用到；"
              "沒選 profile 會開臨時瀏覽器，請在視窗裡登入")
    udd = config.CHROME_USER_DATA_DIR

    browser, tab, ok = await open_and_login(udd, proxy_url=config.CURRENT_PROXY)
    if not ok:
        print("[ERROR] 登入未完成或超時")
        return

    captcha.warmup()  # 開賣前把 OCR 模型載入

    # 分頁先停到活動的選日期頁：之後全走頁內 fetch，分頁必須在 kham.com.tw 同源上。
    # 順便把場次列出來，PRODUCT_ID / DATE_KEYWORD 填錯可以在倒數時就發現
    date_url = f"UTK0201_00.aspx?PRODUCT_ID={product_id}"
    await goto(tab, date_url)
    html, _ = await _fetch_page(tab, date_url)
    perfs = parsing.parse_performance_links(html)
    chosen = None
    if perfs:
        for p in perfs:
            print(f"[NAV] 場次 {p['performance_id']}（{p['kind']}）{p['label']}")
        chosen = parsing.select_performance(perfs, config.DATE_KEYWORD, config.EXCLUDE_AREA_KEYWORD)
        print(f"[NAV] 預計選: {chosen['label'] if chosen else '(沒有符合的場次)'}"
              + ("（T-0 直接打這場的票區頁，省一發選日期頁）" if chosen else ""))
    else:
        print("[NAV] 活動頁目前沒有可訂購的場次（開賣前正常；已開賣就檢查 PRODUCT_ID）")

    if config.ENABLE_TIME_WATCHER:
        watcher = TimeWatcher(config.TARGET_START_TIME, config.TIME_WATCH_URL, lead_seconds=0.3)
        print(f"[TIMER] 目標時間: {config.TARGET_START_TIME}")
        keepalive = asyncio.create_task(_keepalive(tab, date_url))
        try:
            await watcher.wait_for_open_async()
        finally:
            keepalive.cancel()
            try:
                await keepalive
            except asyncio.CancelledError:
                pass
    else:
        print("[TIMER] 定時啟動已關閉，直接開搶")

    amount = int(config.TICKET_AMOUNT or 1)
    result = await _grab(tab, date_url, amount, perf=chosen)
    cart, seat_url, perf = result["cart"], result["seat_url"], result["perf"]
    if cart:
        print(f"[SUCCESS] 🎉 已加入購物車！導航去結帳: {cart}")
        if config.ENABLE_LINE_NOTIFY and config.LINE_USER_ID:
            line_push.notify_grabbed(config.LINE_USER_ID,
                                     slug=perf["label"] if perf else product_id,
                                     amount=str(amount), fee=config.TICKET_FEE, platform="KHAM")
        try:
            await goto(tab, cart, settle=1.5)
            if config.ENABLE_LINE_NOTIFY and config.LINE_USER_ID:
                await line_push.notify_checkout_from_tab(config.LINE_USER_ID, tab)
        except Exception as e:
            print(f"[WARN] 導航購物車失敗: {e!r}")
    else:
        # 清票沒有時間上限，所以走到這裡一定是「重試也沒用」才停的（未登入 / 不支援的場次 /
        # 伺服器回了沒見過的訊息），不是單純搶輸
        print("[FAIL] 停止清票（重試也沒用的狀況，原因看上一行）")
        if seat_url:
            # 分頁一直停在活動頁，把它開到最後那一頁讓人直接手動接手
            await goto(tab, seat_url)
            print(f"[FAIL] 已把瀏覽器開到 {seat_url}，可以手動接手")

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
