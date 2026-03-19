"""
Usage tracking module using SQLite for persistent storage.
Tracks all render requests for admin monitoring.
"""

import json
import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "usage.db"


def _get_conn() -> sqlite3.Connection:
    """Return a new SQLite connection with row_factory set."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they do not already exist."""
    os.makedirs(str(DATA_DIR), exist_ok=True)
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS requests (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email    TEXT NOT NULL,
                user_name     TEXT,
                title_es      TEXT,
                languages     TEXT,
                num_languages INTEGER,
                template_id   TEXT,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def log_request(
    user_email: str,
    user_name: str,
    title_es: str,
    languages_list: list,
    template_id: str,
) -> None:
    """Insert one usage record into the requests table."""
    languages_json = json.dumps(languages_list, ensure_ascii=False)
    num_languages = len(languages_list)
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO requests
                (user_email, user_name, title_es, languages, num_languages, template_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_email, user_name, title_es, languages_json, num_languages, template_id),
        )
        conn.commit()


def get_stats_overview() -> dict:
    """Return high-level aggregate statistics."""
    with _get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*)                          AS total_requests,
                COALESCE(SUM(num_languages), 0)   AS total_translations,
                COUNT(DISTINCT user_email)         AS unique_users,
                COUNT(CASE WHEN DATE(created_at) = DATE('now') THEN 1 END) AS requests_today,
                COALESCE(SUM(CASE WHEN DATE(created_at) = DATE('now') THEN num_languages ELSE 0 END), 0)
                                                  AS translations_today
            FROM requests
            """
        ).fetchone()
    return dict(row)


def get_stats_per_user() -> list:
    """Return per-user aggregates sorted by total requests descending."""
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                user_email                       AS email,
                user_name                        AS name,
                COUNT(*)                         AS total_requests,
                COALESCE(SUM(num_languages), 0)  AS total_translations,
                MAX(created_at)                  AS last_request_date
            FROM requests
            GROUP BY user_email
            ORDER BY total_requests DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_stats_per_day(days: int = 30) -> list:
    """Return daily totals for the last *days* calendar days."""
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                DATE(created_at)                AS date,
                COUNT(*)                        AS total_requests,
                COALESCE(SUM(num_languages), 0) AS total_translations
            FROM requests
            WHERE created_at >= DATE('now', ? || ' days')
            GROUP BY DATE(created_at)
            ORDER BY date ASC
            """,
            (f"-{days}",),
        ).fetchall()
    return [dict(r) for r in rows]


def get_stats_per_language() -> dict:
    """Return {language_code: total_count} across all requests."""
    with _get_conn() as conn:
        rows = conn.execute("SELECT languages FROM requests").fetchall()

    counts: dict = {}
    for row in rows:
        try:
            langs = json.loads(row["languages"] or "[]")
        except (json.JSONDecodeError, TypeError):
            langs = []
        for lang in langs:
            counts[lang] = counts.get(lang, 0) + 1
    return counts


def get_recent_titles_per_user(limit: int = 25) -> dict:
    """Return {email: [{title_es, created_at, num_languages}]} with last *limit* entries per user."""
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT user_email, title_es, created_at, num_languages
            FROM (
                SELECT
                    user_email,
                    title_es,
                    created_at,
                    num_languages,
                    ROW_NUMBER() OVER (
                        PARTITION BY user_email
                        ORDER BY created_at DESC
                    ) AS rn
                FROM requests
            )
            WHERE rn <= ?
            ORDER BY user_email, created_at DESC
            """,
            (limit,),
        ).fetchall()

    result: dict = {}
    for row in rows:
        email = row["user_email"]
        if email not in result:
            result[email] = []
        result[email].append(
            {
                "title_es": row["title_es"],
                "created_at": row["created_at"],
                "num_languages": row["num_languages"],
            }
        )
    return result


def get_stats_per_user_per_day(days: int = 30) -> list:
    """Return [{date, email, requests, translations}] for the last *days* days."""
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT
                DATE(created_at)                AS date,
                user_email                      AS email,
                COUNT(*)                        AS requests,
                COALESCE(SUM(num_languages), 0) AS translations
            FROM requests
            WHERE created_at >= DATE('now', ? || ' days')
            GROUP BY DATE(created_at), user_email
            ORDER BY date ASC, user_email ASC
            """,
            (f"-{days}",),
        ).fetchall()
    return [dict(r) for r in rows]


# Auto-initialize on import
init_db()
