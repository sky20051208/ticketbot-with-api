"""搶到票後：開乾淨 Chrome 注入 cookie 跳到結帳頁。

只有 COOKIE_SOURCE='string' 模式才走這支。userdata 模式由 browser_login.inject_cookies_and_go
把 cookie 灌回原本登入用的 driver（main 裡處理）。

啟用 proxy pool 時，這個 Chrome 也會走同一個 proxy（跟 curl_cffi 同 IP），結帳頁瀏覽
不會洩漏真實 IP — 跟登入/搶票全程一致。
"""
import tempfile
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

import browser_login
import config


def open_chrome_with_session(session, target_url: str):
    """開一個乾淨的 Chrome，注入 session cookie，跳轉到目標頁面。"""
    tmp_dir = tempfile.mkdtemp(prefix="tixcraft_chrome_")
    print(f"[CHROME] 隔離 Profile: {tmp_dir}")

    opts = Options()
    opts.add_argument(f"--user-data-dir={tmp_dir}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--disable-extensions")
    if config.WINDOW_W > 0 and config.WINDOW_H > 0:
        opts.add_argument(f"--window-size={config.WINDOW_W},{config.WINDOW_H}")
    else:
        opts.add_argument("--window-size=1280,900")
    if config.WINDOW_X >= 0 and config.WINDOW_Y >= 0:
        opts.add_argument(f"--window-position={config.WINDOW_X},{config.WINDOW_Y}")

    # proxy 透過 localhost bridge 處理 auth，不會 load extension，所以可以安全 --disable-extensions
    browser_login.apply_proxy_to_options(opts, tmp_dir, config.CURRENT_PROXY)

    driver = webdriver.Chrome(options=opts)

    # 先開 tixcraft 首頁讓 domain 生效
    driver.get("https://tixcraft.com")
    time.sleep(0.5)
    driver.delete_all_cookies()

    for name, value in session.cookies.items():
        try:
            driver.add_cookie({
                "name": name,
                "value": value,
                "domain": "tixcraft.com",
                "path": "/",
            })
        except Exception:
            pass

    print(f"[CHROME] Cookie 注入完畢，跳轉: {target_url}")
    driver.get(target_url)

    # 標題前綴注入（區分多開視窗）
    try:
        driver.execute_script("""
            var prefix = arguments[0];
            setInterval(function() {
                if (document.title && document.title.indexOf(prefix) !== 0) {
                    document.title = prefix + document.title;
                }
            }, 500);
        """, f"【ACC-{config.ACC_ID}】")
    except Exception:
        pass

    input("\n按 Enter 關閉瀏覽器...")
    driver.quit()
