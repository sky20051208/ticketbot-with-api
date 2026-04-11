# bot.py (速度優化版 V15.0 - 修正 Live Nation 新分頁追蹤)
# 優化項目：減少等待時間、並行處理、快取機制、JS執行優化、空白過渡頁防護
# V15.0 新增：雙軌新分頁偵測（TargetCreated事件 + 輪詢掃描）

import asyncio
import nodriver as uc
import random
import time
import os
from datetime import datetime
from config import WANTED_TICKET_COUNT, WANTED_AREA_KEYWORD, WANTED_DATE_KEYWORD, Selector, TARGET_TIME, TIME_WATCH_URL, AREA_AUTO_SELECT_MODE, ENABLE_TIME_WATCHER, EXCLUDE_AREA_KEYWORD, PRE_ORDER_CODE
from timeWatcher import TimeWatcher
from captchaAI.predict import solve_captcha_nodriver

PAUSE_FILE = "pause.lock"
#打包指令1 pyinstaller --onedir --name=TicketBot --exclude-module=config --hidden-import=selenium --hidden-import=selenium.webdriver.common.by --collect-all ddddocr main.py
#打包指令2 pyinstaller --onefile --name=Launcher --exclude-module=config gui.py

# ----------------------------------------------------
# 輔助函式（優化版）
# ----------------------------------------------------

async def check_pause():
    if os.path.exists(PAUSE_FILE):
        print("\n⏸️ 程式已暫停...", end='\r')
        while os.path.exists(PAUSE_FILE):
            await asyncio.sleep(0.5)
        print("\n▶️ 程式繼續執行！        ")

async def random_sleep(min_s=0.2, max_s=0.5):
    await asyncio.sleep(random.uniform(min_s, max_s))

async def pre_fill_form(tab):
    """預填表單"""
    num = WANTED_TICKET_COUNT
    js = f"""
    (function() {{
        let selects = document.querySelectorAll('.mobile-select');
        selects.forEach(s => {{
            s.value = '{num}';
            s.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }});
        var agree = document.getElementById('TicketForm_agree');
        if (agree && !agree.checked) agree.click();
        return selects.length;
    }})()
    """
    try:
        return await tab.evaluate(js)
    except:
        return 0

async def submit_order_nodriver(tab, captcha_code: str):
    """提交訂單"""
    if not captcha_code: return False
    try:
        fill_js = f"""
        (function() {{
            var input = document.getElementById('{Selector.CAPTCHA_INPUT[1]}');
            if (input) {{
                input.value = '{captcha_code}';
                var btn = document.querySelector('button[type="submit"]');
                if (btn) {{
                    setTimeout(() => btn.click(), 0);
                    return true;
                }}
            }}
            return false;
        }})()
        """
        return await tab.evaluate(fill_js)
    except:
        return False

async def refresh_captcha_nodriver(tab):
    """刷新驗證碼"""
    try:
        await tab.evaluate(f"document.getElementById('{Selector.CAPTCHA_IMAGE[1]}').click();")
        await asyncio.sleep(0.15)
        return True
    except:
        return False

# ----------------------------------------------------
# 頁面處理器（速度優化版）
# ----------------------------------------------------

async def handle_verify_page(tab):
    await check_pause()
    print(f"🔐 [驗證頁] 準備輸入預購碼: {PRE_ORDER_CODE}...")

    if not PRE_ORDER_CODE:
        print("⚠️ 警告：未設定預購碼！")
        await asyncio.sleep(0.5)
        return

    verify_js = f"""
    (function() {{
        var input = document.querySelector("#form-ticket-verify input[name='checkCode']")
                  || document.querySelector("#checkCode")
                  || document.querySelector("input.greyInput[name='checkCode']");

        if (input) {{
            input.value = '{PRE_ORDER_CODE}';
            input.dispatchEvent(new Event('input', {{ bubbles: true }}));

            var btn = document.querySelector("#form-ticket-verify button[type='submit']")
                     || document.getElementById('submitButton')
                     || document.querySelector("button.btn-primary");

            if (btn) {{
                setTimeout(() => btn.click(), 0);
                return true;
            }}
        }}
        return false;
    }})()
    """

    if await tab.evaluate(verify_js):
        print("🚀 預購碼已送出")
        await asyncio.sleep(0.3)
    else:
        print("❌ 找不到輸入框，刷新重試...")
        await tab.reload()
        await tab.wait_for("body")

async def handle_game_page(tab):
    await check_pause()

    try:
        current_url = await tab.evaluate("window.location.href")
    except:
        return

    # ==========================================
    # 🛡️ 防止 about:blank 跳轉陷阱
    # ==========================================
    if "about:blank" in current_url or "tixcraft.com" not in current_url:
        print("⏳ [系統] 偵測到跳轉過渡頁面，等待目標網頁載入...", end='\r')
        await asyncio.sleep(0.5)
        return
    # ==========================================

    if "activity/detail" in current_url and "activity/game" in TIME_WATCH_URL:
        print(f"🚀 [場次頁] 偵測到詳情頁，強制跳轉至: {TIME_WATCH_URL}")
        await tab.get(TIME_WATCH_URL)
        return

    search_msg = f"關鍵字 '{WANTED_DATE_KEYWORD}'" if WANTED_DATE_KEYWORD else "任意場次"
    print(f"👀 [場次頁] 搜尋: {search_msg} 立即訂購...")

    scan_js = f"""
    (function() {{
        const keyword = '{WANTED_DATE_KEYWORD}';
        const tags = ["button", "div", "a"];

        function isValidBtn(el) {{
            let text = el.innerText.replace(/\\s/g, '');
            if (el.classList.contains('disabled')) return false;
            if (text.includes("售完") || text.includes("Soldout") || text.includes("尚未開賣")) return false;
            if (text.includes("立即訂購") || text.includes("Order")) return true;
            return false;
        }}

        if (keyword) {{
            let rows = document.querySelectorAll('#gameList tr');
            for (let row of rows) {{
                if (row.innerText.includes(keyword)) {{
                    let elements = row.querySelectorAll('button, a, div');
                    for (let el of elements) {{
                        if (isValidBtn(el)) {{
                            el.click();
                            return true;
                        }}
                    }}
                }}
            }}
            return false;
        }}

        for (let tag of tags) {{
            let elements = document.querySelectorAll(tag);
            for (let el of elements) {{
                if (isValidBtn(el)) {{
                    el.click();
                    return true;
                }}
            }}
        }}
        return false;
    }})();
    """

    is_clicked = await tab.evaluate(scan_js)

    if is_clicked:
        print(f"🔥 [場次頁] 鎖定目標，點擊成功！")
        await asyncio.sleep(0.5)
    else:
        if WANTED_DATE_KEYWORD:
            print(f"⚠️ [場次頁] 找不到符合 '{WANTED_DATE_KEYWORD}' 且可購買的場次，刷新...")
        else:
            print("⚠️ [場次頁] 暫無可購買按鈕，刷新...")
        await tab.reload()
        try:
            await tab.wait_for("body")
        except:
            pass

async def handle_area_page(tab):
    await check_pause()
    strategy = AREA_AUTO_SELECT_MODE
    exclude_keyword = EXCLUDE_AREA_KEYWORD if 'EXCLUDE_AREA_KEYWORD' in globals() else ""

    print(f"🎯 [選區頁] 策略: {strategy} | 關鍵字: {WANTED_AREA_KEYWORD}")

    try:
        await tab.wait_for(".select_form_a, .select_form_b, .zone", timeout=0.3)
    except:
        return

    mode_map = {
        "關鍵字優先": "KEYWORD",
        "由上而下": "TOP_DOWN",
        "由下而上": "BOTTOM_UP",
        "隨機": "RANDOM"
    }
    mode_js_var = mode_map.get(strategy, "KEYWORD")

    js_script = f"""
    (function() {{
        const mode = '{mode_js_var}';
        const keyword = '{WANTED_AREA_KEYWORD}';
        const excludes = '{exclude_keyword}'.split(';').filter(e => e.trim());

        let links = Array.from(document.querySelectorAll('.select_form_a a, .select_form_b a, .zone a'));

        let validLinks = links.filter(link => {{
            let text = link.innerText.replace(/\\s/g, '');
            if (link.classList.contains('disabled') ||
                text.includes('售完') ||
                text.includes('Soldout') ||
                text.includes('剩餘0')) return false;
            return !excludes.some(ex => text.includes(ex));
        }});

        if (validLinks.length === 0) return false;

        let targetLink = null;

        if (mode === 'KEYWORD') {{
            let matches = validLinks.filter(link => link.innerText.replace(/\\s/g, '').includes(keyword));
            if (matches.length > 0) {{
                targetLink = matches[Math.floor(Math.random() * matches.length)];
                console.log("關鍵字命中 " + matches.length + " 個，隨機選擇其一");
            }}
        }}
        else if (mode === 'TOP_DOWN') targetLink = validLinks[0];
        else if (mode === 'BOTTOM_UP') targetLink = validLinks[validLinks.length - 1];
        else if (mode === 'RANDOM') targetLink = validLinks[Math.floor(Math.random() * validLinks.length)];

        if (targetLink) {{
            setTimeout(() => targetLink.click(), 0);
            return true;
        }}
        return false;
    }})()
    """

    if await tab.evaluate(js_script):
        print("🔥 [選區頁] 點擊成功！")
        await asyncio.sleep(0.15)
        return
    else:
        print("⚠️ [選區頁] 無票或未找到 → 進入『1 秒高速偵測』")

    await tab.reload()

    start = time.time()
    while time.time() - start < 2.5:
        await asyncio.sleep(0.05)
        try:
            await tab.wait_for(".select_form_a, .select_form_b, .zone", timeout=0.1)
        except:
            continue
        ok = await tab.evaluate(js_script)
        if ok:
            print("🔥🔥🔥 [選區頁] 高速偵測中 → 抓到票了！")
            await asyncio.sleep(0.1)
            return

    print("❌ [選區頁] 偵測超時仍無票 → 冷卻 5 秒")
    await asyncio.sleep(5)


async def handle_ticket_page(tab):
    await check_pause()
    print("📝 [填單頁] 處理中...")

    fill_task = asyncio.create_task(pre_fill_form(tab))
    captcha_task = asyncio.create_task(solve_captcha_nodriver(tab))

    await fill_task
    captcha_code = await captcha_task

    await check_pause()

    if captcha_code and len(captcha_code) == 4:
        print(f"🚀 送出: {captcha_code}")
        await submit_order_nodriver(tab, captcha_code)
        await asyncio.sleep(0.3)

        try:
            current_url = await tab.evaluate("window.location.href")
            if "/ticket/ticket" in current_url:
                print("⚠️ 驗證碼錯誤，原地重試...")
                return
        except:
            pass
    else:
        print(f"⚠️ 辨識失敗，刷新...")
        await refresh_captcha_nodriver(tab)
        await asyncio.sleep(0.15)

# ----------------------------------------------------
# 【V15.0 新增】新分頁偵測核心函式
# ----------------------------------------------------

async def wait_for_tixcraft_tab(browser, timeout=300):
    """
    雙軌偵測新開的拓元分頁：
      軌道 A：監聽 TargetCreated 事件（速度快）
      軌道 B：定期輪詢所有 targets（保險用）
    回傳新的 tab 物件，或 None（超時）
    """

    # --- 軌道 A：事件監聽容器 ---
    new_target_holder = {"target_id": None}

    async def on_target_created(event: uc.cdp.target.TargetCreated):
        info = event.target_info
        url = info.url or ""
        # 只在意 page 類型、且 URL 已是拓元網址（排除空白過渡頁）
        if info.type_ == "page" and "tixcraft.com" in url:
            print(f"🆕 [事件] 偵測到新拓元分頁: {url}")
            new_target_holder["target_id"] = info.target_id

    # 開啟 Target 探索（讓 browser 能拿到所有分頁資訊）
    try:
        await browser.send(uc.cdp.target.set_discover_targets(discover=True))
    except Exception as e:
        print(f"⚠️ set_discover_targets 失敗（可忽略）: {e}")

    browser.add_handler(uc.cdp.target.TargetCreated, on_target_created)

    print("👀 [等待] 請在瀏覽器完成 Live Nation 操作並按下「尋找票券」...")
    print(f"   （最多等待 {timeout} 秒）")

    deadline = time.time() + timeout

    while time.time() < deadline:
        await asyncio.sleep(0.3)
        await check_pause()

        # --- 軌道 A：事件觸發 ---
        if new_target_holder["target_id"]:
            print("✅ [事件軌道] 拿到新分頁 target_id，切換中...")
            try:
                new_tab = await browser.get_tab(new_target_holder["target_id"])
                return new_tab
            except Exception as e:
                print(f"⚠️ get_tab 失敗，改用輪詢: {e}")
                new_target_holder["target_id"] = None  # 重置，繼續輪詢

        # --- 軌道 B：輪詢掃描所有 targets ---
        try:
            targets = await browser.send(uc.cdp.target.get_targets())
            for t in targets:
                url = t.url or ""
                if ("tixcraft.com/activity/detail" in url or
                        "tixcraft.com/activity/game" in url or
                        "tixcraft.com/ticket" in url):
                    print(f"✅ [輪詢軌道] 發現拓元分頁: {url}")
                    try:
                        new_tab = await browser.get_tab(t.target_id)
                        return new_tab
                    except Exception as e:
                        print(f"⚠️ get_tab 失敗: {e}")
        except Exception as e:
            # get_targets 可能因 nodriver 版本差異而失敗，靜默處理
            pass

    print("❌ 等待新分頁超時！")
    return None


async def attach_handlers_to_tab(tab):
    """
    將必要的 CDP handler（alert 攔截等）附加到指定的 tab。
    切換到新分頁後必須呼叫此函式。
    """
    async def alert_handler(event: uc.cdp.page.JavascriptDialogOpening):
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"⚡ [{timestamp}] Alert 秒殺: {event.message}")
        try:
            await tab.send(uc.cdp.page.handle_java_script_dialog(accept=True))
        except:
            pass

    try:
        await tab.send(uc.cdp.page.enable())
        tab.add_handler(uc.cdp.page.JavascriptDialogOpening, alert_handler)
    except Exception as e:
        print(f"⚠️ attach_handlers_to_tab 失敗: {e}")

    try:
        await tab.send(uc.cdp.network.enable())
    except:
        pass


async def wait_for_tab_stable(tab, keyword="tixcraft.com", max_wait=10):
    """
    等待 tab 的 URL 穩定（不再是 about:blank），
    確保新分頁完成跳轉後再往下執行。
    """
    print("⏳ [系統] 等待新分頁 URL 穩定...", end='\r')
    deadline = time.time() + max_wait
    while time.time() < deadline:
        await asyncio.sleep(0.3)
        try:
            current_url = await tab.evaluate("window.location.href")
            if keyword in current_url:
                print(f"🌐 新分頁已穩定: {current_url}          ")
                return current_url
        except:
            pass
    print(f"\n⚠️ 等待 URL 穩定超時（{max_wait}s）")
    return None

# ----------------------------------------------------
# 啟動流程（V15.0 - Live Nation 新分頁追蹤版）
# ----------------------------------------------------

async def run_initial_setup():
    print("🚀 啟動 nodriver (V15.0 - Live Nation 新分頁追蹤版)...")

    user_data_dir = os.path.abspath("./chrome_profile")

    browser = await uc.start(
        headless=False,
        user_data_dir=user_data_dir,
        browser_args=[
            "--start-maximized",
            "--disable-notifications",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ]
    )

    # ================================================
    # 判斷入口：Live Nation 預購 vs 一般模式
    # ================================================

    # Live Nation 模式：TIME_WATCH_URL 為空，或明確設定為 "live_nation" 觸發
    # 實際判斷條件請依你的 config 調整
    is_live_nation_mode = (not TIME_WATCH_URL) or ("live_nation" in TIME_WATCH_URL.lower())

    if is_live_nation_mode:
        # ============================================
        # 【Live Nation 預購模式】
        # 1. 開啟 Live Nation 官網，讓使用者手動操作
        # 2. 偵測「尋找票券」後新開的拓元分頁
        # 3. 切換到拓元分頁，繼續自動搶票
        # ============================================
        print("\n🎟️  [Live Nation 預購模式]")
        print("   請在瀏覽器完成以下步驟：")
        print("   1. 登入 Live Nation 帳號")
        print("   2. 前往活動頁面")
        print("   3. 按下「尋找票券」（FindTickets）")
        print("   程式將自動偵測新開的拓元分頁並接手...")

        # 開啟 Live Nation（或保持空白讓使用者自己導航）
        live_nation_url = "https://www.livenation.com.tw/"
        tab = await browser.get(live_nation_url)

        # Cookie 同意
        try:
            await tab.evaluate("""
                var btn = document.getElementById('onetrust-accept-btn-handler');
                if (btn) btn.click();
            """)
        except:
            pass

        # ✅ 核心：等待並切換到新開的拓元分頁
        new_tab = await wait_for_tixcraft_tab(browser, timeout=300)

        if new_tab is None:
            print("❌ 未能取得拓元分頁，程式結束。")
            return browser, tab  # 回傳舊 tab 避免 crash，main 應自行處理

        # 切換成功，之後所有操作都用 new_tab
        tab = new_tab

        # 附加 handler 到新分頁
        await attach_handlers_to_tab(tab)

        # 等待 URL 穩定（跳過 about:blank 過渡）
        stable_url = await wait_for_tab_stable(tab, keyword="tixcraft.com", max_wait=15)

        if stable_url is None:
            print("⚠️ URL 穩定等待失敗，嘗試繼續...")

        print(f"✅ [Live Nation] 成功切換至拓元分頁，開始自動搶票！")

    else:
        # ============================================
        # 【一般模式 / 時間監控模式】（原有邏輯保留）
        # ============================================
        tab = await browser.get("https://tixcraft.com/")

        # CDP handler
        async def alert_handler(event: uc.cdp.page.JavascriptDialogOpening):
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"⚡ [{timestamp}] Alert 秒殺: {event.message}")
            try:
                await tab.send(uc.cdp.page.handle_java_script_dialog(accept=True))
            except:
                pass

        await tab.send(uc.cdp.page.enable())
        tab.add_handler(uc.cdp.page.JavascriptDialogOpening, alert_handler)

        try:
            await tab.send(uc.cdp.network.enable())
        except:
            pass

        # Cookie 同意
        try:
            await tab.evaluate("""
                var btn = document.getElementById('onetrust-accept-btn-handler');
                if (btn) btn.click();
            """)
        except:
            pass

        if ENABLE_TIME_WATCHER:
            print("\n🛑 [待命模式] 等待倒數...")
            watcher = TimeWatcher(TARGET_TIME, TIME_WATCH_URL)
            await watcher.wait_for_open_async()
            print(f"⚡ 時間到！直連...")

            current_url = await tab.evaluate("window.location.href")
            if TIME_WATCH_URL not in current_url:
                await tab.get(TIME_WATCH_URL)
            else:
                await tab.reload()
        else:
            print("\n🚀 [即時模式] 請手動進入活動頁...")
            target_id = TIME_WATCH_URL.split("/")[-1]
            print(f"   目標: {target_id}")

            while True:
                await check_pause()
                try:
                    current_url = await tab.evaluate("window.location.href")
                    if f"/activity/detail/{target_id}" in current_url:
                        print(f"⚡ 偵測到目標頁！跳轉...")
                        await tab.get(TIME_WATCH_URL)
                        break
                    if f"/activity/game/{target_id}" in current_url:
                        print("✅ 已在場次頁")
                        break
                except:
                    pass
                await asyncio.sleep(0.3)

        try:
            await tab.wait_for("body", timeout=2)
        except:
            pass

    return browser, tab