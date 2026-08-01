"""KKTIX API 模式 entry point — `python -m kktix_api --config <path>` 啟動。

架構（瀏覽器 fetch 版）：
  開 nodriver 登入 → 保持瀏覽器開著 → 倒數 → 用 base_info/register_info API 高頻偵測開賣
  → fetch 送 queue 候位 → 導航瀏覽器到 order/報名頁讓使用者完成。
  （票種是 Angular 前端渲染，raw HTML 拿不到，所以走 KKTIX 自己的 JSON API，見 register.py）

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


# 清票冷卻（對齊拓元 FSM）：售完/被搶走 = 5s（拓元 AREA 重入）；撞 403 被擋 = 8s（拓元 BLOCKED）。
# 「有票就搶」永遠不冷卻（第一拍黃金時間）。
CLEAR_COOLDOWN_SECONDS = 5.0
BLOCKED_COOLDOWN_SECONDS = 8.0


async def grab_loop(tab, slug: str, catalog: list, csrf: str) -> str | None:
    """清票主迴圈：反覆「偵測開賣/有票 → 送單」，送單沒成就回頭重搶。

    **不設時間上限** —— 一直跑到「搶到」或「硬失敗（如資格不符，重試無用）」，
    否則就無限清票等回流票，由使用者按 GUI STOP 手動停（STOP 會直接砍掉整個 process）。

    - reserve 回 (None, fatal=False) = 售完 / 被搶走 / 逾時 / csrf 過期 → 刷新 csrf、
      （catalog 空的話）補抓 base_info，再回頭繼續搶（這就是「清票」）。
    - reserve 回 (None, fatal=True)  = 硬失敗（資格不符）→ 停止，印原因。
    - poll_until_open 一輪都沒符合條件的票也回來重跑，等別人釋票。"""
    round_no = 0
    while True:
        round_no += 1
        # catalog 還空就補抓一次（base_info 可能開賣才開放，或登入瞬間那發失敗）
        if not catalog:
            refetched = await kk_register.fetch_catalog(tab, slug, retries=2, delay=0.2)
            if refetched:
                catalog = refetched
                print(f"[GRAB] 第 {round_no} 輪補抓到票種目錄（{len(catalog)} 種）")

        result = await kk_register.poll_until_open(
            tab, slug, catalog, csrf, max_duration=30.0,
            clear_cooldown=CLEAR_COOLDOWN_SECONDS,      # 開賣但沒票 → 5s（拓元 AREA 重入）
            blocked_cooldown=BLOCKED_COOLDOWN_SECONDS)  # 撞 403 → 8s（拓元 BLOCKED）
        if result is None:
            continue  # 這輪沒等到符合條件的票，回頭再等（清票）

        order_url, fatal = await kk_reserve.reserve_ticket(tab, slug, result)
        if order_url:
            return order_url
        if fatal:
            # 硬失敗（如「非身心障礙認證會員不可選購」）—— 重試無用，停止清票，
            # 讓使用者看到原因去改 config（AREA_KEYWORD / EXCLUDE_AREA_KEYWORD）。
            print("[GRAB] 送單硬失敗（非售完，重試無用），停止清票")
            return None

        print(f"[GRAB] 第 {round_no} 輪送單未成（售完/被搶走），{CLEAR_COOLDOWN_SECONDS:.0f}s 後刷新 csrf 繼續清票…")
        fresh = await kk_register.refetch_csrf(tab, slug)
        if fresh:
            csrf = fresh
        await asyncio.sleep(CLEAR_COOLDOWN_SECONDS)

    print(f"[GRAB] 清票逾時 {GRAB_TOTAL_SECONDS:.0f}s，未搶到")
    return None


async def main_async():
    slug = config.ACTIVITY_SLUG
    udd = config.CHROME_USER_DATA_DIR  # 空字串 → 臨時 profile，使用者每次手動登入

    browser, tab, ok = await open_and_login(udd, proxy_url=config.CURRENT_PROXY)
    if not ok:
        print("[ERROR] 登入未完成或超時")
        return

    # --- 倒數前先預抓靜態資料（票種目錄 + csrf），T-0 一到只剩最快的 register_info 輪詢 ---
    catalog, csrf = await kk_register.fetch_catalog_and_csrf(tab, slug)

    # --- 倒數（TimeWatcher 是 async，直接 await）---
    if config.ENABLE_TIME_WATCHER:
        watcher = TimeWatcher(config.TARGET_START_TIME, config.TIME_WATCH_URL, lead_seconds=0.3)
        print(f"[TIMER] 目標時間: {config.TARGET_START_TIME}")
        # 倒數期間背景定期重抓 csrf + keep-alive，避免等太久 session/csrf 失效
        holder = {"csrf": csrf}
        keepalive = asyncio.create_task(kk_register.keep_alive_refresh(tab, slug, holder))
        try:
            await watcher.wait_for_open_async()
        finally:
            keepalive.cancel()
            try:
                await keepalive
            except asyncio.CancelledError:
                pass
        csrf = holder["csrf"]  # 用倒數期間刷到的最新 csrf
    else:
        print("[TIMER] 定時啟動已關閉，直接開搶")

    # --- 清票迴圈：偵測開賣 → 送單，送單沒成（被搶走/售完/csrf 過期）就回頭重搶，
    #     直到搶到或整體逾時。catalog 若還空（base_info 開賣後才開放）就開賣後補抓。---
    order_url = await grab_loop(tab, slug, catalog, csrf)
    target = order_url or registration_url(slug)
    if order_url:
        print(f"[SUCCESS] 搶到了! 跳轉: {order_url}")
    else:
        print(f"[FALLBACK] 未搶到，導航到報名頁手動嘗試: {target}")
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
