import unittest

from src.game import (
    BATTING_STYLES,
    BOWLING_STYLES,
    create_match,
    format_overs,
    is_match_over,
    simulate_ball,
)
from src.teams import PITCHES, TEAMS


class GameTests(unittest.TestCase):
    def test_includes_requested_batting_and_bowling_styles(self):
        self.assertEqual(list(BATTING_STYLES), ["aggressive", "defensive", "ultra", "gaps", "rotate"])
        self.assertEqual(list(BOWLING_STYLES), ["aggressive", "defensive", "variation", "knuckle"])

    def test_international_and_franchise_teams_have_real_player_rosters_and_pitch_types(self):
        self.assertGreaterEqual(len(TEAMS), 18)
        self.assertIn("India", [team["name"] for team in TEAMS.values()])
        self.assertIn("Australia", [team["name"] for team in TEAMS.values()])
        self.assertIn("Mumbai Indians", [team["name"] for team in TEAMS.values()])
        self.assertIn("Chennai Super Kings", [team["name"] for team in TEAMS.values()])
        self.assertTrue(all(len(team["players"]) >= 12 for team in TEAMS.values()))
        self.assertEqual(set(PITCHES), {"balanced", "batting", "green", "spin"})

    def test_creates_match_and_rejects_invalid_over_limits(self):
        self.assertEqual(
            create_match(2),
            {"overs_limit": 2, "balls": 0, "score": 0, "wickets": 0},
        )
        for overs in (0, 21, 1.5, True):
            with self.subTest(overs=overs), self.assertRaises(ValueError):
                create_match(overs)

    def test_wicket_completes_innings(self):
        match = create_match(2)
        match["wickets"] = 9
        delivery = simulate_ball(match, "aggressive", "aggressive", rng=lambda: 0)
        self.assertEqual(delivery["outcome"], "wicket")
        self.assertEqual(match["wickets"], 10)
        self.assertTrue(is_match_over(match))

    def test_counts_balls_and_formats_completed_overs(self):
        match = create_match(1)
        for _ in range(6):
            simulate_ball(match, "rotate", "defensive", rng=lambda: 0.5)
        self.assertEqual(match["balls"], 6)
        self.assertEqual(format_overs(match["balls"]), "1.0")
        self.assertTrue(is_match_over(match))
        with self.assertRaisesRegex(ValueError, "already over"):
            simulate_ball(match, "rotate", "defensive")

    def test_rejects_unknown_styles(self):
        match = create_match(1)
        with self.assertRaisesRegex(ValueError, "batting style"):
            simulate_ball(match, "unknown", "aggressive")
        with self.assertRaisesRegex(ValueError, "bowling style"):
            simulate_ball(match, "aggressive", "unknown")


if __name__ == "__main__":
    unittest.main()
