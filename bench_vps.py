"""VPS 選址 benchmark —— 在候選機房跑一次，判斷值不值得把 bot 搬過去。

用法（VPS 上，只要有 python3，不用裝任何套件）：
    scp bench_vps.py user@vps:~/ && ssh user@vps 'python3 bench_vps.py'

回答三個問題：
  1. 這台機器落在哪個 Fastly POP（x-served-by）
  2. 「你→edge」和「edge→origin」各花多久（用快取命中 vs 動態頁的差值拆出來）
  3. **eps 擋不擋這個 IP** —— 這比延遲更可能是 deal breaker，機房 IP 常被直接 401/403

同一支腳本在本機也跑得動，拿來當對照組。
"""
import io
import gzip
import json
import socket
import ssl
import statistics
import sys
import time
import urllib.request

HOST = "tixcraft.com"
# 快取命中的靜態資源 → 只到 edge，量出「你→POP」的往返
CACHED_PATH = "/epsf/asset/shared.js"
# 動態頁 → 一定回源，量出「連 origin 那段」
DYNAMIC_PATH = "/activity/game/26_joji"
N = 5

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Windows 的 Python 常常抓不到 Fastly 的中繼憑證 -> 憑證驗證失敗。
# 這支只是量延遲，驗不驗證無所謂，握手失敗時就退回不驗證模式（會印出來）。
INSECURE = False


def make_ctx():
    if INSECURE:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except ImportError:
        pass
    return ctx


def public_ip():
    try:
        req = urllib.request.Request("https://api.ipify.org?format=json",
                                     headers={"User-Agent": UA})
        ip = json.load(urllib.request.urlopen(req, timeout=10))["ip"]
    except Exception as e:
        return f"(查不到: {type(e).__name__})", ""
    try:
        rdns = socket.gethostbyaddr(ip)[0]
    except Exception:
        rdns = "-"
    return ip, rdns


def handshake_breakdown():
    """DNS / TCP / TLS 各花多久 —— TCP 那段就是到 POP 的實際網路 RTT。"""
    global INSECURE
    t0 = time.perf_counter()
    ip = socket.gethostbyname(HOST)
    t_dns = time.perf_counter()
    sock = socket.create_connection((ip, 443), timeout=10)
    t_tcp = time.perf_counter()
    try:
        tls = make_ctx().wrap_socket(sock, server_hostname=HOST)
    except ssl.SSLCertVerificationError as e:
        print(f"  [!] 憑證驗證失敗（{e.verify_message}），退回不驗證模式量測")
        print("      要根治：pip install certifi")
        INSECURE = True
        sock.close()
        sock = socket.create_connection((ip, 443), timeout=10)
        t_tcp = time.perf_counter()
        tls = make_ctx().wrap_socket(sock, server_hostname=HOST)
    t_tls = time.perf_counter()
    proto = tls.version()
    tls.close()
    return {
        "ip": ip,
        "dns": (t_dns - t0) * 1000,
        "tcp": (t_tcp - t_dns) * 1000,
        "tls": (t_tls - t_tcp) * 1000,
        "proto": proto,
    }


def fetch(path, n=N):
    """同一條 TLS 連線連打 n 次（跟搶票時的熱連線一致），回 (rtt list, 最後一次的 headers, status)。"""
    conn = None
    rtts, headers, status, size = [], {}, 0, 0
    try:
        import http.client
        conn = http.client.HTTPSConnection(HOST, 443, timeout=15, context=make_ctx())
        conn.request("GET", path, headers={"User-Agent": UA, "Accept": "*/*",
                                           "Accept-Encoding": "gzip", "Connection": "keep-alive"})
        r = conn.getresponse()
        r.read()                       # 第一發：建連線，不計入
        for _ in range(n):
            t0 = time.perf_counter()
            conn.request("GET", path, headers={"User-Agent": UA, "Accept": "*/*",
                                               "Accept-Encoding": "gzip",
                                               "Connection": "keep-alive"})
            r = conn.getresponse()
            body = r.read()
            rtts.append((time.perf_counter() - t0) * 1000)
            headers = dict(r.getheaders())
            status = r.status
            size = len(body)
            if r.getheader("Content-Encoding") == "gzip":
                try:
                    body = gzip.GzipFile(fileobj=io.BytesIO(body)).read()
                except Exception:
                    pass
            last_body = body
    except Exception as e:
        print(f"  [!] {path} 失敗: {type(e).__name__}: {e}")
        return [], {}, 0, 0, b""
    finally:
        if conn:
            conn.close()
    return rtts, headers, status, size, last_body


def hget(headers: dict, name: str) -> str:
    """HTTP header 名稱大小寫不敏感（http.client 回傳的是原始大小寫）。"""
    name = name.lower()
    for k, v in headers.items():
        if k.lower() == name:
            return v
    return ""


def line(label, rtts):
    if not rtts:
        return f"  {label:32s} —"
    return (f"  {label:32s} min {min(rtts):6.0f}ms  中位 {statistics.median(rtts):6.0f}ms"
            f"  max {max(rtts):6.0f}ms")


def main():
    print("=" * 74)
    ip, rdns = public_ip()
    print(f"本機出口 IP : {ip}   {rdns}")
    hs = handshake_breakdown()
    print(f"tixcraft IP : {hs['ip']}  ({hs['proto']})")
    print(f"握手拆解    : DNS {hs['dns']:.0f}ms | TCP {hs['tcp']:.0f}ms ← 到 POP 的網路 RTT"
          f" | TLS {hs['tls']:.0f}ms")
    print("=" * 74)

    c_rtts, c_hdr, c_status, c_size, _ = fetch(CACHED_PATH)
    d_rtts, d_hdr, d_status, d_size, d_body = fetch(DYNAMIC_PATH)

    pop = (hget(d_hdr, "x-served-by") or hget(c_hdr, "x-served-by") or "?").split(",")[0].strip()
    print(f"\nFastly POP  : {pop}")
    print(f"  快取狀態  : 靜態 {hget(c_hdr,'x-cache') or '?'} / 動態 {hget(d_hdr,'x-cache') or '?'}")

    print("\n延遲（同一條熱連線連打 5 次）")
    print(line(f"[1] 靜態資源 HTTP {c_status} ({c_size}B)", c_rtts))
    print(line(f"[2] 動態頁   HTTP {d_status} ({d_size}B)", d_rtts))
    if c_rtts and d_rtts:
        edge = statistics.median(c_rtts)
        total = statistics.median(d_rtts)
        print(f"\n  你 -> edge          約 {edge:6.0f}ms   ([1] 只到 POP)")
        print(f"  edge -> origin+處理 約 {total - edge:6.0f}ms   ([2] 減 [1]，回源那段)")
        print(f"  一發動態請求        約 {total:6.0f}ms")

    # eps 判定：**401 和 403 意義完全不同**（2026-07-30 實測釐清）
    #   401 = 還沒跑 JS 挑戰，任何乾淨訪客（含使用者家裡的台灣 IP）都是這個 -> 可用
    #   403 + Paused = ASN 進黑名單，真 Chrome 跑完挑戰也拿不到通行證 -> 無解
    blocked = (d_status in (403, 429)
               or b"Browsing Activity Has Been Paused" in d_body)
    print("\neps 防護")
    if blocked:
        print(f"  [!!] 這個 ASN 被封 (HTTP {d_status}) —— 換 IP 沒用，只能換機房")
    elif d_status == 401:
        print(f"  [OK] HTTP 401 待挑戰 —— 跟住宅 IP 同待遇，瀏覽器跑一次 JS 就會拿到 eps_sid")
        print("       確認整條鏈請跑 bench_browser.py")
    else:
        print(f"  [OK] 裸請求就拿到內容 (HTTP {d_status}) —— 這個 IP 的 eps 信譽良好")
    print("=" * 74)
    print("判讀：[2] 減 [1] 掉到 100ms 以內 -> origin 就在附近，值得搬；")
    print("      仍是 200ms 上下 -> 這個位置沒賺頭（跟台灣打一樣遠）。")


if __name__ == "__main__":
    sys.exit(main())
