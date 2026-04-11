# ticketplus/ticketplusbot.py (V14 嚴格深度修正版)

import asyncio
import nodriver as uc
import os
import sys
import json
import time

# --- 修正後的路徑設定 (支援 PyInstaller) ---
if getattr(sys, 'frozen', False):
    # 如果是被打包成 exe 的狀態
    # 根目錄就是 exe 所在的資料夾
    root_dir = os.path.dirname(sys.executable)
else:
    # 如果是直接跑 .py 的開發狀態
    # 根目錄是這個檔案的上一層
    current_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(current_dir)

# 將根目錄加入系統路徑，這樣才能 import config
if root_dir not in sys.path:
    sys.path.append(root_dir)

from timeWatcher import TimeWatcher
from config import (
    WANTED_TICKET_COUNT, WANTED_AREA_KEYWORD, WANTED_DATE_KEYWORD, 
    TARGET_TIME, TIME_WATCH_URL, ENABLE_TIME_WATCHER,
    AREA_AUTO_SELECT_MODE 
)

PAUSE_FILE = "pause.lock"

# ----------------------------------------------------
# 輔助函式
# ----------------------------------------------------

async def check_pause():
    if os.path.exists(PAUSE_FILE):
        print("\n⏸️ [Ticketplus] 暫停中...", end='\r')
        while os.path.exists(PAUSE_FILE):
            await asyncio.sleep(1)
        print("\n▶️ [Ticketplus] 繼續！       ")

# ----------------------------------------------------
# 頁面處理邏輯
# ----------------------------------------------------

async def try_find_and_click_activity(tab, timeout=5):
    """
    [活動頁] V14 嚴格層級鎖定
    防止爬到父容器導致日期誤判
    """
    start_time = time.time()
    date_keyword = WANTED_DATE_KEYWORD.strip()

    js = f"""
    (function() {{
        let targetDate = '{date_keyword}';
        
        function isBuyBtn(el) {{
            let text = el.innerText.trim();
            return text.includes('立即') || text.includes('Buy') || text.includes('選購') || text.includes('訂購');
        }}

        // 嚴格檢查函式: 遇到 row 就停止
        function checkButtonRow(btn, keyword) {{
            if (!keyword) return true;
            
            let current = btn;
            // 往上找，最多 6 層
            for (let i = 0; i < 6; i++) {{
                if (!current.parentElement) break;
                current = current.parentElement;
                
                // 1. 取得該層文字
                let rowText = current.innerText || "";
                
                // 2. 邊界檢查: 遇到 row, d-flex, tr 就視為當前區域的邊界
                if (current.classList.contains('row') || current.classList.contains('d-flex') || current.tagName === 'TR') {{
                    return rowText.includes(keyword);
                }}
                
                // 若還沒碰到邊界，但文字符合，且該容器看起來不像大雜燴(只有一個購買按鈕)
                if (rowText.includes(keyword)) {{
                    let buyBtns = current.querySelectorAll('button');
                    let buyBtnCount = 0;
                    for(let b of buyBtns) if(isBuyBtn(b)) buyBtnCount++;
                    
                    if (buyBtnCount <= 1) return true;
                }}
            }}
            return false;
        }}

        let container = document.querySelector('#buyTicket');
        let scope = container ? container : document;
        let allBtns = scope.querySelectorAll('button, div[role="button"]');
        
        for (let btn of allBtns) {{
            if (btn.disabled) continue;
            if (isBuyBtn(btn)) {{
                if (btn.offsetParent !== null) {{
                    if (checkButtonRow(btn, targetDate)) {{
                        btn.click();
                        return JSON.stringify({{ success: true, msg: "命中日期: " + targetDate }});
                    }}
                }}
            }}
        }}

        return JSON.stringify({{ success: false }});
    }})()
    """

    print(f"👀 掃描活動按鈕 (日期: {date_keyword if date_keyword else '不限'})...", end='\r')
    
    while time.time() - start_time < timeout:
        try:
            res_str = await tab.evaluate(js)
            res = json.loads(res_str)
            if res.get("success"):
                print(f"\n🔥 找到了！{res.get('msg')}，已點擊！")
                return True
        except:
            pass
        await asyncio.sleep(0.1)
    return False

async def try_find_and_order(tab, tickets_added_flag, timeout=5):
    """
    [選位頁] V27 結構透視版
    修正:
    1. 針對使用者提供的 DOM 路徑，改用「按鈕層級定位」法。
    2. 放棄依賴 v-expansion-panel class，改抓 button 的父層結構。
    3. 完整支援「熱賣中」判定與所有選位策略。
    """
    try: count = int(WANTED_TICKET_COUNT)
    except: count = 1
    
    js_added_flag = "true" if tickets_added_flag else "false"
    select_mode = str(AREA_AUTO_SELECT_MODE).strip()

    js = f"""
    (function() {{
        let rawWanted = '{WANTED_AREA_KEYWORD}';
        let wanted = rawWanted.replace(/[$,\\s]/g, '');
        let count = {count};
        let added = {js_added_flag};
        let mode = '{select_mode}';
        let output = {{ status: "NONE" }};

        try {{
            // --- 0. 失敗救援 ---
            let errorDialog = document.querySelector('.v-dialog__content--active');
            if (errorDialog) {{
                let btns = errorDialog.querySelectorAll('button');
                for (let b of btns) {{
                    let txt = b.innerText.trim();
                    if (txt.includes('我知道了') || txt.includes('確定') || txt.includes('OK')) {{
                        b.click();
                        return JSON.stringify({{ status: "BOOKING_FAILED" }});
                    }}
                }}
            }}

            // --- 1. 輔助函式 ---
            function safeClick(btn) {{
                btn.click();
                btn.dispatchEvent(new Event('change', {{bubbles:true}}));
                btn.dispatchEvent(new Event('input', {{bubbles:true}}));
            }}

            function isPlusBtn(btn) {{
                let txt = btn.innerText.trim();
                if (txt === '+' || txt === '＋') return true;
                if (btn.querySelector('i') || btn.querySelector('svg') || btn.classList.contains('v-btn--icon')) {{
                    // 檢查是否為父容器內的最後一個按鈕
                    let p = btn.parentElement;
                    if (p) {{
                        let siblings = p.querySelectorAll('button');
                        if (siblings.length >= 2 && siblings[siblings.length-1] === btn) return true;
                    }}
                }}
                return false;
            }}

            // 檢查庫存 (熱賣中 = 999)
            function checkAvailability(container) {{
                let text = container.innerText || "";
                if (text.includes('售完') || text.includes('Soldout') || text.includes('暫無')) return 0;
                
                let smalls = container.querySelectorAll('small');
                for (let s of smalls) {{
                    let match = s.innerText.match(/剩餘\\s*(\\d+)/);
                    if (match) return parseInt(match[1]);
                }}
                let match = text.match(/剩餘\\s*(\\d+)/);
                if (match) return parseInt(match[1]);

                if (text.includes('熱賣中') || text.includes('銷售中') || text.includes('熱銷')) return 999; 
                return 999; 
            }}

            if (!added) {{
                // A. [收集] 根據使用者提供的結構，尋找關鍵字所在的「按鈕」
                // 路徑特徵：... > button > div.row > div.col (文字在這裡)
                
                // 我們直接搜尋最底層的 div 或 span
                let allElements = document.querySelectorAll('div, span'); 
                let targetContainers = [];
                let seen = new Set();

                for (let el of allElements) {{
                    // 只檢查底層文字
                    if (el.children.length === 0 && el.innerText) {{
                        let txt = el.innerText.replace(/[$,\\s]/g, '');
                        
                        if (txt === wanted || (wanted.length > 2 && txt.includes(wanted))) {{
                            
                            // 往上找，目標是找到包裹這個文字的 <button>
                            let current = el;
                            let headerBtn = null;
                            
                            for(let i=0; i<4; i++) {{ // 往上找4層夠了
                                if(!current.parentElement) break;
                                current = current.parentElement;
                                if (current.tagName === 'BUTTON') {{
                                    headerBtn = current;
                                    break;
                                }}
                            }}

                            // 決定 Scope (容器)
                            let scope = null;
                            if (headerBtn) {{
                                // 如果文字在按鈕裡，這個按鈕就是標題
                                // 我們需要的 Scope 是這個按鈕的「爺爺」或「曾爺爺」，因為要包含隱藏的內容區
                                // 嘗試往上抓 2 層
                                if (headerBtn.parentElement && headerBtn.parentElement.parentElement) {{
                                    scope = headerBtn.parentElement.parentElement;
                                }}
                            }} else {{
                                // 如果文字不在按鈕裡 (可能已經展開，或結構不同)
                                // 使用舊邏輯：抓 Row 或 Panel
                                scope = el.closest('.v-expansion-panel') || el.closest('.row');
                            }}

                            if (scope && !seen.has(scope)) {{
                                // 把 headerBtn 綁定在 scope 上，方便後續點擊
                                scope._headerBtn = headerBtn; 
                                targetContainers.push(scope);
                                seen.add(scope);
                            }}
                        }}
                    }}
                }}

                // B. [排序]
                if (targetContainers.length > 1) {{
                    if (mode === '由下而上' || mode === 'BOTTOM_UP') targetContainers.reverse();
                    else if (mode === '隨機' || mode === 'RANDOM') targetContainers.sort(() => Math.random() - 0.5);
                    else if (mode === '關鍵字優先' || mode === 'KEYWORD_PRIORITY') {{
                        targetContainers.sort((a, b) => {{
                            let txtA = (a.innerText || "").replace(/[$,\\s]/g, '');
                            let txtB = (b.innerText || "").replace(/[$,\\s]/g, '');
                            return txtA.length - txtB.length;
                        }});
                    }}
                }}

                // C. [執行]
                let anyStockFound = false;

                for (let container of targetContainers) {{
                    // 1. 檢查可用性
                    let stock = checkAvailability(container);
                    if (stock === 0) continue; 
                    anyStockFound = true;

                    // 2. 找加號按鈕 (在 Scope 裡面找)
                    let btns = container.querySelectorAll('button');
                    let targetPlus = null;
                    
                    for (let btn of btns) {{
                        // 排除掉標題按鈕本身 (因為標題按鈕裡面通常沒有加號)
                        if (btn === container._headerBtn) continue;
                        
                        if (!btn.disabled && btn.offsetParent !== null && btn.offsetWidth > 0 && isPlusBtn(btn)) {{
                            targetPlus = btn;
                            break;
                        }}
                    }}

                    // 3. 動作
                    if (targetPlus) {{
                        // 找到加號 -> 猛點
                        for(let i=0; i<count; i++) {{
                            safeClick(targetPlus);
                        }}
                        output.status = "ADDED";
                        return JSON.stringify(output);
                    }} else {{
                        // 沒找到加號 -> 點擊標題展開
                        // 優先使用我們剛才找到的 headerBtn
                        let clickTarget = container._headerBtn;
                        
                        // 如果沒抓到 headerBtn，試著找 header class
                        if (!clickTarget) {{
                             clickTarget = container.querySelector('.v-expansion-panel-header');
                        }}
                        
                        // 防呆: 檢查是否等待動畫中
                        // 如果容器有 active class 但沒找到按鈕，可能是動畫延遲
                        let htmlStr = container.outerHTML;
                        if (htmlStr.includes('v-item--active') || htmlStr.includes('v-expansion-panel--active')) {{
                             return JSON.stringify({{ status: "WAITING_ANIMATION" }});
                        }}
                        
                        if (clickTarget) {{
                            clickTarget.click();
                            output.status = "EXPANDING"; 
                            return JSON.stringify(output);
                        }}
                    }}
                }}

                if (targetContainers.length > 0 && !anyStockFound) {{
                    return JSON.stringify({{ status: "ALL_SOLD_OUT" }});
                }}
            }}

            // --- 3. 提交 ---
            if (added) {{
                let nextBtn = null;
                let footer = document.querySelector('.order-footer');
                if (footer) {{
                    let btns = footer.querySelectorAll('button');
                    for (let b of btns) if (b.innerText.includes('下一步')) nextBtn = b;
                }}
                if (!nextBtn) {{
                    let all = document.querySelectorAll('button');
                    for (let b of all) if (b.innerText.includes('下一步')) nextBtn = b;
                }}

                if (nextBtn) {{
                    if (nextBtn.disabled) nextBtn.disabled = false;
                    nextBtn.click();
                    output.status = "SUBMITTED";
                }}
            }}

        }} catch(e) {{
            output.status = "ERROR";
            output.msg = e.toString();
        }}
        return JSON.stringify(output);
    }})()
    """

    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            res_str = await tab.evaluate(js)
            res = json.loads(res_str)
            status = res.get("status")

            if status == "BOOKING_FAILED":
                print("\n❌ 搶票失敗，重刷！")
                return "RELOAD"

            if status == "ALL_SOLD_OUT": return "RELOAD"
            
            if status == "EXPANDING":
                pass 
            elif status == "WAITING_ANIMATION":
                await asyncio.sleep(0.1)
            elif status == "ADDED":
                print(f"\n🎫 已加票數！(策略: {select_mode})")
                return "ADDED"
            elif status == "SUBMITTED":
                print("\n🚀 點擊下一步！")
                return "SUBMITTED"
            
            await asyncio.sleep(0.1)
        except:
            await asyncio.sleep(0.1)
    return "RELOAD"
# ----------------------------------------------------
# 主程式
# ----------------------------------------------------

async def run_ticketplus_setup():
    print("🚀 TicketPlus (V8+ 持續掃描版) 啟動...")
    browser = await uc.start(headless=False, browser_args=["--start-maximized", "--disable-notifications"])
    tab = await browser.get("https://ticketplus.com.tw/")
    return browser, tab

async def main():
    browser, tab = await run_ticketplus_setup()
    watcher = TimeWatcher(TARGET_TIME, TIME_WATCH_URL)
    has_waited = not ENABLE_TIME_WATCHER
    
    tickets_added = False
    order_submitted = False

    while True:
        try:
            await check_pause()
            if not browser or not tab: break

            current_url = await tab.evaluate("window.location.href")

            # 1. 成功頁 -> 停住 (支援 confirmSeat 和 confirm)
            if "/confirmSeat" in current_url or "/confirm" in current_url:
                print("\n🎉 搶票成功！請付款。")
                while True: await asyncio.sleep(600)

            # 2. 避免重複送出
            if order_submitted and "/order" in current_url:
                print("⏳ 訂單處理中...", end='\r')
                await asyncio.sleep(1)
                continue

            # 3. 時間鎖
            if not has_waited:
                if "/activity/" in current_url:
                    await watcher.wait_for_open_async()
                    has_waited = True
                    await tab.reload()
                else:
                    await asyncio.sleep(1)
                    continue

            # === 核心邏輯 ===
            
            # [活動頁]
            if "/activity/" in current_url:
                tickets_added = False
                order_submitted = False
                success = await try_find_and_click_activity(tab, timeout=5)
                if success:
                    await asyncio.sleep(1)
                else:
                    print("\n🔄 5秒內未偵測到按鈕，刷新重試！")
                    await tab.reload()

            # [選位頁]
            elif "/order" in current_url:
                if not order_submitted:
                    status = await try_find_and_order(tab, tickets_added, timeout=5)
                    
                    if status == "ADDED":
                        tickets_added = True
                    elif status == "SUBMITTED":
                        order_submitted = True
                    elif status == "RELOAD":
                        print("\n🔄 5秒內無票或已售完，刷新搶釋出！")
                        await tab.reload()
                        tickets_added = False 

            else:
                await asyncio.sleep(1)

        except Exception as e:
            await asyncio.sleep(1)

if __name__ == "__main__":
    uc.loop().run_until_complete(main())