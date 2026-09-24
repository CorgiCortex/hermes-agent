"""Durable admission receipts for signed, serialized webhook work."""

import sqlite3
from pathlib import Path
from uuid import uuid4


class WebhookLifecycle:
    def __init__(self, path: Path):
        self.boot = str(uuid4())
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
            route TEXT NOT NULL, identity TEXT NOT NULL, scope TEXT NOT NULL,
            chat TEXT NOT NULL, status TEXT NOT NULL, boot TEXT NOT NULL,
            PRIMARY KEY(route,identity))""")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS deliveries_chat ON deliveries(chat,status,boot)"
        )
        self.db.commit()

    def get(self, route: str, identity: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM deliveries WHERE route=? AND identity=?", (route, identity)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if result["status"] == "accepted" and result["boot"] != self.boot:
            result["status"] = "interrupted"
        return result

    def admit(self, route: str, identity: str, scope: str, chat: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO deliveries VALUES(?,?,?,?,?,?)",
                (route, identity, scope, chat, "accepted", self.boot),
            )

    def finish(self, chat: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET status='completed' "
                "WHERE chat=? AND status='accepted' AND boot=?",
                (chat, self.boot),
            )

    def has_other_work(self, chat: str, route: str, identity: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM deliveries WHERE chat=? AND status='accepted' "
                "AND NOT (route=? AND identity=?) LIMIT 1",
                (chat, route, identity),
            ).fetchone()
            is not None
        )

    def cancel(self, route: str, identity: str, scope: str) -> None:
        row = self.get(route, identity)
        if row is not None and row["scope"] != scope:
            raise ValueError("cancellation scope mismatch")
        if row is not None and row["status"] == "cancelled":
            return
        with self.db:
            self.db.execute(
                "INSERT INTO deliveries VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(route,identity) DO UPDATE SET status='cancelled'",
                (route, identity, scope, "", "cancelled", self.boot),
            )
