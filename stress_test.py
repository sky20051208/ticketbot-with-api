"""多開壓力測試 —— 回答「這台機器同時開幾個 instance 才不會互相拖累」。

為什麼要分模式測，不能只「開 20 個看會不會爆」：三個瓶頸互相獨立，解法也不同。

  --mode cpu   T-0 那一秒的 CPU 爭搶（**最重要**，零網路請求、零風險）
               每個 instance 在開賣瞬間要做的 CPU 工作是固定的兩件：
                 1. 用 regex 解析 ~570KB 的活動頁（tixcraftapi.parsing）
                 2. 跑一次 ONNX 驗證碼推論（captchaAI.predict）
               N 個 process 用 Barrier 同步引爆，比較 N=1 跟 N=20 的耗時差距。
               開 20 個但每個都慢 300ms，不如開 8 個各自都快 —— 這模式就是在找那個交叉點。

  --mode mem   記憶體上限。實際開 N 隻 Chrome（userdata 模式每個 instance 一隻）量 RSS，
               直接回答「16GB 塞得下幾個」「要不要換 32GB」「string cookie 模式差多少」。

  --mode net   N 個 process 同一瞬間各打一發 GET，量 RTT 分佈。總請求數就是 N，
               不會踩到速率限制。

**沒有測「每 IP 的 eps 速率上限」** —— 那個會讓 IP 被標記，而且真正的解法是一個 instance
一顆 IP（config.LOCAL_BIND_IP），卡在 OCI 的 reserved public IP 限額，等升級 PAYG 再說。

用法（在 VPS 上）：
    ~/venv/bin/python stress_test.py --mode cpu --n 1 2 4 8 16 20
    ~/venv/bin/python stress_test.py --mode mem --n 4 8 12
    ~/venv/bin/python stress_test.py --mode net --n 1 5 10 20
"""
import argparse
import multiprocessing as mp
import os
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
FIXTURE = HERE / "tests" / "fixtures" / "stress_area_page.html"
ACTIVITY_URL = "https://tixcraft.com/activity"
ROUNDS = 8          # 每個 process 跑幾輪，取分佈


# --------------------------------------------------------------------------
# fixture
# --------------------------------------------------------------------------

def ensure_fixture() -> str:
    """準備一份真實大小的活動頁 HTML。/activity 不用登入就拿得到（實測 ~570KB）。"""
    if FIXTURE.exists():
        return FIXTURE.read_text(encoding="utf-8", errors="ignore")
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    print(f"[FIXTURE] 抓 {ACTIVITY_URL} …")
    from curl_cffi import requests as cf
    res = cf.get(ACTIVITY_URL, impersonate="chrome124", timeout=30)
    FIXTURE.write_text(res.text, encoding="utf-8")
    print(f"[FIXTURE] 存到 {FIXTURE}（{len(res.text)/1024:.0f}KB）")
    return res.text


def fake_captcha_png() -> bytes:
    """產一張跟拓元驗證碼同尺寸的圖。

    **內容不重要** —— 我們量的是推論耗時，那只跟輸入張量大小有關，跟畫的是什麼字無關。
    用真圖反而麻煩（要有 session 才拿得到 /ticket/captcha）。
    """
    import io
    from PIL import Image
    import random
    img = Image.new("RGB", (120, 100), (255, 255, 255))
    px = img.load()
    for _ in range(2000):     # 灑點雜訊，避免整張純色被某些前處理短路
        px[random.randrange(120), random.randrange(100)] = (
            random.randrange(256), random.randrange(256), random.randrange(256))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------
# mode: cpu
# --------------------------------------------------------------------------

def _cpu_worker(html, png, barrier, out):
    """一個 worker = 一個搶票 instance 在 T-0 那一秒要做的 CPU 工作。"""
    sys.path.insert(0, str(HERE))
    from tixcraftapi.parsing import parse_area_availables
    from captchaAI.predict import recognize_captcha, warmup_ocr

    warmup_ocr()                      # 攤掉 lazy init，不然第一輪會失真
    parse_area_availables(html)

    barrier.wait()                    # 全部就緒才一起引爆，模擬 T-0

    parse_ms, ocr_ms = [], []
    for _ in range(ROUNDS):
        t0 = time.perf_counter()
        parse_area_availables(html)
        t1 = time.perf_counter()
        recognize_captcha(png)
        t2 = time.perf_counter()
        parse_ms.append((t1 - t0) * 1000)
        ocr_ms.append((t2 - t1) * 1000)
    out.put((parse_ms, ocr_ms))


def run_cpu(n_list):
    html = ensure_fixture()
    png = fake_captcha_png()
    print(f"\nHTML {len(html)/1024:.0f}KB | 驗證碼 {len(png)}B | 每個 worker 跑 {ROUNDS} 輪")
    print(f"CPU: {os.cpu_count()} 邏輯核心\n")
    print(f"  {'N':>3}  {'解析 p50':>9} {'解析 p95':>9}  {'OCR p50':>8} {'OCR p95':>8}  {'合計 p50':>9}  劣化")
    print("  " + "-" * 68)

    base = None
    for n in n_list:
        ctx = mp.get_context("spawn")     # 跟真實情況一致：每個 instance 是獨立 process
        barrier = ctx.Barrier(n)
        out = ctx.Queue()
        procs = [ctx.Process(target=_cpu_worker, args=(html, png, barrier, out))
                 for _ in range(n)]
        for p in procs:
            p.start()
        parse_all, ocr_all = [], []
        for _ in range(n):
            pm, om = out.get()
            parse_all += pm
            ocr_all += om
        for p in procs:
            p.join()

        parse_all.sort()
        ocr_all.sort()
        p50p = statistics.median(parse_all)
        p95p = parse_all[int(len(parse_all) * 0.95) - 1]
        p50o = statistics.median(ocr_all)
        p95o = ocr_all[int(len(ocr_all) * 0.95) - 1]
        total = p50p + p50o
        if base is None:
            base = total
            note = "基準"
        else:
            note = f"×{total / base:.2f}"
        print(f"  {n:>3}  {p50p:>8.1f}ms {p95p:>8.1f}ms  {p50o:>7.1f}ms {p95o:>7.1f}ms"
              f"  {total:>8.1f}ms  {note}")


# --------------------------------------------------------------------------
# mode: mem
# --------------------------------------------------------------------------

def _pss_kb(pid) -> int:
    """PSS 而不是 RSS。

    RSS 會把共享分頁（共用函式庫、Chrome 各 process 之間共享的那一大塊）在每個
    process 各算一次，把整棵子樹的 RSS 加起來會嚴重灌水 —— 第一版就是這樣量出
    「每隻 Chrome 1GB、12 隻共 12.7GB」，但系統可用記憶體只掉了 3.5GB。
    PSS 把每個共享分頁按共用者數量分攤，加總才等於實際佔用。
    """
    try:
        for line in open(f"/proc/{pid}/smaps_rollup"):
            if line.startswith("Pss:"):
                return int(line.split()[1])
    except Exception:
        pass
    return 0


def _tree_pss_mb(root_pid) -> float:
    """Chrome 是多進程的（browser / renderer / gpu / utility），只看主 pid 會嚴重低估。"""
    total = _pss_kb(root_pid)
    try:
        children = subprocess.run(["pgrep", "-P", str(root_pid)],
                                  capture_output=True, text=True).stdout.split()
        for c in children:
            total += int(_tree_pss_mb(int(c)) * 1024)
    except Exception:
        pass
    return total / 1024


def _mem_available_gb() -> float:
    mem = dict(l.split(":", 1) for l in open("/proc/meminfo"))
    return int(mem["MemAvailable"].split()[0]) / 1024 / 1024


def run_mem(n_list):
    print("\n開 N 隻 Chrome（各自獨立 user-data-dir，載入真實活動頁）量 PSS")
    print("這對應 userdata 模式：每個搶票 instance 一隻 Chrome")
    print("兩個數字要對得起來：PSS 合計 ≈ 可用記憶體的下降量，對不上就是量錯了\n")
    for n in n_list:
        base_avail = _mem_available_gb()
        procs, dirs = [], []
        for i in range(n):
            d = f"/tmp/stress_chrome_{i}"
            dirs.append(d)
            procs.append(subprocess.Popen(
                ["google-chrome", "--no-sandbox", "--disable-dev-shm-usage",
                 f"--user-data-dir={d}", "--no-first-run", "--no-default-browser-check",
                 "--window-size=1280,900", ACTIVITY_URL],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(25)     # 等頁面載完、記憶體用量穩定

        per = [_tree_pss_mb(p.pid) for p in procs]
        mem = dict(l.split(":", 1) for l in open("/proc/meminfo"))
        avail_gb = _mem_available_gb()
        swap_used_gb = (int(mem["SwapTotal"].split()[0]) - int(mem["SwapFree"].split()[0])) / 1024 / 1024

        print(f"  N={n:>3}  每隻平均 {statistics.mean(per):>5.0f}MB  "
              f"PSS 合計 {sum(per)/1024:>5.2f}GB  |  可用掉了 {base_avail - avail_gb:>5.2f}GB  "
              f"（剩 {avail_gb:.2f}GB）  swap {swap_used_gb:.2f}GB")

        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()
        subprocess.run(["rm", "-rf"] + dirs)
        time.sleep(3)


# --------------------------------------------------------------------------
# mode: net
# --------------------------------------------------------------------------

def _net_worker(barrier, out):
    from curl_cffi import requests as cf
    s = cf.Session(impersonate="chrome124")
    s.get(ACTIVITY_URL, timeout=30)      # 暖機：建立連線 + TLS，不計入
    barrier.wait()
    t0 = time.perf_counter()
    res = s.get(ACTIVITY_URL, timeout=30)
    out.put(((time.perf_counter() - t0) * 1000, res.status_code))


def run_net(n_list):
    print("\nN 個 process 同一瞬間各打一發 GET（總請求數 = N，不會踩速率限制）\n")
    print(f"  {'N':>3}  {'min':>8} {'p50':>8} {'max':>8}   狀態碼")
    print("  " + "-" * 48)
    for n in n_list:
        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(n)
        out = ctx.Queue()
        procs = [ctx.Process(target=_net_worker, args=(barrier, out)) for _ in range(n)]
        for p in procs:
            p.start()
        rtts, codes = [], []
        for _ in range(n):
            ms, code = out.get()
            rtts.append(ms)
            codes.append(code)
        for p in procs:
            p.join()
        rtts.sort()
        uniq = ",".join(f"{c}×{codes.count(c)}" for c in sorted(set(codes)))
        print(f"  {n:>3}  {rtts[0]:>7.0f}ms {statistics.median(rtts):>7.0f}ms "
              f"{rtts[-1]:>7.0f}ms   {uniq}")
        time.sleep(5)      # 兩輪之間喘一下，別讓 eps 覺得是連續打擊


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["cpu", "mem", "net"])
    ap.add_argument("--n", type=int, nargs="+", default=[1, 2, 4, 8, 16, 20])
    args = ap.parse_args()

    print("=" * 74)
    print(f"壓力測試 mode={args.mode}  N={args.n}")
    print("=" * 74)
    {"cpu": run_cpu, "mem": run_mem, "net": run_net}[args.mode](args.n)
    print("=" * 74)


if __name__ == "__main__":
    sys.exit(main())
