"""kktix_api.parsing 純函式測試（不連網、隨時可跑）。

分兩類：
- select_ticket / detect_challenge / extract_csrf_token：合成資料即可，覆蓋所有分支
- parse_registration_ticket_units：需要真實報名頁 HTML fixture
  （tests/fixtures/kktix/reg_com.html，由 scratchpad/cap_browser.py 擷取後放入）；
  沒有 fixture 時該類自動 skip
"""
import pytest

from kktix_api import parsing
from conftest import load_fixture


# --------- detect_challenge ---------

class TestDetectChallenge:
    def test_real_cloudflare_page(self):
        html = load_fixture("kktix", "challenge_page.html")
        assert html, "缺 challenge_page.html fixture"
        assert parsing.detect_challenge(html) is True

    def test_normal_page_not_challenge(self):
        assert parsing.detect_challenge("<html><body>報名頁</body></html>") is False

    def test_empty(self):
        assert parsing.detect_challenge("") is False

    @pytest.mark.parametrize("marker", [
        "Just a moment",
        "Enable JavaScript and cookies to continue",
        "正在執行安全驗證",
        "window._cf_chl_opt",
    ])
    def test_each_marker(self, marker):
        assert parsing.detect_challenge(f"<html>{marker}</html>") is True


# --------- extract_csrf_token ---------

class TestExtractCsrf:
    def test_found(self):
        html = '<meta name="csrf-token" content="abc123==">'
        assert parsing.extract_csrf_token(html) == "abc123=="

    def test_missing(self):
        assert parsing.extract_csrf_token("<html></html>") == ""


# --------- select_ticket（純選票邏輯，覆蓋所有分支）---------

def _unit(tid, name="A區", price="TWD$1,000", status="available", selectable=True, label=""):
    return {"ticket_id": tid, "name": name, "label": label, "price": price,
            "status": status, "selectable": selectable}


class TestSelectTicket:
    def test_none_when_all_sold_out(self):
        units = [_unit("1", status="sold_out"), _unit("2", status="sold_out")]
        assert parsing.select_ticket(units, keyword="", exclude="", mode="由上而下", amount=1) is None

    def test_none_when_not_selectable(self):
        units = [_unit("1", selectable=False)]
        assert parsing.select_ticket(units, keyword="", exclude="", mode="由上而下", amount=1) is None

    def test_basic_pick_first_and_amount(self):
        units = [_unit("1", name="A區"), _unit("2", name="B區")]
        chosen = parsing.select_ticket(units, keyword="", exclude="", mode="由上而下", amount=2)
        assert chosen["ticket_id"] == "1"
        assert chosen["amount"] == 2

    def test_amount_clamped_to_one(self):
        units = [_unit("1")]
        chosen = parsing.select_ticket(units, keyword="", exclude="", mode="由上而下", amount=0)
        assert chosen["amount"] == 1

    def test_bottom_up_mode_reverses(self):
        units = [_unit("1", name="A區"), _unit("2", name="B區")]
        chosen = parsing.select_ticket(units, keyword="", exclude="", mode="由下而上", amount=1)
        assert chosen["ticket_id"] == "2"

    def test_keyword_match(self):
        units = [_unit("1", name="搖滾區"), _unit("2", name="看台區")]
        chosen = parsing.select_ticket(units, keyword="看台", exclude="", mode="由上而下", amount=1)
        assert chosen["ticket_id"] == "2"

    def test_keyword_priority_falls_back_when_no_match(self):
        units = [_unit("1", name="搖滾區"), _unit("2", name="看台區")]
        chosen = parsing.select_ticket(units, keyword="貴賓", exclude="", mode="關鍵字優先", amount=1)
        assert chosen["ticket_id"] == "1"  # 找不到關鍵字 → 退回第一個有票的

    def test_non_priority_mode_no_match_returns_none(self):
        units = [_unit("1", name="搖滾區")]
        chosen = parsing.select_ticket(units, keyword="貴賓", exclude="", mode="由上而下", amount=1)
        assert chosen is None  # 硬篩選，沒中就 None

    def test_exclude_filters(self):
        units = [_unit("1", name="身障席"), _unit("2", name="搖滾區")]
        chosen = parsing.select_ticket(units, keyword="", exclude="身障", mode="由上而下", amount=1)
        assert chosen["ticket_id"] == "2"

    def test_exclude_multiple_semicolon(self):
        units = [_unit("1", name="身障席"), _unit("2", name="輪椅席"), _unit("3", name="搖滾區")]
        chosen = parsing.select_ticket(units, keyword="", exclude="身障;輪椅", mode="由上而下", amount=1)
        assert chosen["ticket_id"] == "3"

    def test_exclude_removes_all_returns_none(self):
        units = [_unit("1", name="身障席")]
        chosen = parsing.select_ticket(units, keyword="", exclude="身障", mode="由上而下", amount=1)
        assert chosen is None


# --------- parse_registration_ticket_units（對真實報名頁）---------

class TestParseRegistrationUnits:
    """真實 fixture: 2026-07 擷取自 kktix.com/events/cb2818b8/registrations/new
    （y9abe2f0 活動的一個場次，register_status=IN_STOCK 有票）。

    ⚠️ 已證實的關鍵事實（bot 的 KKTIX 開賣偵測 bug 根因）：
    KKTIX 報名頁的票種是 **Angular 前端渲染**的。
      - raw HTML（bot 現行 page_fetch 拿到的）→ 0 個 ticket_ 區塊 → parser 回 []
      - 渲染後 DOM（Angular 跑完）→ 有 ticket_ 區塊，parser 能正確解出票種
    parser 本身沒錯，錯在 register.fetch_once 餵它 raw HTML 而非渲染後 DOM / register_info。
    這組測試把「parser 對真 DOM 正確、對 raw HTML 抓不到」鎖成回歸線。
    """
    @pytest.fixture
    def raw_html(self):
        html = load_fixture("kktix", "session_registrations_new_raw.html")
        if not html:
            pytest.skip("缺 session_registrations_new_raw.html fixture")
        return html

    @pytest.fixture
    def rendered_dom(self):
        html = load_fixture("kktix", "session_registrations_new_rendered.html")
        if not html:
            pytest.skip("缺 session_registrations_new_rendered.html fixture")
        return html

    def test_raw_html_has_no_units(self, raw_html):
        # raw HTML（Angular 未執行）→ 抓不到票種，這正是 bot 偵測不到開賣的原因
        assert parsing.parse_registration_ticket_units(raw_html) == []
        assert parsing.is_registration_open(raw_html) is False
        # 但 csrf 在 raw HTML 裡（下單需要）
        assert parsing.extract_csrf_token(raw_html) != ""

    def test_rendered_dom_extracts_units(self, rendered_dom):
        units = parsing.parse_registration_ticket_units(rendered_dom)
        by_id = {u["ticket_id"]: u for u in units}
        assert set(by_id) == {"1048265", "1048275"}
        assert by_id["1048265"]["name"] == "全票"
        assert by_id["1048265"]["price"] == "TWD$3,280"
        assert by_id["1048275"]["name"] == "身障席"
        for u in units:
            assert u["status"] == "available"
            assert u["selectable"] is True

    def test_rendered_dom_is_open(self, rendered_dom):
        assert parsing.is_registration_open(rendered_dom) is True

    def test_select_ticket_on_real_units(self, rendered_dom):
        units = parsing.parse_registration_ticket_units(rendered_dom)
        # 排除身障席 → 選到全票
        chosen = parsing.select_ticket(units, keyword="", exclude="身障",
                                       mode="由上而下", amount=2)
        assert chosen["ticket_id"] == "1048265"
        assert chosen["amount"] == 2


# --------- 純封包 API 版：base_info + register_info ---------

class TestApiParsers:
    """真實 fixture（cb2818b8 場次，IN_STOCK）。純封包路徑：base_info 給票名/票價，
    register_info 給票況，兩者 merge 後餵 select_ticket —— 完全不需渲染。"""

    @pytest.fixture
    def catalog(self):
        return parsing.parse_base_info(load_fixture("kktix", "base_info.json"))

    @pytest.fixture
    def reg_info(self):
        return parsing.parse_register_info(load_fixture("kktix", "session_register_info.json"))

    def test_base_info_catalog(self, catalog):
        by_id = {c["ticket_id"]: c for c in catalog}
        assert set(by_id) == {"1048265", "1048275"}
        assert by_id["1048265"]["name"] == "全票"
        assert by_id["1048265"]["price"] == "TWD$3,280"
        assert by_id["1048275"]["name"] == "身障席"
        assert by_id["1048275"]["price"] == "TWD$1,640"
        assert by_id["1048265"]["need_invitation_code"] is False

    def test_base_info_bad_json(self):
        assert parsing.parse_base_info("not json") == []

    def test_register_info_open_and_stock(self, reg_info):
        assert reg_info["register_status"] == "IN_STOCK"
        assert reg_info["open"] is True
        assert reg_info["in_stock_ids"] == {"1048265", "1048275"}
        assert reg_info["sections"]["94835"] == "NEARLY_SOLD_OUT"

    def test_register_info_bad_json(self):
        info = parsing.parse_register_info("nope")
        assert info["open"] is False
        assert info["in_stock_ids"] == set()

    def test_merge_marks_stock(self, catalog, reg_info):
        units = parsing.merge_availability(catalog, reg_info)
        assert all(u["status"] == "available" and u["selectable"] for u in units)

    def test_merge_sold_out_when_not_in_stock(self, catalog):
        reg = {"in_stock_ids": {"1048265"}}  # 只有全票有票
        units = parsing.merge_availability(catalog, reg)
        by_id = {u["ticket_id"]: u for u in units}
        assert by_id["1048265"]["status"] == "available"
        assert by_id["1048275"]["status"] == "sold_out"
        assert by_id["1048275"]["selectable"] is False

    def test_end_to_end_keyword_select(self, catalog, reg_info):
        # 純封包端到端：目錄 + 票況 → 關鍵字「全票」→ 選到 1048265
        units = parsing.merge_availability(catalog, reg_info)
        chosen = parsing.select_ticket(units, keyword="全票", exclude="",
                                       mode="關鍵字優先", amount=2)
        assert chosen["ticket_id"] == "1048265"
        assert chosen["name"] == "全票"
        assert chosen["amount"] == 2


class TestRedeemParse:
    def test_to_param(self):
        # 真實 redeem 回應（GET queue/token/{token}）
        body = '{"to_param":"157460846-3724ee09cbac2ff199b08cd099257f80"}'
        assert parsing.parse_redeem_to_param(body) == "157460846-3724ee09cbac2ff199b08cd099257f80"

    def test_missing(self):
        assert parsing.parse_redeem_to_param('{"foo":1}') == ""

    def test_bad_json(self):
        assert parsing.parse_redeem_to_param("nope") == ""
