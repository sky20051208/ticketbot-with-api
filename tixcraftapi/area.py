"""Step 3: 區域選擇 — 純選位策略。

頁面解析在 parsing.parse_area_availables；被 redirect（verify 頁等）時直接回傳落點 URL
交給 runner classify 派發，本檔不認識 verify / captcha，也不讀 config（參數由 runner 注入）。
"""
import random

from curl_cffi import requests as cf_requests

from tixcraftapi import BASE
from tixcraftapi.errors import raise_if_blocked
from tixcraftapi.parsing import parse_area_availables


def select_area(session: cf_requests.Session, area_url: str, headers: dict,
                area_keyword: str = "", exclude_keyword: str = "",
                strategy: str = "") -> str | None:
    """從 area 頁面挑區。回傳值三態：
      - ticket_url（選區成功 → classify 成 TICKET）
      - redirect 落點 URL（被導去 verify / 其他頁 → classify 決定下一步）
      - None（沒票 / 失敗 → runner fallback GAME）
    """
    res = session.get(area_url, headers={**headers, "Referer": area_url},
                      allow_redirects=False, timeout=10)

    # 被 redirect（驗證頁、活動頁…）→ 不在這裡判斷語意，交回 FSM
    if res.status_code in (301, 302):
        loc = res.headers.get("Location", "")
        full_loc = loc if loc.startswith("http") else BASE + loc
        print(f"[AREA] 被導向: {full_loc}（交回 FSM 分類）")
        return full_loc

    raise_if_blocked(res, "AREA")
    if res.status_code != 200:
        print(f"[AREA] HTTP {res.status_code}")
        return None

    available = parse_area_availables(res.text)
    if available is None:
        print("[AREA] 無可購買區域（全部售完或尚未開賣）")
        return None
    if not available:
        print("[AREA] 沒有可購買的區域")
        return None

    # 排除關鍵字過濾
    if exclude_keyword:
        exclude_list = [kw.strip() for kw in exclude_keyword.split(";") if kw.strip()]
        before = len(available)
        available = [
            (aid, text, url) for aid, text, url in available
            if not any(ex in text for ex in exclude_list)
        ]
        if len(available) < before:
            print(f"[AREA] 排除 {before - len(available)} 個區域 (排除詞: {', '.join(exclude_list[:3])}...)")

    if not available:
        print("[AREA] 排除後沒有可購買的區域")
        return None

    # 印 summary（過去會列出所有 54 個區域，hot path 上拖時間 ~10-20ms，砍掉）
    print(f"[AREA] 找到 {len(available)} 個有票區域")
    print(f"[AREA] 策略: {strategy} | 關鍵字: {area_keyword}")

    if strategy == "關鍵字優先" and area_keyword:
        # 關鍵字語法：
        #   ;  分隔 = OR 優先順序（依序嘗試，第一個命中就用）
        #   +  分隔 = AND 同時必須含（一個 keyword 內可以含多個子條件）
        #
        # 例 "G05+6980;G05" → 先找同時含 G05 跟 6980 的區，沒則 fallback G05 任意
        keywords = [kw.strip() for kw in area_keyword.split(";") if kw.strip()]
        selected = None
        for idx, kw in enumerate(keywords):
            sub_keywords = [s.strip() for s in kw.split("+") if s.strip()]
            if not sub_keywords:
                continue
            filtered = [(aid, text, url) for aid, text, url in available
                         if all(sk in text for sk in sub_keywords)]
            if filtered:
                selected = filtered[0]
                kw_desc = " AND ".join(sub_keywords) if len(sub_keywords) > 1 else sub_keywords[0]
                if len(keywords) > 1:
                    print(f"[AREA] 關鍵字 '{kw_desc}' 命中（優先序 {idx + 1}/{len(keywords)}）")
                elif len(sub_keywords) > 1:
                    print(f"[AREA] 關鍵字 AND 條件 '{kw_desc}' 命中")
                break
        if selected is None:
            print(f"[AREA] 關鍵字 {keywords} 全部無匹配，fallback 選第一個")
            selected = available[0]
    elif strategy == "由下而上":
        selected = available[-1]
    elif strategy == "隨機":
        selected = random.choice(available)
    else:
        selected = available[0]

    aid, text, ticket_url = selected
    print(f"[AREA] 選中: {text} -> {ticket_url}")
    return ticket_url
