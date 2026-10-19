import unittest

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

    def test_search_url_is_not_treated_as_game_id(self):
        self.assertIsNone(bot._game_id_from_url(
            "https://www.roblox.com/search/experiences?keyword=12345"
        ))


if __name__ == "__main__":
    unittest.main()
