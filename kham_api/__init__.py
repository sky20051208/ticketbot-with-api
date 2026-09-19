"""寬宏 Kham 搶票 API 模式套件（nodriver 登入 + 頁內 fetch + ddddocr 驗證碼）。

Entry point: `python -m kham_api --config <path>` → 走 __main__.py 的 main()。
GUI 平台選 KHAM；ACTIVITY SLUG 填 PRODUCT_ID 或整串活動網址。只支援 chrome profile 登入
（`python create_profile.py --name X --platform kham`），COOKIE 欄位不用。

⚠️ **寬宏的登入態 profile 存不住，每次啟動都要人工重登一次**（2026-09-19 查 profile 的
cookie DB 確認）：登入靠 `ASP.NET_SessionId`，那是 session cookie、關掉瀏覽器就沒了，
硬碟裡只留下 `/_702_01`、`/_705` 那幾顆 —— 但**未登入**的請求也拿得到同名 cookie，
所以它們不是登入憑證。跟拓元 / 遠大靠 profile 撐著的做法不一樣，別誤以為登一次就長久有效。
登入頁只有三個欄位（帳號=身分證字號 / 密碼 / 4 碼驗證碼），要做自動登入隨時可以補，
2026-09-19 問過使用者，決定維持手動。

流程（非實名制，自行選位）：
  登入 → 分頁停到 UTK0201_00（選日期）→ 倒數（背景每 30s 打一次續命 + 查登入）
       → 以下全部頁內 fetch，分頁不動：
         UTK0201_00 挑場次 → 票區頁（UTK0204 或 UTK0201_000）輪詢到有空位
       → 選位頁 UTK0205：解析 seatStr 挑空位 + 解 /pic.aspx 驗證碼
       → POST UTK0205 action=ADD_SHOPPING_CAR → 分頁才跳購物車 UTK0206
  UTK0201_00 / 票區頁 / UTK0205 都不用登入就能看，只有加入購物車要登入。
  沒搶到就無限清票等回流（有票秒搶 / 售完 5s / 被擋 8s，跟拓元 FSM 同一套節奏），
  停止條件只有：搶到、重試也沒用的錯誤、使用者按 GUI STOP。
  頁內 fetch 票區頁 145~307ms，整頁導航要 608~695ms（圖片、JS、座位圖 .map 全都要載）。

選日期頁「立即訂購」有三種去處（parsing._PERF_PAGES）：
  UTK0204 / UTK0201_000 = 選票區（後者多了自行選位 / 電腦配位分頁），支援
  UTK0202 = 不劃位輸入張數（音樂節、自由入場），**還沒做**，bot 會直接說不支援

伺服器（2026-09-17 Globalping 實測）：GCP Global LB（anycast 34.160.49.102，`via: 1.1 google`），
後端在台灣（頁面自己寫 `proj = '售票平台_GCP'`）。回源首位元組：台北 12~17ms / 東京·香港 45~51ms /
洛杉磯 135ms / Ashburn 200ms（冷連線 ×4）。**不要跟拓元一起搬美東**，家裡或東京機都可以。
靜態檔（imgs2.utiki.com.tw、favicon）走 Cloud CDN，量延遲要打 robots.txt 這種會回源的 404。

跟 kktix_api 一樣的鐵律：
  - 每個檔都用 `import config` + `config.XXX`，絕不 `from config import X`，
    也絕不把 config.X 當 function default arg。
  - 共用 BASE 從 `from kham_api import BASE` 取；HTML/JS 解析一律放 parsing.py。

⚠️ 驗證碼：/pic.aspx 的 4 碼（大小寫不敏感、只有 25 種字），三個地方會出現 ——
   登入頁 TYPE=UTK1306（人手動登入，bot 不管）、選位頁 TYPE=UTK0205（送單，captcha.py）、
   不劃位張數頁 TYPE=UTK0202（還沒做）。票區頁 / 購物車沒有，全站也沒有 reCAPTCHA。
   拓元的 captchaAI/predict.py 是另一套模型，不共用。
⚠️ 實名制場（IS_NAME_BASED=1）流程多幾步，尚未實作；目前只做非實名制。
"""

BASE = "https://kham.com.tw"
