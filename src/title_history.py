import json
import sqlite3
import unicodedata
from contextlib import closing
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Optional


def _title_key(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title).casefold()
    suffix = normalized.rpartition("|")[2]
    return "".join(char for char in suffix if char.isalnum())


class TitleHistory:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.path = state_dir / "titles.sqlite3"

    def _legacy_title_keys(self) -> Iterable[tuple[str]]:
        for path in self.state_dir.glob("state_*.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(state, dict) or not state.get("draft_media_id"):
                continue
            title = state.get("article_title")
            if isinstance(title, str) and (key := _title_key(title)):
                yield (key,)

    def choose(
        self,
        candidates: Iterable[str],
        *,
        reserve: bool,
        excluded_titles: Iterable[str] = (),
    ) -> Optional[str]:
        with closing(sqlite3.connect(self.path)) as db, db:
            # 比较近似标题和预留标题在同一个事务里，防止并发选中相同短句。
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS titles (title_key TEXT PRIMARY KEY)")
            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                db.executemany(
                    "INSERT OR IGNORE INTO titles (title_key) VALUES (?)",
                    self._legacy_title_keys(),
                )
                db.execute("PRAGMA user_version = 1")

            used_keys = {key for (key,) in db.execute("SELECT title_key FROM titles")}
            used_keys.update(_title_key(title) for title in excluded_titles)
            for title in candidates:
                key = _title_key(title)
                if not key or key in used_keys:
                    continue
                if any(
                    SequenceMatcher(None, key, used, autojunk=False).ratio() >= 0.8
                    for used in used_keys
                ):
                    continue
                if reserve:
                    db.execute("INSERT INTO titles (title_key) VALUES (?)", (key,))
                return title
        return None
