import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.environ.get("NEWSREADER_DB", "/data/newsreader.db")


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                source_url TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS book_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                ts TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(book_id) REFERENCES books(id)
            );

            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                byline TEXT,
                excerpt TEXT,
                content_html TEXT NOT NULL,
                source_domain TEXT,
                published_at_raw TEXT,
                content_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(book_id) REFERENCES books(id)
            );

            CREATE TABLE IF NOT EXISTS issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL,
                issue_date TEXT NOT NULL,
                title TEXT NOT NULL,
                epub_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                build_status TEXT,
                build_started_at TEXT,
                build_finished_at TEXT,
                build_error TEXT,
                audit_path TEXT,
                audit_summary TEXT,
                FOREIGN KEY(book_id) REFERENCES books(id)
            );

            CREATE TABLE IF NOT EXISTS issue_articles (
                issue_id INTEGER NOT NULL,
                article_id INTEGER NOT NULL,
                PRIMARY KEY (issue_id, article_id),
                FOREIGN KEY(issue_id) REFERENCES issues(id),
                FOREIGN KEY(article_id) REFERENCES articles(id)
            );
            """
        )
        _ensure_article_columns(conn)
        _ensure_issue_columns(conn)


def _ensure_article_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
    if "text_content" not in existing:
        conn.execute("ALTER TABLE articles ADD COLUMN text_content TEXT")
    if "section" not in existing:
        conn.execute("ALTER TABLE articles ADD COLUMN section TEXT")


def _ensure_issue_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(issues)").fetchall()}
    if "build_status" not in existing:
        conn.execute("ALTER TABLE issues ADD COLUMN build_status TEXT")
    if "build_started_at" not in existing:
        conn.execute("ALTER TABLE issues ADD COLUMN build_started_at TEXT")
    if "build_finished_at" not in existing:
        conn.execute("ALTER TABLE issues ADD COLUMN build_finished_at TEXT")
    if "build_error" not in existing:
        conn.execute("ALTER TABLE issues ADD COLUMN build_error TEXT")
    if "audit_path" not in existing:
        conn.execute("ALTER TABLE issues ADD COLUMN audit_path TEXT")
    if "audit_summary" not in existing:
        conn.execute("ALTER TABLE issues ADD COLUMN audit_summary TEXT")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.commit()
        conn.close()
