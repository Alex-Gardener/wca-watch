import os
import unittest
from email import message_from_string
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

    def test_email_highlights_clear_dates_and_registration_timezone(self):
        item = competition("beijing", "2026-07-29T13:00:00Z", name="北京魔方公开赛")
        item.update(
            {
                "city": "北京",
                "venue": "示例体育馆",
                "registration_open": "2026-07-29T12:00:00Z",
                "registration_close": "2026-07-30T12:00:00Z",
                "competitor_limit": 120,
                "event_ids": ["333", "222"],
            }
        )
        with patch.dict(os.environ, {"MAIL_TIMEZONE": "Asia/Shanghai"}, clear=False):
            subject, body = wca.build_email([item])
            plain = wca.build_plain_email([item])

        self.assertEqual(subject, "WCA 新赛通知｜1 场比赛已公布")
        self.assertIn("2026年8月1日（周六）— 8月2日（周日）", body)
        self.assertIn("2026年07月29日 20:00（北京时间）", body)
        self.assertIn("参赛名额", body)
        self.assertIn("三阶", body)
        self.assertIn("比赛地点：北京 · 示例体育馆", plain)

    def test_send_email_contains_plain_and_html_versions(self):
        smtp_config = {
            "host": "smtp.example.com",
            "port": 465,
            "user": "sender@example.com",
            "password": "secret",
            "recipients": ["reader@example.com"],
            "from_name": "WCA Watch",
            "use_ssl": True,
        }
        with (
            patch.object(wca, "smtp_config", return_value=smtp_config),
            patch.object(wca.smtplib, "SMTP_SSL") as smtp,
        ):
            wca.send_email("主题", "<p>HTML 内容</p>", "纯文本内容")

        raw_message = smtp.return_value.__enter__.return_value.sendmail.call_args.args[2]
        message = message_from_string(raw_message)
        self.assertTrue(message.is_multipart())
        self.assertEqual(
            [part.get_content_type() for part in message.get_payload()],
            ["text/plain", "text/html"],
        )

    def test_sample_email_is_clearly_marked(self):
        item = competition("sample", "2026-07-29T13:00:00Z", name="演示比赛")
        subject, body = wca.build_email([item], is_sample=True)
        self.assertIn("样例", subject)
        self.assertIn("演示邮件", body)
        self.assertIn("不代表该比赛真实存在", body)


if __name__ == "__main__":
    unittest.main()
