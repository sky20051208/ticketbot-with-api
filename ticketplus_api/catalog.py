"""活動目錄 + 即時票況（CONFIG_API，唯讀且**不需要登入**）。

兩層資料，用途完全不同：
  1. static S3 目錄（`getS3?path=event/<加密eventId>/*.json`）—— 場次 / 票區 / 票種的
     「靜態結構」，開賣前就抓好，跑一次就夠：sessions / ticketAreas / products
  2. `/get?productId=..&ticketAreaId=..` —— 「即時票況」：status(onsale/soldout/…)、
     count(剩餘)、purchaseLimit、saleStart。**開賣偵測就是高頻打這一支**（實測 ~160ms）

本檔只負責拿資料，挑哪一個交給 [parsing.py](ticketplus_api/parsing.py)。
"""
import itertools
import math
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests as cf_requests

from ticketplus_api import CONFIG_API
from ticketplus_api.session import build_headers

# 併行 burst 的逐發去/回 log。預設關（20 發/秒 × 2 行會洗版、還多花 print I/O）；
# 要盯開賣瞬間「pending → onsale 翻牌」時設 TP_POLL_VERBOSE=1 打開。
# 併行時多發同時在飛，回應順序跟送出順序不一定一致，所以每發帶一個序號配對去跟回。
_POLL_VERBOSE = os.environ.get("TP_POLL_VERBOSE") == "1"
_poll_seq = itertools.count(1)

_S3 = f"{CONFIG_API}/getS3"

# 變體數低於這個就沒有繞過快取的意義：TTL 約 1.5s、輪詢 0.3s 代表一個 TTL 內會打 5 發，
# 變體太少就會在快取還沒過期時就轉回同一個 key。留 4 倍餘裕。
_MIN_VARIANTS = 20


def _variants(n_products: int, n_areas: int) -> int:
    """排列組合數 = 兩邊階乘相乘。只要判斷「夠不夠」，所以超過就直接夾住 ——
    158 個票區真的去算 158! 沒有意義（而且慢）。"""
    total = 1
    for n in (n_products, n_areas):
        for k in range(2, min(n, 8) + 1):     # 8! = 40320，遠超 _MIN_VARIANTS
            total *= k
    return total


def _get_json(session: cf_requests.Session, url: str, timeout: float = 10.0) -> dict:
    res = session.get(url, headers=build_headers(), timeout=timeout)
    if res.status_code != 200:
        print(f"[API] HTTP {res.status_code} {url[:80]}")
        return {}
    try:
        return res.json()
    except Exception:
        print(f"[API] 回傳非 JSON: {res.text[:120]}")
        return {}


def get_session_info(session: cf_requests.Session, session_id: str) -> dict:
    """查單一場次的即時設定。session_id 要**明文**（s000002171），加密的先用
    `crypto.decrypt_id()` 轉。

    開賣前檢查用（不在熱路徑上，所以不做 `_fresh_params` 那套繞快取）。要看的是
    `transactionValidType` —— 它非空就代表這個場次開了「專屬代碼」驗證，而且**它比票種的
    `serialKey` 更早出現**：2026-08-08 看 NCT WISH（s000002171）時場次層已經是
    `sk00000466`，71 個票種的 serialKey 卻還全是 null（主辦設定還沒補完）。
    只看 serialKey 會漏掉這種還沒設定完的場次。
    """
    data = _get_json(session, f"{CONFIG_API}/get?sessionId={session_id}&")
    if data.get("errCode") not in ("00", None):
        print(f"[INFO] 場次設定查詢 errCode={data.get('errCode')}")
        return {}
    sessions = (data.get("result") or {}).get("session") or []
    return sessions[0] if sessions else {}


def get_event_info(session: cf_requests.Session, event_id: str) -> dict:
    """查活動層設定。event_id 要**明文**（e000001417），加密的先用 `crypto.decrypt_id()` 轉。

    目前只為了一個欄位：**`isLottery`**。TicketPlus 有一種「登記抽選」活動，前端整套
    UI 都換掉（語系檔有 35 條 `_lottery` 字串：立即登記 / 排隊登記中 / 您的登記已完成），
    判斷依據就是 `campaign.isLottery`，而 campaign 就是這支 API 回的東西。

    對 bot 來說這是**最重要的一個旗標**：抽選活動是「登記完之後隨機抽」
    （AKASAKI 那場的公告原文：「將根據登記訂單進行隨機抽選」），送單快 40 毫秒
    完全不影響中籤率。實測正負樣本：
      e000001417（AKASAKI 登記抽選）→ isLottery=True
      e000001329（一般搶票活動）    → 根本沒有這個欄位

    **注意不要跟 `lotteryDeadline` 搞混** —— 那個一般活動也有，是別的東西（付款期限），
    不能拿來判斷。
    """
    data = _get_json(session, f"{CONFIG_API}/get?eventId={event_id}&")
    if data.get("errCode") not in ("00", None):
        print(f"[INFO] 活動設定查詢 errCode={data.get('errCode')}")
        return {}
    result = (data.get("result") or {}).get("event") or data.get("result") or {}
    if isinstance(result, list):
        result = result[0] if result else {}
    return result if isinstance(result, dict) else {}


def fetch_catalog(session: cf_requests.Session, event_id: str) -> dict:
    """抓活動的靜態目錄。event_id 用**網址上那串加密 id**。
    回 {"sessions": [...], "ticketAreas": [...], "products": [...]}，抓不到的鍵給空 list。"""
    catalog = {}
    for key, filename in (("sessions", "sessions.json"),
                          ("ticketAreas", "ticketAreas.json"),
                          ("products", "products.json")):
        data = _get_json(session, f"{_S3}?path=event/{event_id}/{filename}")
        if data.get("errCode"):
            print(f"[CATALOG] {filename} 取得失敗: {data.get('errCode')} {data.get('errDetail', '')}")
        catalog[key] = data.get(key) or []
    print(f"[CATALOG] 場次 {len(catalog['sessions'])} / 票區 {len(catalog['ticketAreas'])} "
          f"/ 票種 {len(catalog['products'])}")
    return catalog


def _fresh_params(product_ids: list[str] | None,
                  ticket_area_ids: list[str] | None) -> list[str]:
    """組 `/get` 的 query string，並且**每次都換一個 CloudFront cache key**。

    這支 API 被 CloudFront 快取，TTL 約 1.5 秒（實測 `X-Cache` 由 Hit 轉 Miss 的週期）。
    照原樣打的話 3ms 就回來，但拿到的是最多 1.5 秒前的舊票況 —— 偵測延遲直接被綁死在
    ~900ms，**而且再怎麼縮短輪詢間隔都沒用，只是重複讀同一份快取**。

    繞法是把 id 的順序打亂：cache key 是照參數字串算的，`p1,p2` 和 `p2,p1` 是兩個不同
    的 key，但回應內容完全一樣。實測連打 8 發全是 Miss，每發都真的回東京 origin。
    安全性也確認過：`parsing.index_infos()` 是用 `id` 建字典的（`{p["id"]: p}`），
    完全不看順序，所以打亂請求順序不影響任何下游判斷。

    試過但不能用的兩招：
      - 加 `&_=<亂數>`：cache key 只認白名單參數（productId / ticketAreaId），未知的
        直接忽略 → 還是 Hit
      - 重複同一個 id 當變體：行為不一致，重複 2 次回 errCode 102、3 次才又正常，
        不能拿來當機制

    代價是每一發都打到 origin 而不是吃 CDN。單一 IP 實測到 12 req/s 都沒被節流
    （p50 平平的 170ms 沒劣化），但**多開時總速率是 N 倍**，間隔要自己乘回去。

    票種和票區都只有 1~2 個的活動排列數不夠，這時就退回原本的行為 —— 不會更糟，
    只是沒有變好。**變體夠不夠的警告在 `InfoPoller.__init__`**，不在這裡：這支也會被
    開賣前的一次性檢查呼叫（那種只帶 1~2 個 id，警告會變成假警報），而且真正在意
    快取新鮮度的是高頻偵測那條路。
    """
    products = list(product_ids or [])
    areas = list(ticket_area_ids or [])

    params = []
    if products:
        params.append("productId=" + ",".join(random.sample(products, len(products))))
    if areas:
        params.append("ticketAreaId=" + ",".join(random.sample(areas, len(areas))))
    return params


def _status_summary(result: dict, blocked: bool) -> str:
    """把一發票況壓成一行看得懂的摘要，給 TP_POLL_VERBOSE 的「回」用。
    重點是讓人一眼看到「哪些票種是 onsale、還剩幾張」——盯的就是它從 pending 翻過去那刻。"""
    if blocked:
        return "⛔ 被擋/流量管制"
    products = (result or {}).get("product") or []
    if not products:
        return "（空／尚未有票況）"
    tally: dict[str, list] = {}
    for p in products:
        st = str(p.get("status", "?"))
        tally.setdefault(st, []).append(p.get("count"))
    parts = []
    for st, counts in tally.items():
        total = sum(c for c in counts if isinstance(c, (int, float)))
        parts.append(f"{st}×{len(counts)}（共剩 {total}）")
    return "  ".join(parts)


def get_infos(session: cf_requests.Session, product_ids: list[str] | None = None,
              ticket_area_ids: list[str] | None = None,
              timeout: float = 10.0, log: bool = True) -> tuple[dict, bool]:
    """即時票況。id 一律用**明文**（p000…/a000…）。回 (result, blocked)。

    前端就是把 id 用逗號串起來當 query string，一次可以查整場所有票種 —— 所以偵測開賣
    只要一發請求，不用每個票區各打一次。

    blocked=True 代表被流量管制/擋（HTTP 429/403 或 errCode 110 流量管制）→ 呼叫端該退避，
    對齊拓元撞 403 的 BLOCKED cooldown（被擋還用 0.3s 狂打只會更慘）。
    """
    url = f"{CONFIG_API}/get?{'&'.join(_fresh_params(product_ids, ticket_area_ids))}&"

    t0 = time.perf_counter()
    try:
        res = session.get(url, headers=build_headers(), timeout=timeout)
    except Exception as e:
        print(f"[INFO] 票況請求失敗: {type(e).__name__}")
        return {}, False
    rtt = (time.perf_counter() - t0) * 1000
    if res.status_code in (403, 429):
        print(f"[INFO] HTTP {res.status_code}（流量管制/被擋）")
        return {}, True
    if res.status_code != 200:
        print(f"[INFO] HTTP {res.status_code} {url[:80]}")
        return {}, False
    try:
        data = res.json()
    except Exception:
        print(f"[INFO] 回傳非 JSON: {res.text[:120]}")
        return {}, False
    code = data.get("errCode")
    if code not in ("00", None):
        blocked = str(code) == "110"      # 110 = 流量管制
        print(f"[INFO] errCode={code} {data.get('errMsg', '')}")
        return {}, blocked
    if log:
        print(f"[INFO] 票況 RTT {rtt:.0f}ms")
    return data.get("result") or {}, False


# 併行偵測（見 InfoPoller）：RETRY_INTERVAL 小於這個值才值得開，再大的話 RTT 已經不是
# 瓶頸，多開連線只是白白多打對方。
PIPELINE_BELOW = 0.15
# 在途上限。實際同時在途數 ≈ RTT / interval（65ms / 50ms ≈ 2），這裡抓 0.3s 的 RTT
# 餘裕是為了 T-0 塞車那幾發（實測會噴到 127ms），寧可多備 thread 也不要卡住節奏。
_INFLIGHT_RTT_BUDGET = 0.30
_MAX_INFLIGHT = 8


class InfoPoller:
    """票況讀數來源，把「多久送一發」跟「等不等上一發回來」拆開。

    依序輪詢時真正的週期是 **RTT + interval**，不是 interval —— 實測東京→遠大
    65ms + 50ms = 115ms，所以 `RETRY_INTERVAL=0.05` 只跑出 8.7 次/秒，偵測盲區
    平均 58ms。併行模式改成「時間到就送，不管上一發回來沒」，週期就真的等於
    interval：20 次/秒、盲區平均 25ms。

    **只在開賣前併行**。那是唯一在跟別人比毫秒的時候；開賣後的清票迴圈本來就是 5s
    冷卻一輪，併行毫無意義還多打對方，所以呼叫端偵測到開賣就把 `pipelined` 關掉。

    curl_cffi 的連線池是 **thread-local**，所以每條 worker 都要自己建連線，而且
    **一定要在倒數期間就建好**（見 `keepalive`）—— 實測這個網域每建一條新連線，
    第一發要 **~2.1s**，砸在 T-0 會比不併行還糟。

    快取碰撞不必擔心：`_fresh_params` 的變體數是 id 數的階乘，票種一多就幾乎不可能
    撞到同一個 cache key；票種少到會撞的活動，`_MIN_VARIANTS` 那邊本來就會先警告。
    """

    def __init__(self, session: cf_requests.Session, product_ids: list[str],
                 area_ids: list[str], interval: float):
        self.session = session
        self.product_ids = product_ids
        self.area_ids = area_ids
        self.interval = interval
        self.pipelined = interval < PIPELINE_BELOW
        self.workers = min(_MAX_INFLIGHT,
                           max(2, math.ceil(_INFLIGHT_RTT_BUDGET / interval)))
        self._pool: ThreadPoolExecutor | None = None
        self._inflight: list = []
        self._warming: list = []
        self._warmed = False
        self._last_ping = 0.0
        self._next_send: float | None = None

        # 只有高頻偵測在意「繞不繞得過 CDN 快取」，所以警告放這裡（一次性檢查不該喊）。
        # 嚴格清票特別容易踩到：候選被縮到 1~2 個票區時排列組合就不夠了 —— 所以
        # `build_plan` 才會另外給 poll_products/poll_areas，讓偵測照樣打整場的 id。
        variants = _variants(len(product_ids or []), len(area_ids or []))
        if variants < _MIN_VARIANTS:
            print(f"[INFO] ⚠ 偵測只帶 {len(product_ids or [])} 票種 / "
                  f"{len(area_ids or [])} 票區，排列組合 {variants} 種繞不過 CDN 快取"
                  f"（偵測會慢約 1 秒）")

    def describe(self) -> str:
        if not self.pipelined:
            return (f"依序輪詢，間隔 {self.interval * 1000:.0f}ms"
                    f"（實際頻率 = 1/(RTT+間隔)，會比設定值低）")
        return (f"併行偵測，每 {self.interval * 1000:.0f}ms 送一發不等回應"
                f"（{1 / self.interval:.0f} 次/秒，最多 {self.workers} 發在途）")

    def keepalive(self, remaining: float, start_at: float = 30.0,
                  stop_before: float = 12.0):
        """倒數期間把 worker 連線建好並維持住。掛在 TimeWatcher 的 on_tick 上。

        **這步非做不可**：實測這個網域**每建一條新連線第一發就要 ~2.1s** —— DNS 只佔
        29ms，換新 session、換新 thread、把 6 條錯開 80ms 送，量到的都是 2.1~2.2s，
        是連線本身的成本不是併發互卡。等到 poll_and_grab 才建，那 2.1s 會整個吃掉
        lead_seconds，**比不併行還糟**。

        剩 30s 才開始建（太早建，連線閒置會被對方關掉），之後每 5s 續一次，剩 12s 停手
        —— 跟 session.make_keepalive 的 stop_before 同一個理由：不讓網路 IO 干擾倒數精度。
        送出去就不等結果，所以不會卡住倒數。

        注意 `stop_before` **只擋續命、不擋第一次建線**：使用者可能在剩 5 秒才按 START，
        那時候仍然得把連線建起來（晚建也遠比不建好，不然 T-0 每條各吃一次 2.1s）。
        """
        if not self.pipelined or remaining > start_at:
            return
        if self._pool is None:
            self._spawn()
        elif remaining > stop_before and time.monotonic() - self._last_ping >= 5.0:
            self._ping()
        self._report()

    def warm(self):
        """沒走倒數（`ENABLE_TIME_WATCHER=False`）時的補救：同步把連線建好。

        會擋住約 2.1s，但那條路本來就沒有 T-0 可言，晚 2 秒開始無所謂 ——
        真正不能接受的是「該暖沒暖」，那會讓前幾發偵測各自吃一次 2.1s。
        """
        if not self.pipelined or self._warmed:
            return
        if self._pool is None:
            self._spawn()
        for fut in self._warming:
            fut.result()
        self._report()

    def _spawn(self):
        """生出 N 條常駐 thread，每條各自把連線建起來。

        用 Barrier 卡住是為了**逼 ThreadPoolExecutor 真的生出 N 條 thread** —— 不然它會
        重用先跑完的那條，剩下的到 T-0 才第一次跑，2.1s 就砸在最貴的時候。
        """
        self._pool = ThreadPoolExecutor(max_workers=self.workers,
                                        thread_name_prefix="tp-poll")
        self._ping(gated=True)

    def _ping(self, gated: bool = False):
        gate = threading.Barrier(self.workers) if gated else None

        def _one():
            if gate is not None:
                try:
                    gate.wait(timeout=20)
                except threading.BrokenBarrierError:
                    pass      # 湊不齊就各自跑，頂多少暖到一條
            return self._read()

        self._warming = [self._pool.submit(_one) for _ in range(self.workers)]
        self._last_ping = time.monotonic()

    def _report(self):
        """暖機結果回收（非阻塞：還沒跑完就下次再說）。"""
        if not self._warming or not all(f.done() for f in self._warming):
            return
        rtts = [f.result()[2] for f in self._warming]
        self._warming = []
        if not self._warmed:
            self._warmed = True
            print(f"[WARMUP] 偵測執行緒已暖機 {self.workers} 條"
                  f"（RTT {min(rtts):.0f}~{max(rtts):.0f}ms）")

    def _read(self) -> tuple[dict, bool, float]:
        """一發票況。**不會 raise**（get_infos 自己吞掉所有例外）。
        TP_POLL_VERBOSE=1 時印出這一發的「去」和「回」（含序號、RTT、各票種即時狀態）。"""
        if not _POLL_VERBOSE:
            t0 = time.perf_counter()
            result, blocked = get_infos(self.session, self.product_ids, self.area_ids,
                                        log=False)
            return result, blocked, (time.perf_counter() - t0) * 1000

        seq = next(_poll_seq)
        print(f"[POLL→] #{seq} 送出（{len(self.product_ids)} 票種）")
        t0 = time.perf_counter()
        result, blocked = get_infos(self.session, self.product_ids, self.area_ids,
                                    log=False)
        rtt = (time.perf_counter() - t0) * 1000
        print(f"[POLL←] #{seq} 回應 RTT {rtt:.0f}ms — {_status_summary(result, blocked)}")
        return result, blocked, rtt

    def next(self) -> tuple[dict, bool, float]:
        """下一筆 (result, blocked, rtt_ms)。併行模式回的是**最早送出且已完成**的那發。"""
        if self._next_send is None:
            self._next_send = time.monotonic()
        if not self.pipelined or self._pool is None:
            _sleep_until(self._next_send)
            reading = self._read()
            # 依序模式維持原本語意：**回應之後**才等 interval（週期 = RTT + interval）。
            # 「固定節奏送」是併行模式專屬的 —— 依序模式照抄的話等於偷偷幫所有舊設定
            # 加速 20%，那不是這次要改的東西。
            self._next_send = time.monotonic() + self.interval
            return reading

        while True:
            now = time.monotonic()
            while len(self._inflight) < self.workers and now >= self._next_send:
                self._inflight.append(self._pool.submit(self._read))
                self._next_send += self.interval
                if self._next_send <= now:
                    # 落後了（GIL 抖動 / 剛結束冷卻）就重新對時，不要補打一串
                    self._next_send = now + self.interval

            for fut in self._inflight:
                if fut.done():
                    self._inflight.remove(fut)
                    return fut.result()

            if not self._inflight:
                _sleep_until(self._next_send)      # 冷卻中，不用忙等
            else:
                time.sleep(0.002)

    def pause(self, seconds: float):
        """冷卻：停送新的、丟掉在途結果（冷卻完那些讀數已經過期了）。"""
        self._inflight.clear()
        self._next_send = time.monotonic() + seconds

    def close(self):
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None


def _sleep_until(deadline: float):
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
