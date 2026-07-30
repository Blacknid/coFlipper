"""Persistent interface settings, kept across restarts.

Small, user-facing preferences that belong to the person rather than to a conversation - the
model chosen in the picker, for now - written to a readable JSON file next to the memory, so
they survive closing the application and are restored when it opens again.

Deliberately tiny and forgiving: a missing or corrupt file just means "no preference yet", so
a bad settings file can never stop the application from starting. Like memory.json, this is
user runtime data, not part of the project, so it is gitignored.
"""

import json
import pathlib
import threading

DEFAULT_PATH = pathlib.Path(__file__).resolve().parent / "settings.json"


class Settings:
    """A durable key/value store for interface preferences."""

    def __init__(self, path=DEFAULT_PATH):
        self._path = pathlib.Path(path)
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self):
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self):
        # Written to a temporary file and moved into place, so a crash mid-write cannot leave
        # a half-written settings file that would fail to load next time.
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, ensure_ascii=False, indent=2)
        tmp.replace(self._path)

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key, value):
        """Stores one preference and persists it immediately. A no-op if unchanged, so it does
        not rewrite the file on every read of an already-current value."""
        with self._lock:
            if self._data.get(key) == value:
                return
            self._data[key] = value
            self._save()
