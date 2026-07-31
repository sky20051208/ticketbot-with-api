"""proxy 路徑 RTT 對比 —— 「這台機器 + 這條 proxy 打到拓元，一發要多久」。

跟 bench_eps.py 的差別：那支在答「通不通」，這支在答「多快」，所以
  - 只打**動態頁**（回源、body 只有 23B），不打大頁面，避免下載時間污染 RTT
  - 每條路徑打 20 發，報 min / p50 / p95，看得出抖動
  - 順便量「到 proxy gateway 的 TCP RTT」，把第一跳從總時間裡拆出來

用法（一次比多條路徑，label 自己取）：
    python3 bench_proxy_rtt.py \
        --config "直連=" \
        --config "sg2+US=http://user-region-US-sid-x-t-90:pw@sg2.cliproxy.io:3010" \
        --config "sg2+TW=http://user-region-TW-sid-y-t-90:pw@sg2.cliproxy.io:3010"
"""
import argparse
import socket
import statistics
import sys
import time
from urllib.parse import urlparse

from curl_cffi import requests as cf_requests

BASE = "https://tixcraft.com"
# 動態頁：一定回源，而且沒 cookie 時回 401 + 23B，body 小到可以忽略下載時間
PROBE = f"{BASE}/activity/game/26_joji"
N = 20
WARM = 3

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


def gateway_rtt(proxy: str) -> float:
    """到 proxy gateway 的 TCP 握手時間 —— 這是路徑的第一跳，跟出口節點無關。"""
    u = urlparse(proxy)
    rtts = []
    for _ in range(5):
        t0 = time.perf_counter()
        try:
            s = socket.create_connection((u.hostname, u.port or 3010), timeout=8)
            rtts.append((time.perf_counter() - t0) * 1000)
            s.close()
        except Exception:
            return -1.0
    return statistics.median(rtts)


def measure(label: str, proxy: str):
    session = cf_requests.Session(impersonate="chrome124")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}

    try:
        exit_ip = session.get("https://api.ipify.org", timeout=20).text.strip()
    except Exception as e:
        print(f"\n[{label}] 出口 IP 查不到（{type(e).__name__}），跳過")
        return

    gw = f"{gateway_rtt(proxy):.0f}ms" if proxy else "—（直連）"

    rtts, status = [], 0
    try:
        for _ in range(WARM):     # 暖機：建立連線 + TLS，不計入
            session.get(PROBE, headers=HEADERS, timeout=20, allow_redirects=False)
        for _ in range(N):
            t0 = time.perf_counter()
            res = session.get(PROBE, headers=HEADERS, timeout=20, allow_redirects=False)
            rtts.append((time.perf_counter() - t0) * 1000)
            status = res.status_code
    except Exception as e:
        print(f"\n[{label}] 打不通：{type(e).__name__}: {e}")
        return

    rtts.sort()
    p95 = rtts[int(len(rtts) * 0.95) - 1]
    print(f"\n[{label}]")
    print(f"  出口 IP      : {exit_ip}")
    print(f"  到 gateway   : {gw}")
    print(f"  HTTP         : {status}")
    print(f"  一發動態請求 : min {rtts[0]:5.0f}ms | p50 {statistics.median(rtts):5.0f}ms"
          f" | p95 {p95:5.0f}ms | max {rtts[-1]:5.0f}ms")
    return statistics.median(rtts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", action="append", required=True,
                    metavar="LABEL=PROXY_URL", help="可重複；PROXY_URL 留空 = 直連")
    args = ap.parse_args()

    print("=" * 74)
    print(f"目標 {PROBE}")
    print(f"每條路徑暖機 {WARM} 發後量 {N} 發（同一條熱連線）")
    print("=" * 74)

    results = {}
    for item in args.config:
        label, _, proxy = item.partition("=")
        med = measure(label.strip(), proxy.strip())
        if med:
            results[label.strip()] = med

    if len(results) > 1:
        print("\n" + "=" * 74)
        print("排名（p50，越小越好）")
        best = min(results.values())
        for label, med in sorted(results.items(), key=lambda kv: kv[1]):
            print(f"  {label:12s} {med:6.0f}ms   {'← 最快' if med == best else f'+{med - best:.0f}ms'}")
        print("=" * 74)


if __name__ == "__main__":
    sys.exit(main())
