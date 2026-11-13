"""Group multiplayer cricket bot implemented with Python's standard library."""

import html
import hmac
import json
import os
import random
import re
import secrets
import sqlite3
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .teams import PITCHES, TEAMS

API_BASE = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 5
OUTCOME_TEXT = {
    "wicket": "OUT! The bowler gets the breakthrough.",
    "dot": "Dot ball.",
    "one": "1 run.",
    "two": "2 runs.",
    "four": "FOUR!",
    "six": "SIX!",
}
PITCH_TEXT = {
    "balanced": "Balanced pitch",
    "batting": "Batting paradise",
    "green": "Green seamer",
    "spin": "Turning track",
}

# Batter milestones (runs -> label)
BATTER_MILESTONES = {
    50: "🔥 FIFTY",
    100: "💯 CENTURY",
    150: "🚀 150 RUNS",
    200: "👑 DOUBLE CENTURY",
}
# Team total milestones
TEAM_MILESTONES = (50, 100, 150, 200, 250, 300)
# Partnership milestones
PARTNERSHIP_MILESTONES = (50, 100, 150, 200)
# Bowler wicket-haul milestones (wickets -> label)
BOWLER_MILESTONES = {
    3: "🎳 3-WICKET HAUL",
    5: "🏆 5-WICKET HAUL",
}
# Safe limit (Telegram's hard limit is 4096 characters per message)
SCORECARD_SAFE_LENGTH = 3900


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() and not key.strip().startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def build_https_context() -> ssl.SSLContext:
    verify_ssl = os.environ.get("TELEGRAM_SSL_VERIFY", "true").strip().lower() not in {"0", "false", "no"}
    if verify_ssl:
        return ssl.create_default_context()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class TelegramBot:
    def __init__(self, token: str, database_path: str | Path | None = None):
        self.token = token
        self.ssl_context = build_https_context()
        self.database = sqlite3.connect(
            database_path or Path(__file__).resolve().parent.parent / "bot_state.sqlite3"
        )
        self.database.execute(
            "CREATE TABLE IF NOT EXISTS matches (chat_id INTEGER PRIMARY KEY, game_json TEXT NOT NULL)"
        )
        self.sessions = {
            chat_id: json.loads(game_json)
            for chat_id, game_json in self.database.execute(
                "SELECT chat_id, game_json FROM matches"
            )
        }
        self.offset = 0
        self.username = None
        self.started_at = time.time()
        self.callback_acknowledged = False
        self.callback_chat_id = None

    def persist_sessions(self) -> None:
        with self.database:
            self.database.execute("DELETE FROM matches")
            self.database.executemany(
                "INSERT INTO matches (chat_id, game_json) VALUES (?, ?)",
                [
                    (chat_id, json.dumps(game, ensure_ascii=False))
                    for chat_id, game in self.sessions.items()
                ],
            )

    def api(self, method: str, parameters: dict | None = None):
        payload = json.dumps(parameters or {}).encode("utf-8")
        request = Request(
            API_BASE.format(token=self.token, method=method),
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=40, context=self.ssl_context) as response:
                result = json.loads(response.read())
        except HTTPError as error:
            try:
                error_body = json.loads(error.read())
                description = error_body.get("description", "unknown Telegram API error")
            except (json.JSONDecodeError, UnicodeDecodeError):
                description = "Telegram returned an unreadable error response"
            raise RuntimeError(f"Telegram API {method} failed (HTTP {error.code}): {description}") from error
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API {method} failed: {result.get('description', 'unknown error')}")
        return result["result"]

    def reply(
        self,
        chat_id: int,
        text: str,
        keyboard: list | None = None,
        parse_mode: str | None = None,
    ):
        parameters = {"chat_id": chat_id, "text": text}
        if keyboard:
            parameters["reply_markup"] = {"inline_keyboard": keyboard}
        if parse_mode:
            parameters["parse_mode"] = parse_mode
        return self.api("sendMessage", parameters)

    def edit(
        self,
        game: dict,
        text: str,
        keyboard: list | None = None,
        parse_mode: str | None = None,
    ) -> None:
        if not game.get("message_id"):
            sent = self.reply(game["chat_id"], text, keyboard, parse_mode)
            if isinstance(sent, dict):
                game["message_id"] = sent.get("message_id")
            return
        parameters = {
            "chat_id": game["chat_id"],
            "message_id": game["message_id"],
            "text": text,
            "reply_markup": {"inline_keyboard": keyboard or []},
        }
        if parse_mode:
            parameters["parse_mode"] = parse_mode
        try:
            self.api("editMessageText", parameters)
        except RuntimeError as error:
            if "message is not modified" in str(error).casefold():
                return
            if "HTTP 429" in str(error):
                print(
                    "Could not redraw the match message because Telegram rate-limited edits.",
                    file=sys.stderr,
                )
                return
            print(f"Could not update the match message; posting a fresh one instead: {error}", file=sys.stderr)
            sent = self.reply(game["chat_id"], text, keyboard, parse_mode)
            if isinstance(sent, dict):
                game["message_id"] = sent.get("message_id")

    def button(self, text: str, data: str) -> dict:
        return {"text": text, "callback_data": data}

    @staticmethod
    def league_name(league: str | None) -> str:
        if league == "ipl":
            return "IPL Franchise"
        return "International"

    def mode_badge(self, league: str | None) -> str:
        if league == "ipl":
            return "🏆 IPL Franchise"
        return "🌍 International Cricket"

    def team_badge(self, team_key: str | None) -> str:
        if not team_key or team_key not in TEAMS:
            return "🏏 Team"
        team = TEAMS[team_key]
        return f"{team.get('logo', '🏏')} {team['name']}"

    def mode_keyboard(self) -> list:
        return [[
            self.button("🌍 International Cricket", "mode:international"),
            self.button("🏆 IPL Franchise", "mode:ipl"),
        ]]

    def team_keyboard(self, side: str, league: str | None = None) -> list:
        selected_league = league or "international"
        mode_row = [
            self.button(
                ("✅ " if selected_league == "international" else "") + "🌍 International Cricket",
                "mode:international",
            ),
            self.button(
                ("✅ " if selected_league == "ipl" else "") + "🏆 IPL Franchise",
                "mode:ipl",
            ),
        ]
        teams = [
            (key, team)
            for key, team in TEAMS.items()
            if team.get("league", "international") == selected_league
        ]
        if not teams:
            teams = list(TEAMS.items())
        rows = [mode_row]
        for index in range(0, len(teams), 2):
            rows.append([
                self.button(
                    f"{TEAMS[key].get('logo', '🏏')} {TEAMS[key]['name']}",
                    f"team:{side}:{key}",
                )
                for key, _ in teams[index:index + 2]
            ])
        return rows

    @staticmethod
    def normalize_lineup(lineup: list[int] | None) -> list[int]:
        if not lineup:
            return []
        seen = set()
        normalized = []
        for player_index in lineup:
            try:
                player_index = int(player_index)
            except (TypeError, ValueError):
                continue
            if 0 <= player_index and player_index not in seen:
                seen.add(player_index)
                normalized.append(player_index)
        return sorted(normalized)

    def lineup_keyboard(self, game: dict, side: str) -> list:
        team = TEAMS[game["teams"][side]]
        selected = self.normalize_lineup(game["lineups"].get(side))
        game["lineups"][side] = selected
        rows = []
        for index in range(0, len(team["players"]), 2):
            row = []
            for player_index in range(index, min(index + 2, len(team["players"]))):
                player = team["players"][player_index]
                mark = "✅ " if player_index in selected else ""
                row.append(self.button(f"{mark}{player}", f"xi:{side}:{player_index}"))
            rows.append(row)
        rows.append([self.button(
            f"Confirm XI ({len(selected)}/11)",
            f"xi-confirm:{side}",
        )])
        return rows

    def overs_keyboard(self) -> list:
        return [
            [self.button("🟢 5 overs", "overs:5"), self.button("🔵 10 overs", "overs:10")],
            [self.button("🟣 20 overs", "overs:20"), self.button("⚡ 2 overs", "overs:2")],
        ]

    def pitch_keyboard(self) -> list:
        return [
            [self.button("⚖️ Balanced", "pitch:balanced"), self.button("🏏 Batting paradise", "pitch:batting")],
            [self.button("🟢 Green seamer", "pitch:green"), self.button("🌀 Turning track", "pitch:spin")],
        ]

    def batting_keyboard(self) -> list:
        return [
            [self.button("🔥 Aggressive", "bat:aggressive"), self.button("🛡 Defensive", "bat:defensive")],
            [self.button("🚀 Ultra aggressive", "bat:ultra"), self.button("🎯 Hit gaps", "bat:gaps")],
            [self.button("🔄 Rotate strike", "bat:rotate")],
        ]

    def bowling_keyboard(self) -> list:
        return [
            [self.button("⚡ Aggressive", "bowl:aggressive"), self.button("🛡 Defensive", "bowl:defensive")],
            [self.button("🌀 Variation", "bowl:variation"), self.button("🎭 Knuckle ball", "bowl:knuckle")],
        ]

    # ------------------------------------------------------------------
    # Toss
    # ------------------------------------------------------------------
    def toss_call_keyboard(self) -> list:
        return [[
            self.button("🪙 Heads", "toss-call:heads"),
            self.button("🪙 Tails", "toss-call:tails"),
        ]]

    def toss_choice_keyboard(self) -> list:
        return [[
            self.button("🏏 Bat first", "toss-choice:bat"),
            self.button("🎳 Bowl first", "toss-choice:bowl"),
        ]]

    def toss_prompt(self, game: dict) -> str:
        return (
            f"🪙 TOSS TIME!\n"
            f"{self.mode_badge(game.get('league'))}\n"
            f"{game['captain_names']['home']} vs {game['captain_names'].get('away') or 'Opponent'}\n\n"
            f"{game['captain_names']['home']}, call it in the air:"
        )

    def toss_summary(self, game: dict) -> str:
        toss = game.get("toss")
        if not toss:
            return ""
        winner_name = game["captain_names"][toss["winner"]]
        text = (
            f"🪙 {game['captain_names']['home']} called {toss['call'].title()}. "
            f"It's {toss['result'].title()}!\n"
            f"🏆 {winner_name} won the toss"
        )
        if toss.get("decision"):
            text += f" and chose to {toss['decision']} first."
        else:
            text += "."
        return text

    def apply_toss_decision(self, game: dict, decision: str) -> None:
        toss = game["toss"]
        toss["decision"] = decision
        winner = toss["winner"]
        loser = "away" if winner == "home" else "home"
        game["first_batting"] = winner if decision == "bat" else loser
        game["stage"] = "home_team"
        self.edit(
            game,
            f"{self.toss_summary(game)}\n\n"
            f"{game['captain_names']['home']}, choose your team "
            f"({self.league_name(game.get('league'))} mode):",
            self.team_keyboard("home", game.get("league")),
        )
 
    @staticmethod
    def player_label(user: dict) -> str:
        return user.get("first_name") or user.get("username") or str(user.get("id", "Captain"))

    @staticmethod
    def _format_duration(total_seconds: int) -> str:
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        days, hours = divmod(hours, 24)
        parts = []
        if days:
            parts.append(f"{days}d")
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if seconds or not parts:
            parts.append(f"{seconds}s")
        return " ".join(parts)

    @staticmethod
    def ordinal(number: int) -> str:
        if 10 <= number % 100 <= 20:
            suffix = "th"
        else:
            suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
        return f"{number}{suffix}"

    def create_lobby(self, chat: dict, user: dict, league: str | None = None) -> None:
        self.create_match_lobby(chat, user, league, vs_bot=False)

    def create_match_lobby(
        self,
        chat: dict,
        user: dict,
        league: str | None,
        vs_bot: bool,
    ) -> None:
        chat_id = chat["id"]
        if not vs_bot and chat.get("type") not in ("group", "supergroup"):
            self.reply(chat_id, "Add this bot to a Telegram group to start a multiplayer match.")
            return
        if not user.get("id"):
            self.reply(chat_id, "I couldn't identify the captain. Please start the match from your own account.")
            return
        selected_league = league if league in ("international", "ipl") else "international"
        game = {
            "chat_id": chat_id,
            "stage": "join",
            "host": user.get("id"),
            "captains": {"home": user.get("id"), "away": None},
            "captain_names": {
                "home": self.player_label(user),
                "away": "Cricket AI" if vs_bot else None,
            },
            "teams": {},
            "lineups": {"home": [], "away": []},
            "message_id": None,
            "league": selected_league,
            "vs_bot": vs_bot,
            "toss": None,
            "first_batting": "home",
        }
        self.sessions[chat_id] = game
        if vs_bot:
            # Single player: straight to the toss.
            game["stage"] = "toss_call"
            self.edit(game, self.toss_prompt(game), self.toss_call_keyboard())
            return
        self.edit(
            game,
            f"🏏 {game['captain_names']['home']} is hosting a multiplayer "
            f"{self.mode_badge(game['league'])} match.\n"
            "Another group member should join as away captain:",
            [[self.button("Join as away captain", "join")]],
        )

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        if self.callback_acknowledged:
            if text and self.callback_chat_id is not None:
                self.reply(self.callback_chat_id, f"⚠️ {text}")
            return
        self.callback_acknowledged = True
        parameters = {"callback_query_id": callback_id}
        if text:
            parameters["text"] = text
        try:
            self.api("answerCallbackQuery", parameters)
        except RuntimeError as error:
            # An expired callback should not prevent its match action from being processed.
            print(f"Could not acknowledge Telegram callback {callback_id}: {error}", file=sys.stderr)

    @staticmethod
    def is_current_screen(game: dict, message: dict) -> bool:
        message_id = message.get("message_id")
        if game.get("message_id") is None and message_id is not None:
            game["message_id"] = message_id
        return message_id is not None and message_id == game.get("message_id")

    def handle_message(self, message: dict) -> None:
        chat = message["chat"]
        chat_id = chat["id"]
        text = message.get("text", "").strip()
        command_match = re.match(r"^/([A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?(?=\s|$)", text)
        if not command_match:
            return
        command = command_match.group(1).lower()
        target = command_match.group(2)
        if target and self.username and target.casefold() != self.username.casefold():
            return
        if target and not self.username:
            return
        user = message.get("from") or {}

        if command in ("start", "help"):
            self.reply(
                chat_id,
                "Cricket Simulator supports multiplayer and single-player Vs Bot. "
                "In groups, use /newmatch, /playint or /playipl; for solo games, use /vsbot or /vsbotipl. "
                "Flow: toss, choose teams, set overs and pitch, pick your XIs, then play ball by ball "
                "with live milestones and partnerships. "
                "Use /ping to test uptime, /members to see the group size, and /refresh to redraw the current board. Use /cancel to cancel a lobby or match.",
            )
        elif command == "ping":
            uptime_seconds = int(time.time() - self.started_at)
            uptime_text = self._format_duration(uptime_seconds)
            self.reply(chat_id, f"Pong! Bot is online. Uptime: {uptime_text}.")
        elif command == "members":
            try:
                count = self.api("getChatMembersCount", {"chat_id": chat_id})
            except (RuntimeError, URLError, TimeoutError) as error:
                self.reply(chat_id, f"I could not fetch group members right now: {error}")
                return
            self.reply(chat_id, f"Group members: {count}")
        elif command == "refresh":
            game = self.sessions.get(chat_id)
            if not game:
                self.reply(chat_id, "There is no active match or lobby to refresh.")
                return
            self.refresh_game(game)
        elif command in ("playint", "newmatch"):
            current_game = self.sessions.get(chat_id)
            if current_game and current_game.get("stage") != "complete":
                self.reply(chat_id, "A match is already in progress. Use /cancel before starting another.")
                return
            self.create_lobby(chat, user, "international")
        elif command == "playipl":
            current_game = self.sessions.get(chat_id)
            if current_game and current_game.get("stage") != "complete":
                self.reply(chat_id, "A match is already in progress. Use /cancel before starting another.")
                return
            self.create_lobby(chat, user, "ipl")
        elif command in ("vsbot", "vsbotipl"):
            current_game = self.sessions.get(chat_id)
            if current_game and current_game.get("stage") != "complete":
                self.reply(chat_id, "A match is already in progress. Use /cancel before starting another.")
                return
            league = "ipl" if command == "vsbotipl" else "international"
            self.create_match_lobby(chat, user, league, vs_bot=True)
        elif command == "cancel":
            game = self.sessions.get(chat_id)
            if not game:
                self.reply(chat_id, "There is no match to cancel.")
            elif game.get("host") != user.get("id"):
                self.reply(chat_id, "Only the match host can cancel this match.")
            else:
                self.sessions.pop(chat_id, None)
                self.reply(chat_id, "Match cancelled. Send /playint or /playipl to start another.")
        else:
            self.reply(chat_id, "Unknown command. Use /help to see the available commands.")

    def handle_callback(self, callback: dict) -> None:
        callback_id = callback["id"]
        self.callback_acknowledged = False
        message = callback.get("message", {})
        chat = message.get("chat")
        if not chat:
            self.answer_callback(callback_id)
            return
        chat_id = chat["id"]
        self.callback_chat_id = chat_id
        self.answer_callback(callback_id)
        user = callback.get("from", {})
        user_id = user.get("id")
        data = callback.get("data", "")

        if data == "new":
            self.answer_callback(callback_id)
            self.create_lobby(chat, user)
            return
        if data.startswith("mode:"):
            league = data.partition(":")[2]
            if league not in ("international", "ipl"):
                self.answer_callback(callback_id, "Choose a valid match format.")
                return
            game = self.sessions.get(chat_id)
            if not game or game["stage"] not in ("home_team", "away_team"):
                self.answer_callback(callback_id, "No team selection is active to change the mode.")
                return
            if not self.is_current_screen(game, message):
                self.answer_callback(callback_id, "This screen is out of date. Use /refresh for the current options.")
                return
            side = "home" if game["stage"] == "home_team" else "away"
            if game["captains"].get(side) != user_id:
                self.answer_callback(callback_id, "Only the active captain can change the match mode.")
                return
            game["league"] = league
            self.answer_callback(callback_id)
            self.edit(
                game,
                f"✨ {self.mode_badge(league)} selected.\nChoose the {side} team for this roster:",
                self.team_keyboard(side, league),
            )
            return
        if data == "join":
            game = self.sessions.get(chat_id)
            if not game or game["stage"] != "join":
                self.answer_callback(callback_id, "This match is not waiting for a captain.")
                return
            if not self.is_current_screen(game, message):
                self.answer_callback(callback_id, "This screen is out of date. Use /refresh for the current options.")
                return
            if user_id == game["host"]:
                self.answer_callback(callback_id, "A different group member must join as the away captain.")
                return
            game["captains"]["away"] = user_id
            game["captain_names"]["away"] = self.player_label(user)
            game["stage"] = "toss_call"
            self.answer_callback(callback_id)
            self.edit(
                game,
                f"🧢 {game['captain_names']['away']} joined as away captain.\n\n"
                + self.toss_prompt(game),
                self.toss_call_keyboard(),
            )
            return

        game = self.sessions.get(chat_id)
        if not game:
            self.answer_callback(callback_id, "No active match. Send /playint or /playipl in a group.")
            return
        if not self.is_current_screen(game, message):
            self.answer_callback(callback_id, "This screen is out of date. Use /refresh for the current options.")
            return

        # ---------------- Toss ----------------
        if data.startswith("toss-call:"):
            call = data.partition(":")[2]
            if game["stage"] != "toss_call" or user_id != game["host"]:
                self.answer_callback(callback_id, "Only the host can call the toss.")
                return
            if call not in ("heads", "tails"):
                self.answer_callback(callback_id, "Call heads or tails.")
                return
            result = random.choice(["heads", "tails"])
            winner = "home" if call == result else "away"
            game["toss"] = {"call": call, "result": result, "winner": winner, "decision": None}
            self.answer_callback(callback_id)
            if game.get("vs_bot") and winner == "away":
                self.apply_toss_decision(game, random.choice(["bat", "bowl"]))
                return
            game["stage"] = "toss_decision"
            winner_name = game["captain_names"][winner]
            self.edit(
                game,
                f"{self.toss_summary(game)}\n\n{winner_name}, choose to bat or bowl first:",
                self.toss_choice_keyboard(),
            )
            return

        if data.startswith("toss-choice:"):
            decision = data.partition(":")[2]
            if game["stage"] != "toss_decision":
                self.answer_callback(callback_id, "The toss decision is no longer active.")
                return
            winner = game["toss"]["winner"]
            if game["captains"][winner] != user_id:
                self.answer_callback(callback_id, "Only the toss winner can choose.")
                return
            if decision not in ("bat", "bowl"):
                self.answer_callback(callback_id, "Choose to bat or bowl.")
                return
            self.answer_callback(callback_id)
            self.apply_toss_decision(game, decision)
            return

        # ---------------- Teams ----------------
        if data.startswith("team:"):
            _, side, team_key = data.split(":", 2)
            expected_stage = f"{side}_team"
            if side not in ("home", "away") or game["stage"] != expected_stage:
                self.answer_callback(callback_id, "That team selection is no longer active.")
                return
            if game["captains"][side] != user_id:
                self.answer_callback(callback_id, "Only that side's captain can choose its team.")
                return
            if team_key not in TEAMS:
                self.answer_callback(callback_id, "Unknown team.")
                return
            if TEAMS[team_key].get("league", "international") != game.get("league", "international"):
                self.answer_callback(callback_id, f"Choose a team from the {self.league_name(game.get('league'))} selection.")
                return
            if side == "away" and team_key == game["teams"].get("home"):
                self.answer_callback(callback_id, "Choose a different team from the home side.")
                return
            self.answer_callback(callback_id)
            game["teams"][side] = team_key
            if side == "home":
                if game.get("vs_bot"):
                    candidates = [
                        key for key, team in TEAMS.items()
                        if team.get("league", "international") == game.get("league")
                        and key != team_key
                    ]
                    if not candidates:
                        self.answer_callback(callback_id, "No opponent team is available.")
                        return
                    away_team = random.choice(candidates)
                    game["teams"]["away"] = away_team
                    game["lineups"]["away"] = sorted(
                        random.sample(range(len(TEAMS[away_team]["players"])), 11)
                    )
                    game["stage"] = "overs"
                    self.edit(
                        game,
                        f"🏠 Your team: {self.team_badge(team_key)}\n"
                        f"🤖 Opponent: {self.team_badge(away_team)}\n\n"
                        "Choose the match length:",
                        self.overs_keyboard(),
                    )
                    return
                game["stage"] = "away_team"
                self.edit(
                    game,
                    f"🏠 Home team: {self.team_badge(team_key)} (captain: {game['captain_names']['home']})\n\n"
                    f"{game['captain_names']['away']}, choose the away team:",
                    self.team_keyboard("away", game.get("league")),
                )
            else:
                game["stage"] = "overs"
                self.edit(
                    game,
                    f"🏠 Home: {self.team_badge(game['teams']['home'])}\n"
                    f"✈️ Away: {self.team_badge(team_key)}\n\n"
                    "Both teams are set. Host, choose the match length:",
                    self.overs_keyboard(),
                )
            return

        # ---------------- Overs ----------------
        if data.startswith("overs:"):
            if game["stage"] != "overs" or user_id != game["host"]:
                self.answer_callback(callback_id, "Only the host can choose overs at this stage.")
                return
            try:
                game["overs_limit"] = int(data.partition(":")[2])
                create_match(game["overs_limit"])
            except ValueError:
                self.answer_callback(callback_id, "Choose a valid over limit.")
                return
            game["stage"] = "pitch"
            self.answer_callback(callback_id)
            self.edit(game, "Choose the pitch for this match:", self.pitch_keyboard())
            return

        # ---------------- Pitch (now BEFORE the XI selection) ----------------
        if data.startswith("pitch:"):
            pitch = data.partition(":")[2]
            if game["stage"] != "pitch" or user_id != game["host"]:
                self.answer_callback(callback_id, "Only the host can choose the pitch at this stage.")
                return
            if pitch not in PITCHES:
                self.answer_callback(callback_id, "Choose a valid pitch.")
                return
            game["pitch"] = pitch
            game["stage"] = "home_xi"
            self.answer_callback(callback_id)
            home_key = game["teams"]["home"]
            self.edit(
                game,
                f"🏟 Pitch: {PITCHES[pitch]['name']} • {game['overs_limit']} overs\n\n"
                f"{game['captain_names']['home']}, select exactly 11 players for "
                f"{self.team_badge(home_key)} "
                f"({len(game['lineups'].get('home', []))}/11 selected):",
                self.lineup_keyboard(game, "home"),
            )
            return

        # ---------------- XI selection ----------------
        if data.startswith("xi:"):
            _, side, raw_index = data.split(":", 2)
            if side not in ("home", "away") or game["stage"] != f"{side}_xi":
                self.answer_callback(callback_id, "That lineup selection is no longer active.")
                return
            if game["captains"][side] != user_id:
                self.answer_callback(callback_id, "Only that side's captain can select its XI.")
                return
            try:
                player_index = int(raw_index)
                roster_size = len(TEAMS[game["teams"][side]]["players"])
            except (ValueError, KeyError):
                self.answer_callback(callback_id, "Invalid player selection.")
                return
            if not 0 <= player_index < roster_size:
                self.answer_callback(callback_id, "Invalid player selection.")
                return
            lineup = self.normalize_lineup(game["lineups"].get(side))
            if player_index in lineup:
                lineup.remove(player_index)
            elif len(lineup) < 11:
                lineup.append(player_index)
            else:
                self.answer_callback(callback_id, "Your XI already has 11 players. Unselect one to change it.")
                return
            game["lineups"][side] = self.normalize_lineup(lineup)
            self.answer_callback(callback_id)
            captain = game["captain_names"][side]
            self.edit(
                game,
                f"{captain}, select exactly 11 players for {TEAMS[game['teams'][side]]['name']} "
                f"({len(game['lineups'][side])}/11 selected):",
                self.lineup_keyboard(game, side),
            )
            return

        if data.startswith("xi-confirm:"):
            side = data.partition(":")[2]
            if side not in ("home", "away") or game["stage"] != f"{side}_xi":
                self.answer_callback(callback_id, "That lineup selection is no longer active.")
                return
            if game["captains"][side] != user_id:
                self.answer_callback(callback_id, "Only that side's captain can confirm its XI.")
                return
            valid_lineup = self.normalize_lineup(game["lineups"].get(side))
            if len(valid_lineup) != 11:
                self.answer_callback(callback_id, "Select exactly 11 players first.")
                return
            game["lineups"][side] = valid_lineup
            self.answer_callback(callback_id)
            if side == "home" and not game.get("vs_bot"):
                game["stage"] = "away_xi"
                self.edit(
                    game,
                    f"{game['captain_names']['away']}, select exactly 11 players for "
                    f"{self.team_badge(game['teams']['away'])} "
                    f"({len(game['lineups'].get('away', []))}/11 selected):",
                    self.lineup_keyboard(game, "away"),
                )
                return
            # Vs bot (home confirmed) or away confirmed -> everything is ready.
            game["stage"] = "ready"
            self.edit(
                game,
                self.match_preview(game) + "\nHost, start the match when both captains are ready:",
                [[self.button("Start match", "start-match")]],
            )
            return

        if data == "start-match":
            if game["stage"] != "ready" or user_id != game["host"]:
                self.answer_callback(callback_id, "Only the host can start the match.")
                return
            self.answer_callback(callback_id)
            game["innings_number"] = 1
            game["batting_side"] = game.get("first_batting", "home")
            game["results"] = []
            self.start_innings(game)
            return

        if data == "second-innings":
            if game["stage"] != "innings_break" or user_id != game["host"]:
                self.answer_callback(callback_id, "The host starts the second innings.")
                return
            self.answer_callback(callback_id)
            game["innings_number"] = 2
            game["batting_side"] = "away" if game["batting_side"] == "home" else "home"
            game["target"] = game["results"][0]["score"] + 1
            self.start_innings(game)
            return

        if data.startswith("bat:"):
            self.handle_batting_choice(callback_id, game, user_id, data.partition(":")[2])
            return
        if data.startswith("bowl:"):
            self.handle_bowling_choice(callback_id, game, user_id, data.partition(":")[2])
            return

        self.answer_callback(callback_id, "Unknown action.")

    def match_preview(self, game: dict) -> str:
        home, away = game["teams"]["home"], game["teams"]["away"]
        home_team = TEAMS[home]
        away_team = TEAMS[away]
        lines = [
            f"🏏 {home_team.get('logo', '🏏')} {home_team['name']} vs {away_team['name']} {away_team.get('logo', '🏏')}",
            f"Mode: {'Vs Bot • ' if game.get('vs_bot') else ''}{self.mode_badge(game.get('league'))}",
        ]
        toss = game.get("toss")
        if toss and toss.get("decision"):
            winner_team = TEAMS[game["teams"][toss["winner"]]]["name"]
            lines.append(f"🪙 Toss: {winner_team} won and chose to {toss['decision']} first")
        lines.append(f"{len(game['lineups']['home'])} players selected for each side")
        lines.append(f"Format: {game['overs_limit']} overs per innings")
        lines.append(f"Pitch: {PITCHES[game['pitch']]['name']}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def new_partnership() -> dict:
        return {"runs": 0, "balls": 0, "contrib": {}}

    @staticmethod
    def new_innings(lineup: list[str], overs_limit: int) -> dict:
        return {
            "match": create_match(overs_limit),
            "batters": [
                {"name": name, "runs": 0, "balls": 0, "fours": 0, "sixes": 0, "out": False}
                for name in lineup
            ],
            "bowlers": {},
            "next_batter": 2,
            "striker": 0,
            "non_striker": 1,
            "bowler_rotation": [],
            "bowler_index": 0,
            "last_wicket": None,
            "commentary": [],
            "partnership": {"runs": 0, "balls": 0, "contrib": {}},
            "partnerships": [],
            "milestones": [],
            "milestone_alert": [],
        }

    def start_innings(self, game: dict) -> None:
        side = game["batting_side"]
        team_key = game["teams"][side]
        batting_xi = [TEAMS[team_key]["players"][index] for index in game["lineups"][side]]
        game["innings"] = self.new_innings(batting_xi, game["overs_limit"])
        bowling_side = self.bowling_side(game)
        bowling_team_key = game["teams"][bowling_side]
        bowling_xi = [TEAMS[bowling_team_key]["players"][index] for index in game["lineups"][bowling_side]]
        game["innings"]["bowler_rotation"] = bowling_xi
        game["innings"]["bowler_index"] = 0
        game["innings"]["last_bowler"] = bowling_xi[0]
        game["stage"] = "live"
        game["phase"] = "bat"
        self.advance_vs_bot_turn(game)

    def batting_side_name(self, game: dict) -> str:
        return TEAMS[game["teams"][game["batting_side"]]]["name"]

    def bowling_side(self, game: dict) -> str:
        return "away" if game["batting_side"] == "home" else "home"

    def innings_over(self, game: dict) -> bool:
        return is_match_over(game["innings"]["match"]) or (
            game["innings_number"] == 2 and game["innings"]["match"]["score"] >= game["target"]
        )

    def handle_batting_choice(self, callback_id: str, game: dict, user_id: int, style: str) -> None:
        if game.get("stage") != "live" or game.get("phase") != "bat":
            self.answer_callback(callback_id, "Wait for the batting captain's turn.")
            return
        if game["captains"][game["batting_side"]] != user_id:
            self.answer_callback(callback_id, "Only the batting captain can choose this ball.")
            return
        if style not in BATTING_STYLES:
            self.answer_callback(callback_id, "Unknown batting style.")
            return
        game["batting_style"] = style
        game["phase"] = "bowl"
        self.answer_callback(callback_id)
        self.advance_vs_bot_turn(game)

    def handle_bowling_choice(self, callback_id: str, game: dict, user_id: int, style: str) -> None:
        if game.get("stage") != "live" or game.get("phase") != "bowl":
            self.answer_callback(callback_id, "Wait for the bowling captain's turn.")
            return
        side = self.bowling_side(game)
        if game["captains"][side] != user_id:
            self.answer_callback(callback_id, "Only the bowling captain can choose this ball.")
            return
        if style not in BOWLING_STYLES:
            self.answer_callback(callback_id, "Unknown bowling style.")
            return

        self.answer_callback(callback_id)
        self.play_delivery(game, style)

    def ai_batting_style(self, game: dict) -> str:
        match = game["innings"]["match"]
        overs_left = max(0, game["overs_limit"] * 6 - match["balls"])
        if match["wickets"] >= 7:
            return "defensive"
        if game["innings_number"] == 2:
            runs_needed = max(0, game["target"] - match["score"])
            required_rate = runs_needed * 6 / overs_left if overs_left else float("inf")
            if required_rate >= 12:
                return "ultra"
            if required_rate >= 8:
                return "aggressive"
            return "gaps" if required_rate > 5 else "rotate"
        if overs_left <= 12:
            return "aggressive"
        return "gaps"

    def ai_bowling_style(self, game: dict) -> str:
        innings = game["innings"]
        match = innings["match"]
        current_rate = self.run_rate(match["score"], match["balls"])
        if game["pitch"] == "green":
            return "knuckle"
        if game["pitch"] == "spin":
            return "variation"
        if current_rate >= 9:
            return "defensive"
        if match["wickets"] >= 4:
            return "aggressive"
        return "variation"

    def advance_vs_bot_turn(self, game: dict) -> None:
        if game.get("stage") != "live" or not game.get("vs_bot"):
            self.render_scorecard(game)
            return
        if game["phase"] == "bat" and game["batting_side"] == "away":
            game["batting_style"] = self.ai_batting_style(game)
            game["phase"] = "bowl"
        elif game["phase"] == "bowl" and self.bowling_side(game) == "away":
            self.play_delivery(game, self.ai_bowling_style(game))
            return
        self.render_scorecard(game)

    def play_delivery(self, game: dict, style: str) -> None:
        innings = game["innings"]
        match = innings["match"]
        side = self.bowling_side(game)
        bowler_team = TEAMS[game["teams"][side]]["players"]
        bowler_xi = [bowler_team[index] for index in game["lineups"][side]]
        rotation = innings.get("bowler_rotation") or bowler_xi
        current_index = innings.get("bowler_index", 0) % len(rotation)
        bowler_name = rotation[current_index]

        # Make sure older saved matches have the new tracking fields.
        innings.setdefault("partnership", self.new_partnership())
        innings["partnership"].setdefault("contrib", {})
        innings.setdefault("partnerships", [])
        innings.setdefault("milestones", [])

        score_before = match["score"]

        delivery = simulate_ball(
            match,
            game["batting_style"],
            style,
            PITCHES[game["pitch"]]["modifiers"],
        )

        events: list[str] = []
        batter = innings["batters"][innings["striker"]]
        non_striker_name = innings["batters"][innings["non_striker"]]["name"]
        runs_before = batter["runs"]

        batter["balls"] += 1
        batter["runs"] += delivery["runs"]
        if delivery["runs"] == 4:
            batter["fours"] += 1
        elif delivery["runs"] == 6:
            batter["sixes"] += 1
        bowler = innings["bowlers"].setdefault(
            bowler_name, {"balls": 0, "runs": 0, "wickets": 0}
        )
        bowler["balls"] += 1
        bowler["runs"] += delivery["runs"]

        # --- Batter milestones: 50 / 100 / 150 / 200 ---
        for threshold, label in BATTER_MILESTONES.items():
            if runs_before < threshold <= batter["runs"]:
                events.append(
                    f"{label}: {batter['name']} reaches {threshold} off {batter['balls']} balls"
                )

        # --- Team milestones: 50 / 100 / 150 / 200 ... ---
        for threshold in TEAM_MILESTONES:
            if score_before < threshold <= match["score"]:
                events.append(f"📈 {self.batting_side_name(game)} reach {threshold}")

        # --- Partnership tracking ---
        partnership = innings["partnership"]
        partnership_before = partnership["runs"]
        partnership["runs"] += delivery["runs"]
        partnership["balls"] += 1
        contrib = partnership["contrib"]
        contrib[batter["name"]] = contrib.get(batter["name"], 0) + delivery["runs"]
        contrib.setdefault(non_striker_name, 0)
        for threshold in PARTNERSHIP_MILESTONES:
            if partnership_before < threshold <= partnership["runs"]:
                events.append(
                    f"🤝 {threshold}-run partnership: {batter['name']} & {non_striker_name}"
                )

        if delivery["outcome"] == "wicket":
            batter["out"] = True
            bowler["wickets"] += 1
            innings["last_wicket"] = batter["name"]

            # --- Bowler milestones: 3-wicket haul / 5-wicket haul ---
            if bowler["wickets"] in BOWLER_MILESTONES:
                events.append(
                    f"{BOWLER_MILESTONES[bowler['wickets']]}: {bowler_name} "
                    f"{bowler['wickets']}/{bowler['runs']}"
                )

            # --- Close the partnership ---
            innings["partnerships"].append({
                "wicket": match["wickets"],
                "runs": partnership["runs"],
                "balls": partnership["balls"],
                "pair": [batter["name"], non_striker_name],
                "contrib": dict(contrib),
                "unbroken": False,
            })
            innings["partnership"] = self.new_partnership()

            if innings["next_batter"] < len(innings["batters"]):
                innings["striker"] = innings["next_batter"]
                innings["next_batter"] += 1
            if match["balls"] % 6 == 0:
                innings["striker"], innings["non_striker"] = (
                    innings["non_striker"],
                    innings["striker"],
                )
        else:
            if delivery["runs"] % 2:
                innings["striker"], innings["non_striker"] = (
                    innings["non_striker"],
                    innings["striker"],
                )
            if match["balls"] % 6 == 0:
                innings["striker"], innings["non_striker"] = (
                    innings["non_striker"],
                    innings["striker"],
                )

        commentary = self.ball_commentary(
            bowler_name, batter["name"], delivery["outcome"], delivery["runs"]
        )
        innings["last_ball"] = (
            f"{BATTING_STYLES[game['batting_style']]['label']} vs "
            f"{BOWLING_STYLES[style]['label']}: {OUTCOME_TEXT[delivery['outcome']]}"
        )
        innings["commentary"].append({"over": delivery["over"], "text": commentary})
        del innings["commentary"][:-6]

        # Milestone alert for this ball + permanent milestone log.
        innings["milestone_alert"] = events
        for event in events:
            innings["milestones"].append({"over": delivery["over"], "text": event})
        del innings["milestones"][:-40]

        innings["last_bowler"] = bowler_name
        if match["balls"] % 6 == 0:
            innings["bowler_index"] = (innings.get("bowler_index", 0) + 1) % len(rotation)
        else:
            innings["bowler_index"] = current_index

        if self.innings_over(game):
            self.finish_innings(game)
        else:
            game["phase"] = "bat"
            self.advance_vs_bot_turn(game)

    @staticmethod
    def ball_commentary(
        bowler: str, batter: str, outcome: str, runs: int
    ) -> str:
        if outcome == "wicket":
            return f"OUT! {bowler} beats {batter}; the batter is gone."
        if outcome == "six":
            return f"SIX! {batter} launches {bowler} over the ropes."
        if outcome == "four":
            return f"FOUR! {batter} finds the boundary off {bowler}."
        if outcome == "dot":
            return f"{bowler} keeps it tight; {batter} can't score."
        if runs == 1:
            return f"{batter} nudges a single off {bowler}."
        return f"{batter} races back for {runs} runs off {bowler}."

    @staticmethod
    def match_result_text(game: dict) -> str:
        first, second = game["results"]
        if second["score"] > first["score"]:
            margin = 10 - second["wickets"]
            return f"{second['team']} win by {margin} wickets!"
        if second["score"] < first["score"]:
            return f"{first['team']} win by {first['score'] - second['score']} runs!"
        return "The match is tied!"

    def finish_innings(self, game: dict) -> None:
        innings = game["innings"]
        match = innings["match"]
        side = game["batting_side"]

        # An unbroken partnership at the end of the innings still counts.
        partnership = innings.get("partnership")
        if partnership and partnership.get("balls") and match["wickets"] < 10:
            striker = innings["batters"][innings["striker"]]["name"]
            non_striker = innings["batters"][innings["non_striker"]]["name"]
            innings.setdefault("partnerships", []).append({
                "wicket": match["wickets"] + 1,
                "runs": partnership["runs"],
                "balls": partnership["balls"],
                "pair": [striker, non_striker],
                "contrib": dict(partnership.get("contrib", {})),
                "unbroken": True,
            })
            innings["partnership"] = self.new_partnership()

        partnerships = innings.get("partnerships", [])
        best_partnership = max(partnerships, key=lambda p: p["runs"], default=None)

        game["results"].append({
            "side": side,
            "team": TEAMS[game["teams"][side]]["name"],
            "score": match["score"],
            "wickets": match["wickets"],
            "balls": match["balls"],
            "milestones": [item["text"] for item in innings.get("milestones", [])],
            "best_partnership": best_partnership,
        })
        if game["innings_number"] == 1:
            game["stage"] = "innings_break"
            self.render_scorecard(game)
            return

        game["stage"] = "complete"
        self.render_scorecard(game, self.match_result_text(game))

    @staticmethod
    def run_rate(runs: int, balls: int) -> float:
        return runs * 6 / balls if balls else 0.0

    @staticmethod
    def batter_strike_rate(batter: dict) -> float:
        return batter["runs"] * 100 / batter["balls"] if batter["balls"] else 0.0

    @staticmethod
    def batter_badge(runs: int) -> str:
        if runs >= 200:
            return " 👑"
        if runs >= 150:
            return " 🚀"
        if runs >= 100:
            return " 💯"
        if runs >= 50:
            return " 🔥"
        return ""

    @staticmethod
    def bowler_badge(wickets: int) -> str:
        if wickets >= 5:
            return " 🏆5W"
        if wickets >= 3:
            return " 🎳3W"
        return ""

    def match_net_run_rates(self, game: dict) -> dict[str, float] | None:
        if len(game.get("results", [])) != 2:
            return None

        results = {result["side"]: result for result in game["results"]}
        net_run_rates = {}
        for side in ("home", "away"):
            batting = results[side]
            bowling = results["away" if side == "home" else "home"]
            batting_overs = (
                game["overs_limit"] if batting["wickets"] == 10 else batting["balls"] / 6
            )
            bowling_overs = (
                game["overs_limit"] if bowling["wickets"] == 10 else bowling["balls"] / 6
            )
            net_run_rates[side] = (
                self.run_rate(batting["score"], batting_overs * 6)
                - self.run_rate(bowling["score"], bowling_overs * 6)
            )
        return net_run_rates

    def scorecard_text(
        self,
        game: dict,
        final_result: str | None = None,
        compact: bool = False,
    ) -> str:
        escape = html.escape
        innings = game["innings"]
        match = innings["match"]
        team_name = self.batting_side_name(game)
        overs = game["overs_limit"]
        striker = innings["batters"][innings["striker"]]
        non_striker = innings["batters"][innings["non_striker"]]
        rotation = innings.get("bowler_rotation") or []
        current_index = innings.get("bowler_index", 0) % len(rotation) if rotation else 0
        current_bowler = rotation[current_index] if rotation else innings.get("last_bowler") or "To be assigned"
        next_bowler = rotation[(current_index + 1) % len(rotation)] if rotation else "To be assigned"
        bowler_stats = innings["bowlers"].get(
            current_bowler, {"balls": 0, "runs": 0, "wickets": 0}
        )
        current_run_rate = self.run_rate(match["score"], match["balls"])

        lines = [
            f"🏏 <b>{escape(TEAMS[game['teams']['home']]['name'])} vs "
            f"{escape(TEAMS[game['teams']['away']]['name'])}</b>",
            f"✨ <b>{game['innings_number']}{'st' if game['innings_number'] == 1 else 'nd'} INNINGS</b>"
            f"  •  {escape(team_name)}",
            f"🟢 <b>Score: {match['score']}/{match['wickets']} "
            f"({format_overs(match['balls'])}/{overs} overs)</b>",
            f"📈 <b>Run rate (RR/CRR): {current_run_rate:.2f} runs/over</b>",
        ]
        if game.get("target"):
            runs_needed = max(0, game["target"] - match["score"])
            balls_remaining = max(0, overs * 6 - match["balls"])
            required_run_rate = (
                runs_needed * 6 / balls_remaining if balls_remaining else 0.0
            )
            lines.append(
                f"🎯 <b>Target: {game['target']}  •  Need {runs_needed} runs"
                f"  •  Required RR (RRR): {required_run_rate:.2f}</b>"
            )
        for index, result in enumerate(game.get("results", []), start=1):
            lines.append(
                f"📋 {index}{'st' if index == 1 else 'nd'} innings: "
                f"{escape(result['team'])} <b>{result['score']}/{result['wickets']}</b> "
                f"<code>({format_overs(result['balls'])}/{overs} overs)</code>"
            )
        lines.append(f"🏟 Pitch: {escape(PITCHES[game['pitch']]['name'])}")
        toss = game.get("toss")
        if toss and toss.get("decision") and not compact:
            winner_team = TEAMS[game["teams"][toss["winner"]]]["name"]
            lines.append(
                f"🪙 Toss: {escape(winner_team)} won, chose to {toss['decision']}"
            )

        # Live milestone banner for the ball just played.
        if game["stage"] == "live":
            for alert in innings.get("milestone_alert", []):
                lines.append(f"🎉 <b>{escape(alert)}</b>")

        lines.append(
            f"🏏 <b>Strike:</b> {escape(striker['name'])}* "
            f"{striker['runs']}({striker['balls']}){self.batter_badge(striker['runs'])} | "
            f"<b>Non-striker:</b> {escape(non_striker['name'])} "
            f"{non_striker['runs']}({non_striker['balls']}){self.batter_badge(non_striker['runs'])}"
        )
        lines.append(
            f"⚡ <b>Batting SR: {self.batter_strike_rate(striker):.2f} | "
            f"Non-striker SR: {self.batter_strike_rate(non_striker):.2f}</b>"
        )

        # Current partnership.
        partnership = innings.get("partnership") or self.new_partnership()
        contrib = partnership.get("contrib", {})
        lines.append(
            f"🤝 <b>Partnership:</b> <b>{partnership['runs']}</b> runs "
            f"({partnership['balls']} balls) "
            f"<code>{escape(striker['name'])} {contrib.get(striker['name'], 0)}"
            f" • {escape(non_striker['name'])} {contrib.get(non_striker['name'], 0)}</code>"
        )

        lines.append(
            f"🎳 <b>Current bowler:</b> {escape(current_bowler)} "
            f"<code>{format_overs(bowler_stats['balls'])}-{bowler_stats['runs']}-{bowler_stats['wickets']}</code>"
            f"{self.bowler_badge(bowler_stats['wickets'])} "
            f"(Econ {self.run_rate(bowler_stats['runs'], bowler_stats['balls']):.2f})"
            f" | <b>Next bowler:</b> {escape(next_bowler)}"
        )
        if innings.get("last_wicket"):
            lines.append(f"🧤 Last wicket: {escape(innings['last_wicket'])}")
        if innings.get("last_ball"):
            lines.append(f"💥 Last ball: {escape(innings['last_ball'])}")
        commentary = innings.get("commentary", [])
        if commentary and not compact:
            lines.append("\n<b>🎙 BALL-BY-BALL COMMENTARY</b>")
            lines.extend(
                f"<code>{escape(item['over'])}</code> {escape(item['text'])}"
                for item in commentary
            )
        lines.append("\n<b>📊 BATTERS</b>  <code>R(B)   4s 6s</code>")
        for index, batter in enumerate(innings["batters"]):
            if (
                batter["balls"] == 0
                and not batter["out"]
                and index not in (innings["striker"], innings["non_striker"])
            ):
                continue
            marker = "*" if index == innings["striker"] and not batter["out"] else ""
            if batter["out"]:
                marker = " out"
            lines.append(
                f"{escape(batter['name'])}{marker}  <b>{batter['runs']}({batter['balls']})</b>"
                f"   {batter['fours']}  {batter['sixes']}{self.batter_badge(batter['runs'])}"
            )
        lines.append("\n<b>🎯 BOWLING</b>")
        if innings["bowlers"]:
            for name, stats in innings["bowlers"].items():
                lines.append(
                    f"{escape(name)}: "
                    f"<code>{format_overs(stats['balls'])}-{stats['runs']}-{stats['wickets']}</code>"
                    f"{self.bowler_badge(stats['wickets'])}"
                )
        else:
            lines.append("No balls bowled yet.")

        if not final_result and not compact:
            # Completed partnerships for the live / break view.
            partnerships = innings.get("partnerships", [])
            if partnerships:
                lines.append("\n<b>🤝 PARTNERSHIPS</b>")
                for item in partnerships[-10:]:
                    suffix = "*" if item.get("unbroken") else ""
                    lines.append(
                        f"{self.ordinal(item['wicket'])} wkt: "
                        f"{escape(item['pair'][0])} &amp; {escape(item['pair'][1])} "
                        f"<b>{item['runs']}{suffix}</b>({item['balls']})"
                    )
            # Milestone log.
            milestones = innings.get("milestones", [])
            if milestones:
                lines.append("\n<b>🏅 MILESTONES</b>")
                for item in milestones[-5:]:
                    lines.append(f"<code>{escape(item['over'])}</code> {escape(item['text'])}")

        if game["stage"] == "innings_break":
            lines.append(f"\nInnings complete: {match['score']}/{match['wickets']}.")
            lines.append("The other captain will chase.")
        elif final_result:
            lines.append(f"\n🏁 <b>{escape(final_result)}</b>")
            if not compact:
                # Match-wide highlights from both innings.
                for number, result in enumerate(game.get("results", []), start=1):
                    highlights = result.get("milestones") or []
                    best = result.get("best_partnership")
                    if not highlights and not best:
                        continue
                    lines.append(
                        f"\n<b>🏅 {self.ordinal(number)} INNINGS HIGHLIGHTS</b> "
                        f"({escape(result['team'])})"
                    )
                    if best:
                        lines.append(
                            f"🤝 Best partnership: {escape(best['pair'][0])} &amp; "
                            f"{escape(best['pair'][1])} <b>{best['runs']}</b>({best['balls']})"
                            f"{'*' if best.get('unbroken') else ''}"
                        )
                    for text in highlights[-6:]:
                        lines.append(escape(text))
            net_run_rates = self.match_net_run_rates(game)
            if net_run_rates is not None:
                lines.append("\n━━━━━━━━━━━━━━━━━━")
                lines.append("🏆 <b>MATCH NET RUN RATE (NRR)</b>")
                home_nrr = net_run_rates["home"]
                away_nrr = net_run_rates["away"]
                if home_nrr == away_nrr:
                    lines.append("🤝 Net run rate is tied")
                    lines.append(
                        f"{escape(self.team_badge(game['teams']['home']))}: <b>{home_nrr:+.2f}</b>"
                    )
                    lines.append(
                        f"{escape(self.team_badge(game['teams']['away']))}: <b>{away_nrr:+.2f}</b>"
                    )
                else:
                    leading_side = "home" if home_nrr > away_nrr else "away"
                    trailing_side = "away" if leading_side == "home" else "home"
                    lines.append(
                        f"🟢🥇 <b>Best NRR:</b> "
                        f"{escape(self.team_badge(game['teams'][leading_side]))} "
                        f"<b>{net_run_rates[leading_side]:+.2f}</b>"
                    )
                    lines.append(
                        f"🔴 {escape(self.team_badge(game['teams'][trailing_side]))} "
                        f"<b>{net_run_rates[trailing_side]:+.2f}</b>"
                    )
                lines.append("━━━━━━━━━━━━━━━━━━")
        elif game["stage"] == "live":
            active_side = game["batting_side"] if game["phase"] == "bat" else self.bowling_side(game)
            role = "Batting" if game["phase"] == "bat" else "Bowling"
            captain = game["captain_names"][active_side]
            lines.append(f"\n🎮 <b>{role} captain:</b> {escape(captain)}")
            lines.append(
                "Choose batting approach:" if game["phase"] == "bat" else "Choose bowling style:"
            )
        return "\n".join(lines)

    def live_keyboard(self, game: dict) -> list | None:
        if game["stage"] == "innings_break":
            return [[self.button("Start second innings", "second-innings")]]
        if game["stage"] != "live":
            return None
        return self.batting_keyboard() if game["phase"] == "bat" else self.bowling_keyboard()

    def render_scorecard(self, game: dict, final_result: str | None = None) -> None:
        text = self.scorecard_text(game, final_result)
        if len(text) > SCORECARD_SAFE_LENGTH:
            # Telegram messages are capped at 4096 characters; fall back to a compact card.
            text = self.scorecard_text(game, final_result, compact=True)
        self.edit(
            game,
            text,
            self.live_keyboard(game),
            parse_mode="HTML",
        )

    def refresh_game(self, game: dict) -> None:
        stage = game.get("stage")
        if stage == "join":
            self.edit(
                game,
                f"🏏 {game['captain_names']['home']} is hosting a multiplayer "
                f"{self.mode_badge(game.get('league'))} match.\n"
                "Another group member should join as away captain:",
                [[self.button("Join as away captain", "join")]],
            )
            return
        if stage == "toss_call":
            self.edit(game, self.toss_prompt(game), self.toss_call_keyboard())
            return
        if stage == "toss_decision":
            winner_name = game["captain_names"][game["toss"]["winner"]]
            self.edit(
                game,
                f"{self.toss_summary(game)}\n\n{winner_name}, choose to bat or bowl first:",
                self.toss_choice_keyboard(),
            )
            return
        if stage == "home_team":
            self.edit(
                game,
                f"{self.toss_summary(game)}\n\n"
                f"{game['captain_names']['home']}, choose the home team ({self.league_name(game['league'])} mode):",
                self.team_keyboard("home", game.get("league")),
            )
            return
        if stage == "away_team":
            self.edit(
                game,
                f"🏠 Home team: {self.team_badge(game['teams']['home'])}\n\n"
                f"{game['captain_names']['away']}, choose the away team:",
                self.team_keyboard("away", game.get("league")),
            )
            return
        if stage == "overs":
            self.edit(game, "Both teams are set. Host, choose the match length:", self.overs_keyboard())
            return
        if stage == "pitch":
            self.edit(game, "Choose the pitch for this match:", self.pitch_keyboard())
            return
        if stage == "home_xi":
            self.edit(
                game,
                f"{game['captain_names']['home']}, select exactly 11 players for {self.team_badge(game['teams']['home'])} ({len(game['lineups'].get('home', []))}/11 selected):",
                self.lineup_keyboard(game, "home"),
            )
            return
        if stage == "away_xi":
            self.edit(
                game,
                f"{game['captain_names']['away']}, select exactly 11 players for {self.team_badge(game['teams']['away'])} ({len(game['lineups'].get('away', []))}/11 selected):",
                self.lineup_keyboard(game, "away"),
            )
            return
        if stage == "ready":
            self.edit(
                game,
                self.match_preview(game) + "\nHost, start the match when both captains are ready:",
                [[self.button("Start match", "start-match")]],
            )
            return
        if stage in ("live", "innings_break", "complete"):
            final_result = self.match_result_text(game) if stage == "complete" else None
            self.render_scorecard(game, final_result)
            return
        self.reply(game["chat_id"], "This screen cannot be refreshed right now.")

    def handle_update(self, update: dict) -> None:
        try:
            message = update.get("message")
            if message:
                self.handle_message(message)
                return
            callback = update.get("callback_query")
            if callback:
                self.handle_callback(callback)
        finally:
            self.persist_sessions()

    def create_webhook_server(self, webhook_secret: str) -> HTTPServer:
        bot = self

        class WebhookHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/health":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"ok")

            def do_POST(self) -> None:
                if self.path != "/telegram":
                    self.send_error(404)
                    return
                provided_secret = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
                if not hmac.compare_digest(provided_secret, webhook_secret):
                    self.send_error(403)
                    return
                try:
                    content_length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    self.send_error(400)
                    return
                if content_length <= 0 or content_length > 1_000_000:
                    self.send_error(413)
                    return
                try:
                    update = json.loads(self.rfile.read(content_length))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self.send_error(400)
                    return
                if not isinstance(update, dict):
                    self.send_error(400)
                    return
                try:
                    bot.handle_update(update)
                except (RuntimeError, URLError, KeyError, ValueError) as error:
                    print(f"Could not handle Telegram webhook update: {error}", file=sys.stderr)
                    if "HTTP 429" in str(error):
                        self.send_response(200)
                        self.end_headers()
                        return
                    self.send_error(500)
                    return
                self.send_response(200)
                self.end_headers()

        return HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))), WebhookHandler)

    def run_webhook(self, hostname: str) -> None:
        webhook_secret = secrets.token_urlsafe(32)
        server = self.create_webhook_server(webhook_secret)
        try:
            self.api(
                "setWebhook",
                {
                    "url": f"https://{hostname}/telegram",
                    "secret_token": webhook_secret,
                    "allowed_updates": ["message", "callback_query"],
                },
            )
            print("Cricket Simulator bot is running with Telegram webhooks.", flush=True)
            server.serve_forever()
        finally:
            server.server_close()

    def run(self) -> None:
        bot_info = self.api("getMe")
        self.username = bot_info.get("username")
        if not self.username:
            raise RuntimeError("Telegram getMe did not return this bot's username.")
        hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
        if hostname:
            self.run_webhook(hostname)
            return
        try:
            pending = self.api(
                "getUpdates",
                {
                    "offset": -1,
                    "timeout": 0,
                    "allowed_updates": ["message", "callback_query"],
                },
            )
            if pending:
                self.offset = pending[-1]["update_id"] + 1
        except (RuntimeError, URLError, TimeoutError) as error:
            print(f"Could not clear stale Telegram updates at startup: {error}", file=sys.stderr)
        print("Cricket Simulator bot is running.", flush=True)
        while True:
            try:
                updates = self.api(
                    "getUpdates",
                    {
                        "offset": self.offset,
                        "timeout": POLL_TIMEOUT,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
                for update in updates:
                    self.offset = update["update_id"] + 1
                    try:
                        self.handle_update(update)
                    except (RuntimeError, URLError, KeyError, ValueError) as error:
                        print(f"Could not handle Telegram update {update['update_id']}: {error}", file=sys.stderr)
            except (RuntimeError, URLError, TimeoutError) as error:
                print(f"Telegram polling error: {error}. Retrying in 5 seconds.", file=sys.stderr)
                time.sleep(5)


def main() -> None:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or token == "put_your_bot_token_here":
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in your environment or .env file before starting the bot.")
    TelegramBot(token).run()


if __name__ == "__main__":
    main()
