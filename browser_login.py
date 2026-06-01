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
import time
from urllib.parse import urlparse

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from tixcraftapi.proxy_bridge import LocalProxyBridge

BASE = "https://tixcraft.com"

# 保留 bridge 物件 reference 避免 GC；thread 是 daemon，process 結束會自動收
_active_bridges: list[LocalProxyBridge] = []


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


def apply_proxy_to_options(opts: Options, _unused_dir: str, proxy_url: str) -> bool:
    """把 proxy 設定塞進 Chrome Options（透過 localhost bridge 處理 auth）。
    回傳 True 表示真的套用了 proxy；False 表示 proxy_url 空或解析失敗。
    第二個參數保留是為了相容介面（bridge 不需要 fs 路徑）。"""
    if not proxy_url:
        return False
    p = parse_proxy_url(proxy_url)
    if not (p["host"] and p["port"]):
        print(f"[PROXY-CHROME] URL 解析失敗: {proxy_url} — Chrome 改走直連")
        return False

    # 起 localhost bridge 自動塞 auth header（Chrome 那端不用處理認證）
    bridge = LocalProxyBridge(p["host"], p["port"], p["user"] or "", p["password"] or "")
    local_port = bridge.start()
    _active_bridges.append(bridge)

    opts.add_argument(f"--proxy-server=http://127.0.0.1:{local_port}")
    # WebRTC 預設會繞過 proxy 用本機 IP 探測 → 鎖死只允許 proxied UDP
    opts.add_argument("--webrtc-ip-handling-policy=disable_non_proxied_udp")

    print(f"[PROXY-CHROME] 127.0.0.1:{local_port} → {p['host']}:{p['port']} (bridge, auth={'on' if p['user'] else 'off'})")
    return True


# ==========================================
# 主要對外函式
# ==========================================

def launch_browser(user_data_dir: str, window_w: int = -1, window_h: int = -1,
                   window_x: int = -1, window_y: int = -1,
                   proxy_url: str = ""):
    """用指定 user-data-dir 開 Chrome（可見視窗），回 driver。
    proxy_url 非空時 Chrome 全程走該 proxy（透過 localhost bridge 處理 auth）；
    空字串時直連，行為跟舊版相同。"""
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

    apply_proxy_to_options(opts, user_data_dir, proxy_url)

    driver = webdriver.Chrome(options=opts)
    print(f"[LOGIN] Chrome 已開: {user_data_dir}")
    return driver


def wait_for_login(driver, timeout: int = 600) -> bool:
    """跳到 /login 頁，每次都走一遍登入流程；輪詢直到 URL 回到 tixcraft 主站。

    判定「登入完成」= URL 的 **hostname 屬於 tixcraft.com** 且 **path 不在 /login**。
    必須用 urlparse 比對 hostname/path，**不能直接 substring 比 URL 整串** —
    FB OAuth 的 consent URL 會把 `tixcraft.com/login/facebook` 塞進 query param
    當 callback URL，substring 比對會被騙。

    這樣涵蓋所有 OAuth 流程（Google / Facebook / Apple 等）— 中途會跳到第三方 domain
    等使用者按「以某某身分繼續」之類授權，授權完成 OAuth callback 一定會繞回
    tixcraft.com/login/{provider}?code=...，server 處理完再 redirect 到 /user/info
    或 / 等非 /login 頁，那時才算完成。
    """
    print(f"[LOGIN] 開啟登入頁，請在 Chrome 視窗完成登入（最多 {timeout}s）...")
    driver.get(f"{BASE}/login")
    time.sleep(1.0)

    deadline = time.monotonic() + timeout
    last_url = ""
    while time.monotonic() < deadline:
        try:
            cur = driver.current_url or ""
        except Exception:
            print("[LOGIN] driver 異常（視窗被關閉？）")
            return False

        parsed = urlparse(cur)
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
        # hostname 屬於 tixcraft.com（含 www.tixcraft.com）且 path 不在 /login
        if (host == "tixcraft.com" or host.endswith(".tixcraft.com")) and "/login" not in path:
            print(f"[LOGIN] 登入完成: {cur}")
            return True

        if cur != last_url:
            print(f"[LOGIN] 等待中... 目前: {cur}")
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
