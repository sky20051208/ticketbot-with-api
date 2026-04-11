# main.py (正式版 V17.1 - 支援跨分頁追蹤)

import sys
import os
import asyncio
import nodriver as uc

# 路徑設定 (保持不變)
if getattr(sys, 'frozen', False):
    application_path = os.path.dirname(sys.executable)
    sys.path.append(application_path)
else:
    application_path = os.path.dirname(os.path.abspath(__file__))
    sys.path.append(application_path)

# 匯入設定檔與拓元模組
from config import PLATFORM, TIME_WATCH_URL
from bot import run_initial_setup, handle_game_page, handle_area_page, handle_ticket_page, handle_verify_page, check_pause

# [匯入] KKTIX 模組
try:
    from kktix import kkbot as kktix_bot
except ImportError:
    kktix_bot = None

# [匯入] TicketPlus (遠大售票) 模組
try:
    from ticketplus import ticketplusbot
except ImportError:
    ticketplusbot = None

async def main():
    print(f"--- 搶票輔助機器人 V17.1 (目前平台: {PLATFORM}) ---")
    
    # === 根據平台選擇進入點 ===
    
    # 1. KKTIX 模式
    if PLATFORM == "KKTIX":
        if kktix_bot:
            await kktix_bot.main()
        else:
            print("❌ 找不到 kktix/kkbot.py 檔案，請檢查路徑。")
        return

    # 2. TicketPlus 遠大模式
    elif PLATFORM == "TICKETPLUS":
        if ticketplusbot:
            print("🚀 啟動遠大售票 (TicketPlus) 模組...")
            await ticketplusbot.main()
        else:
            print("❌ 找不到 ticketplus/ticketplusbot.py 檔案，請檢查路徑。")
        return

    # 3. 預設模式: 拓元 (Tixcraft)
    try:
        browser, tab = await run_initial_setup()
    except Exception as e:
        print(f"❌ 啟動失敗: {e}")
        input("按 Enter 鍵退出...") 
        return
    
    if not tab: return

    print("\n🤖 拓元模式已接管... (關閉視窗可結束)")
    print("🛡️ CDP 全域監聽器運作中，自動防禦彈窗。")
    
    fail_count = 0
    MAX_FAIL_COUNT = 20

    # 【新增】記錄啟動時的初始分頁數量
    current_tab_count = len(browser.tabs)

    last_url = ""

    while True:
        try:
            await check_pause()

            if not browser: 
                print("🛑 瀏覽器物件遺失。")
                break
            
            # ==========================================
            # 🎯 戰術 A：分頁屍體檢測與重新接管機制
            # ==========================================
            try:
                # 試著戳一下目前的分頁，看它還是不是活著的
                current_url = await tab.evaluate("window.location.href")
            except Exception:
                # 如果報錯了，代表你在暫停期間把舊分頁關掉了！
                if len(browser.tabs) > 0:
                    print("\n🔄 [系統] 偵測到原分頁已關閉，切換至剩餘分頁...")
                    
                    # 抓取你留下來的那個唯一分頁（拓元）
                    tab = browser.tabs[0]
                    await tab.bring_to_front()
                    
                    # ⚠️ 關鍵：重新幫新分頁掛上 Alert 防護罩，不然遇到彈窗會死機
                    async def handle_dialog_new_tab(event: uc.cdp.page.JavascriptDialogOpening):
                        try:
                            await tab.send(uc.cdp.page.handle_java_script_dialog(accept=True))
                            print("❌ [新分頁] 已自動攔截並關閉系統彈窗")
                        except:
                            pass
                    
                    await tab.send(uc.cdp.page.enable())
                    tab.add_handler(uc.cdp.page.JavascriptDialogOpening, handle_dialog_new_tab)
                    
                    # 重新取得新分頁的網址，重置追蹤器
                    current_url = await tab.evaluate("window.location.href")
                    last_url = "" 
                    print(f"✅ [系統] 成功接管新分頁，恢復自動化流程！")
                else:
                    print("🛑 所有分頁都已關閉，結束程式。")
                    break
            # ==========================================

            fail_count = 0 
            
            # 📡 全域網址跳轉監控 Log
            if current_url != last_url:
                if last_url != "":
                    try:
                        short_current = current_url.split(".com")[1]
                    except:
                        short_current = current_url
                    print(f"🔗 [網頁跳轉] 進入 -> {short_current}")
                last_url = current_url
            
            # === 拓元狀態機判斷 ===
            
            if "/ticket/checkout" in current_url:
                print(f"\n🎉🎉🎉 搶票成功！網址: {current_url}")
                print("⏳ 程式保持開啟 1 小時，請盡快付款...")
                await asyncio.sleep(3600)
                break 

            elif "/ticket/order" in current_url:
                print("⏳ [轉圈圈] 訂單處理中... (監聽器待命中)", end='\r')
                await asyncio.sleep(0.5)
                continue

            elif "/ticket/verify" in current_url:
                await handle_verify_page(tab)

            elif "/ticket/ticket" in current_url:
                await handle_ticket_page(tab)
            
            elif "/ticket/area" in current_url:
                await handle_area_page(tab)
            
            elif "/activity/game" in current_url or "/activity/detail" in current_url:
                await handle_game_page(tab)
            
            else:
                await asyncio.sleep(1)

        except Exception as e:
            err_msg = str(e).lower()
            if "connection" in err_msg or "closed" in err_msg:
                fail_count += 1
                if fail_count >= MAX_FAIL_COUNT:
                    print("\n🛑 視窗已關閉，程式結束。")
                    break
            await asyncio.sleep(0.5)

    print("\n程式結束。")

if __name__ == "__main__":
    # Python 3.10+ 在 Windows 預設使用 ProactorEventLoop，這才支援 subprocess (nodriver 需要)
    uc.loop().run_until_complete(main())