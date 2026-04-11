# kktix/kkbot.py

import asyncio
import nodriver as uc
import os
import sys
import random
from datetime import datetime

# --- 路徑修正 ---
if getattr(sys, 'frozen', False):
    root_dir = os.path.dirname(sys.executable)
else:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(current_dir)

if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

try:
    from config import (
        WANTED_TICKET_COUNT, WANTED_AREA_KEYWORD, WANTED_DATE_KEYWORD,
        TARGET_TIME, TIME_WATCH_URL, ENABLE_TIME_WATCHER, AREA_AUTO_SELECT_MODE,
        EXCLUDE_AREA_KEYWORD
    )
except ImportError:
    print("❌ 找不到 config.py 或缺少必要變數")
    sys.exit(1)

from timeWatcher import TimeWatcher
PAUSE_FILE = "pause.lock"

# ----------------------------------------------------
# 輔助函式
# ----------------------------------------------------
async def check_pause():
    if os.path.exists(PAUSE_FILE):
        while os.path.exists(PAUSE_FILE):
            await asyncio.sleep(1)

async def fetch_and_print_logs(tab):
    """將瀏覽器端 JS 記錄的操作日誌拉回 Python 並印出"""
    try:
        js_code = """
        (function() {
            if (window.botLogs && window.botLogs.length > 0) {
                let l = window.botLogs.slice();
                window.botLogs = []; 
                return l;
            }
            return [];
        })()
        """
        logs = await tab.evaluate(js_code)
        if logs:
            for log in logs:
                print(f"📜 [網頁回報] {log}")
    except:
        pass

# ----------------------------------------------------
# 活動頁處理器
# ----------------------------------------------------
async def handle_kktix_event_page(tab):
    await check_pause()
    js = f"""
    (function() {{
        let dateKeyword = '{WANTED_DATE_KEYWORD}'; 
        let btns = document.querySelectorAll('a.btn-point, button.btn-primary');
        for (let btn of btns) {{
            if (btn.classList.contains('disabled')) continue;
            let text = btn.innerText.trim();
            if (!text.includes('下一步') && !text.includes('Next') && !text.includes('立即')) continue;
            
            if (dateKeyword) {{
                let container = btn.closest('tr') || btn.closest('.ticket-unit') || btn.closest('div.description') || btn.closest('li');
                if (container && !container.innerText.includes(dateKeyword)) continue; 
            }}
            btn.click();
            return true;
        }}
        return false;
    }})()
    """
    return await tab.evaluate(js)

# ----------------------------------------------------
# ⚡ 核心：啟動與光速注入
# ----------------------------------------------------
async def run_kktix_setup():
    print("🚀 KKTIX 終極雙軌模式啟動。")
    print("🛡️ [系統] 載入光速注入、Angular 原生點擊、全動作回報與防禦機制")
    
    browser = await uc.start(
        headless=False,
        browser_args=["--start-maximized", "--disable-notifications"]
    )
    
    try:
        now = datetime.now()
        target_dt = datetime.strptime(TARGET_TIME, "%H:%M:%S").replace(
            year=now.year, month=now.month, day=now.day
        )
        activation_timestamp = int(target_dt.timestamp() * 1000)
    except Exception as e:
        print(f"⚠️ 時間解析錯誤，JS 將不檢查時間: {e}")
        activation_timestamp = 0

    # === 預先注入腳本 ===
    injection_js = f"""
    (function() {{
        if (!window.botLogs) window.botLogs = [];
        if (!window.logOnceFlags) window.logOnceFlags = {{}};

        function logInfo(key, msg) {{
            if (!window.logOnceFlags[key]) {{
                window.botLogs.push(msg);
                window.logOnceFlags[key] = true;
            }}
        }}

        if (!window.originalAlert) {{
            window.originalAlert = window.alert;
            window.alert = function(msg) {{
                window.isBotFailed = true; 
                window.botLogs.push("❌ [系統彈窗攔截] " + msg);
                window.originalAlert(msg);
            }};
        }}

        let activationTime = {activation_timestamp};

        let loop = setInterval(() => {{
            if (activationTime > 0 && Date.now() < activationTime) return;
            if (!window.location.href.includes('/registrations/new')) return;

            if (window.isBotFailed) return;
            if (window.isBotClicked) return;

            let mode = '{AREA_AUTO_SELECT_MODE}';
            let targetPrice = '{WANTED_AREA_KEYWORD}';
            let excludeKeywords = '{EXCLUDE_AREA_KEYWORD}'; 
            let count = parseInt('{WANTED_TICKET_COUNT}') || 1;

            let rows = Array.from(document.querySelectorAll('.ticket-unit, tr[id^="ticket_"], .ticket-item'));
            if (rows.length === 0) {{
                logInfo('wait_rows', '⏳ 等待網頁載入票區清單...');
                return; 
            }}
            logInfo('found_rows', '✅ 成功讀取票區清單');

            if (mode === "由下而上") rows.reverse();
            else if (mode === "隨機") rows.sort(() => Math.random() - 0.5);

            let ticketFound = false;

            // 處理所有可能的同意勾選框 (實名制、退票條款等複數勾選框)
            let checkboxes = document.querySelectorAll('input[type="checkbox"]');
            checkboxes.forEach((checkbox, index) => {{
                if (!checkbox.checked) {{
                    checkbox.click();
                    logInfo('checkbox_' + index, '✅ 成功勾選同意條款 (' + (index + 1) + ')');
                }}
            }});

            let excludeArray = excludeKeywords.split(';').map(k => k.trim()).filter(k => k !== '');

            for (let row of rows) {{
                let rowText = row.innerText || "";
                
                if (rowText.includes('未開賣') || rowText.includes('暫無票') || 
                    rowText.includes('已售完') || rowText.includes('Sold Out') || 
                    rowText.includes('完売')) {{
                    continue;
                }}

                let skipByExclude = false;
                for (let ek of excludeArray) {{
                    if (rowText.includes(ek)) {{
                        skipByExclude = true;
                        break;
                    }}
                }}
                if (skipByExclude) continue;

                let matchCht = rowText.match(/剩\\s*(\\d+)\\s*張/);
                if (matchCht && parseInt(matchCht[1]) < count) {{
                    logInfo('skip_qty_' + rowText.substring(0, 10).trim(), '⚠️ 庫存不足跳過: ' + rowText.substring(0, 15).trim() + '...');
                    continue;
                }}
                
                let matchEn = rowText.match(/(\\d+)\\s*Left/i);
                if (matchEn && parseInt(matchEn[1]) < count) continue;

                let matchJp = rowText.match(/残り\\s*(\\d+)\\s*枚/);
                if (matchJp && parseInt(matchJp[1]) < count) continue;

                let priceEl = row.querySelector('.ticket-price');
                let qtyInput = row.querySelector('.ticket-quantity input, input[type="text"], input[type="number"]');
                
                // 如果已經填寫達標，就標記找到並準備送出
                if (qtyInput && parseInt(qtyInput.value) === count) {{
                    ticketFound = true; 
                    break; 
                }}

                if (!qtyInput || qtyInput.disabled || qtyInput.style.display === 'none') continue;

                let shouldSelect = false;
                if (mode === "關鍵字優先") {{
                    if (priceEl && priceEl.innerText.replace(/[^0-9]/g, '') === targetPrice) shouldSelect = true;
                }} else {{
                    shouldSelect = true;
                }}
                
                if (shouldSelect) {{
                    // 【關鍵修正】尋找並點擊 Angular 原生的「＋號按鈕」
                    let plusBtn = row.querySelector('button[ng-click="quantityBtnClick(1)"]');
                    let pText = priceEl ? priceEl.innerText.trim() : "未知區域";

                    if (plusBtn) {{
                        // 計算還需要點擊幾次＋號
                        let currentQty = parseInt(qtyInput.value) || 0;
                        let clicksNeeded = count - currentQty;
                        
                        for (let c = 0; c < clicksNeeded; c++) {{
                            plusBtn.click();
                        }}
                        ticketFound = true;
                        logInfo('fill_ticket_btn', '✅ 透過原生＋號按鈕填寫: ' + count + '張 -> ' + pText);
                    }} else {{
                        // 如果找不到＋號按鈕，才退回原本的強制塞值方法 (備用方案)
                        qtyInput.focus();
                        qtyInput.value = count;
                        qtyInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        qtyInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        qtyInput.dispatchEvent(new Event('blur', {{ bubbles: true }}));
                        ticketFound = true;
                        logInfo('fill_ticket_input', '✅ 透過強制輸入填寫: ' + count + '張 -> ' + pText);
                    }}
                    break;
                }}
            }}

            if (!ticketFound) {{
                logInfo('no_ticket', '❌ 本次未找到符合條件且有庫存的票種');
            }} else {{
                // --- 點擊送出 ---
                let allBtns = Array.from(document.querySelectorAll('.register-new-next-button-area button, button.btn-primary'));
                let nextBtn = allBtns.find(b => b.innerText.includes('電腦配位') && b.style.display !== 'none');
                
                if (!nextBtn) nextBtn = allBtns.find(b => (b.innerText.includes('下一步') || b.innerText.includes('Next')) && b.style.display !== 'none');
                if (!nextBtn) nextBtn = document.querySelector('button.btn-primary[type="submit"]');

                if (nextBtn && !nextBtn.classList.contains('disabled')) {{
                    window.isBotClicked = true; 
                    logInfo('click_btn', '🚀 執行強力點擊送出！');
                    
                    nextBtn.focus();
                    nextBtn.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true}}));
                    nextBtn.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true}}));
                    nextBtn.click();

                    setTimeout(() => {{
                        if (window.location.href.includes('/registrations/new') && !window.isBotFailed) {{
                            window.isBotClicked = false;
                            logInfo('click_retry', '⚠️ 按鈕可能被框架忽略，解除鎖定準備再次重擊！');
                        }}
                    }}, 1000);

                }} else {{
                    logInfo('wait_btn', '⏳ 等待下一步按鈕變亮解鎖...');
                }}
            }}
        }}, 5); 
    }})();
    """

    tab = await browser.get("https://kktix.com/users/sign_in")
    
    await tab.send(uc.cdp.page.add_script_to_evaluate_on_new_document(source=injection_js))

    async def handle_dialog(event: uc.cdp.page.JavascriptDialogOpening):
        await tab.send(uc.cdp.page.handle_java_script_dialog(accept=True))

    await tab.send(uc.cdp.page.enable())
    tab.add_handler(uc.cdp.page.JavascriptDialogOpening, handle_dialog)

    return browser, tab

# ----------------------------------------------------
# 🚦 主控制迴圈
# ----------------------------------------------------
async def main():
    browser, tab = await run_kktix_setup()
    
    watcher = TimeWatcher(TARGET_TIME, TIME_WATCH_URL)
    has_waited = not ENABLE_TIME_WATCHER

    print(f"\n⏳ 監控啟動！目標時間: {TARGET_TIME}")

    while True:
        try:
            await check_pause()
            if not browser or not tab: break
            
            await fetch_and_print_logs(tab)

            try:
                current_url = await tab.evaluate("window.location.href")
            except:
                await asyncio.sleep(0.1)
                continue

            if not has_waited:
                if "/registrations/new" in current_url or "/events/" in current_url:
                    await watcher.wait_for_open_async()
                    has_waited = True
                    print("\n⚡ 倒數結束！全軍突擊！")
                    
                    try:
                        current_url = await tab.evaluate("window.location.href")
                    except:
                        pass
                    
                    if "/registrations/" in current_url:
                        print("🔄 [軌道A: 偷跑刷新] 倒數結束，強制重載頁面獲取票單！")
                        await tab.reload()  
                        await asyncio.sleep(0.5) 
                        continue 
                else:
                    await asyncio.sleep(1)
                    continue

            if "/registrations/new" in current_url:
                await asyncio.sleep(0.3) 
                
                current_url_check = await tab.evaluate("window.location.href")
                if "/registrations/new" in current_url_check:
                    try:
                        is_clicked = await tab.evaluate("window.isBotClicked === true")
                        is_failed = await tab.evaluate("window.isBotFailed === true")
                    except:
                        is_clicked, is_failed = False, False

                    if is_failed:
                        print("⚠️ 準備執行清票刷新...", end='\r')
                        await tab.evaluate("window.isBotFailed = false; window.botAlertMsg = '';")
                        await tab.reload()
                        await asyncio.sleep(random.uniform(0.5, 0.8))
                    elif is_clicked:
                        print("⏳ 已送出表單，等待伺服器配位或跳轉中...", end='\r')
                        await asyncio.sleep(0.5)
                    else:
                        print("🔄 填單頁未見匹配票種，執行清票刷新...", end='\r')
                        await tab.reload()
                        await asyncio.sleep(random.uniform(0.6, 1.0))

            elif "/registrations/" in current_url and "/new" not in current_url:
                print(f"\n🎉🎉🎉 [恭喜] 成功進入配位/結帳流程！")
                while True: await asyncio.sleep(10)

            elif "/events/" in current_url:
                clicked = await handle_kktix_event_page(tab)
                if clicked:
                    await asyncio.sleep(0.1)
                else:
                    print(f"🔄 活動頁未見購票按鈕，刷新中...", end='\r')
                    await tab.reload()
                    await asyncio.sleep(random.uniform(0.8, 1.2))
            
            else:
                await asyncio.sleep(0.5)

        except Exception as e:
            print(f"⚠️ 異常: {e}", end='\r')
            await asyncio.sleep(0.5)

if __name__ == "__main__":
    uc.loop().run_until_complete(main())