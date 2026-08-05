"""Bot 用 user-data-dir 開 Chrome：偵測登入、抓 cookie、搶到票後把 cookie 灌回同一個視窗。

跟 create_profile.py 的差別：
    create_profile.py 是「一次性」建立 profile（手動登入並存檔）
    browser_login.py 是「每次搶票」bot 用既有 profile 開 Chrome、抓 cookie 給 curl_cffi 用

cookie 流向：
    Chrome profile → driver.get_cookies() → cookie 字串 → curl_cffi session
    搶到 /checkout → curl_cffi session 累積的 cookie → 灌回同一個 driver → 跳結帳頁

Proxy：launch_browser 接 proxy_url（proxy_pool 產的 http://user:pass@host:port）後，
這個 Chrome 全程走該 proxy（login、cookie 抓取、結帳頁瀏覽），跟 curl_cffi 出口 IP 一致，
避免「同帳號兩個 IP」被風控標記。**只影響 selenium 啟動的這隻 Chrome**，系統其他應用程式
（包含使用者自己的 Chrome）完全不受影響。

Chrome cmdline 不認 http://user:pass@host:port 的 inline auth；MV3 extension 又因 service
worker race condition 會讓 auth dialog 跳出來。所以改走 **localhost bridge**：起一個 127.0.0.1
的小型轉發器，bridge 自己塞 Proxy-Authorization header 給 CliProxy，Chrome 那邊看到的只是個
no-auth localhost proxy（見 [tixcraftapi/proxy_bridge.py](tixcraftapi/proxy_bridge.py)）。
"""
import sys
import time
from urllib.parse import urlparse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from tixcraftapi.bind_proxy import BindingProxy
from tixcraftapi.proxy_bridge import LocalProxyBridge

BASE = "https://tixcraft.com"

# 保留 bridge 物件 reference 避免 GC；thread 是 daemon，process 結束會自動收
_active_bridges: list[LocalProxyBridge | BindingProxy] = []


# ==========================================
# Proxy 助手（給 launch_browser + tixcraftapi.finalize.open_chrome_with_session 共用）
# ==========================================

def parse_proxy_url(url: str) -> dict:
    """解析 proxy URL → {scheme, host, port, user, password}。"""
    p = urlparse(url if "://" in url else f"http://{url}")
    return {
        "scheme": p.scheme or "http",
        "host": p.hostname or "",
        "port": p.port,
        "user": p.username,
        "password": p.password,
    }


def apply_stealth_to_options(opts: Options) -> None:
    """拔掉 Selenium 的自動化標記。
    主要解決兩件事：
      1. Chrome 頂部黃條「Chrome 正在受自動測試軟體控制」消失
      2. JS 端 `navigator.webdriver` 不再回 true（一些網站用這個擋 bot）
    純化 UI 體驗 + 對抗最基本的 anti-bot 偵測。不影響 cookie / session / proxy 行為。"""
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--disable-blink-features=AutomationControlled")


def apply_platform_options(opts: Options) -> None:
    """Linux（美東 VPS）專用的 Chrome 啟動參數；Windows 完全不受影響。

    Ubuntu 24.04 的 AppArmor 預設擋 unprivileged user namespace，Chrome 的沙箱起不來
    會直接 exit（Selenium 那端只看得到 "Chrome instance exited"）。VPS 是單用途機器，
    關掉沙箱沒有實質風險。`/dev/shm` 在雲端 VM 通常只有 64MB，不改的話 Chrome 開幾個
    分頁就崩。

    平滑捲動要關掉是為了遠端桌面：VPS 的畫面是靠 VNC 一張一張傳回台灣，而平滑捲動
    會把一次滾輪變成十幾張中間影格，每一張都要重繪整個視窗再壓縮送出去。實測
    （1440x900 / Tight+JPEG）一次滾輪從 352KB 掉到 131KB，等於幀率 2.7 倍。
    這個旗標只改瀏覽器內部行為，網站用 JS / CSS 都偵測不到 —— 不像
    --force-prefers-reduced-motion 會被 media query 讀到而多一個指紋差異，
    在 eps 面前不值得冒那個險。
    """
    if sys.platform.startswith("linux"):
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-smooth-scrolling")


def setup_proxy_bridge(proxy_url: str) -> int | None:
    """啟動 localhost bridge 處理 proxy auth；回傳 local port，沒 proxy / 解析失敗回 None。
    bridge 物件存進 _active_bridges 防 GC，跟 Python process 共生死。
    Selenium / subprocess 兩種開 Chrome 的方式都可以共用這個 helper 拿 local port。"""
    if not proxy_url:
        return None
    p = parse_proxy_url(proxy_url)
    if not (p["host"] and p["port"]):
        print(f"[PROXY-CHROME] URL 解析失敗: {proxy_url} — Chrome 改走直連")
        return None

    # 起 localhost bridge 自動塞 auth header（Chrome 那端不用處理認證）
    bridge = LocalProxyBridge(p["host"], p["port"], p["user"] or "", p["password"] or "")
    local_port = bridge.start()
    _active_bridges.append(bridge)
    print(f"[PROXY-CHROME] 127.0.0.1:{local_port} → {p['host']}:{p['port']} (bridge, auth={'on' if p['user'] else 'off'})")
    return local_port


def setup_bind_proxy(bind_ip: str) -> int | None:
    """啟動 localhost proxy 讓 Chrome 從指定的本機 IP 出去；回 local port，沒設回 None。

    Chrome 沒有「指定來源 IP」的參數，只能靠 proxy 繞。用途見
    [tixcraftapi/bind_proxy.py](tixcraftapi/bind_proxy.py)：eps_sid 綁出口 IP，
    Chrome 必須跟 curl_cffi 的 `interface=` 綁同一顆，否則登入拿到的 cookie 直接作廢。
    """
    if not bind_ip:
        return None
    proxy = BindingProxy(bind_ip)
    local_port = proxy.start()
    _active_bridges.append(proxy)          # 防 GC，跟 process 共生死
    print(f"[BIND-CHROME] 127.0.0.1:{local_port} → 來源 IP {bind_ip}")
    return local_port


def apply_proxy_to_options(opts: Options, _unused_dir: str, proxy_url: str,
                           bind_ip: str = "") -> bool:
    """把 proxy 設定塞進 Chrome Options。回傳 True 表示真的套用了。

    兩種模式互斥，proxy_url 優先（有外部 proxy 時來源 IP 綁定沒有意義，出口由 proxy 決定）：
      - `proxy_url` 非空 → 走 CliProxy，經 localhost bridge 補認證
      - `bind_ip` 非空   → 直連但從指定本機 IP 出去（美國 VPS 多 IP 架構）
    第二個參數保留是為了相容介面（bridge 不需要 fs 路徑）。"""
    local_port = setup_proxy_bridge(proxy_url) if proxy_url else setup_bind_proxy(bind_ip)
    if local_port is None:
        return False
    opts.add_argument(f"--proxy-server=http://127.0.0.1:{local_port}")
    # WebRTC 預設會繞過 proxy 用本機 IP 探測 → 鎖死只允許 proxied UDP
    opts.add_argument("--webrtc-ip-handling-policy=disable_non_proxied_udp")
    return True


# ==========================================
# 主要對外函式
# ==========================================

def launch_browser(user_data_dir: str, window_w: int = -1, window_h: int = -1,
                   window_x: int = -1, window_y: int = -1,
                   proxy_url: str = "", bind_ip: str = ""):
    """用指定 user-data-dir 開 Chrome（可見視窗），回 driver。
    proxy_url 非空時 Chrome 全程走該 proxy（透過 localhost bridge 處理 auth）；
    bind_ip 非空時直連但從該本機 IP 出去（要跟 curl_cffi 的 interface= 一致）；
    兩個都空時直連，行為跟舊版相同。"""
    opts = Options()
    opts.add_argument(f"--user-data-dir={user_data_dir}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disk-cache-size=1")
    if window_w > 0 and window_h > 0:
        opts.add_argument(f"--window-size={window_w},{window_h}")
    else:
        opts.add_argument("--window-size=1280,900")
    if window_x >= 0 and window_y >= 0:
        opts.add_argument(f"--window-position={window_x},{window_y}")

    apply_stealth_to_options(opts)
    apply_platform_options(opts)
    apply_proxy_to_options(opts, user_data_dir, proxy_url, bind_ip)

    driver = webdriver.Chrome(options=opts)
    print(f"[LOGIN] Chrome 已開: {user_data_dir}")
    return driver


def _verify_tixcraft_logged_in(driver) -> bool:
    """JS 檢查目前頁面是否處於已登入狀態。

    找這幾種 auth-required 元素任何一個 → 已登入：
      - `a[href*="logout"]`：登出連結（最可靠）
      - `a[href*="/user/info"]` / `a[href*="/user/order"]`：會員專區連結
      - `a[href*="/member"]`：會員中心
    livenation 模式下使用者點按鈕跳到 tixcraft `/activity/detail/`，URL 條件符合但
    其實還是訪客，靠這個 DOM 檢查擋下誤判。"""
    try:
        result = driver.execute_script("""
            return !!(
                document.querySelector('a[href*="logout"]') ||
                document.querySelector('a[href*="/user/info"]') ||
                document.querySelector('a[href*="/user/order"]') ||
                document.querySelector('a[href*="/member"]')
            );
        """)
        return bool(result)
    except Exception:
        return False


def wait_for_login(driver, timeout: int = 600, start_url: str = "") -> bool:
    """跳到登入起始頁，每次都走一遍登入流程；輪詢直到 URL 回到 tixcraft 主站。

    start_url:
      - 空字串：跳到 tixcraft.com/login（預設行為）
      - 非空：跳到指定 URL（livenation 模式：起始 livenation 活動頁，
              使用者在 livenation 登入後點按鈕跳到 tixcraft，繼續完成 tixcraft 登入）

    判定「登入完成」雙條件：
      (1) URL hostname 屬於 tixcraft.com 且 path 不在 /login
      (2) DOM 找得到 logout 連結（或其他 auth-only 連結）

    必須用 urlparse 比對 hostname/path，**不能直接 substring 比 URL 整串** —
    FB OAuth 的 consent URL 會把 `tixcraft.com/login/facebook` 塞進 query param
    當 callback URL，substring 比對會被騙。

    這樣涵蓋所有 OAuth 流程（Google / Facebook / Apple 等）— 中途會跳到第三方 domain
    等使用者按「以某某身分繼續」之類授權，授權完成 OAuth callback 一定會繞回
    tixcraft.com/login/{provider}?code=...，server 處理完再 redirect 到 /user/info
    或 / 等非 /login 頁，那時才算完成。

    livenation 模式特別需要條件 (2)：使用者從 livenation 點過來會直接落在
    `/activity/detail/`，URL 條件馬上符合但 tixcraft session 還是訪客，DOM 沒有
    logout 連結，會繼續等到使用者實際登入完成（通常會被 tixcraft 引導到 /login）。
    """
    target = start_url or f"{BASE}/login"
    if start_url:
        print(f"[LOGIN] (livenation 模式) 開啟起始頁: {target}")
        print(f"[LOGIN] 請在 livenation 登入後點「藝人官方預售」按鈕導向 tixcraft，並完成 tixcraft 登入（最多 {timeout}s）...")
    else:
        print(f"[LOGIN] 開啟登入頁，請在 Chrome 視窗完成登入（最多 {timeout}s）...")
    driver.get(target)
    time.sleep(1.0)

    deadline = time.monotonic() + timeout
    last_url = ""
    locked_handle: str | None = None  # 一旦切到 tixcraft 分頁就鎖定，後續不再亂跳
    while time.monotonic() < deadline:
        # livenation 的「立即購票」是 target="_blank"，點下去 tixcraft 會開在新分頁。
        # 所以每輪都掃所有 window_handles，找到第一個 host 在 tixcraft 域的分頁就切過去。
        # 鎖定後就不再切換（避免使用者開其他 tixcraft tab 干擾）。
        if locked_handle is None:
            try:
                handles = driver.window_handles
            except Exception:
                print("[LOGIN] driver 異常（視窗被關閉？）")
                return False
            for h in handles:
                try:
                    driver.switch_to.window(h)
                    cur_h = driver.current_url or ""
                except Exception:
                    continue
                host_h = (urlparse(cur_h).hostname or "").lower()
                if host_h == "tixcraft.com" or host_h.endswith(".tixcraft.com"):
                    if h != handles[0] or len(handles) > 1:
                        print(f"[LOGIN] 偵測到 tixcraft 分頁，切換 focus: {cur_h}")
                    locked_handle = h
                    break
            else:
                # 所有 tab 都還沒到 tixcraft，留在最後一個 tab，繼續等
                pass

        try:
            cur = driver.current_url or ""
        except Exception:
            print("[LOGIN] driver 異常（視窗被關閉？）")
            return False

        parsed = urlparse(cur)
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
        on_tixcraft = host == "tixcraft.com" or host.endswith(".tixcraft.com")
        off_login_path = "/login" not in path

        # 雙條件：URL 在 tixcraft 且不在 /login，再加 DOM 確認已登入
        if on_tixcraft and off_login_path and _verify_tixcraft_logged_in(driver):
            print(f"[LOGIN] 登入完成: {cur}")
            return True

        if cur != last_url:
            hint = ""
            if on_tixcraft and off_login_path:
                # livenation 模式下會落在這狀態：URL 已到 tixcraft 但還沒登入
                hint = "  ← URL 已到 tixcraft 但尚未登入，請在頁面右上角登入"
            print(f"[LOGIN] 等待中... 目前: {cur}{hint}")
            last_url = cur
        time.sleep(1.0)

    print(f"[LOGIN] 超時 {timeout}s，未偵測到登入完成")
    return False


def extract_cookies(driver) -> str:
    """從 driver 抓 tixcraft cookie，組成 curl_cffi build_session 用的 cookie 字串。"""
    driver.get(f"{BASE}/")
    time.sleep(0.3)
    cookies = driver.get_cookies()
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    print(f"[LOGIN] 抓到 {len(cookies)} 個 cookie")
    return cookie_str


def inject_cookies_and_go(driver, session, target_url: str, acc_id: int = 0):
    """搶到票後：把 curl_cffi session 累積的 cookie 灌回 driver，跳轉到結帳頁。"""
    # Selenium 規定要先在該 domain 才能 add_cookie
    driver.get(f"{BASE}/")
    time.sleep(0.3)
    driver.delete_all_cookies()
    for name, value in session.cookies.items():
        try:
            driver.add_cookie({
                "name": name,
                "value": value,
                "domain": ".tixcraft.com",
                "path": "/",
            })
        except Exception:
            pass
    print(f"[LOGIN] cookie 已灌回瀏覽器，跳轉: {target_url}")
    driver.get(target_url)

    # 標題前綴注入（多開時區分視窗）
    try:
        driver.execute_script("""
            var prefix = arguments[0];
            setInterval(function() {
                if (document.title && document.title.indexOf(prefix) !== 0) {
                    document.title = prefix + document.title;
                }
            }, 500);
        """, f"【ACC-{acc_id}】")
    except Exception:
        pass
