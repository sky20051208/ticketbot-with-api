"""量遠大 TicketPlus 票況 API 的速率上限 —— 決定偵測迴圈可以縮到多快。

**為什麼要量**：`ticketplus_api/__main__.py` 的偵測迴圈是序列的
（`get_infos()` → `sleep(RETRY_INTERVAL)` → 再來一次），預設 0.3s 代表開賣翻
`onsale` 的瞬間平均有 150ms 看不到。那個盲區比「把機器搬到東京」省下的 55ms 還大。

但不能盲目調快：`catalog.get_infos()` 已經認得 errCode 110（流量管制），被擋要退避
8 秒，打太兇反而更慢。拓元實測是同一 URL 約 3 req/s 就 403，遠大沒人量過 —— 這支就是
去把那個數字量出來。

**打的是 `/get`，跟搶票時偵測開賣的請求一模一樣**（唯讀、不需要登入、不會下單）。
用真實的 productId / ticketAreaId，不然只會拿到邊緣快取的 errCode 101，測不到後端。

用法：
    python bench_ticketplus_rate.py --event <32hex 活動id>
    python bench_ticketplus_rate.py --event <id> --rates 1 2 3 5 8 12

**建議從機房 IP 跑，不要從自己家裡跑** —— 萬一那顆 IP 被標記，開賣當天會很難看。
"""
import argparse
import queue
import statistics
import sys
import threading
import time

from curl_cffi import requests as cf_requests

from ticketplus_api import CONFIG_API, catalog
from ticketplus_api.session import build_headers

STEP_SECONDS = 8.0      # 每個速率跑多久
STEP_REST = 5.0         # 兩段之間喘口氣，避免前一段的帳算到下一段
DEFAULT_RATES = [1, 2, 3, 5, 8, 12]


def build_url(event_id: str) -> tuple[str, int]:
    """用真實活動組出偵測用的 /get URL。回 (url, 票種數)。"""
    probe = cf_requests.Session(impersonate="chrome124")
    cat = catalog.fetch_catalog(probe, event_id)
    products = [p["productId"] for p in cat.get("products", []) if p.get("productId")]
    areas = [a["ticketAreaId"] for a in cat.get("ticketAreas", []) if a.get("ticketAreaId")]
    if not products:
        raise SystemExit(f"活動 {event_id} 抓不到票種 —— id 是不是打錯，或活動已下架？")
    params = ["productId=" + ",".join(products)]
    if areas:
        params.append("ticketAreaId=" + ",".join(areas))
    return f"{CONFIG_API}/get?{'&'.join(params)}&", len(products)


class Worker(threading.Thread):
    """一條常駐連線。

    curl_cffi 的連線池是 thread-local，臨時開 thread = 每發都重跑 TLS 握手，量出來的
    會是握手時間不是伺服器的節流行為（拓元那邊實測重用連線 350ms、新建 750~3500ms）。
    所以 worker 和 session 都在測試開始前就建好、整場重用。
    """

    def __init__(self, url: str, jobs: queue.Queue, out: list, lock: threading.Lock):
        super().__init__(daemon=True)
        self.url, self.jobs, self.out, self.lock = url, jobs, out, lock
        self.session = cf_requests.Session(impersonate="chrome124")

    def warm(self):
        try:
            self.session.get(self.url, headers=build_headers(), timeout=10)
        except Exception:
            pass

    def run(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            t0 = time.perf_counter()
            try:
                res = self.session.get(self.url, headers=build_headers(), timeout=10)
                rtt = (time.perf_counter() - t0) * 1000
                code = None
                if res.status_code == 200:
                    try:
                        code = str(res.json().get("errCode"))
                    except Exception:
                        code = "非JSON"
                rec = (res.status_code, code, rtt)
            except Exception as e:
                rec = (0, type(e).__name__, (time.perf_counter() - t0) * 1000)
            with self.lock:
                self.out.append(rec)
            self.jobs.task_done()


def run_step(url: str, rate: int, workers: list, jobs: queue.Queue,
             out: list, lock: threading.Lock) -> dict:
    out.clear()
    interval = 1.0 / rate
    n = int(STEP_SECONDS * rate)
    start = time.perf_counter()
    for i in range(n):
        target = start + i * interval
        now = time.perf_counter()
        if target > now:
            time.sleep(target - now)
        jobs.put(1)
    jobs.join()

    with lock:
        recs = list(out)
    rtts = sorted(r[2] for r in recs)
    codes = {}
    for status, code, _ in recs:
        key = f"HTTP{status}" if status not in (200,) else f"errCode {code}"
        codes[key] = codes.get(key, 0) + 1
    # 被擋的定義跟 catalog.get_infos 一致：HTTP 403/429 或 errCode 110
    blocked = sum(c for k, c in codes.items()
                  if k in ("HTTP403", "HTTP429") or k == "errCode 110")
    return {
        "rate": rate, "sent": len(recs), "blocked": blocked, "codes": codes,
        "p50": statistics.median(rtts) if rtts else 0,
        "p95": rtts[int(len(rtts) * 0.95) - 1] if rtts else 0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True, help="活動 id（網址 /activity/ 後面那 32 碼）")
    ap.add_argument("--rates", type=int, nargs="+", default=DEFAULT_RATES,
                    help="要測的每秒請求數（由低到高）")
    args = ap.parse_args()

    url, n_products = build_url(args.event)
    print("=" * 78)
    print(f"目標  : {url[:70]}…")
    print(f"票種數: {n_products}（一發請求查完整場，跟搶票時一樣）")
    print(f"階梯  : {args.rates} req/s，每段 {STEP_SECONDS:.0f} 秒")
    print("=" * 78)

    # worker 數要夠撐住最高速率：併發數 ≈ 速率 × RTT
    max_workers = max(4, max(args.rates))
    jobs: queue.Queue = queue.Queue()
    out: list = []
    lock = threading.Lock()
    workers = [Worker(url, jobs, out, lock) for _ in range(max_workers)]
    for w in workers:
        w.warm()          # 先把 TLS 握完，第一段才不會被握手時間汙染
        w.start()

    print(f"\n  {'req/s':>6} {'發出':>5} {'被擋':>5}  {'p50':>7} {'p95':>7}   回應分佈")
    print("  " + "-" * 68)
    hit = None
    for rate in args.rates:
        r = run_step(url, rate, workers, jobs, out, lock)
        dist = ", ".join(f"{k}×{v}" for k, v in sorted(r["codes"].items()))
        flag = "  ← 被擋" if r["blocked"] else ""
        print(f"  {r['rate']:>6} {r['sent']:>5} {r['blocked']:>5}  "
              f"{r['p50']:>6.0f}ms {r['p95']:>6.0f}ms   {dist}{flag}")
        if r["blocked"]:
            hit = rate
            break
        time.sleep(STEP_REST)

    for _ in workers:
        jobs.put(None)

    print("\n" + "=" * 78)
    if hit is None:
        top = args.rates[-1]
        print(f"結論：測到 {top} req/s 都沒被擋。")
        print(f"      偵測迴圈的 RETRY_INTERVAL 可以放心降到 {1/top:.2f}s 附近，")
        print(f"      但仍建議留餘裕（真正搶票時是多開同時打，總速率會是這個的 N 倍）。")
    else:
        safe = max([r for r in args.rates if r < hit], default=0)
        print(f"結論：{hit} req/s 開始被擋（HTTP 403/429 或 errCode 110）。")
        print(f"      安全速率取上一階 {safe} req/s → RETRY_INTERVAL 設 {1/safe:.2f}s 以上。"
              if safe else "      連最低速率都被擋，維持現狀不要調快。")
    print("=" * 78)


if __name__ == "__main__":
    sys.exit(main())
