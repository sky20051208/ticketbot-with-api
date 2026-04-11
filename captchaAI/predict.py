import ddddocr
import base64
import time
from typing import Optional
from config import Selector

# 1. 初始化 ddddocr
# Maxbot 設定：show_ad=False, beta=True (這是關鍵)
try:
    OCR_SOLVER = ddddocr.DdddOcr(show_ad=False, beta=True)
    print("✔ ddddocr 引擎已載入 (Maxbot 原生設定: Beta Mode)")
except Exception as e:
    OCR_SOLVER = None
    print(f"❌ ddddocr 載入失敗: {e}")

# 2. 辨識核心 (移除所有影像處理)
def recognize_captcha(image_bytes: bytes) -> str:
    if OCR_SOLVER is None:
        return ""
    try:
        # Maxbot 直接將原始 bytes 丟進去，不做 resize 或 grayscale
        result = OCR_SOLVER.classification(image_bytes)
        return result.lower()
    except Exception as e:
        print(f"❌ 辨識錯誤: {e}")
        return ""

# 3. 取圖函式 (簡化版 JS)
# 參考 Maxbot 的 canvas 邏輯，不強制填白底，保留原始透明度(如果有的話)
async def get_captcha_base64_nodriver(tab) -> Optional[bytes]:
    captcha_id = Selector.CAPTCHA_IMAGE[1] 
    
    # Maxbot 風格的 JS：簡單直接，只負責抓圖
    js_script = f"""
    (async function() {{
        var img = document.getElementById('{captcha_id}');
        if (!img) return null;

        // 基本等待載入 (nodriver 必備防護)
        if (!img.complete || img.naturalWidth === 0) {{
            await new Promise(r => img.onload = r);
        }}

        var canvas = document.createElement('canvas');
        var context = canvas.getContext('2d');
        canvas.width = img.naturalWidth;
        canvas.height = img.naturalWidth; // 注意：Maxbot 原始碼這裡有時會直接用 width, 或許是 typo，我們用正確的 height
        canvas.height = img.naturalHeight; 
        
        // Maxbot 沒有 fillStyle='white'，直接 drawImage
        context.drawImage(img, 0, 0);
        
        var dataURL = canvas.toDataURL('image/png');
        return dataURL.split(',')[1];
    }})()
    """
    
    try:
        # await_promise=True 確保 JS 執行完畢
        base64_str = await tab.evaluate(js_script, await_promise=True)
        if base64_str:
            return base64.b64decode(base64_str)
    except Exception:
        pass
    
    return None

# 4. 主入口
async def solve_captcha_nodriver(tab) -> str:
    image_data = await get_captcha_base64_nodriver(tab)
    
    if not image_data:
        return "" 
    
    captcha_text = recognize_captcha(image_data)
    
    # Maxbot 有做基本的去空白處理
    if captcha_text:
        captcha_text = captcha_text.strip().replace(" ", "")
    
    return captcha_text