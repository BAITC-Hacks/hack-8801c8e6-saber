"""SQLite persistence for saved scenarios (var/akim.db)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "var" / "akim.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scenarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team TEXT NOT NULL,
    score REAL NOT NULL,
    total_cost REAL NOT NULL,
    decisions_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def save_scenario(team: str, decisions: list[dict[str, Any]], score: float, total_cost: float, db_path: Path = DB_PATH) -> int:
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO scenarios (team, score, total_cost, decisions_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (team, score, total_cost, json.dumps(decisions, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def leaderboard(limit: int = 50, db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT id, team, score, total_cost, decisions_json, created_at FROM scenarios ORDER BY score DESC, id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    result = []
    for rank, row in enumerate(rows, start=1):
        result.append({
            "rank": rank,
            "id": row["id"],
            "team": row["team"],
            "score": round(row["score"], 2),
            "total_cost": row["total_cost"],
            "created_at": row["created_at"],
            "decisions": json.loads(row["decisions_json"]),
        })
    return result
