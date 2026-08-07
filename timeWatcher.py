# timeWatcher.py (V19.0 UtimeTool 網站對時版)

import requests
import statistics
import time
import sys
import asyncio
import ntplib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

TAIPEI = timezone(timedelta(hours=8))


def taipei_now() -> datetime:
    """現在的台北時間（naive），**跟機器時區無關**。

    不要用 `datetime.now()` 當台北時間：本機開發是 Asia/Taipei 所以剛好相等，
    但兩台搶票 VPS 的時區都是 Etc/UTC（伺服器慣例），那裡 `datetime.now()` 回的是
    UTC，拿去比 TARGET_START_TIME 會整整差 8 小時 —— 設 12:00 開賣會等到台灣晚上
    8 點才動。2026-08-07 在東京機上實測確認過。
    """
    return datetime.now(TAIPEI).replace(tzinfo=None)


class TimeWatcher:
    def __init__(self, target_time_str, target_url, lead_seconds=0.9):
        self.target_time_str = target_time_str
        self.target_url = target_url
        self.target_time = None
        # 提前幾秒觸發（給 polling 緩衝抓開賣瞬間）。各平台不同：
        # 拓元 0.7 / KKTIX 0.3 / 遠大 0.1（連線 RTT + 送單耗時越大者提前越多）
        self.lead_seconds = lead_seconds
        
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
        # resp.offset 只是「NTP 時間 − 本機時鐘」的毫秒級誤差，**完全不含時區**。
        # 直接拿它當 time_offset 的話，後面 `datetime.fromtimestamp(now) + offset`
        # 得到的是「機器當地時間」而不是台北時間 —— 在 Etc/UTC 的 VPS 上差 8 小時。
        # （HTTP fallback 那幾條路本來就有做 astimezone，所以只有這裡是壞的，
        #   而且會因為 NTP 通不通而行為不一致，特別難查。）
        #
        # 換算交給 _apply_offset —— 跟 HTTP fallback 共用同一套 time_offset 語意，
        # 免得兩條路又各自解讀一次時區。
        tw_now = self._apply_offset(offset)
        print(f"✔ NTP 對時完成（採用 {server}, offset={offset*1000:+.1f}ms, delay={delay*1000:.1f}ms）")
        return tw_now

    def _http_clock_offset(self, url, label, samples=8, interval=0.15):
        """用 HTTP `Date` 標頭量「伺服器時鐘 − 本機時鐘」，回秒數（拿不到回 None）。

        HTTP `Date` 只到「秒」，所以**單發讀取誤差最大 1 秒**，而且是系統性的：
        Date 永遠 ≤ 真實時間，等於平均把時間看早 0.5 秒 —— 換算成搶票就是平均晚
        0.5 秒、最糟晚 1 秒才動手。舊版就是單發讀完直接加 rtt/2 當精確值。

        解法跟 time.navyism.com 那類對時網站一樣，只是換個等價但更嚴謹的寫法 ——
        **區間交集**：
          一發請求送出 t0、收到 t1、回應 Date=D（該秒的起點）。伺服器產生標頭的
          瞬間必定落在 [D, D+1)，而那瞬間在本機時鐘上約是 m=(t0+t1)/2。
          => offset ∈ [D-m, D+1-m)
        每發都給一個寬 1 秒的區間，取交集就會收斂到 RTT 等級（實測 8 發約 ±30ms）。

        注意這量到的是**回應那台機器**的時鐘。掛 CDN 的網址（拓元 Fastly、遠大
        CloudFront）每發可能打到不同邊緣節點、各有各的時鐘，區間會交不出東西 ——
        真發生就回 None 換下一個來源，不要硬湊。（2026-08-07 實測：遠大直連
        origin 的 queue / api 跟 NTP 差 +2ms / −4ms，而 CloudFront 那兩支交集矛盾。）
        """
        lo, hi, used, rtts = -1e9, 1e9, 0, []
        for _ in range(samples):
            try:
                t0 = time.time()
                resp = requests.head(url, headers=self.headers, timeout=3)
                t1 = time.time()
            except Exception as e:
                print(f"  ⚠️ {label} 請求失敗 ({type(e).__name__})")
                return None
            d = resp.headers.get("Date")
            if not d:
                print(f"  ⚠️ {label} 沒有 Date 標頭")
                return None
            D = parsedate_to_datetime(d).timestamp()
            m = (t0 + t1) / 2
            lo, hi = max(lo, D - m), min(hi, D + 1 - m)
            used += 1
            rtts.append((t1 - t0) * 1000)
            if lo > hi:
                print(f"  ⚠️ {label} 區間矛盾（多半是 CDN 多節點各有時鐘），跳過")
                return None
            time.sleep(interval)

        offset = (lo + hi) / 2
        print(f"  ✔ {label} 對時成功（offset={offset*1000:+.0f}ms, "
              f"不確定 ±{(hi-lo)*500:.0f}ms, rtt={statistics.median(rtts):.0f}ms, {used} 發）")
        return offset

    def _apply_offset(self, clock_offset):
        """把「伺服器/NTP 時鐘 − 本機時鐘」換算成 self.time_offset 的語意
        （真實台北牆上時間 − 機器牆上時間），回當下的台北時間。"""
        true_epoch = time.time() + clock_offset
        tw_now = datetime.fromtimestamp(true_epoch, TAIPEI).replace(tzinfo=None)
        self.time_offset = tw_now - datetime.fromtimestamp(time.time())
        return tw_now

    def sync_with_website(self):
        """對時主入口：優先 NTP（~2-15ms），失敗才退 HTTP Date（~±30ms）。

        NTP 為主是實測過的選擇，不是預設值：搶票要對的是「開賣那台機器的時鐘」，
        而 2026-08-07 從東京量到遠大直連 origin 的 queue/api 跟 NTP 只差 +2ms/−4ms
        （小於量測誤差），代表對方本來就有 NTP 同步。改對 HTTP Date 只會更差。
        """
        # ── Primary: NTP ──
        print("⏳ 嘗試 NTP 對時（毫秒級精度）...")
        ntp_result = self.sync_with_ntp()
        if ntp_result:
            return ntp_result

        # ── Fallback: HTTP Date（秒邊界偵測）──
        print("⚠️ NTP 全部失敗，fallback 到 HTTP Date 對時...")
        for url, label in ((self.time_source_url, "tixcraft"),
                           (self.fallback_url_2, "utimetool"),
                           ("https://www.google.com", "google")):
            offset = self._http_clock_offset(url, label)
            if offset is not None:
                return self._apply_offset(offset)

        # 全都失敗：至少把時區處理對，不要拿機器當地時間當台北時間
        print("❌ 所有對時來源都失敗，只能用本機時鐘（VPS 上請確認時區）")
        return self._apply_offset(0.0)

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

    async def wait_for_open_async(self, on_tick=None):
        """on_tick: 每輪倒數呼叫一次 `on_tick(remaining_seconds)`，**跑在主執行緒上**。
        用途是讓呼叫端在倒數期間 ping 自己的連線（curl_cffi 連線池是 thread-local，
        背景 keep-alive thread 暖不到主執行緒這條，見 tixcraftapi/session.py 說明）。
        callback 自己控制頻率、自己吞例外；這裡只保證不讓它把倒數搞爆。"""
        print(f"⏳ 正在與 utimetool.com 對時 (強制轉 GMT+8)...")
        
        # 1. 取得絕對準確的台北時間
        now_tw = self.sync_with_website()
        last_sync_time = time.time()
        
        # 2. 計算目標時間
        self.target_time = self._calculate_target_datetime(now_tw)
        
        print(f"✅ 對時完成！")
        print(f"   - 台北標準時間: {now_tw.strftime('%Y-%m-%d %H:%M:%S')} (已校正)")
        # 印出機器時區：VPS 是 UTC、本機是 CST，這兩行差 8 小時是正常的。
        # 真正該盯的是上面「台北標準時間」那行對不對。
        print(f"   - 本地系統時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
              f"(機器時區 {time.strftime('%Z')})")
        print(f"   - 鎖定目標時間: {self.target_time.strftime('%Y-%m-%d %H:%M:%S')} (GMT+8)")
        
        # 顯示誤差 (若本地時間錯誤，這裡的數值會很大，這是正常的修正)
        offset_sec = self.time_offset.total_seconds()
        print(f"   - 自動補償誤差: {offset_sec:.3f} 秒")

        while True:
            # 每一輪迴圈，都用 (本地時間 + 誤差) = 準確的台北時間
            now = time.time()
            current_tw_time = datetime.fromtimestamp(now) + self.time_offset
            remaining = (self.target_time - current_tw_time).total_seconds()
            
            # 觸發點：提早 self.lead_seconds 秒回傳，給 polling 緩衝抓開賣瞬間
            if remaining <= self.lead_seconds:
                print("\n⚡⚡⚡ 時間到！啟動瀏覽器搶票！ ⚡⚡⚡")
                return True

            if on_tick is not None:
                try:
                    on_tick(remaining)
                except Exception as e:
                    print(f"[TIMER] on_tick 異常（忽略）: {type(e).__name__}: {e}")
            
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