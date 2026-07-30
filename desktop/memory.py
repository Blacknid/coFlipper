"""Persistent memory: facts the agent keeps across sessions.

The conversation itself is the agent's short-term memory - the model sees the whole history
of the current chat, and when that history grows long it is compacted (see agent.py). This
module is the long-term memory: a small set of durable facts the agent chose to remember
(the user's devices and their brands, a stated preference, a lasting finding), written to
disk so they survive a restart and are loaded back into every new conversation.

The model writes to it through the agent.remember tool; it reads from it automatically,
since every session is built with the current memories folded into its system instruction.
Kept deliberately simple and visible: the store is a readable JSON file and the interface
shows what is in it, because a memory the user cannot inspect is one they cannot trust.
"""

import json
import pathlib
import threading
import time

DEFAULT_PATH = pathlib.Path(__file__).resolve().parent / "memory.json"

# A cap, so the memory cannot grow without bound and quietly swell every prompt it is folded
# into. When full, the oldest fact makes room for the newest.
MAX_MEMORIES = 40


class MemoryStore:
    """A durable list of short facts, safe to touch from several chat threads at once."""

    def __init__(self, path=DEFAULT_PATH):
        self._path = pathlib.Path(path)
        self._lock = threading.Lock()
        self._items = self._load()

    def _load(self):
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        if not isinstance(data, list):
            return []
        return [m for m in data if isinstance(m, dict) and m.get("text")]

    def _save(self):
        # Written to a temporary file and moved into place, so a crash mid-write cannot
        # leave a half-written memory file that would fail to load next time.
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self._items, handle, ensure_ascii=False, indent=2)
        tmp.replace(self._path)

    def all(self):
        with self._lock:
            return list(self._items)

    @property
    def count(self):
        with self._lock:
            return len(self._items)

    def remember(self, text):
        """Stores one fact. Returns the dict the dispatcher hands back to the model."""
        text = (text or "").strip()
        if not text:
            return {"status": "error", "error": "the fact to remember is empty"}
        with self._lock:
            for item in self._items:
                if item["text"].strip().lower() == text.lower():
                    return {
                        "status": "ok",
                        "stored": False,
                        "reason": "already remembered",
                        "count": len(self._items),
                    }
            self._items.append({"text": text, "at": time.strftime("%Y-%m-%d %H:%M")})
            if len(self._items) > MAX_MEMORIES:
                # Drop the oldest to stay within the cap.
                self._items = self._items[-MAX_MEMORIES:]
            self._save()
            return {"status": "ok", "stored": True, "count": len(self._items)}

    def forget(self, index):
        """Removes the memory at the given position; returns it, or None if out of range."""
        with self._lock:
            if 0 <= index < len(self._items):
                removed = self._items.pop(index)
                self._save()
                return removed
            return None

    def clear(self):
        with self._lock:
            self._items = []
            self._save()

    def as_prompt(self):
        """The memories as a block appended to the system instruction, or '' when empty."""
        with self._lock:
            if not self._items:
                return ""
            lines = "\n".join(f"- {m['text']}" for m in self._items)
        return (
            "\n\nPERSISTENT MEMORY - facts you chose to remember in earlier sessions. Treat "
            "them as background you already know about this user and their devices; do not "
            "recite them unprompted, only use them when they are relevant:\n" + lines + "\n"
        )
