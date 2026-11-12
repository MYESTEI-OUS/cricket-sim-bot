import json
import unittest
import tempfile
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from src.bot import TelegramBot


class TelegramBotTests(unittest.TestCase):
    def test_https_context_is_built_once_and_reused_for_api_requests(self):
        context = object()
        with patch("src.bot.build_https_context", return_value=context) as build_context:
            with patch("src.bot.urlopen") as urlopen:
                urlopen.return_value.__enter__.return_value.read.return_value = (
                    b'{"ok": true, "result": true}'
                )
                bot = TelegramBot("test-token", database_path=":memory:")

                bot.api("getMe")
                bot.api("getMe")

        build_context.assert_called_once_with()
        self.assertEqual(urlopen.call_count, 2)
        self.assertIs(urlopen.call_args_list[0].kwargs["context"], context)
        self.assertIs(urlopen.call_args_list[1].kwargs["context"], context)

    def setUp(self):
        self.bot = TelegramBot("test-token", database_path=":memory:")
        self.bot.username = "cricket_simulator_bot"
        self.api_calls = []

        def fake_api(method, parameters=None):
            self.api_calls.append((method, parameters))
            if method == "sendMessage":
                return {"message_id": 77}
            return True

        self.bot.api = fake_api

    def test_webhook_server_checks_secret_and_handles_telegram_updates(self):
        message = {
            "chat": {"id": 12, "type": "private"},
            "from": {"id": 12},
            "text": "/start",
        }
        with patch.dict("os.environ", {"PORT": "0"}):
            server = self.bot.create_webhook_server("expected-secret")

        def request(path, method="GET", secret=None, body=None):
            result = {}

            def send_request():
                headers = {}
                data = None
                if secret:
                    headers["X-Telegram-Bot-Api-Secret-Token"] = secret
                if body is not None:
                    headers["Content-Type"] = "application/json"
                    data = json.dumps(body).encode()
                request = Request(
                    f"http://127.0.0.1:{server.server_port}{path}",
                    data=data,
                    headers=headers,
                    method=method,
                )
                try:
                    with urlopen(request, timeout=2) as response:
                        result["status"] = response.status
                except HTTPError as error:
                    result["status"] = error.code

            thread = threading.Thread(target=send_request)
            thread.start()
            server.handle_request()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            return result["status"]

        try:
            self.assertEqual(request("/health"), 200)
            self.assertEqual(
                request("/telegram", method="POST", secret="wrong", body={}),
                403,
            )
            with patch.object(self.bot, "handle_message") as handle_message:
                self.assertEqual(
                    request(
                        "/telegram",
                        method="POST",
                        secret="expected-secret",
                        body={"message": message},
                    ),
                    200,
                )
                handle_message.assert_called_once_with(message)
        finally:
            server.server_close()

    def callback(self, data, user_id=1, first_name=None, chat_id=-100):
        self.bot.handle_callback(
            {
                "id": f"callback-{len(self.api_calls)}",
                "from": {"id": user_id, "first_name": first_name or f"Player {user_id}"},
                "data": data,
                "message": {
                    "message_id": 77,
                    "chat": {
                        "id": chat_id,
                        "type": "private" if chat_id > 0 else "supergroup",
                    },
                },
            }
        )

    def create_ready_match(self):
        self.bot.handle_message(
            {
                "chat": {"id": -100, "type": "supergroup"},
                "from": {"id": 1, "first_name": "Home captain"},
                "text": "/newmatch",
            }
        )
        self.callback("team:home:india", user_id=1)
        self.callback("join", user_id=2, first_name="Away captain")
        self.callback("team:away:australia", user_id=2)
        for index in range(11):
            self.callback(f"xi:home:{index}", user_id=1)
        self.callback("xi-confirm:home", user_id=1)
        for index in range(11):
            self.callback(f"xi:away:{index}", user_id=2)
        self.callback("xi-confirm:away", user_id=2)
        self.callback("overs:2", user_id=1)
        self.callback("pitch:green", user_id=1)
        self.callback("start-match", user_id=1)

    def test_requires_a_group_for_multiplayer_matches(self):
        self.bot.handle_message({"chat": {"id": 12, "type": "private"}, "from": {"id": 1}, "text": "/newmatch"})
        self.assertNotIn(12, self.bot.sessions)
        self.assertIn("Telegram group", self.api_calls[-1][1]["text"])

    def test_match_sessions_restore_from_sqlite_and_cancel_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "matches.sqlite3"
            bot = TelegramBot("test-token", database_path=database_path)
            bot.api = lambda method, parameters=None: (
                {"message_id": 77} if method == "sendMessage" else True
            )
            bot.handle_update({
                "message": {
                    "chat": {"id": -100, "type": "supergroup"},
                    "from": {"id": 1, "first_name": "Host"},
                    "text": "/newmatch",
                }
            })

            restored_bot = TelegramBot("test-token", database_path=database_path)
            self.assertEqual(restored_bot.sessions[-100]["stage"], "home_team")
            self.assertEqual(restored_bot.sessions[-100]["captains"]["home"], 1)

            restored_bot.api = lambda method, parameters=None: True
            restored_bot.handle_update({
                "message": {
                    "chat": {"id": -100, "type": "supergroup"},
                    "from": {"id": 1},
                    "text": "/cancel",
                }
            })
            restarted_bot = TelegramBot("test-token", database_path=database_path)
            self.assertNotIn(-100, restarted_bot.sessions)

            bot.database.close()
            restored_bot.database.close()
            restarted_bot.database.close()

    def test_callback_is_acknowledged_before_scorecard_edit(self):
        self.bot.create_lobby(
            {"id": -100, "type": "supergroup"},
            {"id": 1, "first_name": "Host"},
        )
        self.api_calls.clear()

        self.callback("team:home:india")

        self.assertEqual(self.api_calls[0][0], "answerCallbackQuery")
        self.assertEqual(self.api_calls[1][0], "editMessageText")

    def test_commands_accept_arguments_and_this_bots_username_mention(self):
        self.bot.handle_message({
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 1, "first_name": "Host"},
            "text": "/NEWMATCH@Cricket_Simulator_Bot please",
        })
        self.assertEqual(self.bot.sessions[-100]["stage"], "home_team")

    def test_commands_addressed_to_another_bot_are_ignored(self):
        self.bot.handle_message({
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 1, "first_name": "Host"},
            "text": "/newmatch@some_other_bot",
        })
        self.assertNotIn(-100, self.bot.sessions)
        self.assertEqual(self.api_calls, [])

    def test_play_commands_start_the_correct_league(self):
        self.bot.handle_message({
            "chat": {"id": -101, "type": "supergroup"},
            "from": {"id": 1, "first_name": "Host"},
            "text": "/playipl",
        })
        self.assertEqual(self.bot.sessions[-101]["league"], "ipl")
        self.assertEqual(self.bot.sessions[-101]["stage"], "home_team")

        self.bot.handle_message({
            "chat": {"id": -102, "type": "supergroup"},
            "from": {"id": 1, "first_name": "Host"},
            "text": "/playint",
        })
        self.assertEqual(self.bot.sessions[-102]["league"], "international")
        self.assertEqual(self.bot.sessions[-102]["stage"], "home_team")

    def test_vs_bot_commands_create_private_matches_for_both_leagues(self):
        self.bot.handle_message({
            "chat": {"id": 12, "type": "private"},
            "from": {"id": 1, "first_name": "Solo player"},
            "text": "/vsbot",
        })
        self.assertEqual(self.bot.sessions[12]["league"], "international")
        self.assertTrue(self.bot.sessions[12]["vs_bot"])
        self.assertEqual(self.bot.sessions[12]["stage"], "home_team")

        self.bot.handle_message({
            "chat": {"id": 13, "type": "private"},
            "from": {"id": 2, "first_name": "IPL player"},
            "text": "/vsbotipl",
        })
        self.assertEqual(self.bot.sessions[13]["league"], "ipl")
        self.assertTrue(self.bot.sessions[13]["vs_bot"])

    def test_vs_bot_match_auto_selects_opponent_and_plays_bot_bowling_turns(self):
        chat_id = 12
        self.bot.handle_message({
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": 1, "first_name": "Solo player"},
            "text": "/vsbot",
        })
        self.callback("team:home:india", user_id=1, chat_id=chat_id)
        game = self.bot.sessions[chat_id]

        self.assertEqual(game["stage"], "home_xi")
        self.assertNotEqual(game["teams"]["home"], game["teams"]["away"])
        self.assertEqual(len(game["lineups"]["away"]), 11)

        for index in range(11):
            self.callback(f"xi:home:{index}", user_id=1, chat_id=chat_id)
        self.callback("xi-confirm:home", user_id=1, chat_id=chat_id)
        self.callback("overs:2", user_id=1, chat_id=chat_id)
        self.callback("pitch:green", user_id=1, chat_id=chat_id)
        self.callback("start-match", user_id=1, chat_id=chat_id)
        self.assertEqual(game["phase"], "bat")

        self.callback("bat:aggressive", user_id=1, chat_id=chat_id)
        self.assertEqual(game["innings"]["match"]["balls"], 1)
        self.assertEqual(game["phase"], "bat")
        self.assertIn("Last ball:", self.api_calls[-1][1]["text"])
        self.assertEqual(
            self.api_calls[-1][1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
            "bat:aggressive",
        )

    def test_vs_bot_batting_turn_automatically_faces_user_selected_bowling(self):
        chat_id = 12
        self.bot.handle_message({
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": 1, "first_name": "Solo player"},
            "text": "/vsbot",
        })
        self.callback("team:home:india", user_id=1, chat_id=chat_id)
        for index in range(11):
            self.callback(f"xi:home:{index}", user_id=1, chat_id=chat_id)
        self.callback("xi-confirm:home", user_id=1, chat_id=chat_id)
        self.callback("overs:2", user_id=1, chat_id=chat_id)
        self.callback("pitch:balanced", user_id=1, chat_id=chat_id)
        self.callback("start-match", user_id=1, chat_id=chat_id)

        def finish_first_innings(match, batting_style, bowling_style, pitch_modifiers):
            match["balls"] = 12
            match["score"] = 24
            return {"outcome": "four", "runs": 4, "over": "2.0"}

        def reach_target(match, batting_style, bowling_style, pitch_modifiers):
            match["balls"] += 1
            match["score"] = 25
            return {"outcome": "one", "runs": 1, "over": "0.1"}

        with patch("src.bot.simulate_ball", side_effect=finish_first_innings):
            self.callback("bat:aggressive", user_id=1, chat_id=chat_id)
        game = self.bot.sessions[chat_id]
        self.assertEqual(game["stage"], "innings_break")
        self.callback("second-innings", user_id=1, chat_id=chat_id)
        self.assertEqual(game["batting_side"], "away")
        self.assertEqual(game["phase"], "bowl")
        self.assertIn("Choose bowling style:", self.api_calls[-1][1]["text"])
        with patch("src.bot.simulate_ball", side_effect=reach_target):
            self.callback("bowl:variation", user_id=1, chat_id=chat_id)

        self.assertEqual(game["stage"], "complete")
        self.assertEqual(game["results"][1]["score"], 25)
        self.assertIn("🏆 <b>MATCH NET RUN RATE (NRR)</b>", self.api_calls[-1][1]["text"])

    def test_scorecard_shows_striker_and_bowler_rotation(self):
        self.create_ready_match()
        self.callback("bat:aggressive", user_id=1)
        self.assertIn("Strike:", self.api_calls[-1][1]["text"])
        self.assertIn("Current bowler:", self.api_calls[-1][1]["text"])
        self.assertIn("Next bowler:", self.api_calls[-1][1]["text"])

    def test_live_scorecard_shows_run_rates_batter_rates_and_bowler_figures(self):
        self.create_ready_match()

        text = self.api_calls[-1][1]["text"]
        self.assertIn("Run rate (RR/CRR): 0.00 runs/over", text)
        self.assertIn("Batting SR: 0.00 | Non-striker SR: 0.00", text)
        self.assertIn("Current bowler:", text)
        self.assertIn("Econ 0.00", text)
        self.assertEqual(self.api_calls[-1][1]["parse_mode"], "HTML")

        game = self.bot.sessions[-100]
        game["captain_names"]["home"] = "<Captain & Co>"
        self.assertIn("&lt;Captain &amp; Co&gt;", self.bot.scorecard_text(game))

    def test_refresh_redraws_live_match_scorecard_and_controls(self):
        self.create_ready_match()
        self.bot.handle_message({
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 1},
            "text": "/refresh",
        })

        method, parameters = self.api_calls[-1]
        self.assertEqual(method, "editMessageText")
        self.assertIn("Score: 0/0 (0.0/2 overs)", parameters["text"])
        self.assertIn("Choose batting approach:", parameters["text"])
        self.assertIn("reply_markup", parameters)

    def test_completed_scorecard_clears_old_buttons_without_duplicate_fallback(self):
        game = {"chat_id": -100, "message_id": 77}

        def unchanged_message(method, parameters=None):
            self.api_calls.append((method, parameters))
            raise RuntimeError("Bad Request: message is not modified")

        self.bot.api = unchanged_message
        self.bot.edit(game, "Match complete")

        self.assertEqual(len(self.api_calls), 1)
        self.assertEqual(self.api_calls[0][0], "editMessageText")
        self.assertEqual(
            self.api_calls[0][1]["reply_markup"],
            {"inline_keyboard": []},
        )

    def test_stale_buttons_cannot_change_current_match(self):
        self.bot.create_lobby(
            {"id": -100, "type": "supergroup"},
            {"id": 1, "first_name": "Host"},
        )
        game = self.bot.sessions[-100]
        game["message_id"] = 88

        self.callback("team:home:india", user_id=1)

        self.assertEqual(game["stage"], "home_team")
        self.assertNotIn("home", game["teams"])
        self.assertIn("out of date", self.api_calls[-1][1]["text"])

    def test_branding_labels_are_clear_for_international_and_ipl_modes(self):
        self.assertEqual(self.bot.league_name("international"), "International")
        self.assertEqual(self.bot.league_name("ipl"), "IPL Franchise")
        self.assertEqual(self.bot.mode_badge("international"), "🌍 International Cricket")
        self.assertEqual(self.bot.mode_badge("ipl"), "🏆 IPL Franchise")
        self.assertIn("IPL Franchise", str(self.bot.mode_keyboard()))
        self.assertIn("International Cricket", str(self.bot.mode_keyboard()))
        self.assertIn("India", self.bot.team_badge("india"))

    def test_ping_members_and_refresh_commands_are_available(self):
        self.bot.handle_message({
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 1},
            "text": "/ping",
        })
        self.assertIn("Pong!", self.api_calls[-1][1]["text"])

        def fake_members(method, parameters=None):
            self.api_calls.append((method, parameters))
            if method == "getChatMembersCount":
                return 24
            return {"message_id": 78}

        self.bot.api = fake_members
        self.bot.handle_message({
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 1},
            "text": "/members",
        })
        self.assertIn("Group members: 24", self.api_calls[-1][1]["text"])

        self.bot.create_lobby({"id": -101, "type": "supergroup"}, {"id": 1, "first_name": "Host"})
        self.bot.handle_message({
            "chat": {"id": -101, "type": "supergroup"},
            "from": {"id": 1},
            "text": "/refresh",
        })
        self.assertIn("Choose the home team", self.api_calls[-1][1]["text"])

    def test_unknown_command_gets_helpful_response(self):
        self.bot.handle_message({
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 1},
            "text": "/newmatchplease",
        })
        self.assertIn("Unknown command", self.api_calls[-1][1]["text"])

    def test_newmatch_does_not_discard_an_active_match(self):
        self.bot.create_lobby(
            {"id": -100, "type": "supergroup"},
            {"id": 1, "first_name": "Host"},
        )
        current_game = self.bot.sessions[-100]
        self.bot.handle_message({
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 2, "first_name": "Other member"},
            "text": "/newmatch",
        })
        self.assertIs(self.bot.sessions[-100], current_game)
        self.assertIn("Use /cancel", self.api_calls[-1][1]["text"])

    def test_cancel_is_host_only_and_help_does_not_reset_match(self):
        self.bot.create_lobby(
            {"id": -100, "type": "supergroup"},
            {"id": 1, "first_name": "Host"},
        )
        current_game = self.bot.sessions[-100]
        self.bot.handle_message({
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 2},
            "text": "/cancel",
        })
        self.assertIs(self.bot.sessions[-100], current_game)
        self.assertIn("Only the match host", self.api_calls[-1][1]["text"])

        self.bot.handle_message({
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 2},
            "text": "/help",
        })
        self.assertIs(self.bot.sessions[-100], current_game)
        self.assertIn("/newmatch", self.api_calls[-1][1]["text"])

    def test_targeted_command_is_ignored_before_bot_username_is_known(self):
        self.bot.username = None
        self.bot.handle_message({
            "chat": {"id": -100, "type": "supergroup"},
            "from": {"id": 1},
            "text": "/newmatch@cricket_simulator_bot",
        })
        self.assertNotIn(-100, self.bot.sessions)

    def test_team_selection_falls_back_to_new_message_if_edit_fails(self):
        self.bot.create_lobby(
            {"id": -100, "type": "supergroup"},
            {"id": 1, "first_name": "Home captain"},
        )
        fallback_messages = []

        def fail_edit_but_send(method, parameters=None):
            if method == "editMessageText":
                raise RuntimeError("edit failed")
            fallback_messages.append((method, parameters))
            return {"message_id": 88}

        self.bot.api = fail_edit_but_send
        self.callback("team:home:india", user_id=1)
        game = self.bot.sessions[-100]
        self.assertEqual(game["stage"], "join")
        self.assertEqual(game["message_id"], 88)
        self.assertEqual(fallback_messages[-1][0], "sendMessage")
        self.assertIn("Another group member", fallback_messages[-1][1]["text"])

    def test_expired_callback_ack_does_not_block_player_selection(self):
        self.bot.create_lobby(
            {"id": -100, "type": "supergroup"},
            {"id": 1, "first_name": "Home captain"},
        )
        self.bot.sessions[-100]["stage"] = "home_xi"
        self.bot.sessions[-100]["teams"]["home"] = "india"
        self.bot.sessions[-100]["captains"]["away"] = 2
        self.bot.api = lambda method, parameters=None: (
            (_ for _ in ()).throw(RuntimeError("callback query is too old"))
            if method == "answerCallbackQuery"
            else True
        )

        self.callback("xi:home:0", user_id=1)

        self.assertEqual(self.bot.sessions[-100]["lineups"]["home"], [0])

    def test_full_lobby_setup_and_captain_permissions(self):
        self.create_ready_match()
        game = self.bot.sessions[-100]
        self.assertEqual(game["stage"], "live")
        self.assertEqual(game["teams"], {"home": "india", "away": "australia"})
        self.assertEqual(len(game["lineups"]["home"]), 11)
        self.assertEqual(len(game["lineups"]["away"]), 11)
        self.assertEqual(game["overs_limit"], 2)
        self.assertEqual(game["pitch"], "green")
        self.assertEqual(game["batting_side"], "home")
        self.assertIn("0/0 (0.0/2 overs)", self.api_calls[-1][1]["text"])
        self.callback("bat:aggressive", user_id=2)
        self.assertIn("Only the batting captain", self.api_calls[-1][1]["text"])

    def test_captains_play_delivery_and_scorecard_updates(self):
        self.create_ready_match()
        self.callback("bat:ultra", user_id=1)

        def fake_delivery(match, batting_style, bowling_style, pitch_modifiers):
            match["balls"] += 1
            match["score"] += 4
            return {"outcome": "four", "runs": 4, "over": "0.1"}

        with patch("src.bot.simulate_ball", side_effect=fake_delivery):
            self.callback("bowl:knuckle", user_id=2)

        game = self.bot.sessions[-100]
        self.assertEqual(game["phase"], "bat")
        self.assertIn("Score: 4/0 (0.1/2 overs)", self.api_calls[-1][1]["text"])
        self.assertIn("Run rate (RR/CRR): 24.00 runs/over", self.api_calls[-1][1]["text"])
        self.assertIn("Batting SR: 400.00", self.api_calls[-1][1]["text"])
        self.assertIn("FOUR!", self.api_calls[-1][1]["text"])
        self.assertIn("R(B)", self.api_calls[-1][1]["text"])

    def test_wicket_is_counted_once_in_scorecard(self):
        self.create_ready_match()
        self.callback("bat:aggressive", user_id=1)

        def fake_wicket(match, batting_style, bowling_style, pitch_modifiers):
            match["balls"] += 1
            match["wickets"] += 1
            return {"outcome": "wicket", "runs": 0, "over": "0.1"}

        with patch("src.bot.simulate_ball", side_effect=fake_wicket):
            self.callback("bowl:variation", user_id=2)
        self.assertIn("Score: 0/1 (0.1/2 overs)", self.api_calls[-1][1]["text"])
        innings = self.bot.sessions[-100]["innings"]
        self.assertEqual(innings["striker"], 2)
        self.assertEqual(innings["non_striker"], 1)

    def test_odd_runs_rotate_batting_strike(self):
        self.create_ready_match()
        self.callback("bat:rotate", user_id=1)

        def fake_single(match, batting_style, bowling_style, pitch_modifiers):
            match["balls"] += 1
            match["score"] += 1
            return {"outcome": "one", "runs": 1, "over": "0.1"}

        with patch("src.bot.simulate_ball", side_effect=fake_single):
            self.callback("bowl:defensive", user_id=2)
        innings = self.bot.sessions[-100]["innings"]
        self.assertEqual(innings["striker"], 1)
        self.assertEqual(innings["non_striker"], 0)

    def test_match_transitions_to_chase_and_declares_winner(self):
        self.create_ready_match()
        self.callback("bat:aggressive", user_id=1)

        def finish_first_innings(match, batting_style, bowling_style, pitch_modifiers):
            match["balls"] = 12
            match["score"] = 24
            return {"outcome": "four", "runs": 4, "over": "2.0"}

        with patch("src.bot.simulate_ball", side_effect=finish_first_innings):
            self.callback("bowl:aggressive", user_id=2)
        game = self.bot.sessions[-100]
        self.assertEqual(game["stage"], "innings_break")
        self.assertEqual(game["results"][0]["score"], 24)

        self.callback("second-innings", user_id=1)
        self.assertEqual(game["batting_side"], "away")
        self.assertIn("Required RR (RRR): 12.50", self.api_calls[-1][1]["text"])
        self.callback("bat:aggressive", user_id=2)

        def reach_target(match, batting_style, bowling_style, pitch_modifiers):
            match["balls"] += 1
            match["score"] = 25
            return {"outcome": "one", "runs": 1, "over": "0.1"}

        with patch("src.bot.simulate_ball", side_effect=reach_target):
            self.callback("bowl:aggressive", user_id=1)
        self.assertEqual(game["stage"], "complete")
        self.assertIn("Australia win by 10 wickets!", self.api_calls[-1][1]["text"])
        text = self.api_calls[-1][1]["text"]
        self.assertIn("🏆 <b>MATCH NET RUN RATE (NRR)</b>", text)
        self.assertIn("🟢🥇 <b>Best NRR:</b> 🌍 Australia <b>+138.00</b>", text)
        self.assertIn("🔴 🌍 India <b>-138.00</b>", text)

    def test_match_nrr_section_marks_a_tie_without_selecting_a_leader(self):
        self.create_ready_match()
        game = self.bot.sessions[-100]
        game["innings_number"] = 2
        game["results"] = [
            {"side": "home", "team": "India", "score": 24, "wickets": 0, "balls": 12},
            {"side": "away", "team": "Australia", "score": 24, "wickets": 0, "balls": 12},
        ]
        game["stage"] = "complete"
        text = self.bot.scorecard_text(game, "The match is tied!")

        self.assertIn("🤝 Net run rate is tied", text)
        self.assertNotIn("🥇 Best NRR:", text)


if __name__ == "__main__":
    unittest.main()
