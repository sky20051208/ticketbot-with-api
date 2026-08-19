"""跨次執行的快取：把「這次學到、下次能少送一發請求」的東西留在 profiles/acc_N/。

**為什麼值得做**：2026-08-16 在美東 VPS 實測，拓元每一發動態請求的固定成本是 ~300ms，
而且**跟回應大小無關**（只回 157 bytes 的 search-suggest API 要 335ms、542KB 的活動列表
只要 399ms、傳輸時間量到 0.07ms）。所以「換更小的 endpoint」省不到東西，
**唯一有效的方向是減少請求發數** —— 這支檔案就是為此存在。

存兩樣：
  - `area_url`：`/ticket/area/{slug}/{game_id}`，冷啟動直接從 AREA state 起跑，省掉 GAME 那發
  - `forms`：ticket_url → 票區代碼 + hidden 欄位，給 submit 的 fast path 用（配合
    `session.csrf_from_cookie` 就能整發 form GET 都不用送）

**不需要額外的失效判斷**：快取只在 slug + date_keyword 都對得上時採用；內容過期的話
FSM 自己會失敗 → fallback 回 GAME 重新拿一份（runner 既有行為），下次存檔就更新了。
"""
import json
import os


def cache_path(base_dir: str, acc_id: int) -> str:
    return os.path.join(base_dir, "profiles", f"acc_{acc_id}", "tixcraft_cache.json")


_EMPTY = {"area_url": None, "ticket_url": None, "forms": {}}


def load(path: str, slug: str, date_keyword: str, area_keyword: str) -> dict:
    """回 `{"area_url", "ticket_url", "forms"}`；沒檔案 / 不是同一個活動 → 空的。

    **`area_keyword` 不一致時會丟掉 `ticket_url`**（但保留 `area_url` 和 `forms`）：
    那個欄位是給「T-0 直接盲送上次那一區」用的，而使用者改關鍵字就代表想買的區變了 ——
    照舊盲送等於無視新設定買錯區。`forms` 不受影響，它是照 ticket_url 各自對應的表單
    結構，走到哪一區就用哪一份，不會選錯。

    讀檔失敗一律當作沒有快取：這只是加速用的東西，**不能因為它壞掉就讓 bot 起不來**。
    """
    if not os.path.exists(path):
        return dict(_EMPTY)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[CACHE] 快取讀取失敗，當作沒有（{type(e).__name__}）")
        return dict(_EMPTY)

    if data.get("slug") != slug or data.get("date_keyword", "") != date_keyword:
        print(f"[CACHE] 快取屬於別的活動（{data.get('slug')} / {data.get('date_keyword', '')}），不採用")
        return dict(_EMPTY)

    ticket_url = data.get("ticket_url")
    if ticket_url and data.get("area_keyword", "") != area_keyword:
        print(f"[CACHE] AREA_KEYWORD 已改（{data.get('area_keyword', '')!r} → {area_keyword!r}），"
              f"不沿用上次的票區，改走 AREA 重新選")
        ticket_url = None
    return {"area_url": data.get("area_url"), "ticket_url": ticket_url,
            "forms": data.get("forms") or {}}


def save(path: str, slug: str, date_keyword: str, area_keyword: str,
         area_url: str | None, ticket_url: str | None, forms: dict) -> None:
    """存檔。**失敗只印訊息不 raise** —— 呼叫點在搶完票之後的 finally，
    不能因為寫不了一個加速用的檔案，就蓋掉已經到手的結果或中斷結帳流程。"""
    if not area_url and not forms:
        return
    payload = {"slug": slug, "date_keyword": date_keyword, "area_keyword": area_keyword,
               "area_url": area_url, "ticket_url": ticket_url, "forms": forms}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print(f"[CACHE] 已存：場次 URL + {len(forms)} 個票區表單"
              f"{'（下次 T-0 可直接對該票區送單）' if ticket_url else ''}")
    except OSError as e:
        print(f"[CACHE] 存檔失敗（不影響本次結果）: {e}")
