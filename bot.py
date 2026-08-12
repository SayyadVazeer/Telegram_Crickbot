import json
import os
import re
import sqlite3
import asyncio
import hashlib
from typing import Any, Dict, List, Optional

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

async def cancel_match_wait(user_id: int):
    await asyncio.sleep(300)

    if waiting_for_match.get(user_id):
        waiting_for_match.pop(user_id, None)

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
                "wickets_taken": 0,
                "wickets_lost": 0,
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
        self.teams[team1]["wickets_taken"] += wickets2
        self.teams[team1]["wickets_lost"] += wickets1
        self.teams[team1]["balls_faced"] += balls1
        self.teams[team1]["balls_bowled"] += balls2
        if counts_for_nrr:
            self.teams[team1]["nrr_runs_scored"] += nrr_score1
            self.teams[team1]["nrr_runs_conceded"] += nrr_score2
            self.teams[team1]["nrr_balls_faced"] += nrr_balls1
            self.teams[team1]["nrr_balls_bowled"] += nrr_balls2

        self.teams[team2]["runs_scored"] += score2
        self.teams[team2]["runs_conceded"] += score1
        self.teams[team2]["wickets_taken"] += wickets1
        self.teams[team2]["wickets_lost"] += wickets2
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
                    safe_name = str(player_name)
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
        for line in innings["section"].splitlines():
            bowler_match = re.match(
                r"^(?P<player>[A-Za-z.\- ]+?)\s{2,}(?P<overs>\d+(?:\.\d+)?)\s+(?P<runs>\d+)\s+(?P<wickets>\d+)(?:\s+(?P<econ>\d+(?:\.\d+)?))?$",
                line.strip(),
            )
            if bowler_match:
                player_name = bowler_match.group("player").strip()
                players_payload[player_name] = {
                    "runs": 0,
                    "wickets": int(bowler_match.group("wickets")),
                    "balls_faced": 0,
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

                team1 = str(payload.get("team1", "")).strip()
                team2 = str(payload.get("team2", "")).strip()
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
        return {
            "team1": compact_match.group("team1").strip(),
            "score1": int(compact_match.group("score1")),
            "wickets1": int(compact_match.group("wickets1") or 0),
            "balls1": int(compact_match.group("balls1")),
            "team2": compact_match.group("team2").strip(),
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
    instructions = (
        "Generate a single JSON object for the cricket match simulated above. Use the actual team and player names from the match details. "
        "Return only valid JSON with no markdown fences, explanations, or extra text. Required fields: match_type, balls_per_over, balls_per_innings, team1, team2, score1, wickets1, balls1, score2, wickets2, balls2, players. "
        "balls1 and balls2 must be integer legal balls actually faced, never overs notation. The first match locks the tournament NRR format: use six-ball overs for T20/ODI (include overs, such as 20 or 50), or five-ball overs for The Hundred (set match_type to 'The Hundred', balls_per_innings to 100, and balls_per_over to 5). Every later match must use that same format. "
        "NRR is cumulative: use actual balls for a successful chase; if all out early, provide the actual balls and wickets=10 so the full allotted quota is applied automatically. "
        "For a DLS-adjusted result, include nrr_score1, nrr_balls1, nrr_score2, nrr_balls2, and nrr_quota_balls with the official NRR-accredited scores, balls, and revised allocation; otherwise omit them. "
        "For an abandoned/no-result match, set result_type to 'no_result' and exclude_from_nrr to true; it awards one point to each team but adds no NRR totals. Never include Super Over runs or balls. "
        "Each player entry must include runs, balls_faced, wickets, runs_conceded, and balls_bowled."
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
            f"{idx}. {item['team']}\n"
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
        orange_lines.append(f"{idx}. {prefix}{item['name']}: {item['runs']} runs, SR {item['strike_rate']}")

    purple_lines = []
    for idx, item in enumerate(purple, start=1):
        prefix = "🟣 " if idx == 1 else ""
        purple_lines.append(f"{idx}. {prefix}{item['name']}: {item['wickets']} wickets, Econ {item['economy']}")

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
            lines.append(f"{rank}. {prefix}{item['name']}: {item['runs']} runs, SR {item['strike_rate']}")
        else:
            prefix = "🟣 " if rank == 1 else ""
            lines.append(f"{rank}. {prefix}{item['name']}: {item['wickets']} wickets, Econ {item['economy']}")

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
    return True

# Optional:
# Put your match topic ID here if using Telegram forum topics.
# Example: MATCH_TOPIC_ID = 123456
# Set to None to allow any topic.
MATCH_TOPIC_ID = None
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
        "Send only JSON match data.\n"
        "Buttons:\n"
        "/start - welcome message\n"
        "/standings - view the points table\n"
        "/caps - choose an Orange Cap or Purple Cap leaderboard\n"
        "/orangecap - show Orange Cap leaderboard\n"
        "/purplecap - show Purple Cap leaderboard\n"
        "Admins can use End tournament to clear data after confirmation.\n"
        "/help - show this help\n\n"
        "/Prompt - Display the complete prompt and JSON example for copy paste\n"
    )

async def prompt_command(update, context):

    await update.message.reply_text(
        build_match_prompt()
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


async def standings_command(update: Any, context: Any) -> None:
    user_id = getattr(getattr(update.message, "from_user", None), "id", None)
    await update.message.reply_text(format_standings(state.get_standings()), reply_markup=build_main_keyboard(user_id))


async def caps_command(update: Any, context: Any) -> None:
    await update.message.reply_text(
        "Choose a leaderboard.",
        reply_markup=build_cap_page_keyboard("orange", 0, 1),
    )


async def orange_cap_command(update: Any, context: Any) -> None:
    text, total_pages = format_cap_page(state.players, "orange")
    await update.message.reply_text(text, reply_markup=build_cap_page_keyboard("orange", 0, total_pages))


async def purple_cap_command(update: Any, context: Any) -> None:
    text, total_pages = format_cap_page(state.players, "purple")
    await update.message.reply_text(text, reply_markup=build_cap_page_keyboard("purple", 0, total_pages))


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
        text = "Prompt and JSON example:\n\n" + build_match_prompt()
        await query.message.reply_text(text, reply_markup=build_main_keyboard(user_id))
    elif data == "add_match":

        if user_id is None or not state.is_admin(user_id):
            await query.answer(
                "Only admins can add matches.",
                show_alert=True
            )
            return

        waiting_for_match[user_id] = True

        # Start 5 minute timeout
        asyncio.create_task(
            cancel_match_wait(user_id)
        )

        await query.answer(
            "Waiting for match JSON"
        )


        await query.message.reply_text(
            "📥 Send the match JSON now.\n\n"
            "Only your next message will be processed as a match.\n"
            "All other group messages are ignored."
        )
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

    if MATCH_TOPIC_ID is not None:
        if update.message.message_thread_id != MATCH_TOPIC_ID:
            return

    user_id = update.message.from_user.id if update.message.from_user else None

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
        await update.message.reply_text(
            f"✅ Match added successfully.\n\n"
            f"{format_standings(state.get_standings())}",
            reply_markup=build_main_keyboard(user_id)
        )
    else:
        await update.message.reply_text(
            "❌ Invalid match JSON.\nUse /help for the correct format."
        )

def main() -> None:
    if not TOKEN:
        print("No TELEGRAM_BOT_TOKEN found. Starting console mode instead.")
        run_console()
        return

    if Application is None:
        print("python-telegram-bot is not installed. Install it with: pip install python-telegram-bot")
        run_console()
        return

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("standings", standings_command))
    app.add_handler(CommandHandler("caps", caps_command))
    app.add_handler(CommandHandler("orangecap", orange_cap_command))
    app.add_handler(CommandHandler("purplecap", purple_cap_command))
    app.add_handler(CommandHandler("setadmin", set_admin_command))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(
        CommandHandler(
            "prompt",
            prompt_command
        )
    )

    app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_text_message
    )
    
)

    print("Bot started. Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
