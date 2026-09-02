"""Regression tests for work_list API paging merged into the creator list sync."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from douyin_creator_mcp.browser.extractors import (
    WORK_LIST_CLASSIFICATION_SOURCE,
    collect_all_video_cards,
    normalize_work_list_item,
    paginate_work_list,
)

CST = ZoneInfo("Asia/Shanghai")
NOW = int(datetime(2026, 9, 2, 10, 0, tzinfo=CST).timestamp())


def _item(aweme_id, create_time, status_value=102, **status):
    st = {"is_delete": False, "is_private": False, "is_prohibited": False,
          "in_reviewing": False, "private_status": 0, "self_see": False}
    st.update(status)
    return {
        "aweme_id": aweme_id,
        "desc": f"标题 {aweme_id} \n#tag",
        "create_time": create_time,
        "status_value": status_value,
        "status": st,
        "statistics": {"play_count": 100, "digg_count": 7, "comment_count": 3,
                       "share_count": 1, "collect_count": 2},
        "Cover": {"url_list": ["https://p3-sign.douyinpic.com/x.webp?sig=1"]},
    }


class PaginateTests(unittest.TestCase):
    def _hit(self, pages):
        calls = []

        def hit(count, cursor):
            calls.append((count, cursor))
            return pages[cursor]
        hit.calls = calls
        return hit

    def test_pages_until_has_more_false(self):
        pages = {
            0: {"status_code": 0, "has_more": True, "max_cursor": 111, "total": 3,
                "aweme_list": [_item("a", NOW - 1000), _item("b", NOW - 2000)]},
            111: {"status_code": 0, "has_more": False, "max_cursor": 0, "total": 3,
                  "aweme_list": [_item("c", NOW - 3000)]},
        }
        items, stop, total = paginate_work_list(self._hit(pages), max_pages=5)
        self.assertEqual([i["aweme_id"] for i in items], ["a", "b", "c"])
        self.assertIsNone(stop)
        self.assertEqual(total, 3)

    def test_named_stop_reasons(self):
        stalled = {0: {"status_code": 0, "has_more": True, "max_cursor": 0, "aweme_list": [_item("a", NOW)]}}
        self.assertEqual(paginate_work_list(self._hit(stalled), max_pages=3)[1], "missing_cursor_with_has_more")
        loop = {0: {"status_code": 0, "has_more": True, "max_cursor": 5, "aweme_list": [_item("a", NOW)]},
                5: {"status_code": 0, "has_more": True, "max_cursor": 5, "aweme_list": [_item("b", NOW)]}}
        self.assertEqual(paginate_work_list(self._hit(loop), max_pages=3)[1], "cursor_stalled")
        bad = {0: {"status_code": 8, "aweme_list": []}}
        self.assertEqual(paginate_work_list(self._hit(bad), max_pages=3)[1], "bad_status:8")
        empty = {0: {"status_code": 0, "has_more": True, "max_cursor": 9, "aweme_list": []}}
        self.assertEqual(paginate_work_list(self._hit(empty), max_pages=3)[1], "empty_page")

        def boom(count, cursor):
            raise TimeoutError("slow")
        items, stop, _ = paginate_work_list(boom, max_pages=3)
        self.assertEqual((items, stop), ([], "error:TimeoutError"))

    def test_max_pages_exhausted_is_reported(self):
        pages = {c: {"status_code": 0, "has_more": True, "max_cursor": c + 1, "aweme_list": [_item(str(c), NOW - c)]}
                 for c in range(0, 10)}
        items, stop, _ = paginate_work_list(self._hit(pages), max_pages=2)
        self.assertEqual(len(items), 2)
        self.assertEqual(stop, "max_pages_exhausted")


class NormalizeTests(unittest.TestCase):
    def test_public_post_maps_like_a_dom_card(self):
        rec = normalize_work_list_item(_item("777", NOW - 3600 + 50), now_ts=NOW)
        self.assertEqual(rec["title"], "标题 777 #tag")
        self.assertEqual(rec["publish_time"], NOW - 3600 + 50 - ((NOW - 3600 + 50) % 60))
        self.assertEqual(rec["status"], "已发布")
        self.assertEqual(rec["visibility"], "public")
        self.assertEqual(rec["platform_item_id"], "777")
        self.assertEqual(rec["video_url"], "https://creator.douyin.com/creator-micro/work-management/work-detail/777")
        self.assertEqual((rec["play_count"], rec["like_count"], rec["comment_count"], rec["share_count"], rec["collect_count"]),
                         (100, 7, 3, 1, 2))
        self.assertEqual(rec["cover_url"], "https://p3-sign.douyinpic.com/x.webp")
        self.assertEqual(rec["classification_source"], WORK_LIST_CLASSIFICATION_SOURCE)

    def test_scheduled_post_has_no_status_url_or_metrics(self):
        rec = normalize_work_list_item(_item("s1", NOW + 86400, status_value=140), now_ts=NOW)
        self.assertIsNone(rec["status"])
        self.assertEqual(rec["visibility"], "unknown")
        self.assertIsNone(rec["video_url"])
        self.assertIsNone(rec["play_count"])

    def test_private_and_prohibited_and_deleted(self):
        private = normalize_work_list_item(_item("p1", NOW - 5000, status_value=140, is_private=True, private_status=1), now_ts=NOW)
        self.assertEqual((private["status"], private["visibility"]), ("私密", "private"))
        self.assertIsNone(private["play_count"])
        self.assertTrue(private["video_url"].endswith("/work-detail/p1"))
        prohibited = normalize_work_list_item(_item("x1", NOW - 5000, status_value=140, is_prohibited=True), now_ts=NOW)
        self.assertEqual(prohibited["status"], "不适宜公开")
        self.assertIsNone(prohibited["video_url"])
        self.assertIsNone(normalize_work_list_item(_item("d1", NOW - 5000, is_delete=True), now_ts=NOW))
        self.assertIsNone(normalize_work_list_item({"aweme_id": "", "desc": "x", "create_time": NOW}, now_ts=NOW))


class _FakeResponse:
    def __init__(self, body, status=200):
        self._body, self.status = body, status

    def json(self):
        return self._body


class _FakeRequest:
    def __init__(self, pages, status=200):
        self.pages, self.status, self.urls, self.headers = pages, status, [], []

    def get(self, url, headers=None, timeout=None):
        self.urls.append(url)
        self.headers.append(headers or {})
        cursor = int(url.split("max_cursor=")[1].split("&")[0])
        return _FakeResponse(self.pages[cursor], self.status)


class _FakePage:
    """Grid shows one DOM card; the API knows three posts (one of them the same)."""

    def __init__(self, request, dom_cards):
        self.context = type("Ctx", (), {"request": request})()
        self.url = "https://creator.douyin.com/creator-micro/content/manage"
        self._dom_cards = dom_cards

    def evaluate(self, script):
        if "card_count" in script:
            return {"card_count": len(self._dom_cards), "total_count": None, "scroll_height": 100}
        if "scrollTo" in script:
            return None
        return self._dom_cards

    def wait_for_timeout(self, ms):
        return None


class MergeTests(unittest.TestCase):
    def _dom_card(self, aweme_id, publish_text, title="页面标题…"):
        return {"title": title, "publish_time": publish_text, "status": "已发布",
                "detail_url": f"https://creator.douyin.com/creator-micro/work-management/work-detail/{aweme_id}",
                "platform_item_id": aweme_id, "metrics": {"播放": "1", "点赞": "1"}}

    def test_dom_wins_and_api_fills_the_rest(self):
        t_dom = NOW - 3600
        text = datetime.fromtimestamp(t_dom - t_dom % 60, CST).strftime("%Y年%m月%d日 %H:%M")
        pages = {0: {"status_code": 0, "has_more": False, "total": 3,
                     "aweme_list": [_item("A", t_dom), _item("B", NOW - 7200), _item("C", NOW - 9000)]}}
        req = _FakeRequest(pages)
        page = _FakePage(req, [self._dom_card("A", text)])
        records, stats = collect_all_video_cards(page, max_scrolls=1, stable_rounds=1, wait_ms=0)
        by_id = {r["platform_item_id"]: r for r in records}
        self.assertEqual(sorted(by_id), ["A", "B", "C"])
        self.assertEqual(by_id["A"]["title"], "页面标题…")          # DOM record kept verbatim
        self.assertEqual(by_id["A"]["classification_source"], "creator_card_status_v1")
        self.assertEqual(by_id["B"]["classification_source"], WORK_LIST_CLASSIFICATION_SOURCE)
        self.assertEqual(stats["dom_card_count"], 1)
        self.assertEqual(stats["api_added_count"], 2)
        self.assertEqual(stats["loaded_card_count"], 3)
        self.assertEqual(stats["page_total_video_count"], 3)
        self.assertIsNone(stats["api_stop_reason"])
        self.assertEqual(req.headers[0].get("referer"), page.url)  # same-origin referer like the page itself

    def test_api_failure_leaves_dom_result_untouched(self):
        text = datetime.fromtimestamp(NOW - NOW % 60, CST).strftime("%Y年%m月%d日 %H:%M")
        page = _FakePage(_FakeRequest({0: {"status_code": 0}}, status=500), [self._dom_card("A", text)])
        records, stats = collect_all_video_cards(page, max_scrolls=1, stable_rounds=1, wait_ms=0)
        self.assertEqual(len(records), 1)
        self.assertEqual(stats["api_stop_reason"], "error:RuntimeError")
        self.assertIsNone(stats["page_total_video_count"])

    def test_api_can_be_disabled(self):
        text = datetime.fromtimestamp(NOW - NOW % 60, CST).strftime("%Y年%m月%d日 %H:%M")
        req = _FakeRequest({0: {"status_code": 0, "has_more": False, "aweme_list": [_item("Z", NOW - 50)]}})
        page = _FakePage(req, [self._dom_card("A", text)])
        records, stats = collect_all_video_cards(page, max_scrolls=1, stable_rounds=1, wait_ms=0, api_paging=False)
        self.assertEqual(len(records), 1)
        self.assertEqual(req.urls, [])
        self.assertEqual(stats["api_stop_reason"], "disabled")



class EnrichTests(unittest.TestCase):
    """Cards without a detail link (scheduled posts) must be enriched, not duplicated."""

    def _run(self, dom_title, api_title, t):
        from douyin_creator_mcp.browser.extractors import merge_work_list_records, normalize_video_record
        text = datetime.fromtimestamp(t - t % 60, CST).strftime("%Y年%m月%d日 %H:%M")
        dom = normalize_video_record({"title": dom_title, "publish_time": text, "status": None,
                                      "detail_url": None, "platform_item_id": None, "metrics": {}})
        collected = {dom["source_fingerprint"]: dom}
        item = _item("S9", t + 30, status_value=140)
        item["desc"] = api_title
        api = normalize_work_list_item(item, now_ts=NOW)
        added = merge_work_list_records(collected, [api])
        return added, collected, dom

    def test_scheduled_card_gets_platform_id_from_api_twin(self):
        t = NOW + 3600 * 30
        added, collected, dom = self._run("契约日只签一只搭档 #AI创作浪潮计划 #剑与魔法 #…",
                                          "契约日只签一只搭档 \n#AI创作浪潮计划 #剑与魔法 #异世界 #奇幻", t)
        self.assertEqual(added, 0)
        self.assertEqual(len(collected), 1)
        self.assertEqual(dom["platform_item_id"], "S9")
        self.assertIsNone(dom["video_url"])  # scheduled: still no detail link

    def test_exact_title_match_also_enriches(self):
        t = NOW + 3600 * 30
        added, collected, dom = self._run("短标题 #tag", "短标题 #tag", t)
        self.assertEqual((added, len(collected), dom["platform_item_id"]), (0, 1, "S9"))

    def test_different_post_same_minute_is_added(self):
        t = NOW + 3600 * 30
        added, collected, dom = self._run("完全不同的标题 #tag", "另一篇 #tag", t)
        self.assertEqual((added, len(collected)), (1, 2))
        self.assertIsNone(dom["platform_item_id"])


class PartialAndCountsTests(unittest.TestCase):
    def _dom_card(self, aweme_id, publish_text):
        return {"title": "页面标题…", "publish_time": publish_text, "status": "已发布",
                "detail_url": f"https://creator.douyin.com/creator-micro/work-management/work-detail/{aweme_id}",
                "platform_item_id": aweme_id, "metrics": {"播放": "5.3万", "点赞": "894"}}

    def test_second_page_failure_keeps_first_page_and_names_the_stop(self):
        t = NOW - 3600
        text = datetime.fromtimestamp(t - t % 60, CST).strftime("%Y年%m月%d日 %H:%M")
        pages = {0: {"status_code": 0, "has_more": True, "max_cursor": 77, "total": 3,
                     "aweme_list": [_item("A", t), _item("B", NOW - 7200)]},
                 77: {"status_code": 8, "aweme_list": []}}
        page = _FakePage(_FakeRequest(pages), [self._dom_card("A", text)])
        records, stats = collect_all_video_cards(page, max_scrolls=1, stable_rounds=1, wait_ms=0)
        self.assertEqual(sorted(r["platform_item_id"] for r in records), ["A", "B"])
        self.assertEqual(stats["api_stop_reason"], "bad_status:8")
        self.assertEqual(stats["api_added_count"], 1)
        self.assertIsNone(stats["page_total_video_count"])  # truncated ⇒ never claim a total

    def test_exact_api_counts_replace_rounded_card_counts(self):
        t = NOW - 3600
        text = datetime.fromtimestamp(t - t % 60, CST).strftime("%Y年%m月%d日 %H:%M")
        item = _item("A", t)
        item["statistics"]["play_count"] = 52930
        pages = {0: {"status_code": 0, "has_more": False, "total": 1, "aweme_list": [item]}}
        page = _FakePage(_FakeRequest(pages), [self._dom_card("A", text)])
        records, _ = collect_all_video_cards(page, max_scrolls=1, stable_rounds=1, wait_ms=0)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["title"], "页面标题…")      # page still owns identity fields
        self.assertEqual(records[0]["play_count"], 52930)      # 5.3万 → exact
        self.assertEqual(records[0]["like_count"], 7)

    def test_enrichment_recomputes_fingerprint(self):
        from douyin_creator_mcp.browser.extractors import merge_work_list_records, normalize_video_record
        t = NOW + 3600 * 30
        text = datetime.fromtimestamp(t - t % 60, CST).strftime("%Y年%m月%d日 %H:%M")
        dom = normalize_video_record({"title": "短标题 #tag", "publish_time": text, "status": None,
                                      "detail_url": None, "platform_item_id": None, "metrics": {}})
        before = dom["source_fingerprint"]
        item = _item("S9", t + 30, status_value=140)
        item["desc"] = "短标题 #tag"
        merge_work_list_records({before: dom}, [normalize_work_list_item(item, now_ts=NOW)])
        expected = normalize_video_record({"title": "短标题 #tag", "publish_time": text, "status": None,
                                           "detail_url": None, "platform_item_id": "S9", "metrics": {}})
        self.assertNotEqual(dom["source_fingerprint"], before)
        self.assertEqual(dom["source_fingerprint"], expected["source_fingerprint"])


if __name__ == "__main__":
    unittest.main()
