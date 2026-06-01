"""HTML / JS 解析（純 regex，不打網路）。submit / game 步驟共用。"""
import re


def parse_ticket_form(html: str) -> dict:
    payload = {}
    for m in re.finditer(r'<input[^>]+type=["\']hidden["\'][^>]*>', html):
        tag = m.group(0)
        name_m = re.search(r'name=["\']([^"\']+)["\']', tag)
        val_m = re.search(r'value=["\']([^"\']*)["\']', tag)
        if name_m:
            name = name_m.group(1)
            value = val_m.group(1) if val_m else ""
            if "_csrf" in name or "TicketForm" in name:
                payload[name] = value
    return payload


def find_ticket_codes(payload: dict) -> list[str]:
    codes = []
    for k in payload:
        m = re.search(r'TicketForm\[(?:ticketPrice|priceSize)\]\[(\w+)\]', k)
        if m and m.group(1) not in codes:
            codes.append(m.group(1))
    return codes


def parse_game_area_url(html: str, date_keyword: str = "") -> str | None:
    """從 game page HTML 抽 area URL；找不到回 None。"""
    rows = re.findall(
        r'<tr[^>]*class="gridc[^"]*"[^>]*>(.*?)</tr>',
        html, re.DOTALL
    )
    for row_html in rows:
        href_m = re.search(r'data-href=["\']([^"\']+)["\']', row_html)
        if not href_m:
            continue
        if date_keyword and date_keyword not in row_html:
            continue
        return href_m.group(1)

    fallback = re.search(r'data-href=["\']([^"\']*ticket/area/[^"\']+)["\']', html)
    if fallback:
        return fallback.group(1)
    return None
