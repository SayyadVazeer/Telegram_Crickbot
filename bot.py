import json
import logging
import os
import re
import sqlite3
import asyncio
import hashlib
from typing import Any, Dict, List, Optional

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = ImageDraw = ImageFont = None

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency
    def load_dotenv() -> bool:
        return False

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
except ImportError:  # pragma: no cover - optional dependency
    Update = Any  # type: ignore
    ContextTypes = Any  # type: ignore
    Application = None
    CommandHandler = None
    MessageHandler = None
    filters = None
    InlineKeyboardButton = None
    InlineKeyboardMarkup = None
    CallbackQueryHandler = None

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_USER_IDS = os.getenv("TELEGRAM_ADMIN_USER_IDS", os.getenv("TELEGRAM_ADMIN_USER_IDs", "")).strip()
DATABASE_PATH = os.getenv(
    "DATABASE_PATH",
    "/app/data/tournament.db"
).strip()

async def cancel_match_wait(user_id: int, session_token: int):
    await asyncio.sleep(300)

    # Only cancel if the session hasn't been replaced by a new "Add Match" click
    if _match_session_tokens.get(user_id) == session_token:
        waiting_for_match.pop(user_id, None)
        _match_session_tokens.pop(user_id, None)

def parse_admin_ids(value: str) -> set[int]:
    if not value:
        return set()
    ids: set[int] = set()
    for part in re.split(r"[,;]\s*", value):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return ids


class TournamentState:
    def __init__(self) -> None:
        self.teams: Dict[str, Dict[str, float]] = {}
        self.players: Dict[str, Dict[str, float]] = {}
        self.matches: List[Dict[str, object]] = []
        self.admin_ids: set[int] = set()
        # NRR format is locked from the first recorded match in a tournament.
        self.nrr_balls_per_over: Optional[int] = None
        self.nrr_quota_balls: Optional[int] = None

    def set_admin(self, user_id: int) -> None:
        self.admin_ids.add(user_id)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    def clear_tournament(self) -> None:
        """Clear tournament results while retaining configured admin access."""
        self.teams.clear()
        self.players.clear()
        self.matches.clear()
        self.nrr_balls_per_over = None
        self.nrr_quota_balls = None


    def add_team(self, name: str) -> None:
        if name not in self.teams:
            self.teams[name] = {
                "played": 0,
                "wins": 0,
                "draws": 0,
                "losses": 0,
                "points": 0,
                "runs_scored": 0,
                "runs_conceded": 0,
                "balls_faced": 0,
                "balls_bowled": 0,
                "overs_faced": 0.0,
                "overs_bowled": 0.0,
                "run_rate": 0.0,
                "strike_rate": 0.0,
                "net_run_rate": 0.0,
                "nrr_runs_scored": 0,
                "nrr_runs_conceded": 0,
                "nrr_balls_faced": 0,
                "nrr_balls_bowled": 0,
                "nrr_rate_unit_balls": 6,
            }

    def add_player(self, name: str) -> None:
        if name not in self.players:
            self.players[name] = {
                "runs": 0,
                "wickets": 0,
                "matches": 0,
                "balls_faced": 0,
                "runs_conceded": 0,
                "balls_bowled": 0,
            }

    def apply_match(self, match_data: Dict[str, object]) -> None:
        team1 = str(match_data["team1"])
        team2 = str(match_data["team2"])
        quota_balls = _get_match_quota_balls(match_data)
        balls_per_over = _get_balls_per_over(match_data)
        if self.nrr_balls_per_over is not None and balls_per_over != self.nrr_balls_per_over:
            raise ValueError(
                f"This tournament uses {self.nrr_balls_per_over}-ball overs; "
                f"cannot add a {balls_per_over}-ball match."
            )
        if self.nrr_balls_per_over is None:
            self.nrr_balls_per_over = balls_per_over
            self.nrr_quota_balls = quota_balls
        nrr_rate_unit_balls = self.nrr_balls_per_over
        nrr_quota_balls = int(match_data.get("nrr_quota_balls", self.nrr_quota_balls))
        counts_for_nrr = not bool(match_data.get("exclude_from_nrr", False))
        self.add_team(team1)
        self.add_team(team2)
        self.teams[team1]["nrr_rate_unit_balls"] = nrr_rate_unit_balls
        self.teams[team2]["nrr_rate_unit_balls"] = nrr_rate_unit_balls

        self.teams[team1]["played"] += 1
        self.teams[team2]["played"] += 1

        score1 = int(match_data["score1"])
        score2 = int(match_data["score2"])
        wickets1 = int(match_data["wickets1"])
        wickets2 = int(match_data["wickets2"])
        balls1 = int(match_data["balls1"])
        balls2 = int(match_data["balls2"])
        nrr_score1 = int(match_data.get("nrr_score1", score1))
        nrr_score2 = int(match_data.get("nrr_score2", score2))
        nrr_balls1 = int(match_data.get("nrr_balls1", balls1))
        nrr_balls2 = int(match_data.get("nrr_balls2", balls2))

        self.teams[team1]["runs_scored"] += score1
        self.teams[team1]["runs_conceded"] += score2
        self.teams[team1]["balls_faced"] += balls1
        self.teams[team1]["balls_bowled"] += balls2
        if counts_for_nrr:
            self.teams[team1]["nrr_runs_scored"] += nrr_score1
            self.teams[team1]["nrr_runs_conceded"] += nrr_score2
            self.teams[team1]["nrr_balls_faced"] += nrr_balls1
            self.teams[team1]["nrr_balls_bowled"] += nrr_balls2

        self.teams[team2]["runs_scored"] += score2
        self.teams[team2]["runs_conceded"] += score1
        self.teams[team2]["balls_faced"] += balls2
        self.teams[team2]["balls_bowled"] += balls1
        if counts_for_nrr:
            self.teams[team2]["nrr_runs_scored"] += nrr_score2
            self.teams[team2]["nrr_runs_conceded"] += nrr_score1
            self.teams[team2]["nrr_balls_faced"] += nrr_balls2
            self.teams[team2]["nrr_balls_bowled"] += nrr_balls1

        overs1 = balls1 / balls_per_over
        overs2 = balls2 / balls_per_over
        quota_overs = quota_balls / balls_per_over
        if counts_for_nrr and wickets1 >= 10 and nrr_balls1 < nrr_quota_balls:
            overs1 = quota_overs
            nrr_balls1 = nrr_quota_balls
            self.teams[team1]["nrr_balls_faced"] += nrr_quota_balls - int(match_data.get("nrr_balls1", balls1))
            self.teams[team2]["nrr_balls_bowled"] += nrr_quota_balls - int(match_data.get("nrr_balls1", balls1))
        if counts_for_nrr and wickets2 >= 10 and nrr_balls2 < nrr_quota_balls:
            overs2 = quota_overs
            nrr_balls2 = nrr_quota_balls
            self.teams[team2]["nrr_balls_faced"] += nrr_quota_balls - int(match_data.get("nrr_balls2", balls2))
            self.teams[team1]["nrr_balls_bowled"] += nrr_quota_balls - int(match_data.get("nrr_balls2", balls2))

        self.teams[team1]["overs_faced"] += overs1
        self.teams[team1]["overs_bowled"] += overs2
        self.teams[team2]["overs_faced"] += overs2
        self.teams[team2]["overs_bowled"] += overs1

        player_data = match_data.get("players", {})
        if isinstance(player_data, dict):
            for _, players in player_data.items():
                if not isinstance(players, dict):
                    continue
                for player_name, stats in players.items():
                    if not isinstance(stats, dict):
                        continue
                    raw_name = str(player_name)
                    # Try to find a canonical name via fuzzy matching
                    canonical = _find_canonical_player(raw_name, self.players)
                    safe_name = canonical if canonical else raw_name
                    self.add_player(safe_name)
                    self.players[safe_name]["runs"] += int(stats.get("runs", 0))
                    self.players[safe_name]["wickets"] += int(stats.get("wickets", 0))
                    self.players[safe_name]["matches"] += 1
                    self.players[safe_name]["balls_faced"] += int(stats.get("balls_faced", 0))
                    self.players[safe_name]["runs_conceded"] += int(stats.get("runs_conceded", 0))
                    self.players[safe_name]["balls_bowled"] += int(stats.get("balls_bowled", 0))

        win_points, tie_points = 2, 1
        result_type = str(match_data.get("result_type", "normal")).lower()
        if result_type == "no_result":
            self.teams[team1]["draws"] += 1
            self.teams[team2]["draws"] += 1
            self.teams[team1]["points"] += tie_points
            self.teams[team2]["points"] += tie_points
        elif score1 > score2:
            self.teams[team1]["wins"] += 1
            self.teams[team1]["points"] += win_points
            self.teams[team2]["losses"] += 1
        elif score2 > score1:
            self.teams[team2]["wins"] += 1
            self.teams[team2]["points"] += win_points
            self.teams[team1]["losses"] += 1
        else:
            self.teams[team1]["draws"] += 1
            self.teams[team2]["draws"] += 1
            self.teams[team1]["points"] += tie_points
            self.teams[team2]["points"] += tie_points

        self._refresh_rates()
        self.matches.append(match_data)

    def _refresh_rates(self) -> None:
        for _, stats in self.teams.items():
            balls_faced = float(stats["balls_faced"])
            balls_bowled = float(stats["balls_bowled"])
            overs_faced = float(stats["overs_faced"])
            overs_bowled = float(stats["overs_bowled"])

            run_rate = stats["runs_scored"] / overs_faced if overs_faced else 0.0
            strike_rate = (stats["runs_scored"] / balls_faced * 100.0) if balls_faced else 0.0
            nrr_unit = float(stats["nrr_rate_unit_balls"])
            nrr = nrr_unit * (
                (stats["nrr_runs_scored"] / stats["nrr_balls_faced"] if stats["nrr_balls_faced"] else 0.0)
                - (stats["nrr_runs_conceded"] / stats["nrr_balls_bowled"] if stats["nrr_balls_bowled"] else 0.0)
            )

            stats["run_rate"] = round(run_rate, 2)
            stats["strike_rate"] = round(strike_rate, 2)
            stats["net_run_rate"] = round(nrr, 3)

    def get_standings(self) -> List[Dict[str, object]]:
        standings = []
        for name, stats in self.teams.items():
            standings.append({
                "team": name,
                "played": int(stats["played"]),
                "wins": int(stats["wins"]),
                "draws": int(stats["draws"]),
                "losses": int(stats["losses"]),
                "points": int(stats["points"]),
                "net_run_rate": stats["net_run_rate"],
            })

        standings.sort(key=lambda item: (
            -int(item["points"]),
            -int(item["wins"]),
            -float(item["net_run_rate"]),
        ))
        return standings
class TournamentDatabase:

    def __init__(self, path):
        self._connection_options: Dict[str, Any] = {}
        self._memory_connection = None
        if path == ":memory:":
            self.path = f"file:tournament_{id(self)}?mode=memory&cache=shared"
            self._connection_options = {"uri": True}
            # Keep one connection open so the shared in-memory database persists.
            self._memory_connection = sqlite3.connect(self.path, **self._connection_options)
        else:
            self.path = path
        directory = os.path.dirname(self.path)

        if directory:
            os.makedirs(directory, exist_ok=True)

        with self._connect() as conn:
            conn.executescript("""
            
            CREATE TABLE IF NOT EXISTS standings (
                team TEXT PRIMARY KEY,
                played INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                draws INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                points INTEGER DEFAULT 0,
                runs_scored INTEGER DEFAULT 0,
                runs_conceded INTEGER DEFAULT 0,
                net_run_rate REAL DEFAULT 0,
                balls_faced INTEGER DEFAULT 0,
                balls_bowled INTEGER DEFAULT 0,
                overs_faced REAL DEFAULT 0,
                overs_bowled REAL DEFAULT 0,
                nrr_runs_scored INTEGER DEFAULT 0,
                nrr_runs_conceded INTEGER DEFAULT 0,
                nrr_balls_faced INTEGER DEFAULT 0,
                nrr_balls_bowled INTEGER DEFAULT 0,
                nrr_rate_unit_balls INTEGER DEFAULT 6
            );


            CREATE TABLE IF NOT EXISTS player_stats (
                player TEXT PRIMARY KEY,
                runs INTEGER DEFAULT 0,
                wickets INTEGER DEFAULT 0,
                matches INTEGER DEFAULT 0,
                balls_faced INTEGER DEFAULT 0,
                runs_conceded INTEGER DEFAULT 0,
                balls_bowled INTEGER DEFAULT 0
            );


            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team1 TEXT,
                team2 TEXT,
                score1 INTEGER,
                score2 INTEGER,
                wickets1 INTEGER,
                wickets2 INTEGER,
                balls1 INTEGER,
                balls2 INTEGER,
                data TEXT
            );

            CREATE TABLE IF NOT EXISTS squad_teams (
                name TEXT PRIMARY KEY COLLATE NOCASE
            );

            CREATE TABLE IF NOT EXISTS squads (
                team TEXT NOT NULL COLLATE NOCASE,
                player TEXT NOT NULL,
                price REAL NOT NULL,
                PRIMARY KEY (team, player),
                FOREIGN KEY (team) REFERENCES squad_teams(name)
            );

            CREATE TABLE IF NOT EXISTS team_shortcodes (
                team_name TEXT PRIMARY KEY,
                short_code TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            """)
            for column, definition in (
                ("nrr_runs_scored", "INTEGER DEFAULT 0"),
                ("nrr_runs_conceded", "INTEGER DEFAULT 0"),
                ("nrr_balls_faced", "INTEGER DEFAULT 0"),
                ("nrr_balls_bowled", "INTEGER DEFAULT 0"),
                ("nrr_rate_unit_balls", "INTEGER DEFAULT 6"),
            ):
                try:
                    conn.execute(f"ALTER TABLE standings ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError:
                    pass

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, **self._connection_options)

    def clear_tournament(self):
        with self._connect() as conn:
            conn.execute("DELETE FROM standings")
            conn.execute("DELETE FROM player_stats")
            conn.execute("DELETE FROM matches")
            conn.execute("DELETE FROM squads")
            conn.execute("DELETE FROM squad_teams")
            conn.execute("DELETE FROM team_shortcodes")

    def clear_statistics(self) -> None:
        """Clear derived standings and player stats while retaining match history."""
        with self._connect() as conn:
            conn.execute("DELETE FROM standings")
            conn.execute("DELETE FROM player_stats")

    def delete_last_match(self) -> Optional[Dict[str, object]]:
        with self._connect() as conn:
            row = conn.execute("SELECT id, data FROM matches ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM matches WHERE id = ?", (row[0],))
        return json.loads(row[1])

    def add_team(self, name: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("INSERT OR IGNORE INTO squad_teams (name) VALUES (?)", (name.strip(),))
            return cursor.rowcount == 1

    def add_player(self, team: str, player: str, price: float) -> bool:
        with self._connect() as conn:
            team_row = conn.execute(
                "SELECT name FROM squad_teams WHERE name = ? COLLATE NOCASE", (team.strip(),)
            ).fetchone()
            if team_row is None:
                return False
            cursor = conn.execute(
                "INSERT OR IGNORE INTO squads (team, player, price) VALUES (?, ?, ?)",
                (team_row[0], player.strip(), price),
            )
            return cursor.rowcount == 1

    def get_team_names(self) -> List[str]:
        with self._connect() as conn:
            return [row[0] for row in conn.execute("SELECT name FROM squad_teams ORDER BY name")]

    def get_squads(self) -> List[Dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT team, player, price FROM squads ORDER BY team, player").fetchall()
        return [{"team": row[0], "player": row[1], "price": row[2]} for row in rows]

    def get_team_players(self, team: str) -> List[Dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT player, price FROM squads WHERE team = ? COLLATE NOCASE ORDER BY player", (team.strip(),)
            ).fetchall()
        return [{"player": row[0], "price": row[1]} for row in rows]

    def save_team_shortcodes(self, mappings: Dict[str, str]) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM team_shortcodes")
            for team_name, short_code in mappings.items():
                conn.execute(
                    "INSERT OR REPLACE INTO team_shortcodes (team_name, short_code) VALUES (?, ?)",
                    (team_name, short_code),
                )

    def load_team_shortcodes(self) -> Dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT team_name, short_code FROM team_shortcodes").fetchall()
        return {row[0]: row[1] for row in rows}

    def get_config(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_config(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO config (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = ?",
                (key, value, value),
            )

    def save_match(self, match):

        with self._connect() as conn:

            conn.execute("""
            INSERT INTO matches
            (
            team1,team2,
            score1,score2,
            wickets1,wickets2,
            balls1,balls2,
            data
            )
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
            match["team1"],
            match["team2"],
            match["score1"],
            match["score2"],
            match["wickets1"],
            match["wickets2"],
            match["balls1"],
            match["balls2"],
            json.dumps(match)
            ))
    def save_standing(self,name,stats):

        with self._connect() as conn:

            conn.execute("""
            INSERT INTO standings
        (
            team,
            played,
            wins,
            draws,
            losses,
            points,
            runs_scored,
            runs_conceded,
            net_run_rate,
            balls_faced,
            balls_bowled,
            overs_faced,
            overs_bowled
        )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(team)
            DO UPDATE SET

                        played=?,
            wins=?,
            draws=?,
            losses=?,
            points=?,
            runs_scored=?,
            runs_conceded=?,
            net_run_rate=?,
            balls_faced=?,
            balls_bowled=?,
            overs_faced=?,
            overs_bowled=?

            """,
            (
            name,
            stats["played"],
            stats["wins"],
            stats["draws"],
            stats["losses"],
            stats["points"],
            stats["runs_scored"],
            stats["runs_conceded"],
            stats["net_run_rate"],
            stats["balls_faced"],
            stats["balls_bowled"],
            stats["overs_faced"],
            stats["overs_bowled"],

            stats["played"],
            stats["wins"],
            stats["draws"],
            stats["losses"],
            stats["points"],
            stats["runs_scored"],
            stats["runs_conceded"],
            stats["net_run_rate"],
            stats["balls_faced"],
            stats["balls_bowled"],
            stats["overs_faced"],
            stats["overs_bowled"],
        ))
            conn.execute(
                """UPDATE standings SET nrr_runs_scored=?, nrr_runs_conceded=?, nrr_balls_faced=?,
                   nrr_balls_bowled=?, nrr_rate_unit_balls=? WHERE team=?""",
                (
                    stats["nrr_runs_scored"], stats["nrr_runs_conceded"], stats["nrr_balls_faced"],
                    stats["nrr_balls_bowled"], stats["nrr_rate_unit_balls"], name,
                ),
            )



    def save_player_stat(self,name,stats):

        with self._connect() as conn:

            conn.execute("""
            INSERT INTO player_stats
            VALUES (?,?,?,?,?,?,?)

            ON CONFLICT(player)
            DO UPDATE SET

            runs=?,
            wickets=?,
            matches=?,
            balls_faced=?,
            runs_conceded=?,
            balls_bowled=?

            """,
            (
            name,
            stats["runs"],
            stats["wickets"],
            stats["matches"],
            stats["balls_faced"],
            stats["runs_conceded"],
            stats["balls_bowled"],

            stats["runs"],
            stats["wickets"],
            stats["matches"],
            stats["balls_faced"],
            stats["runs_conceded"],
            stats["balls_bowled"]
            ))
    def load_all(self):
        data = {
            "teams": {},
            "players": {},
            "matches": []
        }

        with self._connect() as conn:

            rows = conn.execute(
                "SELECT * FROM standings"
            ).fetchall()

            for row in rows:
                data["teams"][row[0]] = {
                    "played": row[1],
                    "wins": row[2],
                    "draws": row[3],
                    "losses": row[4],
                    "points": row[5],
                    "runs_scored": row[6],
                    "runs_conceded": row[7],
                    "net_run_rate": row[8],
                    "balls_faced": row[9],
                    "balls_bowled": row[10],
                    "overs_faced": row[11],
                    "overs_bowled": row[12],
                    "run_rate": 0.0,
                    "strike_rate": 0.0,
                    "nrr_runs_scored": row[13] or row[6],
                    "nrr_runs_conceded": row[14] or row[7],
                    "nrr_balls_faced": row[15] or row[9],
                    "nrr_balls_bowled": row[16] or row[10],
                    "nrr_rate_unit_balls": row[17] or 6,
                }

            rows = conn.execute(
                "SELECT * FROM player_stats"
            ).fetchall()

            for row in rows:
                data["players"][row[0]] = {
                    "runs": row[1],
                    "wickets": row[2],
                    "matches": row[3],
                    "balls_faced": row[4],
                    "runs_conceded": row[5],
                    "balls_bowled": row[6],
                }
            rows = conn.execute(
                    "SELECT data FROM matches"
                ).fetchall()

            for row in rows:
                    data["matches"].append(json.loads(row[0]))
        # Older databases did not store the NRR unit. Infer it from recorded
        # matches so existing Hundred standings use five-ball sets on restart.
        if any(_is_hundred_ball_match(match) for match in data["matches"]):
            for stats in data["teams"].values():
                stats["nrr_rate_unit_balls"] = 5
        return data

def _get_match_overs(match_data: Dict[str, object]) -> float:
    if "overs" in match_data and match_data.get("overs") is not None:
        try:
            return float(match_data["overs"])
        except (TypeError, ValueError):
            pass

    match_type = str(match_data.get("match_type", "")).upper()
    if "ODI" in match_type or "ONE DAY" in match_type:
        return 50.0
    if "T20" in match_type or "T20I" in match_type:
        return 20.0
    if "TEST" in match_type:
        return 90.0
    return 20.0


def _is_hundred_ball_match(match_data: Dict[str, object]) -> bool:
    match_type = str(match_data.get("match_type", "")).upper()
    return "HUNDRED" in match_type or "100 BALL" in match_type or "100-BALL" in match_type


def _get_balls_per_over(match_data: Dict[str, object]) -> int:
    """Return the scoring unit used for NRR (five balls in The Hundred)."""
    if _is_hundred_ball_match(match_data):
        return 5
    try:
        configured = int(match_data.get("balls_per_over", 6))
        if configured > 0:
            return configured
    except (TypeError, ValueError):
        pass
    return 6


def _get_match_quota_balls(match_data: Dict[str, object]) -> int:
    """Return the full innings allocation in balls for all-out NRR treatment."""
    try:
        configured = int(match_data.get("balls_per_innings"))
        if configured > 0:
            return configured
    except (TypeError, ValueError):
        pass

    if _is_hundred_ball_match(match_data):
        return 100
    return int(round(_get_match_overs(match_data) * _get_balls_per_over(match_data)))


def _overs_notation_to_balls(value: object, balls_per_over: int = 6) -> int:
    """Convert cricket overs notation (for example, ``18.2``) to balls.

    The digit after the decimal point is a ball count, not a decimal fraction
    of an over.  Keeping totals in balls prevents scorecard inputs from
    introducing small, but material, errors into the tournament NRR.
    """
    text = str(value).strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        raise ValueError(f"Invalid overs value: {value!r}")

    whole, separator, balls = text.partition(".")
    balls_in_over = int(balls) if separator else 0
    if balls_in_over >= balls_per_over:
        raise ValueError(f"Invalid ball count in overs value: {value!r}")
    return int(whole) * balls_per_over + balls_in_over


def _innings_balls(innings: Dict[str, object], payload: Dict[str, object], fallback_key: str) -> int:
    """Read an innings length, preferring an explicit ball count when given."""
    if innings.get("balls") is not None:
        return int(innings["balls"])
    if innings.get("overs") is not None:
        return _overs_notation_to_balls(innings["overs"],_get_balls_per_over(payload))
    return int(payload.get(fallback_key, 0))


def _parse_player_entry(entry: str) -> Optional[Dict[str, int]]:
    entry = entry.strip()
    if not entry:
        return None

    match = re.match(r"^(?P<name>.+?)\s+(?P<runs>-?\d+)(?:\s+(?P<wickets>-?\d+))?$", entry)
    if not match:
        return None

    return {
        "name": match.group("name").strip(),
        "runs": int(match.group("runs")),
        "wickets": int(match.group("wickets")) if match.group("wickets") is not None else 0,
    }


def _normalize_players_payload(players: object) -> Dict[str, Dict[str, Dict[str, int]]]:
    normalized_players: Dict[str, Dict[str, Dict[str, int]]] = {}
    if not isinstance(players, dict):
        return normalized_players

    for team_name, player_entries in players.items():
        if not isinstance(player_entries, dict):
            continue
        normalized_team: Dict[str, Dict[str, int]] = {}
        for player_name, stats in player_entries.items():
            if isinstance(stats, dict):
                normalized_team[str(player_name)] = {
                    "runs": int(stats.get("runs", 0)),
                    "wickets": int(stats.get("wickets", 0)),
                    "balls_faced": int(stats.get("balls_faced", 0)),
                    "runs_conceded": int(stats.get("runs_conceded", 0)),
                    "balls_bowled": int(stats.get("balls_bowled", 0)),
                }
        normalized_players[str(team_name)] = normalized_team
    return normalized_players


def _match_format_metadata(payload: Dict[str, object]) -> Dict[str, object]:
    """Keep format fields needed to calculate an all-out innings correctly."""
    return {
        key: payload[key]
        for key in (
            "match_type", "overs", "balls_per_innings", "balls_per_over",
            "team1_short", "team2_short",
            "nrr_score1", "nrr_balls1", "nrr_score2", "nrr_balls2", "nrr_quota_balls", "exclude_from_nrr", "result_type",
        )
        if key in payload
    }


def _extract_players_from_text(text: str) -> Dict[str, Dict[str, Dict[str, int]]]:
    players_payload: Dict[str, Dict[str, Dict[str, int]]] = {}
    players_section_match = re.search(r"players\s*:\s*(.+)$", text, re.IGNORECASE)
    if not players_section_match:
        return players_payload

    section = players_section_match.group(1).strip()
    team_segments = [segment.strip() for segment in re.split(r"\s*;\s*", section) if segment.strip()]
    for segment in team_segments:
        if ":" not in segment:
            continue
        team_name, entries = segment.split(":", 1)
        team_key = team_name.strip()
        if not team_key:
            continue
        parsed_players: Dict[str, Dict[str, int]] = {}
        for entry in [item.strip() for item in entries.split(",") if item.strip()]:
            parsed = _parse_player_entry(entry)
            if parsed:
                parsed_players[parsed["name"]] = {
                    "runs": parsed["runs"],
                    "wickets": parsed["wickets"],
                    "balls_faced": 0,
                    "runs_conceded": 0,
                    "balls_bowled": 0,
                }
        if parsed_players:
            players_payload[team_key] = parsed_players
    return players_payload


def _extract_json_candidates(text: str) -> List[str]:
    stripped = text.strip()
    if not stripped:
        return []

    candidates: List[str] = []
    code_fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.IGNORECASE | re.DOTALL)
    if code_fence_match:
        candidates.append(code_fence_match.group(1).strip())
    elif stripped.startswith("{") and stripped.endswith("}"):
        candidates.append(stripped)
    return candidates


def _parse_scorecard_text(text: str) -> Optional[Dict[str, object]]:
    innings_matches = list(re.finditer(r"INNINGS\s*(?P<num>\d+):\s*(?P<team>.+?)\s+BATTING", text, re.IGNORECASE))
    if not innings_matches:
        return None

    innings_sections: List[Dict[str, Any]] = []
    for idx, match in enumerate(innings_matches):
        start = match.end()
        end = innings_matches[idx + 1].start() if idx + 1 < len(innings_matches) else len(text)
        innings_sections.append({
            "team": match.group("team").strip(),
            "section": text[start:end],
        })

    if len(innings_sections) < 2:
        return None

    parsed_innings: List[Dict[str, Any]] = []
    for innings in innings_sections:
        total_match = re.search(
            r"TOTAL\s*\((?P<overs>\d+(?:\.\d+)?)\s+overs,\s*(?P<wkts>\d+)\s+wkts\)\s*(?P<runs>\d+)",
            innings["section"],
            re.IGNORECASE,
        )
        if not total_match:
            continue
        score = int(total_match.group("runs"))
        wickets = int(total_match.group("wkts"))
        balls = _overs_notation_to_balls(total_match.group("overs"),5 if "HUNDRED" in text.upper() else 6)
        players_payload: Dict[str, Dict[str, int]] = {}
        bpo = 5 if "HUNDRED" in text.upper() else 6
        for line in innings["section"].splitlines():
            bowler_match = re.match(
                r"^(?P<player>[A-Za-z.\- ]+?)\s{2,}(?P<overs>\d+(?:\.\d+)?)\s+(?P<runs>\d+)\s+(?P<wickets>\d+)(?:\s+(?P<econ>\d+(?:\.\d+)?))?$",
                line.strip(),
            )
            if bowler_match:
                player_name = bowler_match.group("player").strip()
                bowler_balls = _overs_notation_to_balls(bowler_match.group("overs"), bpo)
                if player_name in players_payload:
                    players_payload[player_name]["wickets"] = int(bowler_match.group("wickets"))
                    players_payload[player_name]["runs_conceded"] = int(bowler_match.group("runs"))
                    players_payload[player_name]["balls_bowled"] = bowler_balls
                else:
                    players_payload[player_name] = {
                        "runs": 0,
                        "wickets": int(bowler_match.group("wickets")),
                        "balls_faced": 0,
                        "runs_conceded": int(bowler_match.group("runs")),
                        "balls_bowled": bowler_balls,
                    }
                continue
            # Batter line: name, dismissal, runs, balls, optional SR
            batter_match = re.match(
                r"^(?P<player>.+?)\s{2,}\S.*\s+(?P<runs>\d+)\s+(?P<balls>\d+)(?:\s+(?P<sr>\d+(?:\.\d+)?))?\s*$",
                line.strip(),
            )
            if batter_match:
                player_name = batter_match.group("player").strip()
                if player_name in players_payload:
                    players_payload[player_name]["runs"] = int(batter_match.group("runs"))
                    players_payload[player_name]["balls_faced"] = int(batter_match.group("balls"))
                else:
                    players_payload[player_name] = {
                        "runs": int(batter_match.group("runs")),
                        "wickets": 0,
                        "balls_faced": int(batter_match.group("balls")),
                        "runs_conceded": 0,
                        "balls_bowled": 0,
                    }
        parsed_innings.append({
            "team": innings["team"],
            "score": score,
            "wickets": wickets,
            "balls": balls,
            "players": players_payload,
        })

    if len(parsed_innings) < 2:
        return None

    return {
        "team1": parsed_innings[0]["team"],
        "score1": parsed_innings[0]["score"],
        "wickets1": parsed_innings[0]["wickets"],
        "balls1": parsed_innings[0]["balls"],
        "team2": parsed_innings[1]["team"],
        "score2": parsed_innings[1]["score"],
        "wickets2": parsed_innings[1]["wickets"],
        "balls2": parsed_innings[1]["balls"],
        "players": {
            parsed_innings[0]["team"]: parsed_innings[0]["players"],
            parsed_innings[1]["team"]: parsed_innings[1]["players"],
        },
    }


def parse_match(text: str) -> Optional[Dict[str, object]]:
    stripped = text.strip()

    if not stripped:
        return None

    for candidate in _extract_json_candidates(stripped):
        try:
            payload = json.loads(candidate)
            if isinstance(payload, dict):
                scorecard = payload.get("scorecard")
                if isinstance(scorecard, dict):
                    innings = scorecard.get("innings_1")
                    innings2 = scorecard.get("innings_2")
                    if isinstance(innings, dict) and isinstance(innings2, dict):
                        team1 = str(innings.get("team", payload.get("team1", ""))).strip()
                        team2 = str(innings2.get("team", payload.get("team2", ""))).strip()
                        score1 = int(innings.get("runs", payload.get("score1", 0)))
                        score2 = int(innings2.get("runs", payload.get("score2", 0)))
                        wickets1 = int(innings.get("wickets", payload.get("wickets1", 0)))
                        wickets2 = int(innings2.get("wickets", payload.get("wickets2", 0)))
                        balls1 = _innings_balls(innings, payload, "balls1")
                        balls2 = _innings_balls(innings2, payload, "balls2")
                        players_payload: Dict[str, Dict[str, Dict[str, int]]] = {}
                        for inning in (innings, innings2):
                            inning_team = str(inning.get("team", "")).strip()
                            if not inning_team:
                                continue
                            players_payload[inning_team] = {}
                            batting = inning.get("batting", [])
                            if isinstance(batting, list):
                                for batting_entry in batting:
                                    if not isinstance(batting_entry, dict):
                                        continue
                                    player_name = str(batting_entry.get("player", "")).strip()
                                    if not player_name:
                                        continue
                                    players_payload[inning_team][player_name] = {
                                        "runs": int(batting_entry.get("runs", 0)),
                                        "wickets": 0,
                                        "balls_faced": int(batting_entry.get("balls", 0)),
                                        "runs_conceded": 0,
                                        "balls_bowled": 0,
                                    }
                            bowling = inning.get("bowling", [])
                            if isinstance(bowling, list):
                                for bowling_entry in bowling:
                                    if not isinstance(bowling_entry, dict):
                                        continue
                                    player_name = str(bowling_entry.get("player", "")).strip()
                                    if not player_name:
                                        continue
                                    if player_name not in players_payload[inning_team]:
                                        players_payload[inning_team][player_name] = {
                                            "runs": 0,
                                            "wickets": 0,
                                            "balls_faced": 0,
                                            "runs_conceded": 0,
                                            "balls_bowled": 0,
                                        }
                                    players_payload[inning_team][player_name]["wickets"] += int(bowling_entry.get("wickets", 0))
                                    players_payload[inning_team][player_name]["runs_conceded"] += int(bowling_entry.get("runs", 0))
                        return {
                            **_match_format_metadata(payload),
                            "team1": team1,
                            "score1": score1,
                            "wickets1": wickets1,
                            "balls1": balls1,
                            "team2": team2,
                            "score2": score2,
                            "wickets2": wickets2,
                            "balls2": balls2,
                            "players": players_payload,
                        }

                team1_raw = str(payload.get("team1", "")).strip()
                team2_raw = str(payload.get("team2", "")).strip()
                # Resolve short codes to full team names
                t1_short = str(payload.get("team1_short", "")).strip()
                t2_short = str(payload.get("team2_short", "")).strip()
                team1 = _resolve_team_name(t1_short) if t1_short else team1_raw
                team2 = _resolve_team_name(t2_short) if t2_short else team2_raw
                score1 = int(payload.get("score1", 0))
                score2 = int(payload.get("score2", 0))
                wickets1 = int(payload.get("wickets1", 0))
                wickets2 = int(payload.get("wickets2", 0))
                balls1 = int(payload.get("balls1", 0))
                balls2 = int(payload.get("balls2", 0))
                normalized_players = _normalize_players_payload(payload.get("players", {}))
                return {
                    **_match_format_metadata(payload),
                    "team1": team1,
                    "score1": score1,
                    "wickets1": wickets1,
                    "balls1": balls1,
                    "team2": team2,
                    "score2": score2,
                    "wickets2": wickets2,
                    "balls2": balls2,
                    "players": normalized_players,
                }
        except (TypeError, ValueError, json.JSONDecodeError):
            continue

    compact_match = re.match(
        r"^(?P<team1>.+?)\s+(?P<score1>-?\d+)(?:/(?P<wickets1>\d+))?\s+(?P<balls1>\d+)\s+vs\s+(?P<team2>.+?)\s+(?P<score2>-?\d+)(?:/(?P<wickets2>\d+))?\s+(?P<balls2>\d+)",
        stripped,
        re.IGNORECASE,
    )
    if compact_match:
        players_payload = _extract_players_from_text(stripped)
        t1_raw = compact_match.group("team1").strip()
        t2_raw = compact_match.group("team2").strip()
        return {
            "team1": _resolve_team_name(t1_raw),
            "score1": int(compact_match.group("score1")),
            "wickets1": int(compact_match.group("wickets1") or 0),
            "balls1": int(compact_match.group("balls1")),
            "team2": _resolve_team_name(t2_raw),
            "score2": int(compact_match.group("score2")),
            "wickets2": int(compact_match.group("wickets2") or 0),
            "balls2": int(compact_match.group("balls2")),
            "players": players_payload,
        }

    scorecard_data = _parse_scorecard_text(stripped)
    if scorecard_data:
        return scorecard_data

    return None


def build_match_prompt() -> str:
    if team_mappings:
        mappings_str = ", ".join(f"{code}={name}" for name, code in sorted(team_mappings.items()))
    else:
        mappings_str = "(no team short codes registered yet — use /setshort CODE TeamName to register)"

    instructions = (
        "Generate a single JSON object for the cricket match simulated above. Use the actual team and player names from the match details. "
        "Return only valid JSON with no markdown fences, explanations, or extra text. Required fields: match_type, balls_per_over, balls_per_innings, team1, team2, score1, wickets1, balls1, score2, wickets2, balls2, players, team1_short, team2_short. "
        f"Available team short codes: {mappings_str}. "
        "For each team, set team1_short and team2_short to the registered short code for that team name. "
        "balls1 and balls2 must be integer legal balls actually faced, never overs notation. The first match locks the tournament NRR format: use six-ball overs for T20/ODI (include overs, such as 20 or 50), or five-ball overs for The Hundred (set match_type to 'The Hundred', balls_per_innings to 100, and balls_per_over to 5). Every later match must use that same format. "
        "NRR is cumulative: use actual balls for a successful chase; if all out early, provide the actual balls and wickets=10 so the full allotted quota is applied automatically. "
        "For a DLS-adjusted result, include nrr_score1, nrr_balls1, nrr_score2, nrr_balls2, and nrr_quota_balls with the official NRR-accredited scores, balls, and revised allocation; otherwise omit them. "
        "For an abandoned/no-result match, set result_type to 'no_result' and exclude_from_nrr to true; it awards one point to each team but adds no NRR totals. Never include Super Over runs or balls. "
        "Each player entry must include runs, balls_faced, wickets, runs_conceded, and balls_bowled. "
        "Use the FULL player name consistently across all entries (e.g., always 'Virat Kohli', never 'Kohli' or 'V. Kohli')."
    )
    return f"{instructions}\n\n{build_match_JsonSample()}"

def build_match_JsonSample() -> str:
    example_payload = json.dumps({
            "match_type": "T20I",
            "overs": 20,
            "balls_per_over": 6,
            "balls_per_innings": 120,
            "team1": "Team 1",
            "team2": "Team 2",
            "score1": 180,
            "wickets1": 6,
            "balls1": 120,
            "score2": 175,
            "wickets2": 7,
            "balls2": 110,
            "players": {
                "Team 1": {
                    "Player One": {"runs": 80, "balls_faced": 70, "wickets": 0, "runs_conceded": 0, "balls_bowled": 0},
                    "Player Two": {"runs": 50, "balls_faced": 40, "wickets": 1, "runs_conceded": 10, "balls_bowled": 4},
                },
                "Team 2": {
                    "Player Three": {"runs": 60, "balls_faced": 50, "wickets": 0, "runs_conceded": 0, "balls_bowled": 0},
                    "Player Four": {"runs": 20, "balls_faced": 10, "wickets": 2, "runs_conceded": 18, "balls_bowled": 12},
                },
            },
        }, indent=2)
    return (
        "Sample JSON: normal six-ball T20; omit optional DLS/NRR fields unless applicable.\n"
                f"```json\n{example_payload}\n```"
    )


def build_simulation_prompt() -> str:
    """Build the match simulation prompt with team short codes injected."""
    if team_mappings:
        mappings_str = ", ".join(f"{code}={name}" for name, code in sorted(team_mappings.items()))
    else:
        mappings_str = "(no team short codes registered yet — use /setshort CODE TeamName to register)"

    team_section = (
        f"------------------------------------------------------\n"
        f"BEFORE SIMULATING, VERIFY:\n"
        f"1. Both teams MUST be from the registered teams list below.\n"
        f"2. The user MUST provide playing 11 for both teams.\n"
        f"3. The user MUST provide the venue.\n"
        f"If any of the above is missing or teams are not registered, STOP and ask.\n"
        f"------------------------------------------------------\n"
        f"REGISTERED TEAMS:\n"
        f"{mappings_str}\n"
        f"------------------------------------------------------\n"
    )

    return f"{team_section}\n{SIMULATION_PROMPT_TEXT}"


SIMULATION_PROMPT_TEXT = (
    "Simulate a realistic Mens Hundred league match (two teams) with full ball-by-ball professional TV-broadcast commentary from \n"
    "1st over - 0.1 to 0.5\n"
    "2nd over - 1.1 to 1.5\n"
    "....20 th over - 19.1 to 19.5\n"
    "\n"
    "COMMENTARY STYLE:\n"
    "Write all commentary in the style of the official Hundred broadcast team — Simon Doull and Ian Smith. Keep it punchy, dramatic, and TV-ready. For 4s and 6s, go vivid with shot description and crowd reaction. For wickets, build the drama. Keep 1/2/3 run lines short and crisp.\n"
    "\n"
    "Follow true Hundred league gameplay, proper cricket logics: ups & downs, pressure, partnerships, momentum shifts. Make it independent of whether batting or bowling first, best team should win.Go according to all rules of Mens Hundred league matches. Use given batting orders & bowling sequences only (no changes).\n"
    "------------------------------------------------------\n"
    "Study the below cricket ground venue and it's conditions and use it for the simulation of the match!\n"
    "\n"
    "\U0001f3df Venue & Conditions\n"
    "\n"
    "Venue:\n"
    "\n"
    "DO THE TOSS YOURSELF AND SELECT BAT/BOWL ACCORDING TO GROUND CONDITIONS. \n"
    "------------------------------------------------------\n"
    "Match rules:\n"
    "\n"
    "1) 20 overs per side\n"
    "Powerplay: 5 overs (2 fielders outside 30-yard circle)\n"
    "Max overs per bowler: 4\n"
    "2) Simulate all 20 overs in each innings with ball by ball commentary. Don't skip overs.\n"
    "3) Finish the innings if the target is reached or all the 10 wickets fall. \n"
    "4) Batters must attack part-time bowlers but part-timer can't unrealistically take 3-4 wickets.\n"
    "5)Pure tailenders should struggle vs quality bowling, rotate strike, no consistent sixes.\n"
    "6) Set batters fall only to logical deliveries and match situations.\n"
    "7) Set batters should safeguard tailenders by farming the strike, protecting their wicket, and managing strike rotation smartly.\n"
    "8)During death overs,set batsman must face more balls and play aggressively to maximize runs.\n"
    "9) Include extras (wides, no-balls, byes, leg-byes) + fielding events (catches, drops, misfields, overthrows, edges) + DRS. \n"
    "---------------------------------------\n"
    "STRIKE ROTATION RULES :(OBEY MUST) \n"
    "\n"
    "\U0001f4a5Each set = 2 overs\n"
    "1st set = 1,2 overs\n"
    "2nd set = 3,4 overs\n"
    "... \n"
    "9th set = 17,18 overs\n"
    "10th set = 19,20 overs\n"
    "\n"
    "\U0001f4a5How the bowling end changes :\n"
    "1) At the start of 1st innings,bowler will start in one bowling end say end A.After every set finish(2 overs) , the bowler has to change bowling end say end B.\n"
    "Thus A and B changes alternatively. \n"
    "2) In the 2nd innings, bowler again starts with end A and change alternatively after each set(2 overs).\n"
    "\n"
    "\U0001f4a5How the batter strike changes:\n"
    "1) From odd(1,3,...17,19) over to even (2,4,...18,20) over change :\n"
    "On the last ball if batter :\n"
    "a) hit 1,3,5,.... runs; then that batter is non-striker for next over first ball. \n"
    "b) hit 0,2,4,6,..runs; then that batter is striker for next over first ball.\n"
    "c) gets out ; then new batsman will be striker for next over first ball.\n"
    "\n"
    "2) From even (2,4,....18) to odd(3,5,....17,19) over change :\n"
    "On the last ball if batter :\n"
    "a) hit 1,3,5,.... runs; then that batter is striker for next over first ball.\n"
    "b) hit 0,2,4,6,..runs; then that batter is non-striker next over first ball.\n"
    "c) gets out ; then new batsman will be non-striker next over first ball.\n"
    "\n"
    "After every over, clearly state which batter will be on strike at the start of the next over.(must say)\n"
    "------------------------------------------------------\n"
    "\u26a1 After every 2 overs, give this EXACT sample update, never miss it after 2 overs(no mistakes and don't forget):\n"
    "\n"
    "BATTING TEAM NAME:\n"
    "\U0001f535LIVE Score: 140/5 (18 over)\n"
    "\U0001f525 Last 2 overs - 15/1\n"
    "- Need: 20 off 10 balls (2nd innings only)\n"
    "- CRR: 7.77 \n"
    "- RRR: 10.00 (2nd innings only)\n"
    "\n"
    "\u2696 Opponent was - runs/wickets after 18 overs (2nd innings only)\n"
    "\U0001f3cf Current Batters\n"
    "- M. Patel: 18* (12) | SR: 150.00 | 4s: 2 | 6s: 1\n"
    "- A. Rawat: 6* (4) | SR: 150.00 | 4s: 1 | 6s: 0\n"
    "Partnership = Runs(balls)\n"
    "\n"
    "\U0001f4a5 Last 2 bowlers Stats\n"
    "- Bumrah: 4-0-26-2\n"
    "- Rashid: 4-0-24-1\n"
    "x Last wicket -(batter stats)\n"
    "\n"
    "Double check every over runs and the scorecard runs too.\n"
    "------------------------------------------------------\n"
    "End of match :\n"
    "1)Declare result+POTM (with reason)+Both captains speech about match result.\n"
    "2)Give full batting & bowling scorecards of both innings(Compulsory never miss it).\n"
    "3) Highlight key moments/partnerships/turning points.\n"
)


UPCOMING_SIMULATION_PROMPT_TEXT = (
    "Simulate a realistic Mens Hundred league match (two teams) with full ball-by-ball professional TV-broadcast commentary from \n"
    "Set 1 - balls 1.1 to 1.5\n"
    "Set 2 - balls 2.1 to 2.5\n"
    "....20th set - balls 20.1 to 20.5\n"
    "\n"
    "COMMENTARY STYLE:\n"
    "Write all commentary in the style of the official Hundred broadcast team — Simon Doull and Ian Smith. Keep it punchy, dramatic, and TV-ready. For 4s and 6s, go vivid with shot description and crowd reaction. For wickets, build the drama. Keep 1/2/3 run lines short and crisp.\n"
    "\n"
    "Follow true Hundred league gameplay, proper cricket logics: ups & downs, pressure, partnerships, momentum shifts. Make it independent of whether batting or bowling first, best team should win. Go according to all rules of Mens Hundred league matches. Use given batting orders & bowling sequences only (no changes).\n"
    "------------------------------------------------------\n"
    "Study the below cricket ground venue and it's conditions and use it for the simulation of the match!\n"
    "\n"
    "\U0001f3df Venue & Conditions\n"
    "\n"
    "Venue:\n"
    "\n"
    "DO THE TOSS YOURSELF AND SELECT BAT/BOWL ACCORDING TO GROUND CONDITIONS. \n"
    "------------------------------------------------------\n"
    "Match rules (The Hundred format):\n"
    "\n"
    "1) 100 balls per side (20 sets of 5 balls each)\n"
    "   Powerplay: first 25 balls — only 2 fielders allowed outside the ring\n"
    "   Max 20 balls per bowler (4 sets)\n"
    "2) Simulate all 100 balls (20 sets) in each innings with ball by ball commentary. Don't skip sets.\n"
    "3) Finish the innings if the target is reached or all 10 wickets fall. \n"
    "4) Batters must attack part-time bowlers but part-timer can't unrealistically take 3-4 wickets.\n"
    "5) Pure tailenders should struggle vs quality bowling, rotate strike, no consistent sixes.\n"
    "6) Set batters fall only to logical deliveries and match situations.\n"
    "7) Set batters should safeguard tailenders by farming the strike, protecting their wicket, and managing strike rotation smartly.\n"
    "8) During death overs, set batsman must face more balls and play aggressively to maximize runs.\n"
    "9) Include extras (wides, no-balls, byes, leg-byes) + fielding events (catches, drops, misfields, overthrows, edges) + DRS. \n"
    "---------------------------------------\n"
    "STRIKE ROTATION RULES :(OBEY MUST) \n"
    "\n"
    "\U0001f4a5Each set = 10 balls (5 balls per over x 2 overs)\n"
    "1st set = balls 1-10\n"
    "2nd set = balls 11-20\n"
    "... \n"
    "10th set = balls 91-100\n"
    "\n"
    "\U0001f4a5How the bowling end changes :\n"
    "1) At the start of 1st innings, bowler will start in one bowling end say end A. After every set finish (10 balls), the bowler has to change bowling end say end B.\n"
    "Thus A and B changes alternatively. \n"
    "2) In the 2nd innings, bowler again starts with end A and changes alternatively after each set (10 balls).\n"
    "\n"
    "\U0001f4a5How the batter strike changes:\n"
    "1) From odd set (1,3,...17,19) to even set (2,4,...18,20) :\n"
    "On the last ball if batter :\n"
    "a) hit 1,3,5,.... runs; then that batter is non-striker for next set first ball. \n"
    "b) hit 0,2,4,6,..runs; then that batter is striker for next set first ball.\n"
    "c) gets out ; then new batsman will be striker for next set first ball.\n"
    "\n"
    "2) From even set (2,4,...18,20) to odd set (3,5,...19) :\n"
    "On the last ball if batter :\n"
    "a) hit 1,3,5,.... runs; then that batter is striker for next set first ball.\n"
    "b) hit 0,2,4,6,..runs; then that batter is non-striker for next set first ball.\n"
    "c) gets out ; then new batsman will be non-striker for next set first ball.\n"
    "\n"
    "After every set, clearly state which batter will be on strike at the start of the next set. (must say)\n"
    "------------------------------------------------------\n"
    "\u26a1 After every set (10 balls), give this EXACT sample update, never miss it after a set (no mistakes and don't forget):\n"
    "\n"
    "BATTING TEAM NAME:\n"
    "\U0001f535LIVE Score: 140/5 (18 sets)\n"
    "\U0001f525 Last set - 15/1\n"
    "- Need: 20 off 10 balls (2nd innings only)\n"
    "- CRR: 7.77 \n"
    "- RRR: 10.00 (2nd innings only)\n"
    "\n"
    "\u2696 Opponent was - runs/wickets after 18 sets (2nd innings only)\n"
    "\U0001f3cf Current Batters\n"
    "- M. Patel: 18* (12) | SR: 150.00 | 4s: 2 | 6s: 1\n"
    "- A. Rawat: 6* (4) | SR: 150.00 | 4s: 1 | 6s: 0\n"
    "Partnership = Runs(balls)\n"
    "\n"
    "\U0001f4a5 Last 2 sets bowlers Stats\n"
    "- Bumrah: 20 balls - 0 maidens - 26 runs - 2 wickets\n"
    "- Rashid: 20 balls - 0 maidens - 24 runs - 1 wicket\n"
    "x Last wicket -(batter stats)\n"
    "\n"
    "Double check every set's runs and the scorecard runs too.\n"
    "------------------------------------------------------\n"
    "End of match :\n"
    "1) Declare result+POTM (with reason)+Both captains speech about match result.\n"
    "2) Give full batting & bowling scorecards of both innings (Compulsory never miss it).\n"
    "3) Highlight key moments/partnerships/turning points.\n"
)


def build_upcoming_simulation_prompt() -> str:
    """Build the upcoming simulation prompt with corrected Hundred rules."""
    if team_mappings:
        mappings_str = ", ".join(f"{code}={name}" for name, code in sorted(team_mappings.items()))
    else:
        mappings_str = "(no team short codes registered yet — use /setshort CODE TeamName to register)"

    team_section = (
        f"------------------------------------------------------\n"
        f"BEFORE SIMULATING, VERIFY:\n"
        f"1. Both teams MUST be from the registered teams list below.\n"
        f"2. The user MUST provide playing 11 for both teams.\n"
        f"3. The user MUST provide the venue.\n"
        f"If any of the above is missing or teams are not registered, STOP and ask.\n"
        f"------------------------------------------------------\n"
        f"REGISTERED TEAMS:\n"
        f"{mappings_str}\n"
        f"------------------------------------------------------\n"
    )

    return f"{team_section}\n{UPCOMING_SIMULATION_PROMPT_TEXT}"


def get_caps_leaders(players: Dict[str, Dict[str, Any]], cap_type: str, top_n: int = 10) -> List[Dict[str, Any]]:
    if cap_type == "orange":
        def sort_key(item: tuple[str, Dict[str, Any]]) -> tuple[Any, ...]:
            player_name, stats = item
            runs = int(stats.get("runs", 0))
            balls_faced = int(stats.get("balls_faced", 0))
            strike_rate = (runs / balls_faced * 100.0) if balls_faced else 0.0
            return (-runs, -strike_rate, player_name)
    elif cap_type == "purple":
        def sort_key(item: tuple[str, Dict[str, Any]]) -> tuple[Any, ...]:
            player_name, stats = item
            wickets = int(stats.get("wickets", 0))
            balls_bowled = int(stats.get("balls_bowled", 0))
            runs_conceded = int(stats.get("runs_conceded", 0))
            economy = (runs_conceded / balls_bowled) if balls_bowled else 0.0
            return (-wickets, economy, player_name)
    else:
        raise ValueError("cap_type must be 'orange' or 'purple'")

    if cap_type == "orange":
        eligible_players = [
            item for item in players.items() if int(item[1].get("balls_faced", 0)) > 0
        ]
    else:
        eligible_players = [
            item for item in players.items() if int(item[1].get("balls_bowled", 0)) > 0
        ]

    ordered = sorted(eligible_players, key=sort_key)
    results = []
    for player_name, stats in ordered[:top_n]:
        if cap_type == "orange":
            balls_faced = int(stats.get("balls_faced", 0))
            strike_rate = (int(stats.get("runs", 0)) / balls_faced * 100.0) if balls_faced else 0.0
            results.append({
                "name": player_name,
                "runs": int(stats.get("runs", 0)),
                "strike_rate": round(strike_rate, 2),
            })
        else:
            balls_bowled = int(stats.get("balls_bowled", 0))
            runs_conceded = int(stats.get("runs_conceded", 0))
            economy = (runs_conceded / balls_bowled) if balls_bowled else 0.0
            results.append({
                "name": player_name,
                "wickets": int(stats.get("wickets", 0)),
                "economy": round(economy, 2),
            })
    return results


def format_standings(standings: List[Dict[str, object]]) -> str:
    if not standings:
        return "No matches recorded yet."

    lines = ["Tournament Standings", ""]
    for idx, item in enumerate(standings, start=1):
        lines.append(
            f"{idx}. {_resolve_team_label(str(item['team']))}\n"
            f"   P {item['played']} | W {item['wins']} | D {item['draws']} | L {item['losses']} | Pt {item['points']} | NRR {item['net_run_rate']}"
        )
    return "\n".join(lines)


def format_squads(squads: List[Dict[str, object]]) -> str:
    if not squads:
        return "No squad players recorded yet."
    return "\n".join(
        f"{item['team']} | {item['player']} | {float(item['price']):,.2f}" for item in squads
    )


def format_team_squad(team: str, players: List[Dict[str, object]]) -> str:
    if not players:
        return f"{team}\n\nNo players recorded yet."
    return f"{team}\n\n" + "\n".join(
        f"{item['player']} | {int(item['price']) if float(item['price']).is_integer() else item['price']}"
        for item in players
    )


def format_caps(players: Dict[str, Dict[str, Any]], top_n: int = 10) -> str:
    if not players:
        return "No player stats recorded yet."

    orange = get_caps_leaders(players, "orange", top_n=top_n)
    purple = get_caps_leaders(players, "purple", top_n=top_n)

    orange_lines = []
    for idx, item in enumerate(orange, start=1):
        prefix = "🟠 " if idx == 1 else ""
        orange_lines.append(f"{idx}. {prefix}{_format_cap_player_name(item['name'])}: {item['runs']} runs, SR {item['strike_rate']}")

    purple_lines = []
    for idx, item in enumerate(purple, start=1):
        prefix = "🟣 " if idx == 1 else ""
        purple_lines.append(f"{idx}. {prefix}{_format_cap_player_name(item['name'])}: {item['wickets']} wickets, Econ {item['economy']}")

    return "Orange Cap\n" + "\n".join(orange_lines) + "\n\nPurple Cap\n" + "\n".join(purple_lines)


CAPS_PAGE_SIZE = 10


def format_cap_page(
    players: Dict[str, Dict[str, Any]], cap_type: str, page: int = 0, page_size: int = CAPS_PAGE_SIZE
) -> tuple[str, int]:
    """Format one numbered page of either cap leaderboard."""
    if cap_type not in {"orange", "purple"}:
        raise ValueError("cap_type must be 'orange' or 'purple'")
    if not players:
        return "No player stats recorded yet.", 1

    leaders = get_caps_leaders(players, cap_type, top_n=len(players))
    title = "Orange Cap" if cap_type == "orange" else "Purple Cap"
    if not leaders:
        activity = "faced a ball" if cap_type == "orange" else "bowled a ball"
        return f"No eligible players yet. No player has {activity}.", 1
    total_pages = max(1, (len(leaders) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    page_leaders = leaders[page * page_size : (page + 1) * page_size]
    lines = [f"{title} (Page {page + 1}/{total_pages})", ""]

    for rank, item in enumerate(page_leaders, start=page * page_size + 1):
        if cap_type == "orange":
            prefix = "🟠 " if rank == 1 else ""
            lines.append(f"{rank}. {prefix}{_format_cap_player_name(item['name'])}: {item['runs']} runs, SR {item['strike_rate']}")
        else:
            prefix = "🟣 " if rank == 1 else ""
            lines.append(f"{rank}. {prefix}{_format_cap_player_name(item['name'])}: {item['wickets']} wickets, Econ {item['economy']}")

    return "\n".join(lines), total_pages


state = TournamentState()
database = TournamentDatabase(DATABASE_PATH)
print(f"Database loaded: {DATABASE_PATH}")
print(os.path.exists(DATABASE_PATH))
saved = database.load_all()

state.teams = saved["teams"]
state.players = saved["players"]
state.matches = saved["matches"]
if state.matches:
    first_match = state.matches[0]
    state.nrr_balls_per_over = _get_balls_per_over(first_match)
    state.nrr_quota_balls = _get_match_quota_balls(first_match)
    for team_stats in state.teams.values():
        team_stats["nrr_rate_unit_balls"] = state.nrr_balls_per_over
state._refresh_rates()
# Stores admins who clicked "Add Match" and are allowed to send one JSON
waiting_for_match: Dict[int, bool] = {}
# Per-user session token to prevent stale cancel_match_wait from killing a new session
_match_session_tokens: Dict[int, int] = {}
# Telecast session: collects multi-part simulation text + match link from DM
# {user_id: {"match_link": str, "parts": [str, ...], "total": int, "stage": str}}
# stage: "collecting" (unlimited parts) -> "link" (asked for link) -> "ready" (send to group)
_telecast_sessions: Dict[int, Dict[str, object]] = {}
# Active live broadcasts — used to cancel in-progress sends
_active_live_broadcasts: Dict[int, Dict[str, object]] = {}

# Team name to short code mappings (e.g., {"India": "IND"})
team_mappings: Dict[str, str] = database.load_team_shortcodes()
# Default tournament team mappings
_DEFAULT_TEAM_MAPPINGS: Dict[str, str] = {
    "Manchester Super Giants": "MSG",
    "Trent Rockets": "TR",
    "London Spirits": "LS",
    "Sun Risers Lead": "SRL",
    "Welsh Fire": "WF",
    "MI London": "MI",
    "Southern Braves": "SB",
    "Birmingham Phoenix": "BP",
    "Durham Dukes": "DD",
    "Western Thunders": "WT",
    "Derby Falcons": "DF",
    "Somerset Mavericks": "SM",
}
if not team_mappings:
    team_mappings.update(_DEFAULT_TEAM_MAPPINGS)
    database.save_team_shortcodes(team_mappings)
# Cache for generated leaderboard images
_image_cache: Dict[str, bytes] = {}
# Telegram file_ids — reuse on re-send to avoid re-uploading
_telegram_file_ids: Dict[str, str] = {}


def _reverse_team_mappings() -> Dict[str, str]:
    """Build a short_code -> team_name reverse lookup."""
    return {code: name for name, code in team_mappings.items()}


def _resolve_team_name(name_or_code: str) -> str:
    """Resolve a short code to its full team name, or return as-is if already a name."""
    reverse = _reverse_team_mappings()
    return reverse.get(name_or_code, name_or_code)


def _resolve_team_label(name_or_code: str) -> str:
    """Return 'SHORT - Full Name' for a team identified by name or short code."""
    full_name = _resolve_team_name(name_or_code)
    code = team_mappings.get(full_name)
    if code:
        return f"{code} - {full_name}"
    # name_or_code might be the short code itself
    code = team_mappings.get(name_or_code)
    if code:
        return f"{code} - {name_or_code}"
    return name_or_code


def _resolve_team_code(team_name: str) -> str:
    """Resolve a team name from match data to its registered short code.

    Tolerates case differences and minor spelling variations (e.g.
    'Sun Risers Leeds' vs registered 'Sun Risers Lead') via token overlap.
    Returns an empty string when no mapping is close enough.
    """
    name = str(team_name).strip()
    if not name:
        return ""
    # Exact match
    code = team_mappings.get(name)
    if code:
        return code
    # Case-insensitive exact match
    lowered = name.lower()
    for mapped_name, mapped_code in team_mappings.items():
        if mapped_name.strip().lower() == lowered:
            return mapped_code
    # Fuzzy token-overlap match for near-identical names
    name_tokens = set(lowered.split())
    best_code, best_score = "", 0.0
    for mapped_name, mapped_code in team_mappings.items():
        mapped_tokens = set(mapped_name.strip().lower().split())
        if not name_tokens or not mapped_tokens:
            continue
        overlap = len(name_tokens & mapped_tokens)
        score = overlap / len(name_tokens | mapped_tokens)
        if score > best_score:
            best_score = score
            best_code = mapped_code
    if best_score >= 0.5:
        return best_code
    return ""


def _get_player_team_code(player_name: str) -> str:
    """Return the team short code a player belongs to, based on recorded matches.

    Prefers the short codes stored in the match JSON (team1_short /
    team2_short), falling back to fuzzy-resolving the team name against the
    registered mappings, and finally to the squads table. Returns an empty
    string when the player's team cannot be determined.
    """
    lowered = player_name.strip().lower()

    def _find_in_match(match: Dict[str, object]) -> str:
        players_data = match.get("players", {})
        if not isinstance(players_data, dict):
            return ""
        t1_short = str(match.get("team1_short", "") or "").strip()
        t2_short = str(match.get("team2_short", "") or "").strip()
        team1_name = str(match.get("team1", "") or "").strip().lower()
        team2_name = str(match.get("team2", "") or "").strip().lower()

        for team_name, team_players in players_data.items():
            if not isinstance(team_players, dict):
                continue
            found = (
                player_name in team_players
                or any(str(p).strip().lower() == lowered for p in team_players)
            )
            if not found:
                continue
            team_key = str(team_name).strip()
            team_key_lower = team_key.lower()
            # Prefer the short code carried in the match JSON itself
            if t1_short and team_key_lower == team1_name:
                return t1_short
            if t2_short and team_key_lower == team2_name:
                return t2_short
            # Else resolve the team name against registered mappings
            code = _resolve_team_code(team_key)
            if code:
                return code
            # Single-sided codes: the one present must be this team's
            if t1_short and not t2_short:
                return t1_short
            if t2_short and not t1_short:
                return t2_short
        return ""

    for match in state.matches:
        code = _find_in_match(match)
        if code:
            return code

    # Fallback: registered squads
    try:
        for squad in database.get_squads():
            if str(squad.get("player", "")).strip().lower() == lowered:
                code = _resolve_team_code(str(squad.get("team", "")))
                if code:
                    return code
    except Exception:
        pass

    return ""


def _format_cap_player_name(player_name: str) -> str:
    """Format a player name with their team short code, e.g. 'N. Pooran (SRL)'."""
    code = _get_player_team_code(player_name)
    return f"{player_name} ({code})" if code else player_name


def _normalize_player_name(name: str) -> str:
    """Normalize a player name for fuzzy matching: lowercase, sort tokens alphabetically."""
    tokens = name.strip().lower().split()
    return " ".join(sorted(tokens))


def _find_canonical_player(new_name: str, existing_players: Dict[str, Any]) -> Optional[str]:
    """Find an existing player name that matches new_name via fuzzy logic.

    Returns the canonical (existing) name if a match is found, else None.
    """
    new_norm = _normalize_player_name(new_name)
    new_tokens = set(new_norm.split())
    best_match: Optional[str] = None
    best_score = 0
    for existing_name in existing_players:
        existing_norm = _normalize_player_name(existing_name)
        existing_tokens = set(existing_norm.split())
        # Exact normalized match
        if new_norm == existing_norm:
            return existing_name
        # Subset match: all tokens of the shorter name appear in the longer one
        if new_tokens.issubset(existing_tokens) or existing_tokens.issubset(new_tokens):
            return existing_name
        # Token overlap scoring for non-subset cases
        overlap = len(new_tokens & existing_tokens)
        total = len(new_tokens | existing_tokens)
        score = overlap / total if total else 0
        if score > best_score:
            best_score = score
            best_match = existing_name
    # Require at least 60% token overlap to merge non-subset matches
    if best_score >= 0.6:
        return best_match
    return None


def remove_last_match() -> bool:
    """Delete the latest stored match and rebuild all derived tournament data."""
    if database.delete_last_match() is None:
        return False

    remaining_matches = database.load_all()["matches"]
    admin_ids = set(state.admin_ids)
    state.teams.clear()
    state.players.clear()
    state.matches.clear()
    state.nrr_balls_per_over = None
    state.nrr_quota_balls = None
    for match in remaining_matches:
        state.apply_match(match)
    state.admin_ids = admin_ids

    database.clear_statistics()
    for team, stats in state.teams.items():
        database.save_standing(team, stats)
    for player, stats in state.players.items():
        database.save_player_stat(player, stats)
    _invalidate_image_cache()
    return True

# Optional:
# Put your match chat ID here to send match results to the group from DM.
# Example: MATCH_CHAT_ID = -1001234567890
# Set to None to disable.
MATCH_CHAT_ID = os.getenv("MATCH_CHAT_ID")
# Put your match topic ID here if using Telegram forum topics.
# Example: MATCH_TOPIC_ID = 123456
# Set to None to allow any topic.
MATCH_TOPIC_ID = None
# Telecast topic ID — loaded from database, set via /set_telecast_topic
_telecast_topic_id: Optional[int] = None
_saved_telecast_topic = database.get_config("telecast_topic_id")
if _saved_telecast_topic:
    try:
        _telecast_topic_id = int(_saved_telecast_topic)
    except ValueError:
        pass
for admin_id in parse_admin_ids(ADMIN_USER_IDS):
    state.set_admin(admin_id)


def _is_match_payload(text: str) -> bool:
    return parse_match(text) is not None


def validate_match_json(text: str) -> Optional[str]:
    """Validate the full JSON schema required for standings, NRR, and cap stats."""
    candidates = _extract_json_candidates(text)
    if not candidates:
        return "Please send the complete JSON generated from Prompt."

    try:
        payload = json.loads(candidates[0])
    except json.JSONDecodeError:
        return "The JSON is invalid."
    if not isinstance(payload, dict):
        return "The match payload must be one JSON object."

    required_fields = {
        "match_type", "balls_per_over", "balls_per_innings", "team1", "team2",
        "score1", "wickets1", "balls1", "score2", "wickets2", "balls2", "players",
        "team1_short", "team2_short",
    }
    missing = sorted(field for field in required_fields if field not in payload)
    if missing:
        return "Missing required field(s): " + ", ".join(missing) + "."

    try:
        if int(payload["balls_per_over"]) <= 0 or int(payload["balls_per_innings"]) <= 0:
            return "balls_per_over and balls_per_innings must be positive integers."
        for field in ("score1", "wickets1", "balls1", "score2", "wickets2", "balls2"):
            if int(payload[field]) < 0:
                return f"{field} cannot be negative."
    except (TypeError, ValueError):
        return "Scores, wickets, balls, and format values must be integers."

    players = payload["players"]
    if not isinstance(players, dict) or not players:
        return "players must contain both teams' player statistics."
    required_player_fields = {"runs", "balls_faced", "wickets", "runs_conceded", "balls_bowled"}
    for team_name in (str(payload["team1"]), str(payload["team2"])):
        team_players = players.get(team_name)
        if not isinstance(team_players, dict) or not team_players:
            return f"players must include player statistics for {team_name}."
        for player_name, stats in team_players.items():
            if not isinstance(stats, dict):
                return f"Player {player_name} must have a statistics object."
            missing_stats = sorted(field for field in required_player_fields if field not in stats)
            if missing_stats:
                return f"Player {player_name} is missing: " + ", ".join(missing_stats) + "."
    return None


def validate_match_for_tournament(match_data: Dict[str, object]) -> Optional[str]:
    """Ensure every match uses the NRR format set by the first tournament match."""
    if state.nrr_balls_per_over is None:
        return None
    balls_per_over = _get_balls_per_over(match_data)
    if balls_per_over != state.nrr_balls_per_over:
        return (
            f"This tournament is locked to {state.nrr_balls_per_over}-ball overs from its first match. "
            f"This JSON uses {balls_per_over}-ball overs, so it was not added."
        )
    return None


def build_help() -> str:
    
    return (
        "📋 General\n"
        "/start - Welcome message with buttons\n"
        "/help - Show this help\n\n"
        "📊 Standings & Stats\n"
        "/table - View standings image\n"
        "/standings - View the points table\n"
        "/orangecap - Show Orange Cap image\n"
        "/purplecap - Show Purple Cap image\n"
        "/caps - Choose a cap leaderboard\n\n"
        "🏏 Match Management (Admin)\n"
        "/add_match - Submit a match JSON\n"
        "/cancel_match - Cancel pending match submission\n"
        "/prompt - Display match JSON prompt for copy paste\n"
        "/simulation_prompt - Display match simulation prompt\n"
        "/upcoming_prompt - Display updated Hundred simulation prompt\n\n"
        "🎬 Live Telecast (Admin)\n"
        "/set_telecast_topic - Set the topic for telecast (group, one-time)\n"
        "/telecast - Start collecting (input via DM)\n"
        "/match_link - Send match link after collecting parts (DM)\n"
        "/go_telecast - Start live ball-by-ball telecast (group topic)\n"
        "/cancel_telecast - Cancel active telecast (group or DM)\n\n"
        "⚙️ Admin\n"
        "/setshort CODE TeamName - Register team short code\n"
        "/setadmin user_id - Add an admin user\n"
        "Use End tournament button to clear data after confirmation.\n"
    )

async def prompt_command(update, context):
    user_id = update.message.from_user.id if update.message.from_user else None
    is_group = update.message.chat.type in ("group", "supergroup")

    text = build_match_prompt()
    max_len = 4000

    # Send to DM
    if user_id:
        try:
            await context.bot.send_message(chat_id=user_id, text="📋 Match JSON Prompt")
            if len(text) <= max_len:
                await context.bot.send_message(chat_id=user_id, text=text)
            else:
                parts = []
                while text:
                    if len(text) <= max_len:
                        parts.append(text)
                        break
                    split_at = text.rfind("\n", 0, max_len)
                    if split_at == -1:
                        split_at = max_len
                    parts.append(text[:split_at])
                    text = text[split_at:].lstrip("\n")
                for part in parts:
                    await context.bot.send_message(chat_id=user_id, text=part)
        except Exception:
            await update.message.reply_text("Could not send DM. Please start a private chat with the bot first.")
            return

    # Notify in group if called from a group
    if is_group:
        await update.message.reply_text("✅ Prompt sent to your DM.")


async def simulation_prompt_command(update, context):
    user_id = update.message.from_user.id if update.message.from_user else None
    is_group = update.message.chat.type in ("group", "supergroup")

    text = build_simulation_prompt()
    max_len = 4000

    # Send to DM
    if user_id:
        try:
            await context.bot.send_message(chat_id=user_id, text="🏏 Simulation Prompt")
            if len(text) <= max_len:
                await context.bot.send_message(chat_id=user_id, text=text)
            else:
                parts = []
                while text:
                    if len(text) <= max_len:
                        parts.append(text)
                        break
                    split_at = text.rfind("\n", 0, max_len)
                    if split_at == -1:
                        split_at = max_len
                    parts.append(text[:split_at])
                    text = text[split_at:].lstrip("\n")
                for part in parts:
                    await context.bot.send_message(chat_id=user_id, text=part)
        except Exception:
            await update.message.reply_text("Could not send DM. Please start a private chat with the bot first.")
            return

    # Notify in group if called from a group
    if is_group:
        await update.message.reply_text("✅ Simulation prompt sent to your DM.")


async def upcoming_prompt_command(update, context):
    user_id = update.message.from_user.id if update.message.from_user else None
    is_group = update.message.chat.type in ("group", "supergroup")

    text = build_upcoming_simulation_prompt()
    max_len = 4000

    # Send to DM
    if user_id:
        try:
            await context.bot.send_message(chat_id=user_id, text="🏏 Upcoming Simulation Prompt (Updated Hundred Rules)")
            if len(text) <= max_len:
                await context.bot.send_message(chat_id=user_id, text=text)
            else:
                parts = []
                while text:
                    if len(text) <= max_len:
                        parts.append(text)
                        break
                    split_at = text.rfind("\n", 0, max_len)
                    if split_at == -1:
                        split_at = max_len
                    parts.append(text[:split_at])
                    text = text[split_at:].lstrip("\n")
                for part in parts:
                    await context.bot.send_message(chat_id=user_id, text=part)
        except Exception:
            await update.message.reply_text("Could not send DM. Please start a private chat with the bot first.")
            return

    # Notify in group if called from a group
    if is_group:
        await update.message.reply_text("✅ Upcoming prompt sent to your DM.")


async def add_match_command(update, context):
    """Slash command equivalent of the Add Match button."""
    user_id = update.message.from_user.id if update.message.from_user else None
    is_group = update.message.chat.type in ("group", "supergroup")

    if user_id is None or not state.is_admin(user_id):
        await update.message.reply_text("Only admins can add matches.")
        return

    waiting_for_match[user_id] = True

    # Start 5 minute timeout with a session token
    _match_session_tokens[user_id] = _match_session_tokens.get(user_id, 0) + 1
    asyncio.create_task(
        cancel_match_wait(user_id, _match_session_tokens[user_id])
    )

    # Notify in group if called from a group
    if is_group:
        await update.message.reply_text("✅ Send the match JSON in your DM.")

    # Send instructions via DM
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="📥 Send the match JSON now.\n\n"
                 "Only your next message will be processed as a match.\n"
                 "All other messages are ignored.\n\n"
                 "Type /cancel_match to cancel."
        )
    except Exception:
        await update.message.reply_text("Could not send DM. Please start a private chat with the bot first.")


async def cancel_match_command(update, context):
    """Cancel the pending add-match operation."""
    user_id = update.message.from_user.id if update.message.from_user else None

    if user_id is None or not state.is_admin(user_id):
        await update.message.reply_text("Only admins can use this command.")
        return

    if waiting_for_match.pop(user_id, None):
        _match_session_tokens.pop(user_id, None)
        await update.message.reply_text("❌ Match submission cancelled.")
    else:
        await update.message.reply_text("No pending match submission to cancel.")


async def telecast_command(update: Any, context: Any) -> None:
    """Start collecting unlimited simulation messages in the group topic."""
    user_id = getattr(getattr(update.message, "from_user", None), "id", None)
    is_group = update.message.chat.type in ("group", "supergroup")

    if not is_group:
        await update.message.reply_text("This command must be used in the group.")
        return

    if user_id is None or not state.is_admin(user_id):
        await update.message.reply_text("Only admins can start a telecast.")
        return

    if _telecast_topic_id is None:
        await update.message.reply_text(
            "No telecast topic is set. Admin must run /set_telecast_topic first."
        )
        return

    thread_id = update.message.message_thread_id
    if thread_id != _telecast_topic_id:
        await update.message.reply_text(
            f"Telecast must be started in the designated telecast topic."
        )
        return

    chat_id = update.message.chat_id
    _telecast_sessions[user_id] = {
        "match_link": "",
        "parts": [],
        "total": 0,
        "stage": "collecting",
        "chat_id": chat_id,
        "thread_id": thread_id,
    }
    await update.message.reply_text(
        "🎬 Telecast mode started!\n\n"
        "Now go to your DM with this bot and send your simulation messages one by one.\n"
        "There is no limit on the number of parts.\n\n"
        "When you are done sending all parts, go to your DM and use /match_link\n"
        "to send the match link. Then come back here and use /go_telecast."
    )


async def start_telecast_command(update: Any, context: Any) -> None:
    """Finish collecting in the group. Redirects to /match_link or /go_telecast."""
    user_id = getattr(getattr(update.message, "from_user", None), "id", None)
    is_group = update.message.chat.type in ("group", "supergroup")

    if not is_group:
        await update.message.reply_text("This command must be used in the group.")
        return

    if user_id is None or not state.is_admin(user_id):
        await update.message.reply_text("Only admins can start a telecast.")
        return

    session = _telecast_sessions.get(user_id)
    if not session:
        await update.message.reply_text(
            "No active telecast session. Use /telecast first to start collecting."
        )
        return

    # If link has been collected, redirect to /go_telecast
    if session.get("stage") == "ready":
        parts: List[str] = session["parts"]  # type: ignore
        total_parts = len(parts)
        total_chars = sum(len(p) for p in parts)
        await update.message.reply_text(
            f"✅ Ready to broadcast! ({total_parts} parts, {total_chars} chars total).\n\n"
            f"Use /go_telecast to start the live ball-by-ball broadcast."
        )
        return

    # We have parts but no link yet — tell user to use /match_link in DM
    if not session.get("parts"):
        await update.message.reply_text(
            "No simulation parts collected yet. Use /telecast first to start collecting."
        )
        return

    total_parts = len(session["parts"])
    total_chars = sum(len(p) for p in session["parts"])
    await update.message.reply_text(
        f"✅ {total_parts} parts collected ({total_chars} chars total).\n\n"
        f"Now go to your DM and use /match_link to send the match link."
    )
    # Also send request to DM
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"✅ {total_parts} parts collected ({total_chars} chars total).\n\n"
                 f"Now send the match link (the AI-generated link)."
        )
    except Exception:
        pass


async def match_link_command(update: Any, context: Any) -> None:
    """Request the match link. Works in DM and group."""
    user_id = getattr(getattr(update.message, "from_user", None), "id", None)
    is_group = update.message.chat.type in ("group", "supergroup")
    is_dm = update.message.chat.type == "private"

    if user_id is None or not state.is_admin(user_id):
        await update.message.reply_text("Only admins can use this command.")
        return

    session = _telecast_sessions.get(user_id)
    if not session:
        await update.message.reply_text(
            "No active telecast session. Use /telecast in the group first."
        )
        return

    if session.get("stage") == "ready":
        await update.message.reply_text(
            "✅ Match link already received! Use /go_telecast in the group to start the live broadcast."
        )
        return

    if not session.get("parts"):
        await update.message.reply_text(
            "No simulation parts collected yet. Send your simulation messages in DM first."
        )
        return

    session["stage"] = "link"
    total_parts = len(session["parts"])
    total_chars = sum(len(p) for p in session["parts"])

    if is_group:
        # If used in group, confirm and ask to go to DM
        await update.message.reply_text(
            f"✅ {total_parts} parts collected ({total_chars} chars total).\n\n"
            f"Now go to your DM and send the match link."
        )
    else:
        # If used in DM, ask directly for the link
        await update.message.reply_text(
            f"✅ {total_parts} parts collected ({total_chars} chars total).\n\n"
            f"Now send the match link (the AI-generated link)."
        )

    # Also send request to DM if called from group
    if is_group:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ {total_parts} parts collected ({total_chars} chars total).\n\n"
                     f"Now send the match link (the AI-generated link)."
            )
        except Exception:
            pass


async def cancel_telecast_command(update: Any, context: Any) -> None:
    """Cancel an in-progress telecast collection."""
    user_id = getattr(getattr(update.message, "from_user", None), "id", None)

    if user_id is None or not state.is_admin(user_id):
        await update.message.reply_text("Only admins can cancel a telecast.")
        return

    # Cancel active live broadcast first
    live_broadcast = _active_live_broadcasts.pop(user_id, None)
    if live_broadcast:
        live_broadcast["cancelled"] = True
        await update.message.reply_text("❌ Live telecast cancelled.")
        return

    session = _telecast_sessions.get(user_id)
    if session:
        _telecast_sessions.pop(user_id, None)
        await update.message.reply_text("❌ Telecast cancelled.")
    else:
        await update.message.reply_text("No active telecast to cancel.")


# --- Live ball-by-ball telecast logic ---


def _looks_like_wicket(text: str) -> bool:
    """Return True only for genuine wicket lines.

    Avoids the substring traps where "beats the outside edge!", "Slater out
    to open for TR" or the live block's "x Last wicket - ..." footer were
    all treated as dramatic wicket events.
    """
    text_upper = text.upper()
    # Live-block footer / scorecard lines that merely mention a wicket
    if "LAST WICKET" in text_upper or "FALL OF WICKET" in text_upper:
        return False
    # Explicit dismissal call, e.g. "WICKET!" / "2.1 Mahmood to Slater, WICKET!"
    # Case-sensitive on purpose: uppercase WICKET only appears in genuine
    # dismissal calls, while prose mentions ("deep mid-wicket", "won by 4
    # wickets", "wickets fell") are lowercase. "Wicket!" (capitalised +
    # exclamation) is also accepted as a dismissal call.
    if re.search(r"\bWICKETS?\b", text) or re.search(r"\bWickets?\s*!", text):
        return True
    # Ball line whose result token is a dismissal: ", OUT! ..."
    if re.match(r"^\d+\.\d+\b", text) and re.search(r",\s*OUT\b", text_upper):
        return True
    # Over-end summary announcing a wicket: "Batter hit W."
    if re.search(r"\bHIT\s+W\b\.?", text_upper):
        return True
    # Standalone announcement starting with the word, e.g. "OUT! Caught at deep!"
    if re.match(r"^OUT\b", text_upper):
        return True
    return False


def _is_major_event_line(text: str) -> bool:
    """Return True if the line is a major event that should NOT be part of a live-score block.

    Major events: ball-by-ball (0.1, 12.3), over headers, over summaries,
    innings breaks, wickets, and scorecard/result lines.
    """
    text_upper = text.upper()
    # Ball-by-ball line (e.g. "0.1", "12.3", "5.5")
    if re.match(r"^\d+\.\d+\b", text):
        return True
    # Over header ("3rd over ..." or Hundred-style "Over 3 - John Turner")
    if re.match(r"^(\d+)(ST|ND|RD|TH)?\s+OVER\b", text_upper) or re.match(r"^OVER\s+\d+\b", text_upper):
        return True
    # Strike-update bookkeeping (">> Strike Update: ...") terminates a block
    if re.match(r"^[>\s*]*STRIKE UPDATE\b", text_upper):
        return True
    # Over summary
    if any(kw in text_upper for kw in ["END OF OVER", "OVER SUMMARY", "AFTER OVERS"]):
        return True
    # Innings break
    if any(kw in text_upper for kw in ["INNINGS BREAK", "END OF INNINGS"]):
        return True
    # Innings headers terminate a live block (FIRST INNINGS: TR /
    # SECOND INNINGS: X (Target: N) / INNINGS 1: X BATTING)
    if re.search(r"\b(FIRST|SECOND|1ST|2ND)\s+INNINGS\b", text_upper) or re.search(r"\bINNINGS\s*\d\s*:", text_upper):
        return True
    # Wicket line (standalone, not inside a live score block)
    if _looks_like_wicket(text):
        return True
    return False


def _is_highlight_ball(text: str) -> bool:
    """Return True if a ball-by-ball line is a highlight worth sending.

    Highlights: boundaries (FOUR / SIX words, or bare "4." / "6." result
    tokens), 3-6 run balls, and genuine wickets. Dot balls, singles, and
    doubles are skipped to keep the telecast fast.
    """
    text_upper = text.upper()
    if "FOUR" in text_upper or "SIX" in text_upper:
        return True
    if _looks_like_wicket(text):
        return True
    # Word format: "3 runs" / "4 runs" / "5 runs" / "6 runs"
    if re.search(r"\b[3-6]\s*RUNS?\b", text_upper):
        return True
    # Bare-digit format: "0.4 Mahmood to Slater, 4. Crunched through covers!"
    if re.match(r"^\d+\.\d+\b", text) and re.search(r",\s*[3-6]\s*[.!]", text_upper):
        return True
    return False


def _get_ball_delay(text: str) -> int:
    """Return the delay for a ball-by-ball line in seconds.

    Highlights (FOUR / SIX / 3-6 runs / wicket) → 7 s  |  everything else → 4 s
    """
    text_upper = text.upper()
    if _looks_like_wicket(text):
        return 7
    if "FOUR" in text_upper or "SIX" in text_upper:
        return 7
    # Word format: "3 runs" ... "6 runs"
    if re.search(r"\b[3-6]\s*RUNS?\b", text_upper):
        return 7
    # Bare-digit format: ", 4." / ", 6!"
    if re.search(r",\s*[3-6]\s*[.!]", text_upper):
        return 7
    return 4


def _is_scorecard_line(text: str) -> bool:
    """Return True if the line looks like end-of-match scorecard table content.

    Used to switch the telecast into "dump mode": once scorecard content
    starts, everything remaining is flushed as fast as possible instead of
    line-by-line with delays.
    """
    text_upper = text.upper()
    # Live-score continuation phrases that mention batters/bowlers — not scorecard
    if "CURRENT BATTERS" in text_upper or "CURRENT BOWLERS" in text_upper:
        return False
    # Strong, unambiguous scorecard markers
    if "SCORECARD" in text_upper:
        return True
    # Table header rows: "BATTER ... RUNS BALLS SR" / "BOWLER ... OVERS RUNS WICKETS ECON"
    # Require 2+ stat-column words so ordinary commentary mentioning a batter
    # and their runs can never be mistaken for a scorecard header.
    if re.search(r"\b(BATTER|BOWLER|BATSMAN)\b", text_upper):
        stat_words = re.findall(r"\b(RUNS|BALLS|OVERS|DISMISSAL|ECON|WICKETS|WKTS|SR)\b", text_upper)
        if len(stat_words) >= 2:
            return True
    # Scorecard section rows that start the line
    if re.match(r"^(TOTAL\s*\(|EXTRAS\s*\(|DID NOT BAT|DID NOT BOWL|FALL\s+OF\s+WICKETS)", text_upper):
        return True
    return False


def _classify_telecast_line(line: str) -> tuple[str, int]:
    """Classify a telecast line into a message type and delay in seconds.

    Returns (message_type, delay_seconds).
    """
    text = line.strip()
    if not text:
        return "empty", 0

    text_upper = text.upper()

    # Scorecard table / presentation content — dumped at the end, no delays
    if _is_scorecard_line(text):
        return "scorecard", 0

    # Wicket — dramatic pause
    if _looks_like_wicket(text):
        # Over-end wicket bookkeeping ("End of odd over. Batter hit W. New
        # batter (X) is non-striker.") — short pause to introduce the new
        # batter; the dismissal itself was already telecast at full drama.
        if re.match(r"^END\s+OF\s+(ODD|EVEN)\s+OVER\b", text_upper):
            return "over_summary", 3
        return "wicket", 7

    # Live score update
    if any(kw in text_upper for kw in ["LIVE SCORE", "LIVE:", "NEED:", "CRR:", "RRR:"]):
        return "score_update", 6

    # Over header ("3rd over ..." or Hundred-style "Over 3 - John Turner")
    if re.match(r"^(\d+)(ST|ND|RD|TH)?\s+OVER\b", text_upper) or re.match(r"^OVER\s+\d+\b", text_upper):
        return "over_header", 4

    # Ball-by-ball line (e.g. "0.1", "12.3", "5.5")
    if re.match(r"^\d+\.\d+\b", text):
        return "ball", _get_ball_delay(text)

    # Real innings breaks / end of innings — long dramatic pause
    if any(kw in text_upper for kw in ["INNINGS BREAK", "END OF INNINGS", "INNINGS CLOSED"]):
        return "innings_break", 19

    # Innings headers (FIRST INNINGS: TR / SECOND INNINGS: ...) — normal pace
    if "INNINGS" in text_upper:
        return "over_header", 4

    # Toss result
    if "TOSS" in text_upper:
        return "over_header", 4

    # Target announcement
    if "TARGET" in text_upper:
        return "over_header", 4

    # End of over summary
    if any(kw in text_upper for kw in ["END OF OVER", "OVER SUMMARY", "AFTER OVERS"]):
        return "over_summary", 4

    # Match result — dramatic pause before the scorecard dump
    if "RESULT" in text_upper:
        return "result", 14

    # POTM / presentation — normal commentary pace
    if "POTM" in text_upper or "PRESENTATION" in text_upper:
        return "over_header", 4

    # Match link at the end
    if text_upper.startswith("MATCH LINK") or text_upper.startswith("🔗 MATCH LINK"):
        return "scorecard", 0

    # Default — treat as normal commentary (toss chatter, speeches, POTM,
    # general broadcast lines). Always sent, never skipped by the highlight
    # filter (which only applies to ball-by-ball lines).
    return "commentary", 4


def _find_last_over_number(lines: list[str]) -> int:
    """Return the highest over number found in ball-by-ball lines.

    For example, lines containing '19.1', '19.2' → returns 19.
    Returns -1 if no ball lines are found.
    """
    max_over = -1
    for line in lines:
        m = re.match(r"^(\d+)\.\d+\b", line.strip())
        if m:
            over_num = int(m.group(1))
            if over_num > max_over:
                max_over = over_num
    return max_over


TELECAST_MAX_MESSAGE_CHARS = 3800


def _chunk_scorecard_text(text: str, max_chars: int = TELECAST_MAX_MESSAGE_CHARS) -> list[str]:
    """Split scorecard text into as few large chunks as Telegram allows.

    Prefers breaking at blank lines so each chunk stays a readable block,
    then at line boundaries, and hard-wraps only single oversized lines.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip("\n"))
        current = ""

    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= max_chars:
            current = candidate
            continue
        flush()
        if len(para) <= max_chars:
            current = para
            continue
        # Paragraph itself too large: split at line boundaries
        for line in para.split("\n"):
            candidate_line = f"{current}\n{line}" if current else line
            if len(candidate_line) <= max_chars:
                current = candidate_line
                continue
            flush()
            while len(line) > max_chars:  # single huge line: hard wrap
                chunks.append(line[:max_chars])
                line = line[max_chars:]
            current = line
    flush()
    return chunks


def _group_lines_for_telecast(lines: list[str]) -> list[tuple[str, str, int]]:
    """Group raw lines into (text, msg_type, delay) tuples.

    Live-score blocks (LIVE: / LIVE SCORE plus all following batter/bowler /
    partnership / last-wicket lines) are merged into a single message so the
    group sees a clean scorecard rather than a flood of tiny messages.

    All balls in the last over of each innings are shown regardless of runs
    scored, so the finish is always fully visible.

    Once scorecard/presentation content starts, the telecast enters dump
    mode: every remaining line is marked "dump" (0 delay) and the sender
    flushes it as 1-3 large messages instead of one message per line.
    """
    last_over = _find_last_over_number(lines)

    result: list[tuple[str, str, int]] = []
    i = 0
    while i < len(lines):
        text = lines[i].strip()
        if not text:
            i += 1
            continue

        text_upper = text.upper()

        # --- Over-end bookkeeping lines: mostly never telecast ---
        # "End of odd over. Batter hit 1. ... on strike for next over." etc.
        # Pure strike-rotation info. Two exceptions fall through:
        #   * wicket versions ("Batter hit W. New batter (X) is non-striker.")
        #     announce the new batter — sent with a short 3 s pause;
        #   * a combined innings-close line ("End of even over. Innings
        #     closed.") must survive so the break gets its dramatic pause.
        if re.match(r"^END\s+OF\s+(ODD|EVEN)\s+OVER\b", text_upper) and not any(
            kw in text_upper
            for kw in ["INNINGS CLOSED", "INNINGS BREAK", "END OF INNINGS", "BATTER HIT W"]
        ):
            i += 1
            continue

        # --- Strike-update bookkeeping lines: never telecast ---
        # ">> Strike Update: Warner hit 1 (Odd to Even rule), moves to
        # non-striker. Denly on strike." — pure strike-rotation info.
        if re.match(r"^[>\s*]*STRIKE UPDATE\b", text_upper):
            i += 1
            continue

        # --- "⚡ UPDATE (N Overs)" banner directly before a live block ---
        # Skipped here; the BATTING TEAM NAME branch below merges it into the
        # block (prevents the banner being sent twice). Otherwise keep it.
        if re.search(r"UPDATE\s*\(\d+\s+OVERS?\)", text_upper):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and lines[j].strip().upper().startswith("BATTING TEAM NAME"):
                i += 1
                continue

        # --- "BATTING TEAM NAME:" header directly before a LIVE block ---
        if text_upper.startswith("BATTING TEAM NAME"):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            nxt = lines[j].strip() if j < len(lines) else ""
            if any(kw in nxt.upper() for kw in ["LIVE SCORE", "LIVE:"]):
                # Optional "⚡ UPDATE (N Overs)" banner right before the block
                k = i - 1
                while k >= 0 and not lines[k].strip():
                    k -= 1
                prev = lines[k].strip() if k >= 0 else ""
                # The LIVE score line itself MUST be part of the block
                block = [text, nxt]
                if re.search(r"UPDATE\s*\(\d+\s+OVERS?\)", prev.upper()):
                    block.insert(0, prev)
                j += 1
                while j < len(lines):
                    nxt2 = lines[j].strip()
                    if not nxt2:
                        j += 1
                        continue
                    if _is_major_event_line(nxt2):
                        break
                    block.append(nxt2)
                    j += 1
                result.append(("\n".join(block), "score_update", 6))
                i = j
                continue
            # Standalone header (no live block after it)
            result.append((text, "over_header", 4))
            i += 1
            continue

        # --- Live score block: gather continuation lines ---
        if any(kw in text_upper for kw in ["LIVE SCORE", "LIVE:"]):
            block: list[str] = [text]
            i += 1
            while i < len(lines):
                nxt = lines[i].strip()
                if not nxt:
                    i += 1
                    continue
                if _is_major_event_line(nxt):
                    break
                block.append(nxt)
                i += 1
            result.append(("\n".join(block), "score_update", 6))
            continue

        # --- Scorecard content: enter dump mode ---
        msg_type, delay = _classify_telecast_line(text)
        if msg_type == "scorecard":
            # Once scorecard content starts, swallow ALL remaining lines:
            # they are flushed as a few large messages with no per-line
            # delays instead of one message per line.
            result.append((text, "scorecard", 0))
            result.extend((line, "dump", 0) for line in lines[i + 1:])
            return result

        # --- Everything else: classify individually ---

        # Skip non-highlight ball lines UNLESS it's the last over
        if msg_type == "ball" and not _is_highlight_ball(text):
            # Check if this ball is in the last over
            m = re.match(r"^(\d+)\.\d+\b", text)
            if m and last_over >= 0 and int(m.group(1)) == last_over:
                pass  # last over — show every ball
            else:
                i += 1
                continue

        result.append((text, msg_type, delay))
        i += 1

    return result


async def _live_telecast_sender(
    context: Any,
    chat_id: int,
    thread_id: int,
    combined_text: str,
    session: Dict[str, object],
) -> None:
    """Split combined simulation text into messages and send with delays.

    Highlights (FOUR / SIX / 3-6 runs / wicket) → 7 s, other balls → 4 s.
    Over header 4 s, over summary 4 s, score update 6 s, innings break 19 s,
    result 14 s. Toss and other normal commentary 4 s. Once scorecard content
    appears, everything remaining is flushed in 1-3 large messages with no
    per-line delays. Live-score blocks (LIVE: + batters + bowlers +
    partnership) are merged into a single message.
    """
    raw_lines = combined_text.split("\n")

    # Group lines (merges live-score blocks)
    classified = _group_lines_for_telecast(raw_lines)

    if not classified:
        return

    # Find where the trailing scorecard/dump block starts.
    # "result" is deliberately excluded: the Result line is sent as a normal
    # message so its dramatic pause is actually slept before the dump flushes.
    trailing_start = len(classified)
    for i in range(len(classified) - 1, -1, -1):
        _, msg_type, _ = classified[i]
        if msg_type not in ("scorecard", "empty", "dump"):
            trailing_start = i + 1
            break
    if classified and all(
        msg_type in ("scorecard", "empty", "dump") for _, msg_type, _ in classified
    ):
        trailing_start = 0

    # --- Send regular lines individually ---
    regular = classified[:trailing_start]
    trailing = classified[trailing_start:]
    total_regular = len(regular)

    for idx, (text, msg_type, delay) in enumerate(regular):
        if session.get("cancelled"):
            return

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                message_thread_id=thread_id,
            )
        except Exception:
            pass

        if delay > 0 and (idx < total_regular - 1 or trailing):
            await asyncio.sleep(delay)

    # --- Send trailing scorecard content as 1-3 large messages ---
    if trailing:
        trailing_text = "\n".join(text for text, _, _ in trailing)
        for chunk in _chunk_scorecard_text(trailing_text):
            if session.get("cancelled"):
                return
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    message_thread_id=thread_id,
                )
            except Exception:
                pass

    # Send the match link at the very end if present
    match_link = session.get("match_link", "")
    if match_link:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🔗 Match Link: {match_link}",
                message_thread_id=thread_id,
            )
        except Exception:
            pass

    # Clean up active broadcast tracker
    for uid, broadcast in list(_active_live_broadcasts.items()):
        if broadcast is session:
            _active_live_broadcasts.pop(uid, None)
            break


async def go_telecast_command(update: Any, context: Any) -> None:
    """Trigger live ball-by-ball telecast of the collected simulation."""
    user_id = getattr(getattr(update.message, "from_user", None), "id", None)
    is_group = update.message.chat.type in ("group", "supergroup")

    if not is_group:
        await update.message.reply_text("This command must be used in the group.")
        return

    if user_id is None or not state.is_admin(user_id):
        await update.message.reply_text("Only admins can start a live telecast.")
        return

    session = _telecast_sessions.get(user_id)
    if not session or session.get("stage") != "ready" or not session.get("parts"):
        await update.message.reply_text(
            "No simulation ready. Use /telecast then /match_link in DM first."
        )
        return

    # Verify same topic
    thread_id = update.message.message_thread_id
    if session.get("thread_id") and thread_id != session.get("thread_id"):
        await update.message.reply_text(
            "Please use /go_telecast in the same telecast topic."
        )
        return

    parts: List[str] = session["parts"]  # type: ignore
    match_link: str = session.get("match_link", "")  # type: ignore
    combined = "\n\n".join(parts)
    chat_id = session.get("chat_id")

    # Clear the session so it can't be reused
    _telecast_sessions.pop(user_id, None)

    await update.message.reply_text(
        f"🎬 Live telecast starting! ({len(parts)} parts, "
        f"{len(combined)} chars)\n\n"
        f"Messages will be sent with delays between them."
    )

    # Run the live sender in the background so the bot stays responsive
    live_session = {"cancelled": False, "match_link": match_link}
    _active_live_broadcasts[user_id] = live_session
    asyncio.create_task(
        _live_telecast_sender(context, chat_id, thread_id, combined, live_session)
    )


async def set_telecast_topic_command(update: Any, context: Any) -> None:
    """Set the group topic ID where telecast messages are sent."""
    global _telecast_topic_id
    user_id = getattr(getattr(update.message, "from_user", None), "id", None)
    is_group = update.message.chat.type in ("group", "supergroup")

    if not is_group:
        await update.message.reply_text("This command must be used in the group.")
        return

    if user_id is None or not state.is_admin(user_id):
        await update.message.reply_text("Only admins can set the telecast topic.")
        return

    thread_id = update.message.message_thread_id
    if not thread_id:
        await update.message.reply_text(
            "This command must be used inside a forum topic, not the general chat."
        )
        return

    _telecast_topic_id = thread_id
    database.set_config("telecast_topic_id", str(thread_id))
    await update.message.reply_text(
        f"✅ Telecast topic set to topic {thread_id}.\n"
        f"All telecast messages will be sent here."
    )


def build_main_keyboard(user_id: Optional[int] = None) -> Any:
    if InlineKeyboardButton is None or InlineKeyboardMarkup is None:
        return None
    rows = [
        [
            InlineKeyboardButton("Table", callback_data="standings"),
            InlineKeyboardButton("Caps", callback_data="caps"),
            InlineKeyboardButton("Help", callback_data="help"),
        ],
        [
            InlineKeyboardButton("Prompt", callback_data="prompt"),
            InlineKeyboardButton("➕Match", callback_data="add_match"),
        ],
    ]
    if user_id is not None and state.is_admin(user_id):
        rows.append([InlineKeyboardButton("Remove last match", callback_data="remove_last_match")])
        rows.append([InlineKeyboardButton("End tournament", callback_data="end_tournament")])
    return InlineKeyboardMarkup(rows)


def build_cap_page_keyboard(cap_type: str, page: int, total_pages: int) -> Any:
    if InlineKeyboardButton is None or InlineKeyboardMarkup is None:
        return None

    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton("Previous", callback_data=f"cap:{cap_type}:{page - 1}"))
    if page < total_pages - 1:
        navigation.append(InlineKeyboardButton("Next", callback_data=f"cap:{cap_type}:{page + 1}"))

    rows = []
    if navigation:
        rows.append(navigation)
    rows.append([
        InlineKeyboardButton("Orange Cap", callback_data="cap:orange:0"),
        InlineKeyboardButton("Purple Cap", callback_data="cap:purple:0"),
    ])
    rows.append([InlineKeyboardButton("Main menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def build_clear_confirmation_keyboard() -> Any:
    if InlineKeyboardButton is None or InlineKeyboardMarkup is None:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, clear tournament data", callback_data="confirm_clear_tournament"),
        InlineKeyboardButton("Cancel", callback_data="cancel_clear_tournament"),
    ]])


def build_remove_last_match_keyboard() -> Any:
    if InlineKeyboardButton is None or InlineKeyboardMarkup is None:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, remove last match", callback_data="confirm_remove_last_match"),
        InlineKeyboardButton("Cancel", callback_data="cancel_remove_last_match"),
    ]])


def handle_message(text: str, user_id: Optional[int] = None) -> str:
    text = text.strip()
    if not text:
        return build_help()

    if text.startswith("/start"):
        return "👋 Welcome! Use the buttons to view standings and caps. Only the admin can post match JSON."

    if text.startswith("/help"):
        return build_help()

    if text.startswith("/standings"):
        return format_standings(state.get_standings())

    if text.startswith("/caps"):
        return "Use /orangecap or /purplecap to view a leaderboard."

    if text.startswith("/orangecap"):
        return format_cap_page(state.players, "orange")[0]

    if text.startswith("/purplecap"):
        return format_cap_page(state.players, "purple")[0]

    if user_id is not None and not state.is_admin(user_id):
        return "⚠️ Only the admin can add match JSON."

    match_data = parse_match(text)
    if match_data:
        validation_error = validate_match_json(text)
        if validation_error:
            return f"⚠️ {validation_error} Use /prompt, paste it into AI, then send the complete JSON."
        format_error = validate_match_for_tournament(match_data)
        if format_error:
            return format_error
        state.apply_match(match_data)
        database.save_match(match_data)

        for team, stats in state.teams.items():
            database.save_standing(team, stats)

        for player, stats in state.players.items():
            database.save_player_stat(player, stats)

        _invalidate_image_cache()
        return f"✅ Match added successfully.\n\n{format_standings(state.get_standings())}"

    return "⚠️ Please send valid JSON match data. Use /prompt for the template."


async def start_command(update: Any, context: Any) -> None:
    user_id = getattr(getattr(update.message, "from_user", None), "id", None)
    await update.message.reply_text(
        format_standings(state.get_standings()),
        reply_markup=build_main_keyboard(user_id),
    )


async def help_command(update: Any, context: Any) -> None:
    user_id = getattr(getattr(update.message, "from_user", None), "id", None)
    await update.message.reply_text(build_help(), reply_markup=build_main_keyboard(user_id))


async def _send_cached_photo(update: Any, cache_key: str, img_getter: Any, caption: str) -> bool:
    """Send a photo, reusing Telegram file_id if available. Returns True if sent."""
    # Reuse file_id from a previous send — no re-upload needed
    if cache_key in _telegram_file_ids:
        try:
            await update.message.reply_photo(photo=_telegram_file_ids[cache_key], caption=caption)
            return True
        except Exception:
            # file_id expired or invalid — fall through to re-upload
            _telegram_file_ids.pop(cache_key, None)

    img = img_getter() if callable(img_getter) else img_getter
    if img is None:
        return False
    msg = await update.message.reply_photo(photo=img, caption=caption)
    # Save the file_id from the largest photo Telegram returned
    if msg and msg.photo and len(msg.photo) > 0:
        _telegram_file_ids[cache_key] = msg.photo[-1].file_id
    return True


async def standings_command(update: Any, context: Any) -> None:
    sent = await _send_cached_photo(update, "standings", get_standings_image, "The Hundred Tournament - Standings")
    if not sent:
        user_id = getattr(getattr(update.message, "from_user", None), "id", None)
        await update.message.reply_text(format_standings(state.get_standings()), reply_markup=build_main_keyboard(user_id))


async def caps_command(update: Any, context: Any) -> None:
    await update.message.reply_text(
        "Choose a leaderboard.",
        reply_markup=build_cap_page_keyboard("orange", 0, 1),
    )


async def orange_cap_command(update: Any, context: Any) -> None:
    sent = await _send_cached_photo(update, "orange_cap", lambda: get_cap_image("orange"), "The Hundred - Orange Cap")
    if not sent:
        text, _ = format_cap_page(state.players, "orange")
        await update.message.reply_text(text)


async def purple_cap_command(update: Any, context: Any) -> None:
    sent = await _send_cached_photo(update, "purple_cap", lambda: get_cap_image("purple"), "The Hundred - Purple Cap")
    if not sent:
        text, _ = format_cap_page(state.players, "purple")
        await update.message.reply_text(text)


async def set_admin_command(update: Any, context: Any) -> None:
    user_id = None
    if getattr(update.message, "from_user", None) is not None:
        user_id = update.message.from_user.id


    if user_id is None or not state.is_admin(user_id):
        await update.message.reply_text("Only admins can change admin access.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /setadmin <user_id>")
        return

    try:
        new_admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("The admin id must be a number.")
        return

    state.set_admin(new_admin_id)
    await update.message.reply_text(f"Added admin {new_admin_id}.")




async def handle_callback_query(update: Any, context: Any) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user_id = getattr(getattr(query, "from_user", None), "id", None)
    
    if data.startswith("cap:"):
        try:
            _, cap_type, page_text = data.split(":", 2)
            page = int(page_text)
            text, total_pages = format_cap_page(state.players, cap_type, page)
            page = max(0, min(page, total_pages - 1))
        except (TypeError, ValueError):
            await query.message.reply_text("Unable to open that leaderboard page.")
            return
        if getattr(query.message, "text", None) == text:
            return
        await query.edit_message_text(
            text,
            reply_markup=build_cap_page_keyboard(cap_type, page, total_pages),
        )
    elif data == "main_menu":
        await query.edit_message_text(
            format_standings(state.get_standings()),
            reply_markup=build_main_keyboard(user_id),
        )
    elif data == "standings":
        text = format_standings(state.get_standings())
        await query.message.reply_text(text, reply_markup=build_main_keyboard(user_id))
    elif data == "caps":
        await query.message.reply_text(
            "Choose a leaderboard.",
            reply_markup=build_cap_page_keyboard("orange", 0, 1),
        )
    elif data == "help":
        text = build_help()
        await query.message.reply_text(text, reply_markup=build_main_keyboard(user_id))
    elif data == "prompt":
        # Send notification in group
        await query.message.reply_text("✅ Prompt sent to your DM. Check your private chat with the bot.")
        # Send actual prompt via DM
        try:
            text = "Prompt and JSON example:\n\n" + build_match_prompt()
            max_len = 4000
            if len(text) <= max_len:
                await context.bot.send_message(chat_id=user_id, text=text)
            else:
                parts = []
                while text:
                    if len(text) <= max_len:
                        parts.append(text)
                        break
                    split_at = text.rfind("\n", 0, max_len)
                    if split_at == -1:
                        split_at = max_len
                    parts.append(text[:split_at])
                    text = text[split_at:].lstrip("\n")
                for part in parts:
                    await context.bot.send_message(chat_id=user_id, text=part)
        except Exception:
            await query.message.reply_text("Could not send DM. Please start a private chat with the bot first.")
    elif data == "add_match":

        if user_id is None or not state.is_admin(user_id):
            await query.answer(
                "Only admins can add matches.",
                show_alert=True
            )
            return

        waiting_for_match[user_id] = True

        # Start 5 minute timeout with a session token
        _match_session_tokens[user_id] = _match_session_tokens.get(user_id, 0) + 1
        asyncio.create_task(
            cancel_match_wait(user_id, _match_session_tokens[user_id])
        )

        await query.answer(
            "Waiting for match JSON"
        )

        # Notify in group
        await query.message.reply_text(
            "✅ Send the match JSON in your DM."
        )
        # Send instructions via DM
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="📥 Send the match JSON now.\n\n"
                     "Only your next message will be processed as a match.\n"
                     "All other messages are ignored."
            )
        except Exception:
            await query.message.reply_text("Could not send DM. Please start a private chat with the bot first.")
    elif data == "remove_last_match":
        if user_id is None or not state.is_admin(user_id):
            await query.message.reply_text("Only an admin can remove a match.")
            return
        if not state.matches:
            await query.message.reply_text("There is no recorded match to remove.")
            return
        last_match = state.matches[-1]
        await query.message.reply_text(
            f"Remove the latest match: {last_match['team1']} vs {last_match['team2']}? "
            "This will recalculate the table and cap leaderboards.",
            reply_markup=build_remove_last_match_keyboard(),
        )
    elif data == "confirm_remove_last_match":
        if user_id is None or not state.is_admin(user_id):
            await query.message.reply_text("Only an admin can remove a match.")
            return
        if remove_last_match():
            await query.message.reply_text(
                "Latest match removed.\n\n" + format_standings(state.get_standings()),
                reply_markup=build_main_keyboard(user_id),
            )
        else:
            await query.message.reply_text("There is no recorded match to remove.")
    elif data == "cancel_remove_last_match":
        await query.message.reply_text("The latest match was not removed.", reply_markup=build_main_keyboard(user_id))
    elif data == "end_tournament":
        if user_id is None or not state.is_admin(user_id):
            await query.message.reply_text("Only an admin can end and clear a tournament.")
            return
        await query.message.reply_text(
            "Tournament ended? This will permanently delete all standings, player statistics, and recorded matches. "
            "Confirm only when you are ready to start a new tournament.",
            reply_markup=build_clear_confirmation_keyboard(),
        )
    elif data == "confirm_clear_tournament":
        if user_id is None or not state.is_admin(user_id):
            await query.message.reply_text("Only an admin can clear tournament data.")
            return
        state.clear_tournament()
        database.clear_tournament()
        _invalidate_image_cache()
        await query.message.reply_text(
            "Tournament data cleared. A new tournament is ready to begin.",
            reply_markup=build_main_keyboard(user_id),
        )
    elif data == "cancel_clear_tournament":
        await query.message.reply_text(
            "Tournament data was not deleted.",
            reply_markup=build_main_keyboard(user_id),
        )
    else:
        text = "Choose an option below."
        await query.message.reply_text(text, reply_markup=build_main_keyboard(user_id))


def _load_monospace_font(size: int = 16):
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "C:\\Windows\\Fonts\\consola.ttf",
    ]
    if ImageFont is None:
        return None
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default(size)


def _generate_standings_image(standings: list) -> bytes:
    font = _load_monospace_font(16)
    bold_font = _load_monospace_font(18)
    title = "The Hundred Tournament"
    headers = ["#", "Team", "P", "W", "D", "L", "Pts", "NRR"]
    rows = []
    for idx, item in enumerate(standings, start=1):
        rows.append([
            str(idx),
            _resolve_team_label(str(item["team"])),
            str(item["played"]),
            str(item["wins"]),
            str(item["draws"]),
            str(item["losses"]),
            str(item["points"]),
            f"{item['net_run_rate']:.3f}",
        ])

    pad = 20
    row_h = 32
    header_h = 40
    title_h = 45
    margin = 30

    col_widths = []
    for col_idx in range(len(headers)):
        max_w = font.getlength(headers[col_idx]) if font else len(headers[col_idx]) * 10
        for row in rows:
            w = font.getlength(row[col_idx]) if font else len(row[col_idx]) * 10
            max_w = max(max_w, w)
        col_widths.append(int(max_w) + 24)

    img_w = sum(col_widths) + 2 * pad + 2 * margin
    img_h = pad + title_h + header_h + row_h * len(rows) + margin
    bg = (20, 20, 30)
    accent = (255, 165, 0)
    text_color = (220, 220, 220)
    header_bg = (40, 40, 55)
    even_bg = (28, 28, 40)

    img = Image.new("RGB", (img_w, img_h), bg) if Image else None
    if img is None:
        return b""
    draw = ImageDraw.Draw(img)

    y = pad
    draw.text((margin, y), title, fill=accent, font=bold_font)
    y += title_h
    x = margin
    for col_idx, header in enumerate(headers):
        draw.rectangle([x, y, x + col_widths[col_idx], y + header_h], fill=header_bg)
        draw.text((x + 8, y + 10), header, fill=text_color, font=bold_font)
        x += col_widths[col_idx]

    y += header_h
    for row_idx, row in enumerate(rows):
        row_bg = bg if row_idx % 2 == 0 else even_bg
        x = margin
        for col_idx, cell in enumerate(row):
            draw.rectangle([x, y, x + col_widths[col_idx], y + row_h], fill=row_bg)
            color = accent if col_idx == 1 and row_idx == 0 else text_color
            draw.text((x + 8, y + 7), cell, fill=color, font=font)
            x += col_widths[col_idx]
        y += row_h

    draw.rectangle([margin, y, margin + sum(col_widths), y + 4], fill=accent)

    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _generate_cap_image(cap_type: str, leaders: list) -> bytes:
    leaders = leaders[:10]  # Only show top 10
    font = _load_monospace_font(16)
    bold_font = _load_monospace_font(18)
    if cap_type == "orange":
        title = "The Hundred - Orange Cap"
        headers = ["#", "Player", "Runs", "SR"]
        accent = (255, 140, 0)
    else:
        title = "The Hundred - Purple Cap"
        headers = ["#", "Player", "Wkts", "Econ"]
        accent = (148, 0, 211)

    rows = []
    for idx, item in enumerate(leaders, start=1):
        player_label = _format_cap_player_name(str(item["name"]))
        if cap_type == "orange":
            rows.append([str(idx), player_label, str(item["runs"]), f"{item['strike_rate']:.2f}"])
        else:
            rows.append([str(idx), player_label, str(item["wickets"]), f"{item['economy']:.2f}"])

    pad = 20
    row_h = 32
    header_h = 40
    title_h = 45
    margin = 30

    col_widths = []
    for col_idx in range(len(headers)):
        max_w = font.getlength(headers[col_idx]) if font else len(headers[col_idx]) * 10
        for row in rows:
            w = font.getlength(row[col_idx]) if font else len(row[col_idx]) * 10
            max_w = max(max_w, w)
        col_widths.append(int(max_w) + 24)

    img_w = sum(col_widths) + 2 * pad + 2 * margin
    img_h = pad + title_h + header_h + row_h * len(rows) + margin
    bg = (20, 20, 30)
    text_color = (220, 220, 220)
    header_bg = (40, 40, 55)
    even_bg = (28, 28, 40)

    img = Image.new("RGB", (img_w, img_h), bg) if Image else None
    if img is None:
        return b""
    draw = ImageDraw.Draw(img)

    draw.text((margin, pad), title, fill=accent, font=bold_font)
    y = pad + title_h
    x = margin
    for col_idx, header in enumerate(headers):
        draw.rectangle([x, y, x + col_widths[col_idx], y + header_h], fill=header_bg)
        draw.text((x + 8, y + 10), header, fill=text_color, font=bold_font)
        x += col_widths[col_idx]

    y += header_h
    for row_idx, row in enumerate(rows):
        row_bg = bg if row_idx % 2 == 0 else even_bg
        x = margin
        for col_idx, cell in enumerate(row):
            draw.rectangle([x, y, x + col_widths[col_idx], y + row_h], fill=row_bg)
            color = accent if col_idx == 1 and row_idx == 0 else text_color
            draw.text((x + 8, y + 7), cell, fill=color, font=font)
            x += col_widths[col_idx]
        y += row_h

    draw.rectangle([margin, y, margin + sum(col_widths), y + 4], fill=accent)

    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _invalidate_image_cache() -> None:
    _image_cache.clear()
    _telegram_file_ids.clear()


def get_standings_image() -> Optional[bytes]:
    standings = state.get_standings()
    if not standings:
        return None
    cache_key = json.dumps(standings, sort_keys=True)
    if cache_key in _image_cache:
        return _image_cache[cache_key]
    img_bytes = _generate_standings_image(standings)
    if img_bytes:
        _image_cache[cache_key] = img_bytes
    return img_bytes or None


def get_cap_image(cap_type: str) -> Optional[bytes]:
    leaders = get_caps_leaders(state.players, cap_type, top_n=len(state.players))
    if not leaders:
        return None
    cache_key = f"cap:{cap_type}:" + json.dumps(leaders, sort_keys=True)
    if cache_key in _image_cache:
        return _image_cache[cache_key]
    img_bytes = _generate_cap_image(cap_type, leaders)
    if img_bytes:
        _image_cache[cache_key] = img_bytes
    return img_bytes or None


async def setshort_command(update: Any, context: Any) -> None:
    user_id = getattr(getattr(update.message, "from_user", None), "id", None)
    if user_id is None or not state.is_admin(user_id):
        await update.message.reply_text("Only admins can set team short codes.")
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /setshort CODE TeamName\nExample: /setshort IND India"
        )
        return
    code = context.args[0].upper().strip()
    team_name = " ".join(context.args[1:]).strip()
    if not code or not team_name:
        await update.message.reply_text("Both short code and team name are required.")
        return
    team_mappings[team_name] = code
    database.save_team_shortcodes(team_mappings)
    await update.message.reply_text(f"Mapped {team_name} -> {code}")


async def table_command(update: Any, context: Any) -> None:
    sent = await _send_cached_photo(update, "standings", get_standings_image, "The Hundred Tournament - Standings")
    if not sent:
        await update.message.reply_text(format_standings(state.get_standings()))


async def orange_cap_image_command(update: Any, context: Any) -> None:
    sent = await _send_cached_photo(update, "orange_cap", lambda: get_cap_image("orange"), "The Hundred - Orange Cap")
    if not sent:
        text, _ = format_cap_page(state.players, "orange")
        await update.message.reply_text(text)


async def purple_cap_image_command(update: Any, context: Any) -> None:
    sent = await _send_cached_photo(update, "purple_cap", lambda: get_cap_image("purple"), "The Hundred - Purple Cap")
    if not sent:
        text, _ = format_cap_page(state.players, "purple")
        await update.message.reply_text(text)


def run_console() -> None:
    print("Console mode enabled. Type /help for commands or enter a match result.")
    while True:
        try:
            text = input("> ").strip()
        except KeyboardInterrupt:
            print("Goodbye!")
            break
        if not text:
            continue
        if text in {"exit", "quit"}:
            print("Goodbye!")
            break
        print(handle_message(text))

async def handle_text_message(update, context) -> None:
    if not update.message or not update.message.text:
        return

    is_group = update.message.chat.type in ("group", "supergroup")
    is_dm = update.message.chat.type == "private"

    # In group: only process if in the correct topic
    if is_group and MATCH_TOPIC_ID is not None:
        if update.message.message_thread_id != MATCH_TOPIC_ID:
            return

    user_id = update.message.from_user.id if update.message.from_user else None

    # --- Telecast message collection (DM input only) ---
    if is_dm and user_id in _telecast_sessions:
        session = _telecast_sessions[user_id]
        stage = session["stage"]

        if stage == "collecting":
            # Unlimited parts — keep collecting until /match_link
            parts: List[str] = session["parts"]  # type: ignore
            parts.append(update.message.text)
            await update.message.reply_text(
                f"✅ Part {len(parts)} received ({sum(len(p) for p in parts)} chars total).\n\n"
                f"Send the next part, or /match_link when done."
            )
            return

        elif stage == "link":
            # User sent the match link
            session["match_link"] = update.message.text.strip()
            session["stage"] = "ready"
            await update.message.reply_text(
                f"✅ Match link received!\n\n"
                f"Go to the group topic and use /go_telecast to start the live broadcast,"
                f"\nor /cancel_telecast in the group to discard."
            )
            return

        elif stage == "ready":
            # User sent extra text while ready — ignore
            return

    # Ignore every message unless admin clicked Add Match
    if user_id not in waiting_for_match:
        return

    text = update.message.text

    # Accept only ONE message
    waiting_for_match.pop(user_id, None)

    match_data = parse_match(text)

    if match_data:
        validation_error = validate_match_json(text)
        if validation_error:
            await update.message.reply_text(
                f"❌ {validation_error}\n\nUse the Prompt button, paste it into AI, then send the complete JSON."
            )
            return
        format_error = validate_match_for_tournament(match_data)
        if format_error:
            await update.message.reply_text(f"❌ {format_error}")
            return
        state.apply_match(match_data)
        database.save_match(match_data)
        for team, stats in state.teams.items():
            database.save_standing(team, stats)

        for player, stats in state.players.items():
            database.save_player_stat(player, stats)
        _invalidate_image_cache()

        # Confirm in DM
        await update.message.reply_text("✅ Match added successfully!")

        # Also send standings to the group if match was submitted from DM
        if is_dm and MATCH_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=MATCH_CHAT_ID,
                    text=f"✅ Match added by {update.message.from_user.first_name}.\n\n"
                         f"{format_standings(state.get_standings())}",
                    reply_markup=build_main_keyboard(user_id),
                )
            except Exception:
                pass
        elif is_group:
            await update.message.reply_text(
                f"✅ Match added successfully.\n\n"
                f"{format_standings(state.get_standings())}",
                reply_markup=build_main_keyboard(user_id)
            )
    else:
        await update.message.reply_text(
            "❌ Invalid match JSON.\nUse /help for the correct format."
        )

logger = logging.getLogger(__name__)


async def _handle_error(update: object, context: Any) -> None:
    """Log unhandled exceptions from handlers instead of crashing silently."""
    error = getattr(context, "error", None)

    # Network hiccups (TimeOut, connect failures) are transient — log them
    # briefly at WARNING level; Telegram polling retries on its own.
    from telegram.error import TimedOut, NetworkError  # noqa: PLC0415 — keep import local

    if isinstance(error, (TimedOut, NetworkError)):
        logger.warning("Telegram network issue (%s); operation will be retried automatically.", type(error).__name__)
        return

    logger.error("Exception while processing an update:", exc_info=error)


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        level=logging.INFO,
    )

    if not TOKEN:
        print("No TELEGRAM_BOT_TOKEN found. Starting console mode instead.")
        run_console()
        return

    if Application is None:
        print("python-telegram-bot is not installed. Install it with: pip install python-telegram-bot")
        run_console()
        return

    app = (
        Application.builder()
        .token(TOKEN)
        # More tolerant network timeouts: short defaults cause spurious
        # TimedOut errors on slow or flaky connections.
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .build()
    )
    app.add_error_handler(_handle_error)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("standings", standings_command))
    app.add_handler(CommandHandler("caps", caps_command))
    app.add_handler(CommandHandler("orangecap", orange_cap_command))
    app.add_handler(CommandHandler("purplecap", purple_cap_command))
    app.add_handler(CommandHandler("setadmin", set_admin_command))
    app.add_handler(CommandHandler("table", table_command))
    app.add_handler(CommandHandler("setshort", setshort_command))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(
        CommandHandler(
            "prompt",
            prompt_command
        )
    )
    app.add_handler(
        CommandHandler(
            "simulation_prompt",
            simulation_prompt_command
        )
    )
    app.add_handler(
        CommandHandler(
            "upcoming_prompt",
            upcoming_prompt_command
        )
    )
    app.add_handler(
        CommandHandler(
            "add_match",
            add_match_command
        )
    )
    app.add_handler(
        CommandHandler(
            "cancel_match",
            cancel_match_command
        )
    )
    app.add_handler(
        CommandHandler(
            "telecast",
            telecast_command
        )
    )
    app.add_handler(
        CommandHandler(
            "match_link",
            match_link_command
        )
    )
    app.add_handler(
        CommandHandler(
            "start_telecast",
            start_telecast_command
        )
    )
    app.add_handler(
        CommandHandler(
            "cancel_telecast",
            cancel_telecast_command
        )
    )
    app.add_handler(
        CommandHandler(
            "set_telecast_topic",
            set_telecast_topic_command
        )
    )
    app.add_handler(
        CommandHandler(
            "go_telecast",
            go_telecast_command
        )
    )

    app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_text_message
    )
    
)

    # Register slash commands so Telegram shows the suggestion menu
    from telegram import BotCommand
    bot_commands = [
        # General
        BotCommand("start", "Welcome message"),
        BotCommand("help", "Show help"),
        # Standings & Stats
        BotCommand("table", "View standings image"),
        BotCommand("standings", "View the points table"),
        BotCommand("orangecap", "Orange Cap leaderboard"),
        BotCommand("purplecap", "Purple Cap leaderboard"),
        BotCommand("caps", "Choose a cap leaderboard"),
        # Match Management
        BotCommand("add_match", "Submit a match JSON"),
        BotCommand("cancel_match", "Cancel pending match submission"),
        BotCommand("prompt", "Get the match JSON prompt"),
        BotCommand("simulation_prompt", "Get the match simulation prompt"),
        BotCommand("upcoming_prompt", "Get the Hundred simulation prompt"),
        # Live Telecast
        BotCommand("set_telecast_topic", "Set telecast topic (admin, group)"),
        BotCommand("telecast", "Start collecting simulation messages"),
        BotCommand("match_link", "Send match link after collecting parts"),
        BotCommand("start_telecast", "Finish collecting (legacy, redirects)"),
        BotCommand("go_telecast", "Start live ball-by-ball telecast"),
        BotCommand("cancel_telecast", "Cancel active telecast"),
        # Admin
        BotCommand("setshort", "Register a team short code"),
        BotCommand("setadmin", "Add an admin user"),
    ]

    async def post_init(application) -> None:
        await application.bot.set_my_commands(bot_commands)

    app.post_init = post_init

    print("Bot started. Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
