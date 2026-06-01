"""拓元 API 模式 entry point — `python -m tixcraftapi --config <path>` 啟動。

本檔只負責：
  - argparse + load_config_override (GUI 用 --config 蓋值)
  - log monkey-patch (所有 [XXX] print 加 ms 時間戳)
  - main() 主流程：cookie → 暖機 → 定時 → FSM run → 搶到後接管

實際搶票邏輯走 FSM (tixcraftapi.runner)；URL classify + state transition 在 state.py。
搶票各步驟模組：session / parsing / game / verify / area / captcha / submit / order / finalize。
"""
import sys
import time
import json
import asyncio
import argparse
import threading
from datetime import datetime, timedelta

import config
import proxy_pool
import browser_login
from captchaAI.predict import warmup_ocr
from timeWatcher import TimeWatcher

from tixcraftapi import BASE
from tixcraftapi.session import build_session, build_headers, warmup_session, keep_alive_loop
from tixcraftapi.game import poll_until_open
from tixcraftapi.state import State
from tixcraftapi.runner import Context, run
from tixcraftapi.finalize import open_chrome_with_session


def load_config_override():
    """如果有 --config 參數，從 JSON 覆蓋 config 模組的值"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default=None,
                        help="JSON config 檔路徑（GUI War-Room 用）")
    args = parser.parse_args()

    if args.config:
        with open(args.config, "r", encoding="utf-8") as f:
            overrides = json.load(f)
        for key, val in overrides.items():
            if hasattr(config, key):
                setattr(config, key, val)
        print(f"[CONFIG] 已載入: {args.config}")
    else:
        print("[CONFIG] 使用預設 config.py")


def wait_until_start(target_time_str: str):
    """精準計時器（網路對時）— TimeWatcher 內部處理 NTP / 拓元 server time。"""
    watcher = TimeWatcher(target_time_str, config.TIME_WATCH_URL)
    asyncio.run(watcher.wait_for_open_async())


def wait_until_t_minus(target_time_str: str, t_minus_seconds: float = 300.0):
    """粗略等到 T-(t_minus_seconds)秒（本機時鐘，誤差幾秒內可接受）。
    主用途：proxy 啟用時延遲到搶票前 5 分鐘才連線，避開 sticky session 過期。
    後續 wait_until_start 會做精準對時。"""
    today = datetime.now().date()
    t = datetime.strptime(target_time_str, "%H:%M:%S").time()
    target = datetime.combine(today, t)
    if target < datetime.now():
        target += timedelta(days=1)

    print(f"[STAGE] 階段一：等到 T-{int(t_minus_seconds)}s 才連 proxy（避開 sticky session 過期）")
    last_print = 0
    while True:
        remaining = (target - datetime.now()).total_seconds()
        if remaining <= t_minus_seconds:
            print(f"[STAGE] T-{remaining:.1f}s 抵達，準備啟動 proxy")
            return
        now_int = int(datetime.now().timestamp())
        if now_int - last_print >= 60:
            extra = remaining - t_minus_seconds
            mins = int(extra // 60)
            secs = int(extra % 60)
            print(f"[STAGE] 距離 proxy 啟動還有 {mins}分 {secs}秒", flush=True)
            last_print = now_int
        time.sleep(min(remaining - t_minus_seconds, 30))


def _install_log_timestamp():
    """所有 [XXX] 開頭的 print 自動加 ms 級時間戳。
    例外：[TIMER] 由 GUI 端偵測前綴覆蓋同一行，不能改。
    monkey-patch builtins.print 後所有 import 的模組 (tixcraftapi/*) 都會跟著生效。"""
    import builtins
    _orig = builtins.print
    SKIP_PREFIXES = ("[TIMER]",)

    def _stamped(*args, **kwargs):
        if args and isinstance(args[0], str):
            head = args[0].lstrip()
            if head.startswith("[") and not any(head.startswith(p) for p in SKIP_PREFIXES):
                now = time.time()
                ts = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int((now % 1) * 1000):03d}"
                args = (f"[{ts}] {args[0]}", *args[1:])
        _orig(*args, **kwargs)

    builtins.print = _stamped


def main():
    sys.stdout.reconfigure(line_buffering=True)
    _install_log_timestamp()
    load_config_override()

    # --- userdata 模式：proxy 必須先 acquire 才能讓 Chrome login 走 proxy IP ---
    # （否則登入 cookie 綁到你家 IP，多帳號一比對就破功）
    proxy_acquired_early = False
    if config.ENABLE_PROXY_POOL and config.COOKIE_SOURCE == "userdata":
        proxy_pool.acquire()
        proxy_acquired_early = True

    # --- cookie 來源：userdata 模式用 Chrome profile 自己抓；string 模式用 config.COOKIE ---
    login_driver = None
    if config.COOKIE_SOURCE == "userdata":
        if not config.CHROME_USER_DATA_DIR:
            print("[ERROR] COOKIE_SOURCE=userdata 但沒設 CHROME_USER_DATA_DIR")
            return
        login_driver = browser_login.launch_browser(
            config.CHROME_USER_DATA_DIR,
            window_w=config.WINDOW_W, window_h=config.WINDOW_H,
            window_x=config.WINDOW_X, window_y=config.WINDOW_Y,
            proxy_url=config.CURRENT_PROXY,  # "" 時 Chrome 直連，不影響本機網路
        )
        if not browser_login.wait_for_login(login_driver):
            print("[ERROR] 登入未完成或超時")
            login_driver.quit()
            return
        config.COOKIE = browser_login.extract_cookies(login_driver)

    if not config.COOKIE:
        print("[ERROR] 沒有 COOKIE（檢查 config.py 或 user-data-dir 登入狀態）")
        if login_driver is not None:
            login_driver.quit()
        return

    # OCR 暖機不需要 session，可以最早做
    warmup_ocr()

    # --- string 模式才需要延遲 acquire（原邏輯：等 T-5 才連 proxy 避開 sticky session 過期）---
    # userdata 模式因為要給 Chrome 登入用，proxy 已提早 acquire，這段跳過
    if config.ENABLE_PROXY_POOL and not proxy_acquired_early:
        if config.ENABLE_TIME_WATCHER:
            wait_until_t_minus(config.TARGET_START_TIME, 300)
        proxy_pool.acquire()

    session = build_session(config.COOKIE)
    warmup_session(session, slug=config.ACTIVITY_SLUG)

    # 定時等待（可由 config 關閉，關閉後直接開搶）
    first_area_url: str | None = None
    if config.ENABLE_TIME_WATCHER:
        ka_stop = threading.Event()
        ka_thread = threading.Thread(
            target=keep_alive_loop,
            args=(session, ka_stop, config.ACTIVITY_SLUG),
            daemon=True,
        )
        ka_thread.start()
        print("[KEEPALIVE] 背景 ping 已啟動")
        try:
            wait_until_start(config.TARGET_START_TIME)
        finally:
            ka_stop.set()

        # T-0 後高頻 polling 抓開賣瞬間
        first_area_url = poll_until_open(
            session, config.ACTIVITY_SLUG,
            build_headers(referer=f"{BASE}/activity/detail/{config.ACTIVITY_SLUG}"),
            date_keyword=config.DATE_KEYWORD,
        )
    else:
        print("[TIMER] 定時啟動已關閉，直接開搶")

    # --- 進 FSM ---
    # 若 polling 階段已拿到 area_url，把它塞進 ctx 並從 AREA state 開始；否則從 GAME 開始
    ctx = Context(session=session, slug=config.ACTIVITY_SLUG)
    if first_area_url:
        ctx.area_url = first_area_url
        initial_state = State.AREA
        print(f"[GAME] 沿用 polling 拿到的場次: {first_area_url}")
    else:
        initial_state = State.GAME

    grabbed = False
    try:
        result_url = run(ctx, initial=initial_state, max_iter=30)
        if result_url:
            print(f"[SUCCESS] 搶到了! {result_url}")
            if login_driver is not None:
                # userdata 模式：cookie 灌回原本登入用的瀏覽器
                browser_login.inject_cookies_and_go(
                    login_driver, session, result_url, acc_id=config.ACC_ID)
                grabbed = True
            else:
                # string 模式：開新的乾淨 Chrome 注入 session
                open_chrome_with_session(session, result_url)
        else:
            print("[FAIL] FSM 結束未搶到")

    except KeyboardInterrupt:
        print("\n[EXIT] 手動中斷")
    finally:
        if login_driver is not None:
            if grabbed:
                # 搶到了：瀏覽器停在結帳頁，保持開啟讓使用者付款
                print("[SUCCESS] 結帳頁已開，瀏覽器保持開啟 — 付款完成後關閉本程式（GUI: STOP）")
                try:
                    while True:
                        time.sleep(3600)
                except KeyboardInterrupt:
                    pass
            try:
                login_driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()
