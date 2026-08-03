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

    def set_admin(self, user_id: int) -> None:
        self.admin_ids.add(user_id)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    def clear_tournament(self) -> None:
        """Clear tournament results while retaining configured admin access."""
        self.teams.clear()
        self.players.clear()
        self.matches.clear()


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
        self.add_team(team1)
        self.add_team(team2)

        self.teams[team1]["played"] += 1
        self.teams[team2]["played"] += 1

        score1 = int(match_data["score1"])
        score2 = int(match_data["score2"])
        wickets1 = int(match_data["wickets1"])
        wickets2 = int(match_data["wickets2"])
        balls1 = int(match_data["balls1"])
        balls2 = int(match_data["balls2"])

        self.teams[team1]["runs_scored"] += score1
        self.teams[team1]["runs_conceded"] += score2
        self.teams[team1]["wickets_taken"] += wickets2
        self.teams[team1]["wickets_lost"] += wickets1
        self.teams[team1]["balls_faced"] += balls1
        self.teams[team1]["balls_bowled"] += balls2

        self.teams[team2]["runs_scored"] += score2
        self.teams[team2]["runs_conceded"] += score1
        self.teams[team2]["wickets_taken"] += wickets1
        self.teams[team2]["wickets_lost"] += wickets2
        self.teams[team2]["balls_faced"] += balls2
        self.teams[team2]["balls_bowled"] += balls1

        overs1 = balls1 / balls_per_over
        overs2 = balls2 / balls_per_over
        quota_overs = quota_balls / balls_per_over
        if wickets1 >= 10 and balls1 < quota_balls:
            overs1 = quota_overs
        if wickets2 >= 10 and balls2 < quota_balls:
            overs2 = quota_overs

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

        if score1 > score2:
            self.teams[team1]["wins"] += 1
            self.teams[team1]["points"] += 2
            self.teams[team2]["losses"] += 1
        elif score2 > score1:
            self.teams[team2]["wins"] += 1
            self.teams[team2]["points"] += 2
            self.teams[team1]["losses"] += 1
        else:
            self.teams[team1]["draws"] += 1
            self.teams[team2]["draws"] += 1
            self.teams[team1]["points"] += 1
            self.teams[team2]["points"] += 1

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
            nrr = (stats["runs_scored"] / overs_faced if overs_faced else 0.0) - (stats["runs_conceded"] / overs_bowled if overs_bowled else 0.0)

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
        self.path = path
        directory = os.path.dirname(self.path)

        if directory:
            os.makedirs(directory, exist_ok=True)

        with sqlite3.connect(self.path) as conn:
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
                overs_bowled REAL DEFAULT 0
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

            """)

    def clear_tournament(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("DELETE FROM standings")
            conn.execute("DELETE FROM player_stats")
            conn.execute("DELETE FROM matches")
    def save_match(self, match):

        with sqlite3.connect(self.path) as conn:

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

        with sqlite3.connect(self.path) as conn:

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



    def save_player_stat(self,name,stats):

        with sqlite3.connect(self.path) as conn:

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

        with sqlite3.connect(self.path) as conn:

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
        for key in ("match_type", "overs", "balls_per_innings", "balls_per_over")
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
    return (
        "Generate a single JSON object for the cricket match simulated above. Use the actual team and player names from the match details. "
        "Return only valid JSON with no markdown fences, explanations, or extra text. Required fields: match_type, team1, team2, score1, wickets1, balls1, score2, wickets2, balls2, players. "
        "balls1 and balls2 must be integer legal balls actually faced, never overs notation. For T20/ODI, include overs (for example 20 or 50); these use six balls per over. "
        "For The Hundred, set match_type to 'The Hundred', balls_per_innings to 100, and balls_per_over to 5. "
        "Each player entry must include runs, balls_faced, wickets, runs_conceded, and balls_bowled."
    )

def build_match_JsonSample() -> str:
    example_payload = json.dumps({
            "match_type": "T20I",
            "overs": 20,
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
        "Sample JSON:\n"
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

    ordered = sorted(players.items(), key=sort_key)
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


state = TournamentState()
database = TournamentDatabase(DATABASE_PATH)
print(f"Database loaded: {DATABASE_PATH}")
print(os.path.exists(DATABASE_PATH))
saved = database.load_all()

state.teams = saved["teams"]
state.players = saved["players"]
state.matches = saved["matches"]
# Stores admins who clicked "Add Match" and are allowed to send one JSON
waiting_for_match: Dict[int, bool] = {}

# Optional:
# Put your match topic ID here if using Telegram forum topics.
# Example: MATCH_TOPIC_ID = 123456
# Set to None to allow any topic.
MATCH_TOPIC_ID = None
for admin_id in parse_admin_ids(ADMIN_USER_IDS):
    state.set_admin(admin_id)


def _is_match_payload(text: str) -> bool:
    return parse_match(text) is not None


def build_help() -> str:
    
    return (
        "Send only JSON match data.\n"
        "Buttons:\n"
        "/start - welcome message\n"
        "/standings - view the points table\n"
        "/caps - show purple cap and orange cap leaders\n"
        "Admins can use End tournament to clear data after confirmation.\n"
        "/help - show this help\n\n"
        "/Prompt - Display prompt for generating match JSON for copy paste \n"
        "/JsonSample - Display Json Sample expecting \n"
    )

async def prompt_command(update, context):

    await update.message.reply_text(
        build_match_prompt()
    )


async def jsonsample_command(update, context):

    await update.message.reply_text(
        build_match_JsonSample()
    )

def build_main_keyboard(user_id: Optional[int] = None) -> Any:
    if InlineKeyboardButton is None or InlineKeyboardMarkup is None:
        return None
    rows = [
        [
            InlineKeyboardButton("Standings", callback_data="standings"),
            InlineKeyboardButton("Caps", callback_data="caps"),
            InlineKeyboardButton("Help", callback_data="help"),
        ],
        [
            InlineKeyboardButton("Prompt", callback_data="prompt"),
            InlineKeyboardButton("JsonSample", callback_data="JsonSample"),
            InlineKeyboardButton("➕Match", callback_data="add_match"),
        ],
    ]
    if user_id is not None and state.is_admin(user_id):
        rows.append([InlineKeyboardButton("End tournament", callback_data="end_tournament")])
    return InlineKeyboardMarkup(rows)


def build_clear_confirmation_keyboard() -> Any:
    if InlineKeyboardButton is None or InlineKeyboardMarkup is None:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, clear tournament data", callback_data="confirm_clear_tournament"),
        InlineKeyboardButton("Cancel", callback_data="cancel_clear_tournament"),
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
        return format_caps(state.players)

    if user_id is not None and not state.is_admin(user_id):
        return "⚠️ Only the admin can add match JSON."

    match_data = parse_match(text)
    if match_data:
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
        "Welcome! Use the buttons below to manage the tournament.",
        reply_markup=build_main_keyboard(user_id),
    )


async def help_command(update: Any, context: Any) -> None:
    user_id = getattr(getattr(update.message, "from_user", None), "id", None)
    await update.message.reply_text(build_help(), reply_markup=build_main_keyboard(user_id))


async def standings_command(update: Any, context: Any) -> None:
    user_id = getattr(getattr(update.message, "from_user", None), "id", None)
    await update.message.reply_text(format_standings(state.get_standings()), reply_markup=build_main_keyboard(user_id))


async def caps_command(update: Any, context: Any) -> None:
    user_id = getattr(getattr(update.message, "from_user", None), "id", None)
    await update.message.reply_text(format_caps(state.players), reply_markup=build_main_keyboard(user_id))


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
    
    if data == "standings":
        text = format_standings(state.get_standings())
        await query.message.reply_text(text, reply_markup=build_main_keyboard(user_id))
    elif data == "caps":
        text = format_caps(state.players)
        await query.message.reply_text(text, reply_markup=build_main_keyboard(user_id))
    elif data == "help":
        text = build_help()
        await query.message.reply_text(text, reply_markup=build_main_keyboard(user_id))
    elif data == "prompt":
        text = "Prompt:\n\n" + build_match_prompt()
        await query.message.reply_text(text, reply_markup=build_main_keyboard(user_id))
    elif data == "JsonSample":
        text = "Sample:\n\n" + build_match_JsonSample()
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
    app.add_handler(CommandHandler("setadmin", set_admin_command))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(
        CommandHandler(
            "prompt",
            prompt_command
        )
    )


    app.add_handler(
        CommandHandler(
            "jsonsample",
            jsonsample_command
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
