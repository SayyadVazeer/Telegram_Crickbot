"""Tests for short code validation, image generation, and new command functionality."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import bot


class ShortCodeTests(unittest.TestCase):
    def test_validate_match_json_requires_short_codes(self) -> None:
        payload = json.dumps({
            "match_type": "T20I", "balls_per_over": 6, "balls_per_innings": 120,
            "team1": "India", "team2": "Pakistan", "score1": 180, "wickets1": 6,
            "balls1": 120, "score2": 175, "wickets2": 7, "balls2": 110,
            "players": {
                "India": {"Player": {"runs": 50, "balls_faced": 40, "wickets": 0, "runs_conceded": 0, "balls_bowled": 0}},
                "Pakistan": {"Player2": {"runs": 40, "balls_faced": 35, "wickets": 1, "runs_conceded": 20, "balls_bowled": 12}},
            },
        })
        error = bot.validate_match_json(payload)
        self.assertIn("team1_short", error)
        self.assertIn("team2_short", error)

    def test_validate_match_json_accepts_short_codes(self) -> None:
        payload = json.dumps({
            "match_type": "T20I", "balls_per_over": 6, "balls_per_innings": 120,
            "team1": "India", "team2": "Pakistan", "team1_short": "IND", "team2_short": "PAK",
            "score1": 180, "wickets1": 6, "balls1": 120,
            "score2": 175, "wickets2": 7, "balls2": 110,
            "players": {
                "India": {"Player": {"runs": 50, "balls_faced": 40, "wickets": 0, "runs_conceded": 0, "balls_bowled": 0}},
                "Pakistan": {"Player2": {"runs": 40, "balls_faced": 35, "wickets": 1, "runs_conceded": 20, "balls_bowled": 12}},
            },
        })
        self.assertIsNone(bot.validate_match_json(payload))

    def test_team_mappings_are_loaded_from_database(self) -> None:
        db = bot.TournamentDatabase(":memory:")
        db.save_team_shortcodes({"India": "IND", "Pakistan": "PAK"})
        loaded = db.load_team_shortcodes()
        self.assertEqual(loaded, {"India": "IND", "Pakistan": "PAK"})

    def test_team_mappings_persist_after_save(self) -> None:
        db = bot.TournamentDatabase(":memory:")
        db.save_team_shortcodes({"India": "IND"})
        db.save_team_shortcodes({"India": "IND", "Australia": "AUS"})
        loaded = db.load_team_shortcodes()
        self.assertEqual(loaded, {"India": "IND", "Australia": "AUS"})

    def test_prompt_includes_team_short_codes(self) -> None:
        bot.team_mappings.clear()
        bot.team_mappings["India"] = "IND"
        bot.team_mappings["Pakistan"] = "PAK"
        prompt = bot.build_match_prompt()
        self.assertIn("IND=India", prompt)
        self.assertIn("PAK=Pakistan", prompt)
        self.assertIn("team1_short", prompt)
        self.assertIn("team2_short", prompt)
        bot.team_mappings.clear()

    def test_prompt_handles_no_mappings(self) -> None:
        bot.team_mappings.clear()
        prompt = bot.build_match_prompt()
        self.assertIn("no team short codes registered", prompt)
        self.assertIn("team1_short", prompt)


class ImageGenerationTests(unittest.TestCase):
    def test_standings_image_is_generated(self) -> None:
        state = bot.TournamentState()
        state.apply_match({
            "overs": 20, "team1": "India", "team2": "Pakistan",
            "score1": 180, "wickets1": 6, "balls1": 120,
            "score2": 175, "wickets2": 7, "balls2": 110,
            "players": {},
        })
        standings = state.get_standings()
        img_bytes = bot._generate_standings_image(standings)
        self.assertIsNotNone(img_bytes)
        self.assertGreater(len(img_bytes), 0)
        # Verify it's a valid PNG
        self.assertTrue(img_bytes[:4] == b'\x89PNG')

    def test_orange_cap_image_is_generated(self) -> None:
        players = {
            "Rohit": {"runs": 100, "balls_faced": 80, "wickets": 0, "runs_conceded": 0, "balls_bowled": 0},
            "Kohli": {"runs": 80, "balls_faced": 60, "wickets": 0, "runs_conceded": 0, "balls_bowled": 0},
        }
        leaders = bot.get_caps_leaders(players, "orange", top_n=10)
        img_bytes = bot._generate_cap_image("orange", leaders)
        self.assertIsNotNone(img_bytes)
        self.assertGreater(len(img_bytes), 0)
        self.assertTrue(img_bytes[:4] == b'\x89PNG')

    def test_purple_cap_image_is_generated(self) -> None:
        players = {
            "Bumrah": {"runs": 0, "balls_faced": 0, "wickets": 10, "runs_conceded": 30, "balls_bowled": 60},
            "Shami": {"runs": 0, "balls_faced": 0, "wickets": 8, "runs_conceded": 40, "balls_bowled": 50},
        }
        leaders = bot.get_caps_leaders(players, "purple", top_n=10)
        img_bytes = bot._generate_cap_image("purple", leaders)
        self.assertIsNotNone(img_bytes)
        self.assertGreater(len(img_bytes), 0)
        self.assertTrue(img_bytes[:4] == b'\x89PNG')

    def test_image_cache_is_invalidated(self) -> None:
        bot._image_cache["test"] = b"test_data"
        self.assertIn("test", bot._image_cache)
        bot._invalidate_image_cache()
        self.assertNotIn("test", bot._image_cache)


class TeamResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        bot.team_mappings.clear()
        bot.team_mappings["Manchester Super Giants"] = "MSG"
        bot.team_mappings["Birmingham Phoenix"] = "BP"

    def tearDown(self) -> None:
        bot.team_mappings.clear()

    def test_resolve_team_name_from_short_code(self) -> None:
        self.assertEqual(bot._resolve_team_name("MSG"), "Manchester Super Giants")
        self.assertEqual(bot._resolve_team_name("BP"), "Birmingham Phoenix")

    def test_resolve_team_name_from_full_name(self) -> None:
        self.assertEqual(bot._resolve_team_name("Manchester Super Giants"), "Manchester Super Giants")

    def test_resolve_team_name_unknown(self) -> None:
        self.assertEqual(bot._resolve_team_name("UNKNOWN"), "UNKNOWN")

    def test_resolve_team_label_from_name(self) -> None:
        self.assertEqual(bot._resolve_team_label("Manchester Super Giants"), "MSG - Manchester Super Giants")

    def test_resolve_team_label_from_short_code(self) -> None:
        self.assertEqual(bot._resolve_team_label("MSG"), "MSG - Manchester Super Giants")

    def test_resolve_team_label_unknown(self) -> None:
        self.assertEqual(bot._resolve_team_label("Some Random"), "Some Random")

    def test_simulation_prompt_includes_team_short_codes(self) -> None:
        prompt = bot.build_simulation_prompt()
        self.assertIn("MSG=Manchester Super Giants", prompt)
        self.assertIn("BP=Birmingham Phoenix", prompt)
        self.assertIn("REGISTERED TEAMS", prompt)
        self.assertIn("VERIFY", prompt)
        self.assertIn("playing 11", prompt)

    def test_simulation_prompt_handles_no_mappings(self) -> None:
        bot.team_mappings.clear()
        prompt = bot.build_simulation_prompt()
        self.assertIn("no team short codes registered", prompt)
        self.assertIn("REGISTERED TEAMS", prompt)

    def test_compact_match_resolves_short_codes(self) -> None:
        parsed = bot.parse_match("MSG 180/6 120 vs BP 175/7 110")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["team1"], "Manchester Super Giants")
        self.assertEqual(parsed["team2"], "Birmingham Phoenix")

    def test_json_match_resolves_short_codes(self) -> None:
        payload = json.dumps({
            "match_type": "T20I", "balls_per_over": 6, "balls_per_innings": 120,
            "team1": "Some Name", "team2": "Other Name",
            "team1_short": "MSG", "team2_short": "BP",
            "score1": 180, "wickets1": 6, "balls1": 120,
            "score2": 175, "wickets2": 7, "balls2": 110,
            "players": {},
        })
        parsed = bot.parse_match(payload)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["team1"], "Manchester Super Giants")
        self.assertEqual(parsed["team2"], "Birmingham Phoenix")


class FuzzyPlayerMatchingTests(unittest.TestCase):
    def test_exact_match_returns_existing(self) -> None:
        state = bot.TournamentState()
        state.add_player("Virat Kohli")
        result = bot._find_canonical_player("Virat Kohli", state.players)
        self.assertEqual(result, "Virat Kohli")

    def test_reversed_name_matches(self) -> None:
        state = bot.TournamentState()
        state.add_player("Virat Kohli")
        result = bot._find_canonical_player("Kohli Virat", state.players)
        self.assertEqual(result, "Virat Kohli")

    def test_partial_name_matches(self) -> None:
        state = bot.TournamentState()
        state.add_player("Virat Kohli")
        result = bot._find_canonical_player("Kohli", state.players)
        self.assertEqual(result, "Virat Kohli")

    def test_case_insensitive_match(self) -> None:
        state = bot.TournamentState()
        state.add_player("Virat Kohli")
        result = bot._find_canonical_player("virat kohli", state.players)
        self.assertEqual(result, "Virat Kohli")

    def test_no_match_for_unrelated_name(self) -> None:
        state = bot.TournamentState()
        state.add_player("Virat Kohli")
        result = bot._find_canonical_player("Rohit Sharma", state.players)
        self.assertIsNone(result)

    def test_apply_match_merges_fuzzy_names(self) -> None:
        state = bot.TournamentState()
        state.apply_match({
            "team1": "A", "team2": "B",
            "score1": 100, "wickets1": 3, "balls1": 120,
            "score2": 90, "wickets2": 4, "balls2": 120,
            "players": {
                "A": {"Virat Kohli": {"runs": 50, "wickets": 0, "balls_faced": 40, "runs_conceded": 0, "balls_bowled": 0}},
            },
        })
        state.apply_match({
            "team1": "C", "team2": "A",
            "score1": 80, "wickets1": 5, "balls1": 120,
            "score2": 100, "wickets2": 2, "balls2": 100,
            "players": {
                "A": {"Kohli Virat": {"runs": 30, "wickets": 0, "balls_faced": 25, "runs_conceded": 0, "balls_bowled": 0}},
            },
        })
        # Should be merged into one player entry
        self.assertEqual(len(state.players), 1)
        self.assertIn("Virat Kohli", state.players)
        self.assertEqual(state.players["Virat Kohli"]["runs"], 80)
        self.assertEqual(state.players["Virat Kohli"]["matches"], 2)


class MenuAndShortCommandTests(unittest.TestCase):
    def test_setshort_command_text(self) -> None:
        # Just verify the function exists and is callable
        self.assertTrue(callable(bot.setshort_command))

    def test_table_command_text(self) -> None:
        # Just verify the function exists and is callable
        self.assertTrue(callable(bot.table_command))

    def test_orange_cap_image_command_text(self) -> None:
        # Just verify the function exists and is callable
        self.assertTrue(callable(bot.orange_cap_image_command))

    def test_purple_cap_image_command_text(self) -> None:
        # Just verify the function exists and is callable
        self.assertTrue(callable(bot.purple_cap_image_command))


if __name__ == "__main__":
    unittest.main()
