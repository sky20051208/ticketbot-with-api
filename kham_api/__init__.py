"""寬宏 Kham 搶票 API 模式套件（curl_cffi/瀏覽器 fetch 直打 + ddddocr 驗證碼）。

Entry point: `python -m kham_api --config <path>` → 走 __main__.py 的 main()。

流程（非實名制，自行選位）：
  登入 → GET UTK0201_00(選日期→PERFORMANCE_ID) → GET UTK0204(選票區→PERFORMANCE_PRICE_AREA_ID)
       → 選位頁 UTK0205：讀 seats 物件挑空位 + ddddocr 解 /pic.aspx 驗證碼
       → POST UTK0205 action=ADD_SHOPPING_CAR → 購物車 UTK0206 → 結帳

跟 kktix_api 一樣的鐵律：
  - 每個檔都用 `import config` + `config.XXX`，絕不 `from config import X`，
    也絕不把 config.X 當 function default arg。
  - 共用 BASE 從 `from kham_api import BASE` 取；HTML/JS 解析一律放 parsing.py。

⚠️ 驗證碼：寬宏是 /pic.aspx 的 4 碼英數（大小寫不敏感），用 ddddocr（見 captcha.py）；
   拓元的 captchaAI/predict.py 是另一套自訓模型，兩者不共用。
⚠️ 實名制場（IS_NAME_BASED=1）流程多幾步，尚未實作；目前只做非實名制。
"""

BASE = "https://kham.com.tw"
