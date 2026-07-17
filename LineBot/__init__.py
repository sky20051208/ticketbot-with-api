"""LINE 機器人套件 — 客服選單 + 搶到票後通知客人。

  line_push.py     單向推播（runner 在 TERMINAL_OK 時呼叫 notify_grabbed）

客服選單 / 搶票登記邏輯跑在 Cloudflare Workers（../LineBotWorker/），不是本機常駐
process —— 這樣電腦關著客人也能互動，也不佔本機資源。客人資料存 Cloudflare D1，
webgui 透過 config.py 的 LINE_WORKER_URL + LINE_WORKER_ADMIN_KEY 打 API 讀寫
（見 webgui/server.py 的 customers 相關 route，皆為 proxy，本機不再存客人資料庫）。

token / secret / 匯款資訊在 config.py 的 LINE 區塊。
"""
