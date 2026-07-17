"""拓元搶票 API 模式套件（curl_cffi 直打 HTTP）。

Entry point: `python -m tixcraftapi --config <path>` → 走 __main__.py 的 main()。
__main__.py 只做串接 (cookie → 暖機 → 定時 → 重試迴圈 → 搶到後接管)，搶票步驟在以下各檔。

呼叫鏈（FSM，runner.run 派發）：
  GAME(game.select_game) → AREA(area.select_area) → TICKET(submit.submit_ticket)
  → QUEUE(order.follow_order) → CHECKOUT
  AREA 被導去驗證頁 → VERIFY(verify.handle_verify) → 回 AREA（不吃 cooldown）

協議：步驟函式只收 (session, url, 明確參數)、回「落點 URL 或 None」，不讀 config、
不認識彼此；URL 語意只由 state.classify 判斷；config 注入 / captcha prefetch 的
跨步驟接力（AREA 啟動、TICKET 消費）都在 runner.py 的 handler 層。

  搶到結帳頁後 → finalize.open_chrome_with_session（string 模式）
                  或 browser_login.inject_cookies_and_go（userdata 模式，在 __main__.main 處理）
"""

BASE = "https://tixcraft.com"
