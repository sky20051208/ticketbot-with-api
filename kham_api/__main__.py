"""寬宏 Kham API 模式 entry point — `python -m kham_api --config <path>` 啟動。

流程（非實名制、自行選位）：
  登入 → ddddocr 暖機 → 倒數 → 導航(選日期→選票區) 高頻輪詢開賣 → 選位頁挑空位+解碼+送單 → 購物車。
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

from kham_api import BASE, parsing, captcha
from kham_api.browser_session import open_and_login, goto, read_js
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


async def _read_html(tab) -> str:
    return await read_js(tab, "document.documentElement.outerHTML") or ""


async def _navigate_to_seat_page(tab, product_id: str,
                                 max_duration: float = 120.0, interval: float = 0.4) -> bool:
    """導航：選日期(PERFORMANCE_ID) → 高頻輪詢票區(UTK0204)直到有空位 → 點票區進選位頁。"""
    # 1) 選日期頁 → 依 DATE_KEYWORD 挑場次（場次穩定，抓一次）
    await goto(tab, f"UTK0201_00.aspx?PRODUCT_ID={product_id}")
    perfs = parsing.parse_performance_links(await _read_html(tab))
    perf = parsing.select_performance(perfs, config.DATE_KEYWORD)
    if not perf:
        print(f"[NAV] 抓不到場次（perfs={len(perfs)}），檢查 PRODUCT_ID / DATE_KEYWORD")
        return False
    print(f"[NAV] 選場次: {perf['performance_id']}  {perf['label']}")

    # 2) 高頻輪詢票區頁，直到有符合條件且有空位的票區
    deadline = time.monotonic() + max_duration
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        await goto(tab, perf["url"], settle=0.2)
        areas = parsing.parse_areas(await _read_html(tab))
        area = parsing.select_area(areas, keyword=config.AREA_KEYWORD,
                                   exclude=config.EXCLUDE_AREA_KEYWORD)
        if area:
            print(f"[NAV] #{attempt} 票區開放: {area['area_id']} {area['name']} 空位{area['avail']}")
            # 3) 點該票區列 → 讓 Kham JS 導航到 UTK0205
            clicked = await read_js(
                tab, f"(()=>{{const e=document.getElementById('{area['area_id']}');"
                     f"if(e){{e.click();return true}}return false}})()")
            # 等 URL 進到 UTK0205
            for _ in range(25):
                await asyncio.sleep(0.2)
                url = await read_js(tab, "window.location.href") or ""
                if "UTK0205" in url:
                    print(f"[NAV] 進入選位頁: {url}")
                    return True
            print("[NAV] 點票區後未進入 UTK0205，重試")
        elif attempt <= 3 or attempt % 20 == 0:
            print(f"[NAV] #{attempt} 尚無可選票區（{len(areas)} 區，多為售完/未開）")
        await asyncio.sleep(interval)
    print(f"[NAV] 超時 {max_duration}s 未等到可選票區")
    return False


async def main_async():
    product_id = config.ACTIVITY_SLUG  # 寬宏用 ACTIVITY_SLUG 存 PRODUCT_ID
    udd = config.CHROME_USER_DATA_DIR

    browser, tab, ok = await open_and_login(udd, proxy_url=config.CURRENT_PROXY)
    if not ok:
        print("[ERROR] 登入未完成或超時")
        return

    captcha.warmup()  # 開賣前把 ddddocr 模型載入

    # 倒數
    if config.ENABLE_TIME_WATCHER:
        watcher = TimeWatcher(config.TARGET_START_TIME, config.TIME_WATCH_URL)
        print(f"[TIMER] 目標時間: {config.TARGET_START_TIME}")
        await watcher.wait_for_open_async()
    else:
        print("[TIMER] 定時啟動已關閉，直接開搶")

    if not await _navigate_to_seat_page(tab, product_id):
        print("[FAIL] 未進入選位頁")
    else:
        cart = await kham_reserve.add_to_cart(
            tab, amount=int(config.TICKET_AMOUNT or 1),
            area_keyword=config.AREA_KEYWORD, exclude=config.EXCLUDE_AREA_KEYWORD)
        if cart:
            print(f"[SUCCESS] 已加入購物車！導航去結帳: {cart}")
            try:
                await browser.get(cart)
            except Exception as e:
                print(f"[WARN] 導航購物車失敗: {e!r}")
        else:
            print("[FAIL] 未能加入購物車")

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
