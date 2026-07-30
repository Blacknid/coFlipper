"""The persistent store of generated Flipper apps.

Every app the builder produces is kept on disk, not thrown away after the build: the
owner may later ask to change it ('add a bigger brush to the paint app'), and that
request has to be able to find the existing source and start from it rather than from a
blank page. This module is that memory.

The layout, rooted at COFLIPPER_APP_STORE (default ~/.coflipper/apps), is one directory
per app plus a top-level index:

    <store>/index.json                  the manifest: one entry per app
    <store>/<appid>/application.fam     the FAP manifest ufbt reads
    <store>/<appid>/<appid>.c           the generated C source - this is what stays editable
    <store>/<appid>/.ufbt/  dist/       ufbt's own build tree, isolated per app by cwd
    <store>/<appid>/build.log           the captured output of the last ufbt run
    <store>/<appid>/history/NNNN-*.json  one record per build or edit, with the debate behind it

The store deliberately lives outside the repository. The generated apps are the user's,
not the project's, and building them inside the tree would pollute git and mix the user's
paint app in with the project's own source.
"""

import json
import os
import pathlib
import re
import time


def store_root():
    """The directory the apps live in, honouring COFLIPPER_APP_STORE.

    Resolved on each call rather than cached, so a test can point it at a temporary
    directory through the environment without the module having to be reloaded.
    """
    override = os.environ.get("COFLIPPER_APP_STORE")
    if override:
        return pathlib.Path(override).expanduser()
    return pathlib.Path.home() / ".coflipper" / "apps"


def sanitize_appid(name):
    """A Flipper appid out of an arbitrary human name.

    The appid is used both as a directory name and as the C entry-point stem, so it has to
    be a lowercase identifier: letters, digits and underscores, not starting with a digit.
    'My Paint App!' becomes 'my_paint_app'. An empty or unusable name falls back to a
    generic stem rather than producing an invalid one.
    """
    lowered = (name or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = "app_" + cleaned if cleaned else "flipper_app"
    return cleaned[:32]


class AppStore:
    """Reads and writes the app store. Pure filesystem: no API, no toolchain.

    Kept free of any dependency on Gemini or ufbt on purpose - the builder composes this
    with those, but the store itself can be exercised, and tested, entirely on its own.
    """

    def __init__(self, root=None):
        self.root = pathlib.Path(root) if root else store_root()

    # -- paths -------------------------------------------------------------

    @property
    def index_path(self):
        return self.root / "index.json"

    def app_dir(self, appid):
        return self.root / appid

    def source_path(self, appid):
        return self.app_dir(appid) / f"{appid}.c"

    def fam_path(self, appid):
        return self.app_dir(appid) / "application.fam"

    # -- the index ---------------------------------------------------------

    def _read_index(self):
        if not self.index_path.exists():
            return {}
        try:
            with open(self.index_path, encoding="utf-8") as handle:
                return json.load(handle)
        except (json.JSONDecodeError, OSError):
            # A corrupt index must not sink the whole feature: it is a cache of what is
            # already on disk, so the worst case is a rebuilt entry, not lost source.
            return {}

    def _write_index(self, index):
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.index_path, "w", encoding="utf-8") as handle:
            json.dump(index, handle, indent=2, ensure_ascii=False)

    def list_apps(self):
        """Every app the store knows, newest first."""
        entries = list(self._read_index().values())
        entries.sort(key=lambda e: e.get("created_at", 0), reverse=True)
        return entries

    def get(self, appid_or_name):
        """The manifest entry for an app, found by id or by human name.

        Editing refers to an app the way the user named it ('the paint app'), which may be
        the display name rather than the id, so both are accepted. The id is tried first,
        since it is exact.
        """
        index = self._read_index()
        if appid_or_name in index:
            return index[appid_or_name]
        wanted = sanitize_appid(appid_or_name)
        if wanted in index:
            return index[wanted]
        for entry in index.values():
            if (entry.get("name") or "").strip().lower() == (appid_or_name or "").strip().lower():
                return entry
        return None

    def exists(self, appid):
        return appid in self._read_index()

    # -- creating and updating apps ---------------------------------------

    def allocate_appid(self, name):
        """A free appid derived from a name, disambiguated on collision.

        Two 'paint' apps must not clobber each other: the second becomes 'paint_2'. The
        chosen id is not reserved here - it is committed only when write_source is called -
        so this is a suggestion the builder confirms.
        """
        base = sanitize_appid(name)
        index = self._read_index()
        if base not in index:
            return base
        n = 2
        while f"{base}_{n}" in index:
            n += 1
        return f"{base}_{n}"

    def write_source(self, appid, name, c_source, fam_source):
        """Writes (or overwrites) an app's source and manifest files.

        Returns the app directory. Creates the manifest entry on first write; a later write
        for the same id (an edit) leaves the creation time and history intact.
        """
        app_dir = self.app_dir(appid)
        (app_dir / "history").mkdir(parents=True, exist_ok=True)

        self.source_path(appid).write_text(c_source, encoding="utf-8")
        self.fam_path(appid).write_text(fam_source, encoding="utf-8")

        index = self._read_index()
        entry = index.get(appid)
        if entry is None:
            entry = {
                "appid": appid,
                "name": name or appid,
                "source_path": str(self.source_path(appid)),
                "created_at": time.time(),
                "build_status": "never",
                "last_exit_code": None,
                "last_built_at": None,
                "target_api": None,
                "installed": False,
                "history": [],
            }
        else:
            entry["name"] = name or entry.get("name") or appid
            entry["source_path"] = str(self.source_path(appid))
        index[appid] = entry
        self._write_index(index)
        return app_dir

    def read_source(self, appid):
        """The current C source and FAP manifest of an app, for an edit to start from.

        Returns (c_source, fam_source). Raises KeyError if the app is unknown, so the edit
        path can tell 'change this app' from 'no such app' cleanly.
        """
        if not self.exists(appid):
            raise KeyError(appid)
        source = self.source_path(appid)
        fam = self.fam_path(appid)
        return (
            source.read_text(encoding="utf-8") if source.exists() else "",
            fam.read_text(encoding="utf-8") if fam.exists() else "",
        )

    def record_build(self, appid, *, kind, request, exit_code, built, installed,
                     target_api=None, transcript=None, build_log=None):
        """Records the outcome of a build or edit, and the debate behind it.

        Updates the manifest entry (status, exit code, install flag) and appends a history
        record. The transcript - the full proposer/challenger/arbiter exchange - is written
        to its own file so the manifest stays small, and the last ufbt output is kept as
        build.log for inspection.
        """
        index = self._read_index()
        entry = index.get(appid)
        if entry is None:  # a build recorded before any source: keep it honest, create one
            entry = {"appid": appid, "name": appid, "created_at": time.time(), "history": []}

        seq = len(entry.get("history", [])) + 1
        history_record = {
            "seq": seq,
            "kind": kind,  # "build" or "edit"
            "at": time.time(),
            "request": request,
            "exit_code": exit_code,
            "built": bool(built),
            "installed": bool(installed),
        }
        entry.setdefault("history", []).append(history_record)
        entry["build_status"] = "built" if built else "failed"
        entry["last_exit_code"] = exit_code
        entry["last_built_at"] = time.time()
        entry["installed"] = bool(installed)
        if target_api is not None:
            entry["target_api"] = target_api
        index[appid] = entry
        self._write_index(index)

        app_dir = self.app_dir(appid)
        (app_dir / "history").mkdir(parents=True, exist_ok=True)
        if transcript is not None:
            path = app_dir / "history" / f"{seq:04d}-{kind}.json"
            path.write_text(json.dumps(transcript, indent=2, ensure_ascii=False), encoding="utf-8")
        if build_log is not None:
            (app_dir / "build.log").write_text(build_log, encoding="utf-8")
        return history_record
