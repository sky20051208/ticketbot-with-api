"""LINE Rich Menu 設定 — 一次性帳號設定，讓客人底部有常駐選單可以點，不用手打字。

用法（專案根目錄）：python -m LineBot.setup_richmenu

用 Pillow 產生選單圖（2500x843，左右各半：搶票登記 / 真人專員），呼叫 LINE Rich Menu
API 建立 + 上傳圖片 + 設成全體客人預設選單。按鈕動作是 type=message，點擊會送出跟
客人手打「搶票」／「專員」完全一樣的文字訊息，直接吃 LineBotWorker 現有的關鍵字比對
（見 index.js 的 AGENT_KEYWORDS / TICKET_KEYWORDS），不用改 webhook 邏輯。
"""
import io
import sys

import requests
from PIL import Image, ImageDraw, ImageFont

import config

RICHMENU_API = "https://api.line.me/v2/bot/richmenu"
UPLOAD_API = "https://api-data.line.me/v2/bot/richmenu"

WIDTH, HEIGHT = 2500, 843
FONT_PATH = r"C:\Windows\Fonts\msjhbd.ttc"  # 微軟正黑體 Bold


def _headers(content_type: str | None = None) -> dict:
    h = {"Authorization": f"Bearer {config.LINE_CHANNEL_ACCESS_TOKEN}"}
    if content_type:
        h["Content-Type"] = content_type
    return h


def _build_image() -> bytes:
    img = Image.new("RGB", (WIDTH, HEIGHT), "#ffffff")
    draw = ImageDraw.Draw(img)

    # 左半：搶票登記（藍）／右半：真人專員（橘）
    draw.rectangle([0, 0, WIDTH // 2, HEIGHT], fill="#2e7dd7")
    draw.rectangle([WIDTH // 2, 0, WIDTH, HEIGHT], fill="#e8823c")

    title_font = ImageFont.truetype(FONT_PATH, 100)
    sub_font = ImageFont.truetype(FONT_PATH, 48)

    def center_text(cx: float, cy: float, text: str, font, fill="white"):
        bbox = draw.textbbox((0, 0), text, font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((cx - w / 2 - bbox[0], cy - h / 2 - bbox[1]), text, font=font, fill=fill)

    center_text(WIDTH / 4, HEIGHT / 2 - 60, "搶票登記", title_font)
    center_text(WIDTH / 4, HEIGHT / 2 + 90, "輸入姓名 + 演唱會", sub_font)
    center_text(WIDTH * 3 / 4, HEIGHT / 2 - 60, "真人專員", title_font)
    center_text(WIDTH * 3 / 4, HEIGHT / 2 + 90, "轉真人客服", sub_font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not config.LINE_CHANNEL_ACCESS_TOKEN:
        print("[ERROR] config.py 缺 LINE_CHANNEL_ACCESS_TOKEN")
        sys.exit(1)

    body = {
        "size": {"width": WIDTH, "height": HEIGHT},
        "selected": True,
        "name": "搶票客服選單",
        "chatBarText": "點我開啟選單",
        "areas": [
            {
                "bounds": {"x": 0, "y": 0, "width": WIDTH // 2, "height": HEIGHT},
                "action": {"type": "message", "label": "搶票登記", "text": "搶票"},
            },
            {
                "bounds": {"x": WIDTH // 2, "y": 0, "width": WIDTH // 2, "height": HEIGHT},
                "action": {"type": "message", "label": "真人專員", "text": "專員"},
            },
        ],
    }

    print("[SETUP] 建立 rich menu...")
    res = requests.post(RICHMENU_API, headers=_headers("application/json"), json=body)
    if res.status_code != 200:
        print(f"[ERROR] 建立失敗 HTTP {res.status_code}: {res.text[:300]}")
        sys.exit(1)
    rich_menu_id = res.json()["richMenuId"]
    print(f"[SETUP] richMenuId = {rich_menu_id}")

    print("[SETUP] 產生圖片並上傳...")
    image_bytes = _build_image()
    res2 = requests.post(
        f"{UPLOAD_API}/{rich_menu_id}/content",
        headers=_headers("image/png"),
        data=image_bytes,
    )
    if res2.status_code != 200:
        print(f"[ERROR] 上傳圖片失敗 HTTP {res2.status_code}: {res2.text[:300]}")
        sys.exit(1)
    print("[SETUP] 圖片上傳完成")

    print("[SETUP] 設為全體客人預設選單...")
    res3 = requests.post(f"https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}",
                         headers=_headers())
    if res3.status_code != 200:
        print(f"[ERROR] 設定預設選單失敗 HTTP {res3.status_code}: {res3.text[:300]}")
        sys.exit(1)

    print("[SETUP] 完成！客人的 LINE 聊天視窗底部現在會看到選單，點擊直接送出「搶票」／「專員」。")


if __name__ == "__main__":
    main()
