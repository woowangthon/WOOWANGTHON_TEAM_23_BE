import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS materials (
    id TEXT PRIMARY KEY, device_id TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
    chunks_total INTEGER NOT NULL, chunks_done INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pages (
    material_id TEXT NOT NULL, page INTEGER NOT NULL, text TEXT NOT NULL, PRIMARY KEY (material_id, page)
);
CREATE TABLE IF NOT EXISTS chunks (
    material_id TEXT NOT NULL, idx INTEGER NOT NULL, text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (material_id, idx)
);
CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY, material_id TEXT NOT NULL, device_id TEXT NOT NULL, type TEXT NOT NULL,
    question TEXT NOT NULL, answer TEXT NOT NULL, accepted TEXT NOT NULL, evidence TEXT NOT NULL,
    page INTEGER, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS questions_device ON questions (device_id, created_at);
CREATE TABLE IF NOT EXISTS answers (
    device_id TEXT NOT NULL, question_id TEXT NOT NULL, result TEXT NOT NULL, answered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    device_id TEXT NOT NULL, question_id TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS refills (
    material_id TEXT NOT NULL, chunk_idx INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS usage_days (
    device_id TEXT NOT NULL, day TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (device_id, day)
);
CREATE TABLE IF NOT EXISTS advice (
    device_id TEXT NOT NULL, day TEXT NOT NULL, based_on TEXT NOT NULL, facts TEXT NOT NULL, message TEXT NOT NULL,
    PRIMARY KEY (device_id, day)
);
CREATE TABLE IF NOT EXISTS usage_counts (
    device_id TEXT NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL, count INTEGER NOT NULL,
    PRIMARY KEY (device_id, day, kind)
);
"""


class Database:
    """연결 하나를 작업자와 요청이 함께 씀. 모든 접근을 lock으로 묶어 순서를 보장함"""

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()
