import unittest
from unittest.mock import Mock, patch

import bot


class ParserTests(unittest.TestCase):
    def test_parse_abbreviated_counts(self):
        self.assertEqual(bot.parse_number("12.4K"), 12400)
        self.assertEqual(bot.parse_number("2M"), 2000000)
        self.assertEqual(bot.parse_number("1.5B"), 1500000000)
        self.assertEqual(bot.parse_number("1,200+"), 1200)

    def test_tiers_keep_original_thresholds(self):
        self.assertEqual(bot.determine_tier(39999, 100)[1], "🏆 GOD TIER")
        self.assertIsNone(bot.determine_tier(40000, 599))
        self.assertEqual(bot.determine_tier(99999, 600)[1], "🥇 GREAT GAME")
        self.assertEqual(bot.determine_tier(100000, 800)[1], "🥈 GOOD GAME")
        self.assertEqual(bot.determine_tier(500000, 800)[1], "🥉 MID GAME")
        self.assertIsNone(bot.determine_tier(500001, 1000))

    def test_stable_game_id_preferred(self):
        self.assertEqual(
            bot.game_key("Example", "https://www.roblox.com/games/12345/Example"),
            "roblox:12345",
        )

    def test_name_fallback_is_case_insensitive(self):
        self.assertEqual(bot.game_key("  My   Game "), "name:my game")
        self.assertEqual(bot.game_key("MY GAME"), "name:my game")

    def test_exclusions_use_word_boundaries(self):
        self.assertTrue(bot.is_excluded("A Modded Game"))
        self.assertTrue(bot.is_excluded("Game Test"))
        self.assertFalse(bot.is_excluded("Model Builder"))
        self.assertFalse(bot.is_excluded("Modern Life"))

    def test_creator_validator_rejects_rating_values(self):
        for value in ("14.8", "10.7", "1,234", "23.9%", "1.2K", "@15.6"):
            with self.subTest(value=value):
                self.assertTrue(bot._is_numeric_creator(value))
        for value in ("Studio Name", "Some Developer", "Builder_123"):
            with self.subTest(value=value):
                self.assertFalse(bot._is_numeric_creator(value))

    def test_creator_extracted_from_title_cell(self):
        self.assertEqual(
            bot.extract_creator_from_title_cell("Example Game\n@ExampleStudio"),
            "ExampleStudio",
        )
        self.assertIsNone(
            bot.extract_creator_from_title_cell("Example Game\n14.8")
        )
        self.assertIsNone(
            bot.extract_creator_from_title_cell("Example Game\n@Unknown")
        )

    def test_rotating_page_selection_keeps_page_one_and_advances(self):
        self.assertEqual(bot.select_pages(2, 2, 20), [1, 2])
        self.assertEqual(bot.select_pages(3, 2, 20), [1, 3])
        self.assertEqual(bot.select_pages(20, 2, 20), [1, 20])
        self.assertEqual(bot.select_pages(20, 3, 20), [1, 20, 2])
        self.assertEqual(bot.select_pages(0, 2, 20), [1, 2])

    def test_scan_cursor_persists_and_recovers_from_invalid_file(self):
        import tempfile
        from pathlib import Path

        original_path = bot.SCAN_CURSOR_FILE
        original_max = bot.ROTATING_MAX_PAGE
        try:
            with tempfile.TemporaryDirectory() as tmp:
                bot.SCAN_CURSOR_FILE = Path(tmp) / "cursor.json"
                bot.ROTATING_MAX_PAGE = 5
                self.assertEqual(bot.load_scan_cursor(), 2)
                bot.save_scan_cursor(4)
                self.assertEqual(bot.load_scan_cursor(), 4)
                bot.SCAN_CURSOR_FILE.write_text("{broken", encoding="utf-8")
                self.assertEqual(bot.load_scan_cursor(), 2)
        finally:
            bot.SCAN_CURSOR_FILE = original_path
            bot.ROTATING_MAX_PAGE = original_max

    def test_search_url_is_not_treated_as_game_id(self):
        self.assertIsNone(bot._game_id_from_url(
            "https://www.roblox.com/search/experiences?keyword=12345"
        ))


    def test_discord_delivery_retries_transient_server_error(self):
        original_webhook = bot.WEBHOOK_URL
        bot.WEBHOOK_URL = "https://discord.example/webhook"
        first = Mock(status_code=500, headers={}, text="temporary error")
        first.raise_for_status.side_effect = bot.requests.HTTPError("500")
        second = Mock(status_code=204, headers={}, text="")
        try:
            with patch.object(bot.requests, "post", side_effect=[first, second]) as post:
                with patch.object(bot.time, "sleep") as sleep:
                    bot.post_discord_alert({"title": "test"})
            self.assertEqual(post.call_count, 2)
            sleep.assert_called_once_with(1.0)
            first.close.assert_called_once()
            second.close.assert_called_once()
        finally:
            bot.WEBHOOK_URL = original_webhook

    def test_discord_delivery_does_not_retry_non_retryable_response(self):
        original_webhook = bot.WEBHOOK_URL
        bot.WEBHOOK_URL = "https://discord.example/webhook"
        response = Mock(status_code=400, headers={}, text="bad request")
        response.raise_for_status.side_effect = bot.requests.HTTPError("400")
        try:
            with patch.object(bot.requests, "post", return_value=response) as post:
                with patch.object(bot.time, "sleep") as sleep:
                    with self.assertRaises(bot.requests.HTTPError):
                        bot.post_discord_alert({"title": "test"})
            post.assert_called_once()
            sleep.assert_not_called()
            response.close.assert_called_once()
        finally:
            bot.WEBHOOK_URL = original_webhook

    def test_discord_rate_limit_retry_delay_is_bounded(self):
        original_webhook = bot.WEBHOOK_URL
        bot.WEBHOOK_URL = "https://discord.example/webhook"
        first = Mock(status_code=429, headers={"Retry-After": "99"}, text="rate limited")
        second = Mock(status_code=204, headers={}, text="")
        try:
            with patch.object(bot.requests, "post", side_effect=[first, second]):
                with patch.object(bot.time, "sleep") as sleep:
                    bot.post_discord_alert({"title": "test"})
            sleep.assert_called_once_with(5.0)
        finally:
            bot.WEBHOOK_URL = original_webhook


if __name__ == "__main__":
    unittest.main()
