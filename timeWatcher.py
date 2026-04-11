# timeWatcher.py (V19.0 UtimeTool 網站對時版)

import requests
import time
import sys
import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

class TimeWatcher:
    def __init__(self, target_time_str, target_url):
        self.target_time_str = target_time_str
        self.target_url = target_url 
        self.target_time = None
        
        # 指定對時網站 (UtimeTool)
        # 註: #google_vignette 是網頁廣告錨點，對伺服器請求無影響，我們用主網址即可
        self.time_source_url = "https://utimetool.com/zh-tw/world-clock/"
        
        # 偽裝成瀏覽器，避免被網站擋
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
        }
        
        # 時間誤差 (標準台北時間 - 本地系統時間)
        self.time_offset = timedelta(seconds=0)

    def sync_with_website(self):
        """
        向 UtimeTool 網站發送請求，讀取其 Server Date Header
        並強制轉換為 GMT+8 台北時間
        """
        try:
            start_req = time.time()
            # 使用 HEAD 請求，只拿標頭不下載網頁內容，速度最快
            resp = requests.head(self.time_source_url, headers=self.headers, timeout=5)
            end_req = time.time()
            rtt = end_req - start_req # 網路來回時間

            if "Date" in resp.headers:
                # 1. 解析 HTTP Date (標準 GMT 時間)
                # 格式範例: "Fri, 31 Jan 2026 12:00:00 GMT"
                server_time_gmt = parsedate_to_datetime(resp.headers["Date"])
                
                # 2. 定義台灣時區 (GMT+8)
                tw_timezone = timezone(timedelta(hours=8))
                
                # 3. 強制轉換時區：GMT -> Taiwan (GMT+8)
                # 這樣無論該網站伺服器在哪，我們都拿到它當下的 "絕對時間" 並轉為 +8
                server_time_tw = server_time_gmt.astimezone(tw_timezone).replace(tzinfo=None)
                
                # 4. 加上 RTT/2 的網路延遲校正
                corrected_tw_time = server_time_tw + timedelta(seconds=rtt/2)
                
                # 5. 計算誤差：台北標準時間 - 本地系統時間
                local_now = datetime.fromtimestamp(end_req)
                self.time_offset = corrected_tw_time - local_now
                
                return corrected_tw_time
            else:
                print(f"⚠️ 網站未回傳 Date 標頭，嘗試備案...")
                return self.fallback_google_time()

        except Exception as e:
            print(f"⚠️ UtimeTool 連線失敗 ({e})，切換 Google 對時...")
            return self.fallback_google_time()
            
        return datetime.now()

    def fallback_google_time(self):
        """備案：萬一 UtimeTool 掛了，去抓 Google"""
        try:
            start = time.time()
            resp = requests.head("https://www.google.com", timeout=3)
            end = time.time()
            if "Date" in resp.headers:
                gmt = parsedate_to_datetime(resp.headers["Date"])
                tw_time = gmt.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
                self.time_offset = (tw_time + timedelta(seconds=(end-start)/2)) - datetime.fromtimestamp(end)
                return tw_time
        except:
            pass
        return datetime.now()

    def _calculate_target_datetime(self, current_tw_time):
        """根據「當下台北時間」決定目標是今天還是明天"""
        try:
            t = datetime.strptime(self.target_time_str, "%H:%M:%S").time()
            # 組合：當下台北日期 + 設定的時間
            target = current_tw_time.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
            
            # 如果目標時間 < 現在時間，代表是明天
            # (例如：現在台北 12:00，您設目標 11:00 -> 代表明天 11:00)
            if target < current_tw_time:
                target += timedelta(days=1)
                
            return target
        except ValueError:
            print("❌ 時間格式錯誤！請使用 HH:MM:SS")
            sys.exit(1)

    async def wait_for_open_async(self):
        print(f"⏳ 正在與 utimetool.com 對時 (強制轉 GMT+8)...")
        
        # 1. 取得絕對準確的台北時間
        now_tw = self.sync_with_website()
        last_sync_time = time.time()
        
        # 2. 計算目標時間
        self.target_time = self._calculate_target_datetime(now_tw)
        
        print(f"✅ 對時完成！")
        print(f"   - 台北標準時間: {now_tw.strftime('%Y-%m-%d %H:%M:%S')} (已校正)")
        print(f"   - 本地系統時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"   - 鎖定目標時間: {self.target_time.strftime('%Y-%m-%d %H:%M:%S')} (GMT+8)")
        
        # 顯示誤差 (若本地時間錯誤，這裡的數值會很大，這是正常的修正)
        offset_sec = self.time_offset.total_seconds()
        print(f"   - 自動補償誤差: {offset_sec:.3f} 秒")

        while True:
            # 每一輪迴圈，都用 (本地時間 + 誤差) = 準確的台北時間
            now = time.time()
            current_tw_time = datetime.fromtimestamp(now) + self.time_offset
            remaining = (self.target_time - current_tw_time).total_seconds()
            
            # 觸發點：提早 0.05 秒回傳
            if remaining <= 0.05:
                print("\n⚡⚡⚡ 時間到！啟動瀏覽器搶票！ ⚡⚡⚡")
                return True
            
            # --- 定期校正邏輯 ---
            time_since_sync = now - last_sync_time
            
            # 最後 10 秒不連線以免卡頓
            # 平時每 5 分鐘對時一次
            if remaining > 15 and time_since_sync >= 300:
                print(f"\n🔄 [校正] 同步網路時間... (剩 {remaining/60:.1f} 分)")
                self.sync_with_website()
                last_sync_time = time.time()

            # --- 顯示倒數 ---
            if remaining > 60:
                rem_str = f"{int(remaining//60)}分 {int(remaining%60)}秒"
            else:
                rem_str = f"{remaining:.1f}秒"
                
            sys.stdout.write(f"\r⏳ 台北時間倒數: {rem_str}      ")
            sys.stdout.flush()
            
            if remaining > 60: await asyncio.sleep(1)
            elif remaining > 10: await asyncio.sleep(0.5)
            else: await asyncio.sleep(0.05)