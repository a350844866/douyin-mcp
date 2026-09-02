"""DB-level regression: one local row + one list snapshot per post per sync job,
even when the page card and its work_list twin both reach the upsert."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from douyin_creator_mcp.browser.extractors import normalize_video_record, normalize_work_list_item
from douyin_creator_mcp.config import Settings
from douyin_creator_mcp.services.browser_service import BrowserService
from douyin_creator_mcp.storage.db import Database

CST = ZoneInfo("Asia/Shanghai")
NOW = int(datetime(2026, 9, 2, 10, 0, tzinfo=CST).timestamp())
ACCOUNT = "default"


def _api_item(aweme_id, create_time, status_value=102, **status):
    st = {"is_delete": False, "is_private": False, "is_prohibited": False,
          "in_reviewing": False, "private_status": 0, "self_see": False}
    st.update(status)
    return {"aweme_id": aweme_id, "desc": "契约日只签一只 \n#tag #more", "create_time": create_time,
            "status_value": status_value, "status": st,
            "statistics": {"play_count": 10, "digg_count": 2, "comment_count": 1, "share_count": 0, "collect_count": 0}}


def _dom_card(publish_text, aweme_id=None, status=None):
    raw = {"title": "契约日只签一只 #tag #…", "publish_time": publish_text, "status": status,
           "detail_url": f"https://creator.douyin.com/creator-micro/work-management/work-detail/{aweme_id}" if aweme_id else None,
           "platform_item_id": aweme_id, "metrics": {"播放": "10", "点赞": "2"} if status else {}}
    return normalize_video_record(raw)


class UpsertDedupeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        db = Database(root / "t.sqlite")
        db.init_schema()
        self.service = BrowserService(Settings(data_dir=root, douyin_browser_profile_dir=root / "p"), db)
        self.db = db

    def tearDown(self):
        self.tmp.cleanup()

    def _counts(self):
        rows = self.db.query_one("SELECT COUNT(*) AS n FROM videos WHERE is_active = 1")["n"]
        snaps = self.db.query_one("SELECT COUNT(*) AS n FROM video_metric_snapshots")["n"]
        return rows, snaps

    def test_same_post_twice_in_one_job_does_not_crash_or_duplicate(self):
        t = NOW + 3600 * 30                      # scheduled for tomorrow evening
        text = datetime.fromtimestamp(t - t % 60, CST).strftime("%Y年%m月%d日 %H:%M")
        dom = _dom_card(text)                     # no platform id (no detail link on scheduled cards)
        api = normalize_work_list_item(_api_item("S9", t + 30, status_value=140), now_ts=NOW)
        job = self.service._start_job(ACCOUNT, "browser_sync_creator_data", "creator-manage-v2")
        # Simulate the merge heuristic missing: both records reach the upsert.
        dom_first = dict(dom)
        dom_first["platform_item_id"] = "S9"   # enriched twin
        videos, snaps = self.service._upsert_structured_videos(ACCOUNT, [dom_first, api], job, "2026-09-02T02:00:00+00:00")
        self.assertEqual((videos, snaps), (1, 1))
        self.assertEqual(self._counts(), (1, 1))

    def test_scheduled_row_is_updated_in_place_when_published(self):
        t = NOW - 3600
        text = datetime.fromtimestamp(t - t % 60, CST).strftime("%Y年%m月%d日 %H:%M")
        job1 = self.service._start_job(ACCOUNT, "browser_sync_creator_data", "creator-manage-v2")
        scheduled = normalize_work_list_item(_api_item("S9", t + 30, status_value=140), now_ts=t - 86400)
        self.service._upsert_structured_videos(ACCOUNT, [scheduled], job1, "2026-09-01T02:00:00+00:00")
        row = self.db.query_one("SELECT id, item_id, status, video_url FROM videos")
        self.assertEqual((row["item_id"], row["status"], row["video_url"]), ("S9", None, None))
        # Next day: the page shows the published card (DOM wins) and the API says 已发布.
        job2 = self.service._start_job(ACCOUNT, "browser_sync_creator_data", "creator-manage-v2")
        dom = _dom_card(text, aweme_id="S9", status="已发布")
        self.service._upsert_structured_videos(ACCOUNT, [dom], job2, "2026-09-02T02:00:00+00:00")
        rows = self.db.query_all("SELECT id, item_id, status, video_url, title FROM videos")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], row["id"])
        self.assertEqual(rows[0]["status"], "已发布")
        self.assertTrue(rows[0]["video_url"].endswith("/work-detail/S9"))
        self.assertEqual(rows[0]["title"], "契约日只签一只 #tag #…")   # page title wins on the visible day


class ItemIdIdentityFallbackTests(UpsertDedupeTests):
    """When the page yields no parsable card, identity falls back to platform ids."""

    # inherit setUp/tearDown/_counts only
    test_same_post_twice_in_one_job_does_not_crash_or_duplicate = None
    test_scheduled_row_is_updated_in_place_when_published = None

    def _api(self, aweme_id, t):
        return normalize_work_list_item(_api_item(aweme_id, t), now_ts=NOW)

    def test_overlap_with_existing_rows_verifies(self):
        job = self.service._start_job(ACCOUNT, "browser_sync_creator_data", "creator-manage-v2")
        self.service._upsert_structured_videos(ACCOUNT, [self._api("X1", NOW - 5000)], job, "2026-09-01T02:00:00+00:00")
        res = self.service._verify_account_by_item_ids(ACCOUNT, [self._api("X1", NOW - 5000), self._api("X2", NOW - 9000)])
        self.assertEqual((res["status"], res["overlap_count"]), ("verified_by_item_ids", 1))

    def test_no_overlap_is_refused(self):
        from douyin_creator_mcp.errors import AppError
        job = self.service._start_job(ACCOUNT, "browser_sync_creator_data", "creator-manage-v2")
        self.service._upsert_structured_videos(ACCOUNT, [self._api("X1", NOW - 5000)], job, "2026-09-01T02:00:00+00:00")
        with self.assertRaises(AppError):
            self.service._verify_account_by_item_ids(ACCOUNT, [self._api("Y9", NOW - 5000)])

    def test_empty_database_is_left_unbound(self):
        res = self.service._verify_account_by_item_ids(ACCOUNT, [self._api("X1", NOW - 5000)])
        self.assertEqual((res["status"], res["bound"]), ("unbound_no_page_cards", False))
        self.assertIsNone(self.db.query_one("SELECT account_id FROM browser_account_bindings"))


if __name__ == "__main__":
    unittest.main()
