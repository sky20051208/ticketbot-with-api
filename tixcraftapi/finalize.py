"""搶到票後：開乾淨 Chrome 注入 cookie 跳到結帳頁。

只有 COOKIE_SOURCE='string' 模式才走這支。userdata 模式由 browser_login.inject_cookies_and_go
把 cookie 灌回原本登入用的 driver（main 裡處理）。

啟用 proxy pool 時，這個 Chrome 也會走同一個 proxy（跟 curl_cffi 同 IP），結帳頁瀏覽
不會洩漏真實 IP — 跟登入/搶票全程一致。

視窗正常開啟顯示（不做最小化/移出螢幕範圍這些花招）——實測過 Chrome 對真正最小化的
視窗會停止渲染合成，Selenium 截圖會被迫把視窗還原才能拍；改成開在螢幕外雖然能避開
這個問題，但座標抓不好會變得很難手動找回視窗。折騰一輪後決定不值得，直接正常開窗。
"""
import tempfile
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

import browser_login
import config
from LineBot import line_push


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
    browser_login.apply_stealth_to_options(opts)
    browser_login.apply_platform_options(opts)
    # 結帳頁的 Chrome 也要跟 session 走同一個出口，不然 cookie 注進去馬上被 eps 打回
    browser_login.apply_proxy_to_options(opts, tmp_dir, config.CURRENT_PROXY,
                                         config.LOCAL_BIND_IP)

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

    if config.ENABLE_LINE_NOTIFY and config.LINE_USER_ID:
        time.sleep(1.5)  # 等結帳頁渲染完再截圖
        line_push.notify_checkout_from_driver(config.LINE_USER_ID, driver)

    # 截圖已經拍完，不會再對這個視窗做 resize/捲動/截圖，這時候縮到工具列是安全的
    # （之前踩的坑是「截圖當下視窗已經最小化」，不是「截圖後才最小化」）
    try:
        driver.minimize_window()
    except Exception:
        pass

    input("\n按 Enter 關閉瀏覽器...")
    driver.quit()
