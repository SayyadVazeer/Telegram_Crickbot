import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import bot


class TournamentBotTests(unittest.TestCase):
    def test_parse_match_and_points(self) -> None:
        parsed = bot.parse_match("India 180/6 120 vs Pakistan 175/7 110")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["team1"], "India")
        self.assertEqual(parsed["score1"], 180)
        self.assertEqual(parsed["wickets1"], 6)
        self.assertEqual(parsed["balls1"], 120)

        state = bot.TournamentState()
        state.apply_match(parsed)

        self.assertEqual(state.teams["India"]["played"], 1)
        self.assertEqual(state.teams["India"]["wins"], 1)
        self.assertEqual(state.teams["India"]["points"], 2)
        self.assertEqual(state.teams["Pakistan"]["losses"], 1)
        self.assertEqual(state.teams["Pakistan"]["points"], 0)
        self.assertEqual(state.teams["India"]["runs_scored"], 180)
        self.assertEqual(state.teams["India"]["runs_conceded"], 175)

    def test_parse_scorecard_format(self) -> None:
        scorecard = """INNINGS 1: DF BATTING

TOTAL (20 overs, 8 wkts) 133 CRR: 6.65

INNINGS 2: SUNRISERS LEEDS BATTING

TOTAL (18.2 overs, 10 wkts) 98 CRR: 5.40"""
        parsed = bot.parse_match(scorecard)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["team1"], "DF")
        self.assertEqual(parsed["team2"], "SUNRISERS LEEDS")
        self.assertEqual(parsed["score1"], 133)
        self.assertEqual(parsed["score2"], 98)
        self.assertEqual(parsed["wickets1"], 8)
        self.assertEqual(parsed["wickets2"], 10)
        self.assertEqual(parsed["balls1"], 120)
        self.assertEqual(parsed["balls2"], 110)

    def test_cricket_overs_notation_uses_base_six_balls(self) -> None:
        self.assertEqual(bot._overs_notation_to_balls("18.2"), 110)
        self.assertEqual(bot._overs_notation_to_balls("19.5"), 119)
        with self.assertRaises(ValueError):
            bot._overs_notation_to_balls("18.6")

    def test_nrr_uses_correct_ball_count_for_partial_over(self) -> None:
        state = bot.TournamentState()
        state.apply_match({
            "overs": 20,
            "team1": "India",
            "team2": "Pakistan",
            "score1": 100,
            "wickets1": 5,
            "balls1": bot._overs_notation_to_balls("18.2"),
            "score2": 101,
            "wickets2": 5,
            "balls2": 120,
            "players": {},
        })
        self.assertAlmostEqual(state.teams["India"]["net_run_rate"], 0.405)

    def test_nrr_supports_the_hundred_five_ball_sets(self) -> None:
        state = bot.TournamentState()
        state.apply_match({
            "match_type": "The Hundred",
            "team1": "Oval Invincibles",
            "team2": "Southern Brave",
            "score1": 100,
            "wickets1": 10,
            "balls1": 80,
            "score2": 101,
            "wickets2": 5,
            "balls2": 75,
            "players": {},
        })
        # Hundred NRR is expressed per five-ball set: 100/20 - 101/15.
        self.assertAlmostEqual(state.teams["Oval Invincibles"]["net_run_rate"], -1.733)
        self.assertEqual(state.teams["Southern Brave"]["points"], 2)

    def test_nrr_supports_configured_five_and_six_ball_overs(self) -> None:
        six_ball = bot.TournamentState()
        six_ball.apply_match({
            "overs": 20, "team1": "Six A", "team2": "Six B",
            "score1": 120, "wickets1": 5, "balls1": 120,
            "score2": 100, "wickets2": 5, "balls2": 120, "players": {},
        })
        self.assertAlmostEqual(six_ball.teams["Six A"]["net_run_rate"], 1.0)

        five_ball = bot.TournamentState()
        five_ball.apply_match({
            "overs": 20, "balls_per_over": 5, "team1": "Five A", "team2": "Five B",
            "score1": 100, "wickets1": 5, "balls1": 100,
            "score2": 80, "wickets2": 5, "balls2": 100, "players": {},
        })
        self.assertAlmostEqual(five_ball.teams["Five A"]["net_run_rate"], 1.0)

    def test_no_result_is_excluded_from_nrr(self) -> None:
        state = bot.TournamentState()
        state.apply_match({
            "overs": 20, "team1": "A", "team2": "B",
            "score1": 100, "wickets1": 5, "balls1": 120,
            "score2": 80, "wickets2": 5, "balls2": 120, "players": {},
        })
        state.apply_match({
            "overs": 20, "team1": "A", "team2": "C",
            "score1": 200, "wickets1": 5, "balls1": 120,
            "score2": 0, "wickets2": 0, "balls2": 1,
            "exclude_from_nrr": True, "players": {},
        })
        self.assertAlmostEqual(state.teams["A"]["net_run_rate"], 1.0)

    def test_mixed_over_formats_are_rejected_after_first_match(self) -> None:
        state = bot.TournamentState()
        state.apply_match({
            "overs": 20, "team1": "A", "team2": "B",
            "score1": 100, "wickets1": 5, "balls1": 120,
            "score2": 90, "wickets2": 5, "balls2": 120, "players": {},
        })
        with self.assertRaises(ValueError):
            state.apply_match({
                "match_type": "The Hundred", "team1": "A", "team2": "C",
                "score1": 100, "wickets1": 5, "balls1": 100,
                "score2": 90, "wickets2": 5, "balls2": 100, "players": {},
            })

    def test_no_result_awards_one_point_without_changing_nrr(self) -> None:
        state = bot.TournamentState()
        state.apply_match({
            "overs": 20, "team1": "A", "team2": "B",
            "score1": 0, "wickets1": 0, "balls1": 0,
            "score2": 0, "wickets2": 0, "balls2": 0,
            "result_type": "no_result", "exclude_from_nrr": True, "players": {},
        })
        self.assertEqual(state.teams["A"]["points"], 1)
        self.assertEqual(state.teams["B"]["points"], 1)
        self.assertEqual(state.teams["A"]["net_run_rate"], 0.0)

    def test_json_match_format_is_preserved_for_nrr(self) -> None:
        parsed = bot.parse_match(json.dumps({
            "match_type": "The Hundred",
            "balls_per_innings": 100,
            "balls_per_over": 5,
            "team1": "A",
            "team2": "B",
            "score1": 100,
            "wickets1": 10,
            "balls1": 80,
            "score2": 101,
            "wickets2": 5,
            "balls2": 75,
            "players": {},
        }))
        self.assertEqual(parsed["match_type"], "The Hundred")
        self.assertEqual(parsed["balls_per_innings"], 100)

        state = bot.TournamentState()
        state.apply_match(parsed)
        self.assertAlmostEqual(state.teams["A"]["net_run_rate"], -1.733)

    def test_dls_nrr_uses_officially_accredited_inputs(self) -> None:
        state = bot.TournamentState()
        state.apply_match({
            "match_type": "The Hundred", "team1": "A", "team2": "B",
            "score1": 120, "wickets1": 5, "balls1": 100,
            "score2": 81, "wickets2": 3, "balls2": 60,
            "nrr_score1": 80, "nrr_balls1": 60,
            "nrr_score2": 81, "nrr_balls2": 60,
            "players": {},
        })
        self.assertAlmostEqual(state.teams["A"]["net_run_rate"], -0.083)

    def test_match_prompt_requests_the_hundred_metadata(self) -> None:
        prompt = bot.build_match_prompt()
        self.assertIn("balls_per_innings to 100", prompt)
        self.assertIn("balls_per_over to 5", prompt)
        self.assertIn("Sample JSON:", prompt)
        self.assertIn("first match locks the tournament NRR format", prompt)
        self.assertIn("Never include Super Over runs or balls", prompt)

    def test_match_json_validation_requires_format_and_player_fields(self) -> None:
        incomplete = json.dumps({
            "match_type": "T20I", "team1": "A", "team2": "B",
            "score1": 100, "wickets1": 5, "balls1": 120,
            "score2": 90, "wickets2": 5, "balls2": 120, "players": {},
        })
        complete = json.dumps({
            "match_type": "T20I", "balls_per_over": 6, "balls_per_innings": 120,
            "team1": "A", "team2": "B", "team1_short": "AA", "team2_short": "BB",
            "score1": 100, "wickets1": 5, "balls1": 120,
            "score2": 90, "wickets2": 5, "balls2": 120,
            "players": {
                "A": {"A Player": {"runs": 50, "balls_faced": 40, "wickets": 0, "runs_conceded": 0, "balls_bowled": 0}},
                "B": {"B Player": {"runs": 40, "balls_faced": 35, "wickets": 1, "runs_conceded": 20, "balls_bowled": 12}},
            },
        })

        self.assertIn("balls_per_innings", bot.validate_match_json(incomplete) or "")
        self.assertIsNone(bot.validate_match_json(complete))

    def test_cap_pages_show_ten_players_and_keep_global_ranks(self) -> None:
        players = {
            f"Player {number}": {
                "runs": 130 - number,
                "balls_faced": 100,
                "wickets": 0,
                "runs_conceded": 0,
                "balls_bowled": 0,
            }
            for number in range(1, 13)
        }

        first_page, total_pages = bot.format_cap_page(players, "orange", page=0)
        second_page, _ = bot.format_cap_page(players, "orange", page=1)

        self.assertEqual(total_pages, 2)
        self.assertIn("1. 🟠 Player 1", first_page)
        self.assertIn("10. Player 10", first_page)
        self.assertNotIn("11. Player 11", first_page)
        self.assertIn("11. Player 11", second_page)
        self.assertIn("12. Player 12", second_page)

    def test_player_caps_are_tracked(self) -> None:
        parsed = bot.parse_match(
            "India 180/6 120 vs Pakistan 175/7 110 | players: India: Rohit 80 0, Kohli 50 1; Pakistan: Babar 60 0, Shaheen 20 2"
        )
        self.assertIsNotNone(parsed)

        state = bot.TournamentState()
        state.apply_match(parsed)

        self.assertEqual(state.players["Rohit"]["runs"], 80)
        self.assertEqual(state.players["Kohli"]["wickets"], 1)
        self.assertEqual(state.players["Shaheen"]["wickets"], 2)
        self.assertEqual(state.players["Babar"]["runs"], 60)

    def test_player_names_with_spaces_are_parsed(self) -> None:
        parsed = bot.parse_match(
            "India 180/6 120 vs Pakistan 175/7 110 | players: India: Rohit Sharma 80 0, S. Smith 50 1; Pakistan: Babar Azam 60 0, N. Shah 20 2"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["players"]["India"]["Rohit Sharma"]["runs"], 80)
        self.assertEqual(parsed["players"]["India"]["S. Smith"]["wickets"], 1)
        self.assertEqual(parsed["players"]["Pakistan"]["N. Shah"]["wickets"], 2)

    def test_json_payload_in_code_fence_is_parsed(self) -> None:
        payload = """```json
{
  \"team1\": \"India\",
  \"team2\": \"Pakistan\",
  \"score1\": 180,
  \"wickets1\": 6,
  \"balls1\": 120,
  \"score2\": 175,
  \"wickets2\": 7,
  \"balls2\": 110,
  \"players\": {
    \"India\": {
      \"Rohit\": {\"runs\": 80, \"wickets\": 0}
    }
  }
}
```"""
        parsed = bot.parse_match(payload)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["team1"], "India")
        self.assertEqual(parsed["players"]["India"]["Rohit"]["runs"], 80)

    def test_nested_scorecard_payload_is_parsed(self) -> None:
        payload = {
            "scorecard": {
                "innings_1": {
                    "team": "RCB",
                    "runs": 189,
                    "wickets": 4,
                    "overs": 18.2,
                    "batting": [{"player": "Virat Kohli", "runs": 52, "balls": 39, "out": True}],
                    "bowling": [{"player": "Khaleel Ahmed", "wickets": 1, "runs": 34}],
                },
                "innings_2": {
                    "team": "CSK",
                    "runs": 192,
                    "wickets": 5,
                    "overs": 20.0,
                    "batting": [{"player": "MS Dhoni", "runs": 34, "balls": 14, "out": False}],
                    "bowling": [{"player": "Josh Hazlewood", "wickets": 2, "runs": 32}],
                },
            }
        }
        parsed = bot.parse_match(json.dumps(payload))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["team1"], "RCB")
        self.assertEqual(parsed["team2"], "CSK")
        self.assertEqual(parsed["score1"], 189)
        self.assertEqual(parsed["score2"], 192)
        self.assertEqual(parsed["balls1"], 110)
        self.assertEqual(parsed["players"]["RCB"]["Virat Kohli"]["runs"], 52)
        self.assertEqual(parsed["players"]["CSK"]["Josh Hazlewood"]["wickets"], 2)

    def test_full_scorecard_sample_is_parsed(self) -> None:
        scorecard = """================================================================================
INNINGS 1: DF BATTING

BATTER                     DISMISSAL                       RUNS  BALLS  SR

Z. Crawley                 c (sub) b Theekshana            8     8      100.00
T. Abell                   run out (Carse)                 42    34     123.53

Extras                     (lb 1)                          1
TOTAL                      (20 overs, 8 wkts)              133         CRR: 6.65

BOWLER                     OVERS   RUNS   WICKETS   ECON

Sam Cook                   4.0     26     2         6.50

================================================================================
INNINGS 2: SUNRISERS LEEDS BATTING

BATTER                     DISMISSAL                       RUNS  BALLS  SR

R. Gurbaz                  c (sub) b Gleeson               7     4      175.00

Extras                     (lb 1)                          1
TOTAL                      (18.1 overs, 10 wkts)           98          CRR: 5.40

BOWLER                     OVERS   RUNS   WICKETS   ECON

Naseem Shah                3.1     14     3         4.42"""
        parsed = bot.parse_match(scorecard)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["team1"], "DF")
        self.assertEqual(parsed["team2"], "SUNRISERS LEEDS")
        self.assertEqual(parsed["score1"], 133)
        self.assertEqual(parsed["score2"], 98)
        self.assertIn("Sam Cook", parsed["players"]["DF"])
        self.assertIn("Naseem Shah", parsed["players"]["SUNRISERS LEEDS"])

    def test_caps_use_tie_breakers_and_top_ten(self) -> None:
        players = {
            "A": {"runs": 100, "balls_faced": 100, "wickets": 5, "runs_conceded": 20, "balls_bowled": 60},
            "B": {"runs": 100, "balls_faced": 50, "wickets": 5, "runs_conceded": 10, "balls_bowled": 30},
            "C": {"runs": 90, "balls_faced": 60, "wickets": 6, "runs_conceded": 30, "balls_bowled": 60},
        }

        orange = bot.get_caps_leaders(players, "orange", top_n=3)
        purple = bot.get_caps_leaders(players, "purple", top_n=3)

        self.assertEqual([item["name"] for item in orange], ["B", "A", "C"])
        self.assertEqual([item["name"] for item in purple], ["C", "A", "B"])

    def test_caps_exclude_players_without_relevant_participation(self) -> None:
        players = {
            "Batter": {"runs": 20, "balls_faced": 10, "wickets": 0, "runs_conceded": 0, "balls_bowled": 0},
            "Bowler": {"runs": 0, "balls_faced": 0, "wickets": 1, "runs_conceded": 12, "balls_bowled": 6},
            "Did Not Bat": {"runs": 0, "balls_faced": 0, "wickets": 0, "runs_conceded": 0, "balls_bowled": 0},
            "Did Not Bowl": {"runs": 5, "balls_faced": 2, "wickets": 1, "runs_conceded": 0, "balls_bowled": 0},
        }

        orange = bot.get_caps_leaders(players, "orange")
        purple = bot.get_caps_leaders(players, "purple")

        self.assertEqual([player["name"] for player in orange], ["Batter", "Did Not Bowl"])
        self.assertEqual([player["name"] for player in purple], ["Bowler"])

    def test_empty_cap_page_explains_eligibility_requirement(self) -> None:
        text, total_pages = bot.format_cap_page(
            {"Did Not Play": {"runs": 0, "balls_faced": 0, "wickets": 0, "runs_conceded": 0, "balls_bowled": 0}},
            "purple",
        )

        self.assertEqual(total_pages, 1)
        self.assertEqual(text, "No eligible players yet. No player has bowled a ball.")

    def test_standings_format_excludes_runs_wickets_and_strike_rate(self) -> None:
        standings = [{
            "team": "India",
            "played": 1,
            "wins": 1,
            "draws": 0,
            "losses": 0,
            "points": 2,
            "runs": 180,
            "wickets": 6,
            "run_rate": 9.0,
            "strike_rate": 100.0,
            "net_run_rate": 1.25,
        }]
        formatted = bot.format_standings(standings)
        self.assertNotIn("Runs:", formatted)
        self.assertNotIn("Wkts:", formatted)
        self.assertNotIn("SR:", formatted)

    def test_standings_format_uses_simple_text(self) -> None:
        standings = [{
            "team": "India",
            "played": 1,
            "wins": 1,
            "draws": 0,
            "losses": 0,
            "points": 2,
            "net_run_rate": 1.25,
        }]
        formatted = bot.format_standings(standings)
        self.assertIn("Tournament Standings", formatted)
        self.assertNotIn("🏏", formatted)
        self.assertNotIn("📈", formatted)
        self.assertIn("P 1 | W 1 | D 0 | L 0 | Pt 2 | NRR 1.25", formatted)

    def test_admin_can_be_set_and_checked(self) -> None:
        state = bot.TournamentState()
        self.assertFalse(state.is_admin(123))
        state.set_admin(123)
        self.assertTrue(state.is_admin(123))
        self.assertFalse(state.is_admin(456))

    def test_clear_tournament_removes_data_but_keeps_admins(self) -> None:
        state = bot.TournamentState()
        state.set_admin(123)
        state.apply_match({
            "team1": "A", "team2": "B", "score1": 100, "wickets1": 3,
            "balls1": 120, "score2": 90, "wickets2": 4, "balls2": 120, "players": {},
        })
        state.clear_tournament()

        self.assertEqual(state.teams, {})
        self.assertEqual(state.players, {})
        self.assertEqual(state.matches, [])
        self.assertTrue(state.is_admin(123))

    def test_sqlite_squads_store_and_clear(self) -> None:
        database = bot.TournamentDatabase(":memory:")
        self.assertTrue(database.add_team("Mumbai"))
        self.assertFalse(database.add_team("mumbai"))
        self.assertTrue(database.add_player("Mumbai", "Rohit Sharma", 2_500_000))
        self.assertFalse(database.add_player("Unknown", "Player", 100))

        self.assertEqual(database.get_team_names(), ["Mumbai"])
        squads = database.get_squads()
        self.assertEqual(squads[0]["team"], "Mumbai")
        self.assertEqual(squads[0]["player"], "Rohit Sharma")
        self.assertIn("2,500,000.00", bot.format_squads(squads))
        self.assertEqual(
            bot.format_team_squad("Mumbai", database.get_team_players("Mumbai")),
            "Mumbai\n\nRohit Sharma | 2500000",
        )

        database.clear_tournament()
        self.assertEqual(database.get_squads(), [])

    def test_database_can_delete_the_last_match_only(self) -> None:
        database = bot.TournamentDatabase(":memory:")
        first_match = {"team1": "A", "team2": "B", "score1": 100, "score2": 90, "wickets1": 5, "wickets2": 6, "balls1": 120, "balls2": 120}
        second_match = {"team1": "C", "team2": "D", "score1": 110, "score2": 100, "wickets1": 5, "wickets2": 6, "balls1": 120, "balls2": 120}
        database.save_match(first_match)
        database.save_match(second_match)

        self.assertEqual(database.delete_last_match(), second_match)
        self.assertEqual(database.load_all()["matches"], [first_match])

    def test_parse_admin_ids_supports_multiple_values(self) -> None:
        self.assertEqual(bot.parse_admin_ids("123,456"), {123, 456})
        self.assertEqual(bot.parse_admin_ids("123;456"), {123, 456})
        self.assertEqual(bot.parse_admin_ids(""), set())

    def test_nrr_uses_match_overs_quota_for_all_out_innings(self) -> None:
        state = bot.TournamentState()
        match_data = {
            "match_type": "ODI",
            "overs": 50,
            "team1": "India",
            "team2": "Pakistan",
            "score1": 200,
            "wickets1": 10,
            "balls1": 240,
            "score2": 180,
            "wickets2": 10,
            "balls2": 300,
            "players": {},
        }
        state.apply_match(match_data)
        self.assertAlmostEqual(state.teams["India"]["net_run_rate"], 0.4)


if __name__ == "__main__":
    unittest.main()
