import os
import unittest
from unittest.mock import patch

import wca


def competition(identifier, announced_at, country="CN", name=None):
    return {
        "id": identifier,
        "name": name or identifier,
        "announced_at": announced_at,
        "country_iso2": country,
        "start_date": "2026-08-01",
        "end_date": "2026-08-02",
    }


class IncrementalFetchTests(unittest.TestCase):
    def test_api_is_sorted_by_announcement_time(self):
        with patch.object(wca, "request_json", return_value=[]) as request:
            wca.fetch_page(3)
        self.assertEqual(request.call_args.args[0]["sort"], "-announced_at")
        self.assertEqual(request.call_args.args[0]["page"], 3)

    def test_fetches_only_items_after_cursor(self):
        page = [
            competition("new-2", "2026-07-29T14:00:00.000Z"),
            competition("new-1", "2026-07-29T13:00:00.000Z"),
            competition("old", "2026-07-29T11:00:00.000Z"),
        ]
        with patch.object(wca, "fetch_page", return_value=page):
            new_items, fetched = wca.fetch_since("2026-07-29T12:00:00.000Z", {"old"})
        self.assertEqual([item["id"] for item in new_items], ["new-2", "new-1"])
        self.assertEqual(fetched, page)

    def test_unseen_item_at_same_timestamp_is_not_lost(self):
        page = [
            competition("same-new", "2026-07-29T12:00:00.000Z"),
            competition("same-seen", "2026-07-29T12:00:00.000Z"),
            competition("old", "2026-07-29T11:00:00.000Z"),
        ]
        with patch.object(wca, "fetch_page", return_value=page):
            new_items, _ = wca.fetch_since(
                "2026-07-29T12:00:00.000Z", {"same-seen", "old"}
            )
        self.assertEqual([item["id"] for item in new_items], ["same-new"])


class PresentationAndFilterTests(unittest.TestCase):
    def test_country_filter(self):
        items = [competition("china", "2026-07-29T13:00:00Z", "CN"), competition("japan", "2026-07-29T13:00:00Z", "JP")]
        with patch.dict(os.environ, {"WCA_COUNTRY_CODES": "CN, HK"}, clear=False):
            self.assertEqual([item["id"] for item in wca.filter_items(items)], ["china"])

    def test_email_escapes_api_text(self):
        item = competition("safe", "2026-07-29T13:00:00Z", name="<script>alert(1)</script>")
        _, body = wca.build_email([item])
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;", body)


if __name__ == "__main__":
    unittest.main()
