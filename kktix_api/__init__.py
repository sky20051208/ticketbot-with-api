"""KKTIX 搶票 API 模式套件（curl_cffi 直打 HTTP + 瀏覽器交棒完成下單）。

Entry point: `python -m kktix_api --config <path>` → 走 __main__.py 的 main()。
__main__.py 只做串接 (cookie → 暖機 → 倒數 → 偵測開賣 → 下單 → 交棒結帳)，
搶票步驟在以下各檔。

呼叫鏈（搶票順序）：
  register.poll_until_open → reserve.reserve_ticket → browser_session.open_chrome_with_session
  register 內部用 parsing.parse_registration_page 解析 Angular 票區，
  parsing.select_ticket 依 config（AREA_KEYWORD / EXCLUDE / MODE / AMOUNT）挑票。

跟 tixcraftapi 一樣的鐵律：
  - 每個檔都用 `import config` + `config.XXX`，絕不 `from config import X`，
    也絕不把 config.X 當 function default arg。
  - 共用 BASE 從 `from kktix_api import BASE` 取。

⚠️ KKTIX 與拓元的差異：reserve（下單）那一步是 Angular SPA + 可能有 reCAPTCHA，
   純 HTTP POST 的 payload 形狀需要對真實活動抓一次包才能 100% 鎖死。reserve.py 內
   的 HTTP 下單為 best-effort（已標註），失敗會 fallback 到瀏覽器交棒由使用者完成。
"""

BASE = "https://kktix.com"
