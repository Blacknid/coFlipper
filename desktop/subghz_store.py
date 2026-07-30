"""The persistent store of captured Sub-GHz signals.

When the listener subagent harvests a frequency it does not merely report the codes and
forget them: each distinct signal is written to disk as a Flipper .sub file, so it can be
searched for later ('did I ever capture a doorbell on 433?') and, for a device the user
owns, replayed. This module is that memory - the Sub-GHz counterpart of app_store.py.

The layout, rooted at COFLIPPER_ASSETS (default ~/.coflipper/apps_assets), is flat: one
.sub file per captured signal, plus a JSON sidecar of the same stem holding the structured
metadata (frequency, protocol, key, RSSI, the listener's guess at the source, when it was
seen). The folder is called apps_assets to sit alongside the generated apps' own assets,
and it is created on first save.

The filename is the whole point of the exercise. A stock Flipper names a capture something
like 'Sub_2024-01-01.sub'; here the name is built to be DESCRIPTIVE - band, protocol and
the human guess - e.g. '433MHz_Princeton_wireless_doorbell_remote_4e7b90.sub'. That is what
makes a later substring search ('doorbell', 'relay', 'Princeton') actually find it.

Nothing here transmits: writing a .sub file is passive. Replaying one is a separate,
offensive act, gated on the device layer and behind the authorization confirmation.
"""

import json
import os
import pathlib
import re
import time


def assets_root():
    """The folder the captures live in, honouring COFLIPPER_ASSETS.

    Resolved on each call, not cached, so a test can point it at a temporary directory
    through the environment - the same discipline app_store.store_root() follows.
    """
    override = os.environ.get("COFLIPPER_ASSETS")
    if override:
        return pathlib.Path(override).expanduser()
    return pathlib.Path.home() / ".coflipper" / "apps_assets"


def _mhz(frequency_hz):
    """433920000 -> '433MHz'. A coarse band label for the filename, not exact tuning.

    Truncated, not rounded, so 433.92 MHz reads as the familiar '433MHz' rather than '434MHz'
    - the label is what a person searches for, and they search for the ISM band's common name.
    """
    return f"{int(frequency_hz) // 1_000_000}MHz"


def descriptive_name(frequency_hz, protocol, guess, key):
    """A filename stem that says what the capture IS, so search over it is useful.

    Built from the band, the protocol, the listener's plain-English guess at the source,
    and a short tail of the key to disambiguate two captures that are otherwise alike. All
    lowercased to a safe identifier - '433mhz_princeton_wireless_doorbell_remote_4e7b90' -
    since it becomes a filename and a search target. The guess is what a person would search
    for ('doorbell', 'relay'), so it is kept in the name rather than only in the sidecar.
    """
    guess_words = re.sub(r"[^a-z0-9]+", "_", (guess or "").lower()).strip("_")
    # A guess can be a whole sentence; keep it descriptive but not unwieldy.
    guess_words = "_".join(guess_words.split("_")[:6])
    key_tail = re.sub(r"[^0-9a-zA-Z]", "", str(key or ""))[-6:].lower()
    parts = [_mhz(frequency_hz), (protocol or "unknown").lower(), guess_words, key_tail]
    stem = "_".join(p for p in parts if p)
    stem = re.sub(r"_+", "_", stem).strip("_")
    return stem[:80] or "subghz_capture"


def _sub_file_text(frequency_hz, protocol, key, bits):
    """The body of a Flipper .sub file, in the format the firmware's RAW/BinRAW loader reads.

    A real .sub also carries a Preset and, for a decoded protocol, the protocol/bit/key
    triple. This writes exactly that: enough for subghz.replay to reconstruct and re-send the
    signal on a device the user owns. It is deliberately the stock Flipper format so the file
    is usable by the plain Sub-GHz app too, not only by this project.
    """
    return (
        "Filetype: Flipper SubGhz Key File\n"
        "Version: 1\n"
        f"Frequency: {int(frequency_hz)}\n"
        "Preset: FuriHalSubGhzPresetOok650Async\n"
        f"Protocol: {protocol or 'RAW'}\n"
        f"Bit: {int(bits) if bits else 0}\n"
        f"Key: {key or '00 00 00 00 00 00 00 00'}\n"
    )


class SubGhzStore:
    """Reads and writes the captured-signal store. Pure filesystem, no radio, no API.

    Mirrors AppStore: the listener subagent composes this with the device readings, but the
    store on its own can be exercised and tested without any of that.
    """

    def __init__(self, root=None):
        self.root = pathlib.Path(root) if root else assets_root()

    def sub_path(self, stem):
        return self.root / f"{stem}.sub"

    def meta_path(self, stem):
        return self.root / f"{stem}.json"

    def _unique_stem(self, stem):
        """A free stem, disambiguated on collision so two captures never clobber each other."""
        if not self.sub_path(stem).exists():
            return stem
        n = 2
        while self.sub_path(f"{stem}_{n}").exists():
            n += 1
        return f"{stem}_{n}"

    def save(self, *, frequency, protocol, key, bits, rssi=None, guess=None, source_freq=None):
        """Writes one captured signal as a descriptively named .sub plus a JSON sidecar.

        Returns the metadata dict, including the stem and both file paths, so the caller can
        report where the capture went and later find it by name. Passive: it only writes files.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        stem = self._unique_stem(descriptive_name(frequency, protocol, guess, key))

        self.sub_path(stem).write_text(
            _sub_file_text(frequency, protocol, key, bits), encoding="utf-8"
        )
        meta = {
            "stem": stem,
            "frequency": int(frequency),
            "protocol": protocol,
            "key": key,
            "bits": int(bits) if bits else 0,
            "rssi": rssi,
            "guess": guess,
            "captured_at": time.time(),
            "sub_path": str(self.sub_path(stem)),
        }
        self.meta_path(stem).write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return meta

    def list_captures(self):
        """Every saved capture's metadata, newest first."""
        if not self.root.exists():
            return []
        captures = []
        for meta_file in self.root.glob("*.json"):
            try:
                captures.append(json.loads(meta_file.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        captures.sort(key=lambda m: m.get("captured_at", 0), reverse=True)
        return captures

    def resolve(self, name):
        """The .sub path for a capture named by its stem, a substring of it, or a filename.

        Replay refers to a capture the way a person would ('replay the doorbell one'), which
        is rarely the exact stem, so a case-insensitive substring match over the stems is
        accepted. Returns (stem, path) for a single unambiguous match; raises LookupError with
        a message that lists the candidates when nothing matches or several do - never guesses.
        """
        wanted = (name or "").strip().lower()
        if not wanted:
            raise LookupError("no capture name was given")
        wanted = wanted[:-4] if wanted.endswith(".sub") else wanted

        stems = [m["stem"] for m in self.list_captures() if m.get("stem")]
        exact = [s for s in stems if s.lower() == wanted]
        matches = exact or [s for s in stems if wanted in s.lower()]
        if not matches:
            available = ", ".join(stems) or "(none saved)"
            raise LookupError(f"no saved capture matches '{name}'. Available: {available}")
        if len(matches) > 1:
            raise LookupError(
                f"'{name}' matches several captures ({', '.join(matches)}); be more specific"
            )
        stem = matches[0]
        return stem, self.sub_path(stem)
