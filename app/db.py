import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from .config import DB_PATH

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    video_path    TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    hashtags      TEXT NOT NULL DEFAULT '',
    yt_visibility TEXT NOT NULL DEFAULT 'PUBLIC',
    scheduled_at  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|running|success|failed|cancelled
    result_url    TEXT,
    error         TEXT,
    screenshot    TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, scheduled_at);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(accounts)")}
        if "browser_group" not in cols:
            c.execute("ALTER TABLE accounts ADD COLUMN browser_group TEXT NOT NULL DEFAULT ''")
        if "target_name" not in cols:  # Facebook: tên Trang để đăng với tư cách Trang
            c.execute("ALTER TABLE accounts ADD COLUMN target_name TEXT NOT NULL DEFAULT ''")
        jcols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
        if "first_comment" not in jcols:  # bình luận đầu tiên (affiliate) cho Facebook/Threads
            c.execute("ALTER TABLE jobs ADD COLUMN first_comment TEXT NOT NULL DEFAULT ''")
        if "note" not in jcols:  # ghi chú kết quả bình luận
            c.execute("ALTER TABLE jobs ADD COLUMN note TEXT")
        if "publish_mode" not in jcols:  # 'platform' = tải lên trước, hẹn giờ trên chính nền tảng
            c.execute("ALTER TABLE jobs ADD COLUMN publish_mode TEXT NOT NULL DEFAULT ''")
        if "upload_at" not in jcols:  # lúc tool chạy job (khác giờ đăng khi hẹn trên nền tảng)
            c.execute("ALTER TABLE jobs ADD COLUMN upload_at TEXT")
        if "cmt_tries" not in jcols:  # số lần thử gắn bình luận sau khi bài hẹn giờ lên sóng
            c.execute("ALTER TABLE jobs ADD COLUMN cmt_tries INTEGER NOT NULL DEFAULT 0")
        if "run_now" not in jcols:  # người dùng bấm "Chạy" cho riêng bài này (chạy cả khi đang tạm dừng)
            c.execute("ALTER TABLE jobs ADD COLUMN run_now INTEGER NOT NULL DEFAULT 0")
        # Job đang chạy dở khi tắt chương trình -> đánh dấu lỗi để người dùng thử lại
        c.execute(
            "UPDATE jobs SET status='failed', error='Bị gián đoạn do chương trình tắt', finished_at=? "
            "WHERE status='running'",
            (now(),),
        )


def rows(sql, params=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def row(sql, params=()):
    r = rows(sql, params)
    return r[0] if r else None


def execute(sql, params=()):
    with conn() as c:
        cur = c.execute(sql, params)
        return cur.lastrowid


def get_setting(key, default=None):
    r = row("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


def set_setting(key, value):
    execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# ---------------- Link affiliate theo kênh ----------------
def chan_key(acc) -> str:
    """Khoá 'kênh': tên nhóm Chrome; tài khoản không có nhóm thì theo id tài khoản."""
    g = (acc.get("browser_group") or "").strip()
    return g if g else f"acc:{acc.get('account_id') or acc.get('id')}"


def _json_setting(key, default):
    try:
        v = json.loads(get_setting(key, "") or "null")
        return v if isinstance(v, type(default)) else default
    except ValueError:
        return default


def channel_comments() -> dict:
    return _json_setting("channel_comments", {})


def channel_comment_off() -> list:
    return _json_setting("channel_comment_off", [])


def channel_comment_enabled(acc) -> bool:
    return chan_key(acc) not in channel_comment_off()
