"""SQLite-хранилище: задания (живут до даты сдачи), оценки, подписчики."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime

from eljur import Day, SubjectGrades

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    due         TEXT NOT NULL,
    subject     TEXT NOT NULL,
    text        TEXT NOT NULL,
    files       TEXT NOT NULL DEFAULT '[]',
    test_label  TEXT NOT NULL DEFAULT '',
    first_seen  TEXT NOT NULL DEFAULT '',
    done        INTEGER NOT NULL DEFAULT 0,
    removed     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS marks (
    key         TEXT PRIMARY KEY,
    day         TEXT NOT NULL,
    subject     TEXT NOT NULL,
    value       TEXT NOT NULL,
    weight      TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT '',
    comment     TEXT NOT NULL DEFAULT '',
    first_seen  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS grades (
    key         TEXT PRIMARY KEY,
    subject     TEXT NOT NULL,
    day         TEXT NOT NULL,
    value       TEXT NOT NULL,
    first_seen  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


@dataclass
class StoredTask:
    id: str
    due: date
    subject: str
    text: str
    files: list[tuple[str, str]]
    test_label: str
    done: bool


@dataclass
class StoredMark:
    day: date
    subject: str
    value: str
    weight: str
    kind: str
    comment: str


def _task(row: sqlite3.Row) -> StoredTask:
    return StoredTask(
        id=row["id"],
        due=date.fromisoformat(row["due"]),
        subject=row["subject"],
        text=row["text"],
        files=[tuple(f) for f in json.loads(row["files"])],
        test_label=row["test_label"],
        done=bool(row["done"]),
    )


class Storage:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---- meta
    def get_meta(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row["v"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, value))

    def meta_with_prefix(self, prefix: str) -> dict[str, str]:
        rows = self.db.execute("SELECT k, v FROM meta WHERE substr(k, 1, ?) = ?", (len(prefix), prefix))
        return {r["k"]: r["v"] for r in rows}

    # ---- синхронизация с журналом
    def sync(self, days: dict[date, Day], today: date) -> None:
        """Сохраняет задания и оценки. При самой первой синхронизации ничего не считается «новым»."""
        initial = self.get_meta("synced") == ""
        stamp = "" if initial else datetime.now().isoformat(timespec="seconds")
        with self.db:
            for d, day in days.items():
                seen_ids = []
                for lesson in day.lessons:
                    for task in lesson.tasks:
                        seen_ids.append(task.id)
                        self.db.execute(
                            """INSERT INTO tasks (id, due, subject, text, files, test_label, first_seen)
                               VALUES (?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(id) DO UPDATE SET
                                 due = excluded.due, subject = excluded.subject, text = excluded.text,
                                 files = excluded.files, test_label = excluded.test_label, removed = 0""",
                            (task.id, d.isoformat(), lesson.subject, task.text,
                             json.dumps(task.files, ensure_ascii=False), lesson.test_label, stamp),
                        )
                    for mark in lesson.marks:
                        self.db.execute(
                            """INSERT OR IGNORE INTO marks (key, day, subject, value, weight, kind, comment, first_seen)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (mark.key, d.isoformat(), lesson.subject, mark.value,
                             mark.weight, mark.kind, mark.comment, stamp),
                        )
                # задание, которое учитель удалил из журнала, больше не показываем
                if d >= today:
                    marks = ",".join("?" * len(seen_ids))
                    self.db.execute(
                        f"UPDATE tasks SET removed = 1 WHERE due = ? AND id NOT IN ({marks})",
                        (d.isoformat(), *seen_ids),
                    )
        if initial:
            self.set_meta("synced", "1")

    # ---- задания
    def upcoming(self, after: date, include_done: bool = True) -> list[StoredTask]:
        sql = "SELECT * FROM tasks WHERE due > ? AND removed = 0"
        if not include_done:
            sql += " AND done = 0"
        rows = self.db.execute(sql + " ORDER BY due, subject", (after.isoformat(),)).fetchall()
        return [_task(r) for r in rows]

    def new_tasks_since(self, since: str, after: date) -> list[StoredTask]:
        rows = self.db.execute(
            """SELECT * FROM tasks WHERE first_seen != '' AND first_seen > ? AND due > ? AND removed = 0
               ORDER BY due, subject""",
            (since, after.isoformat()),
        ).fetchall()
        return [_task(r) for r in rows]

    def get_task(self, task_id: str) -> StoredTask | None:
        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _task(row) if row else None

    def toggle_done(self, task_id: str) -> bool:
        with self.db:
            self.db.execute("UPDATE tasks SET done = 1 - done WHERE id = ?", (task_id,))
        task = self.get_task(task_id)
        return bool(task and task.done)

    # ---- оценки
    def new_marks_since(self, since: str) -> list[StoredMark]:
        rows = self.db.execute(
            "SELECT * FROM marks WHERE first_seen != '' AND first_seen > ? ORDER BY day, subject",
            (since,),
        ).fetchall()
        return [
            StoredMark(date.fromisoformat(r["day"]), r["subject"], r["value"], r["weight"], r["kind"], r["comment"])
            for r in rows
        ]

    # ---- успеваемость
    def sync_grades(self, subjects: list[SubjectGrades], today: date, fresh_days: int = 7) -> None:
        """Запоминает, когда бот впервые увидел каждую оценку.
        При самой первой загрузке «новыми» считаем только оценки за последние fresh_days дней."""
        initial = self.db.execute("SELECT 1 FROM grades LIMIT 1").fetchone() is None
        stamp = datetime.now().isoformat(timespec="seconds")
        with self.db:
            for s in subjects:
                for g in s.grades:
                    seen = "" if initial and (today - g.date).days > fresh_days else stamp
                    # исправленная учителем оценка снова становится новой
                    self.db.execute(
                        """INSERT INTO grades (key, subject, day, value, first_seen) VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT(key) DO UPDATE SET
                             first_seen = CASE WHEN value != excluded.value THEN excluded.first_seen ELSE first_seen END,
                             value = excluded.value""",
                        (g.key, g.subject, g.date.isoformat(), g.value, seen),
                    )

    def new_grade_keys(self, since: str) -> set[str]:
        rows = self.db.execute(
            "SELECT key FROM grades WHERE first_seen != '' AND first_seen > ?", (since,)
        ).fetchall()
        return {r["key"] for r in rows}

    # ---- подписчики
    def add_subscriber(self, chat_id: int) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO subscribers (chat_id) VALUES (?)", (chat_id,))

    def remove_subscriber(self, chat_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))

    def subscribers(self) -> list[int]:
        return [r["chat_id"] for r in self.db.execute("SELECT chat_id FROM subscribers")]
