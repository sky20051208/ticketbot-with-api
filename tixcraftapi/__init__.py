"""拓元搶票 API 模式套件（curl_cffi 直打 HTTP）。

Entry point: `python -m tixcraftapi --config <path>` → 走 __main__.py 的 main()。
__main__.py 只做串接 (cookie → 暖機 → 定時 → 重試迴圈 → 搶到後接管)，搶票步驟在以下各檔。

呼叫鏈（搶票順序）：
  game.select_game → area.select_area → submit.submit_ticket → order.follow_order
  area.select_area 內部會在被 redirect 時 call verify.handle_verify
  submit.submit_ticket 並行抓表單+驗證碼 (captcha.fetch_captcha_image)
  搶到結帳頁後 → finalize.open_chrome_with_session（string 模式）
                  或 browser_login.inject_cookies_and_go（userdata 模式，在 __main__.main 處理）
"""

BASE = "https://tixcraft.com"
