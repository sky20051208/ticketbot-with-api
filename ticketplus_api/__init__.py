"""遠大 TicketPlus 純 API 搶票套件（`python -m ticketplus_api --config X`）。

TicketPlus 前台是 Vue SPA，所有資料都走 JSON API，**沒有任何 HTML 要解析**，所以整條
鏈路除了「登入」以外都是純封包（curl_cffi），比 KKTIX / 寬宏的頁面內 fetch 更快。

四組 API（都是從前端 bundle 的 VUE_APP_* 環境變數挖出來的）：
  CONFIG_API  唯讀、**不需要登入**：活動目錄 + 即時票況（開賣偵測就靠這支）
  QUEUE_API   送單第一段：enqueue 拿 uuid（排隊系統）
  TICKET_API  送單第二段：reserve 用 uuid 換 orderId
  USER_API    會員資料（目前只用來驗 token 有效）

**id 有兩種形式，別搞混**（見 [crypto.py](ticketplus_api/crypto.py)）：
  - 加密 id（32 hex）：出現在網址 `/activity/<id>` 與 static S3 json，例 ac64dbcc...218e
  - 明文 id：`e000001451` / `s000002136` / `a000008654` / `p000015682`
  S3 靜態目錄檔吃「加密 eventId」，CONFIG_API 的 /get 吃「明文 id」。
  ticketAreaId / productId 在 S3 裡本來就是明文，所以主線流程不需要解密。
"""
BASE = "https://ticketplus.com.tw"
CONFIG_API = "https://apis.ticketplus.com.tw/config/api/v1"
QUEUE_API = "https://queue.ticketplus.com.tw/queue/api/v1"
TICKET_API = "https://api.ticketplus.com.tw/ticket/api/v1"
USER_API = "https://api.ticketplus.com.tw/user/api/v1"
