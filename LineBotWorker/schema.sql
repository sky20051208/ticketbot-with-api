-- D1 schema，部署後跑一次：
--   wrangler d1 execute linebot-ticket-db --remote --file=schema.sql

CREATE TABLE IF NOT EXISTS customers (
  user_id TEXT PRIMARY KEY,
  name    TEXT NOT NULL,
  concert TEXT
);

-- 客人在「搶票」流程問到哪一步（name / concert），取代本機版用記憶體字典的做法，
-- 因為 Workers 無狀態、不能跨請求保留記憶體。
CREATE TABLE IF NOT EXISTS pending (
  user_id TEXT PRIMARY KEY,
  stage   TEXT NOT NULL,
  name    TEXT
);

-- 結帳頁截圖暫存（LINE 圖片訊息要求公開 HTTPS 網址，本機 Python 沒有，借這裡當圖床）。
CREATE TABLE IF NOT EXISTS screenshots (
  id         TEXT PRIMARY KEY,
  data       BLOB NOT NULL,
  created_at INTEGER NOT NULL
);
