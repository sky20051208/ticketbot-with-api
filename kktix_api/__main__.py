"""KKTIX API 模式 entry point — `python -m kktix_api --config <path>` 啟動。

架構（瀏覽器 fetch 版）：
  開 nodriver 登入 → 保持瀏覽器開著 → 倒數 → 用頁面內 fetch 高頻偵測開賣 → fetch 送 queue
  候位 → 導航瀏覽器到 order/報名頁讓使用者完成。

為什麼整段跑在瀏覽器：KKTIX 在 Cloudflare 後面，把 httpOnly cookie 掏出來餵 curl_cffi 在
這台機器上行不通（CDP getCookies 卡死、Network 事件不派發、cookie 檔 App-Bound 加密）。
改用 tab.evaluate(fetch(...)) 在已登入頁面同源環境打 —— 瀏覽器自動帶 cf_clearance + 登入
session，不會 403。詳見 browser_session.py。
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

from kktix_api.session import registration_url
from kktix_api.browser_session import open_and_login
from kktix_api import register as kk_register
from kktix_api import reserve as kk_reserve


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


async def main_async():
    slug = config.ACTIVITY_SLUG
    udd = config.CHROME_USER_DATA_DIR  # 空字串 → 臨時 profile，使用者每次手動登入

    browser, tab, ok = await open_and_login(udd, proxy_url=config.CURRENT_PROXY)
    if not ok:
        print("[ERROR] 登入未完成或超時")
        return

    # --- 倒數（TimeWatcher 是 async，直接 await）---
    if config.ENABLE_TIME_WATCHER:
        watcher = TimeWatcher(config.TARGET_START_TIME, config.TIME_WATCH_URL)
        print(f"[TIMER] 目標時間: {config.TARGET_START_TIME}")
        await watcher.wait_for_open_async()
    else:
        print("[TIMER] 定時啟動已關閉，直接開搶")

    # --- fetch 高頻偵測開賣 + 選票 ---
    result = await kk_register.poll_until_open(tab, slug)
    if result is None:
        print("[FAIL] 未抓到可選票")
    else:
        # --- 送 queue 候位（fetch，已驗證）---
        order_url = await kk_reserve.reserve_ticket(tab, slug, result)
        target = order_url or registration_url(slug)
        if order_url:
            print(f"[SUCCESS] 搶到了! 跳轉: {order_url}")
        else:
            print(f"[FALLBACK] 已在候位佇列，導航到報名頁手動完成: {target}")
        try:
            await browser.get(target)
        except Exception as e:
            print(f"[WARN] 導航失敗: {e!r}")

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

    # proxy 先 acquire（瀏覽器登入 + 後續 fetch 都走同 IP）
    if config.ENABLE_PROXY_POOL:
        proxy_pool.acquire()

    uc.loop().run_until_complete(main_async())


if __name__ == "__main__":
    main()
