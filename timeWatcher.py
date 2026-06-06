# timeWatcher.py (V19.0 UtimeTool 網站對時版)

import requests
import time
import sys
import asyncio
import ntplib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

class TimeWatcher:
    def __init__(self, target_time_str, target_url):
        self.target_time_str = target_time_str
        self.target_url = target_url 
        self.target_time = None
        
        # 直接對 tixcraft 自己的伺服器時間 — 搶票判斷的權威時間
        # （fallback: utimetool → google）
        self.time_source_url = "https://tixcraft.com/"
        self.fallback_url_2 = "https://utimetool.com/zh-tw/world-clock/"
        
        # 偽裝成瀏覽器，避免被網站擋
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
        }
        
        # 時間誤差 (標準台北時間 - 本地系統時間)
        self.time_offset = timedelta(seconds=0)

    def sync_with_ntp(self):
        """
        NTP 對時（毫秒級精度）。
        試多個 server，用 RTT 最小的那次為準。
        time.cloudflare.com 跟 tixcraft 同 CDN 體系，最接近 server 真實時間。
        """
        servers = [
            "time.cloudflare.com",  # tixcraft 用 Cloudflare CDN
            "time.google.com",
            "pool.ntp.org",
        ]
        client = ntplib.NTPClient()
        best = None  # (offset, delay, server)

        for server in servers:
            try:
                resp = client.request(server, version=3, timeout=2)
                print(f"  ✔ NTP {server}: offset={resp.offset*1000:+.1f}ms delay={resp.delay*1000:.1f}ms")
                if best is None or resp.delay < best[1]:
                    best = (resp.offset, resp.delay, server)
            except Exception as e:
                print(f"  ⚠️ NTP {server} 失敗: {e}")

        if best is None:
            return None

        offset, delay, server = best
        self.time_offset = timedelta(seconds=offset)
        tw_now = datetime.fromtimestamp(time.time()) + self.time_offset
        print(f"✔ NTP 對時完成（採用 {server}, offset={offset*1000:+.1f}ms, delay={delay*1000:.1f}ms）")
        return tw_now

    def sync_with_website(self):
        """
        對時主入口：優先 NTP（~5-15ms 精度），失敗 fallback HTTP Date。
        """
        # ── Primary: NTP ──
        print("⏳ 嘗試 NTP 對時（毫秒級精度）...")
        ntp_result = self.sync_with_ntp()
        if ntp_result:
            return ntp_result

        # ── Fallback: tixcraft HTTP Date ──
        print("⚠️ NTP 全部失敗，fallback 到 tixcraft HTTP Date...")
        try:
            start_req = time.time()
            resp = requests.head(self.time_source_url, headers=self.headers, timeout=5)
            end_req = time.time()
            rtt = end_req - start_req

            if "Date" in resp.headers:
                server_time_gmt = parsedate_to_datetime(resp.headers["Date"])
                tw_timezone = timezone(timedelta(hours=8))
                server_time_tw = server_time_gmt.astimezone(tw_timezone).replace(tzinfo=None)
                corrected_tw_time = server_time_tw + timedelta(seconds=rtt/2)
                local_now = datetime.fromtimestamp(end_req)
                self.time_offset = corrected_tw_time - local_now
                return corrected_tw_time
            else:
                print(f"⚠️ 網站未回傳 Date 標頭，嘗試備案...")
                return self.fallback_google_time()

        except Exception as e:
            print(f"⚠️ tixcraft 對時失敗 ({e})，切換 utimetool...")
            return self._sync_fallback(self.fallback_url_2, label="utimetool")

        return datetime.now()

    def _sync_fallback(self, url, label):
        """通用 fallback：抓 url 的 Date header 對時，再失敗就 google。"""
        try:
            start = time.time()
            resp = requests.head(url, headers=self.headers, timeout=3)
            end = time.time()
            rtt = end - start
            if "Date" in resp.headers:
                gmt = parsedate_to_datetime(resp.headers["Date"])
                tw_time = (gmt.astimezone(timezone(timedelta(hours=8)))
                              .replace(tzinfo=None)
                           + timedelta(seconds=rtt / 2))
                self.time_offset = tw_time - datetime.fromtimestamp(end)
                print(f"  ✔ {label} 對時成功 (rtt={rtt*1000:.0f}ms)")
                return tw_time
        except Exception as e:
            print(f"  ⚠️ {label} 失敗 ({e})")
        print("  → 嘗試 google fallback...")
        return self.fallback_google_time()

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
            
            # 觸發點：提早 0.9 秒回傳，給 polling 緩衝抓開賣瞬間
            if remaining <= 0.9:
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
                
            if sys.stdout.isatty():
                sys.stdout.write(f"\r⏳ 台北時間倒數: {rem_str}      ")
                sys.stdout.flush()
            else:
                # GUI 模式：用換行 + 固定前綴，讓 GUI 偵測並覆蓋同一行
                print(f"[TIMER] 倒數: {rem_str}", flush=True)
            
            if remaining > 60: await asyncio.sleep(1)
            elif remaining > 10: await asyncio.sleep(0.5)
            else: await asyncio.sleep(0.05)