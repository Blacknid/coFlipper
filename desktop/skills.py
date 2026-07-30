"""Skills: desktop capabilities a subagent may be granted, beyond the device tools.

The device subagents (scanner, wifi_recon) reach the world through CFP commands sent to
the Flipper. The audit subagent has a different job - it searches the project's own files
on disk - and the Flipper cannot do that. A skill is that missing kind of tool: a named
function that runs desktop Python, granted to a subagent the same way a device command is,
and described in commands.json under the command that spawns the subagent.

Keeping skills here, separate from both the CFP catalog and the subagent that uses them,
mirrors the split the project already makes between the device layer and the agent layer:
a device command touches hardware, a skill touches the desktop, and neither is allowed to
masquerade as the other. A skill invents nothing - it only reports what it read on disk,
so the audit subagent stays as honest as the ones that only read the radio.
"""

import os
import pathlib

from app_store import AppStore
from subghz_store import SubGhzStore

# How many characters of a matching line to hand back. A whole generated C source can run
# to thousands of lines; the subagent needs enough of each hit to judge it, not the file.
_SNIPPET_LEN = 200
# Ceiling on the hits returned from one search, so a query like 'e' cannot drag the entire
# store into the subagent's context (and, through it, into a model request).
_MAX_HITS = 40


def _iter_app_files(store):
    """Every file worth searching in the app store, as (appid, label, path).

    The store keeps, per app, the editable C source, the FAP manifest, the last build log
    and one history record per build/edit. All of them are things a person might ask about
    ('which app failed to compile?', 'which one uses the OK button?'), so all of them are
    searched. The store's own build tree (.ufbt, dist) is skipped: it is ufbt's scratch
    space, not the user's source, and searching it would bury real hits under toolchain noise.
    """
    for entry in store.list_apps():
        appid = entry.get("appid")
        if not appid:
            continue
        app_dir = store.app_dir(appid)
        source = store.source_path(appid)
        if source.exists():
            yield appid, "source", source
        fam = store.fam_path(appid)
        if fam.exists():
            yield appid, "manifest", fam
        build_log = app_dir / "build.log"
        if build_log.exists():
            yield appid, "build_log", build_log
        history = app_dir / "history"
        if history.is_dir():
            for record in sorted(history.glob("*.json")):
                yield appid, "history", record


def search_app_store(query, appid=None, store=None):
    """Case-insensitive substring search across the generated-app files on disk.

    Returns a dict shaped like every other tool result in the system: 'status' plus the
    findings, so the subagent can treat it exactly as it treats a device reading. Each hit
    names the app, which kind of file it was, the line number and a snippet of the line -
    enough to locate the match without shipping whole files back.

    'query' is the text to look for; 'appid' optionally narrows the search to one app.
    """
    query = (query or "").strip()
    if not query:
        return {"status": "error", "error": "the 'query' argument is required and cannot be empty"}

    store = store or AppStore()
    if not store.root.exists():
        return {
            "status": "ok",
            "query": query,
            "hits": [],
            "note": "the app store does not exist yet: no apps have been built, so there is nothing to search",
        }

    wanted = query.lower()
    narrow = appid.strip() if appid else None
    hits = []
    files_searched = 0
    truncated = False

    for file_appid, kind, path in _iter_app_files(store):
        if narrow and file_appid != narrow:
            continue
        files_searched += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            hits.append({"appid": file_appid, "file": kind, "path": str(path), "error": str(exc)})
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if wanted in line.lower():
                snippet = line.strip()[:_SNIPPET_LEN]
                # The path travels with every hit so a 'where is it?' question can be
                # answered with a real location, not just the app id and file kind.
                hits.append(
                    {
                        "appid": file_appid,
                        "file": kind,
                        "path": str(path),
                        "line": lineno,
                        "snippet": snippet,
                    }
                )
                if len(hits) >= _MAX_HITS:
                    truncated = True
                    break
        if truncated:
            break

    result = {
        "status": "ok",
        "query": query,
        "files_searched": files_searched,
        "hits": hits,
        "match_count": len(hits),
    }
    if narrow:
        result["scoped_to_app"] = narrow
        if files_searched == 0:
            result["note"] = f"no app with id '{narrow}' is in the store"
    if truncated:
        result["truncated"] = True
        result["note"] = (
            f"stopped at the first {_MAX_HITS} matches; narrow the query or pass an appid "
            "to see the rest"
        )
    return result


def list_app_store(store=None):
    """The apps in the store, so the subagent can survey what exists before searching one.

    A thin wrapper over the store's own listing, trimmed to the fields worth reasoning about
    (id, name, build status, whether it is installed), so the subagent can answer 'what have
    I built?' directly and pick which app to search into.
    """
    store = store or AppStore()
    if not store.root.exists():
        return {"status": "ok", "apps": [], "note": "the app store does not exist yet: nothing has been built"}
    apps = [
        {
            "appid": entry.get("appid"),
            "name": entry.get("name"),
            "build_status": entry.get("build_status"),
            "installed": entry.get("installed"),
            "last_exit_code": entry.get("last_exit_code"),
            # The directory and source path let a 'where is the X app?' question be answered
            # from the listing alone, without needing a text search inside the app.
            "dir": str(store.app_dir(entry["appid"])) if entry.get("appid") else None,
            "source_path": entry.get("source_path"),
        }
        for entry in store.list_apps()
    ]
    return {"status": "ok", "count": len(apps), "apps": apps}


def save_subghz(frequency, protocol, key, bits=0, rssi=None, guess=None, store=None):
    """Save one captured Sub-GHz signal to the apps_assets folder as a descriptive .sub file.

    Granted to the listener subagent so that harvesting a frequency leaves durable, findable
    files behind, not just a one-off report. The store builds the descriptive filename from
    the band, protocol and the 'guess' at the source, so pass a plain-English guess ('a
    wireless doorbell remote') - that is what makes the file searchable later. Passive: it
    writes a file, it transmits nothing. Returns where the capture was saved.
    """
    if frequency in (None, "") or not protocol:
        return {"status": "error", "error": "frequency and protocol are required to save a capture"}
    try:
        freq = int(frequency)
    except (TypeError, ValueError):
        return {"status": "error", "error": f"invalid frequency: {frequency!r}"}
    store = store or SubGhzStore()
    meta = store.save(
        frequency=freq, protocol=protocol, key=key,
        bits=bits or 0, rssi=rssi, guess=guess,
    )
    return {
        "status": "ok",
        "saved": meta["stem"],
        "path": meta["sub_path"],
        "frequency": meta["frequency"],
        "protocol": meta["protocol"],
    }


def list_subghz(store=None):
    """List the Sub-GHz captures saved in apps_assets, newest first.

    Lets a subagent (or the replay path) survey what has been harvested and pick one by its
    descriptive name. Reads files only.
    """
    store = store or SubGhzStore()
    captures = store.list_captures()
    return {
        "status": "ok",
        "count": len(captures),
        "captures": [
            {
                "name": c.get("stem"),
                "frequency": c.get("frequency"),
                "protocol": c.get("protocol"),
                "guess": c.get("guess"),
                "path": c.get("sub_path"),
            }
            for c in captures
        ],
    }


# The skill registry: the name a subagent calls -> the desktop function behind it, plus the
# schema the model is shown. Kept as data so build_skill_tool() and dispatch_skill() derive
# both the tool declaration and the routing from one place, exactly as the CFP catalog does
# for device commands. Adding a skill is adding an entry here and naming it in commands.json.
SKILLS = {
    "search_app_store": {
        "fn": search_app_store,
        "description": (
            "Search the generated Flipper apps on disk for a piece of text: the editable C "
            "source, the FAP manifest, the last build log, and the build/edit history. "
            "Case-insensitive substring match. Returns each hit with the app id, which file "
            "it was found in (source, manifest, build_log, history), the full file path, the "
            "line number and a snippet. Use it to answer questions about what has been built "
            "and where it lives - 'which app uses the OK button?', 'which one failed to "
            "compile and why?', 'find the app that draws to the screen', 'where is the paint "
            "app's source?'. The path in each hit is the location of that file on disk. It "
            "reads files only; it changes nothing."
        ),
        "args": [
            {
                "name": "query",
                "type": "string",
                "required": True,
                "description": "the text to search for, e.g. 'InputKeyOk' or 'error:'",
            },
            {
                "name": "appid",
                "type": "string",
                "required": False,
                "description": "restrict the search to one app by its id, e.g. 'paint'; omit to search every app",
            },
        ],
    },
    "list_app_store": {
        "fn": list_app_store,
        "description": (
            "List the generated Flipper apps in the store, newest first, with each app's id, "
            "name, build status, whether it is installed, its directory and its source path. "
            "Use it to survey what exists, to answer 'what apps do I have?' or 'what is the "
            "name of the X app?', and to find where an app lives on disk, before searching "
            "into a specific app with search_app_store."
        ),
        "args": [],
    },
    "save_subghz": {
        "fn": save_subghz,
        "description": (
            "Save one captured Sub-GHz signal to the apps_assets folder on disk, as a Flipper "
            ".sub file with a DESCRIPTIVE name built from the band, the protocol and your guess "
            "at what the device is - e.g. '433MHz_Princeton_wireless_doorbell_remote_4e7b90.sub'. "
            "Pass a plain-English 'guess' ('a wireless doorbell remote', 'an electric relay'): "
            "it goes into the filename, which is what makes the capture findable by a later "
            "search. Call this once per DISTINCT signal you harvest. It writes a file and "
            "transmits nothing. Returns the name it was saved under and its path."
        ),
        "args": [
            {
                "name": "frequency",
                "type": "integer",
                "required": True,
                "description": "the frequency the signal was captured on, in Hz, e.g. 433920000",
            },
            {
                "name": "protocol",
                "type": "string",
                "required": True,
                "description": "the decoded protocol, e.g. Princeton, CAME, KeeLoq",
            },
            {
                "name": "key",
                "type": "string",
                "required": False,
                "description": "the key/code the signal carried, e.g. 0x4E7B90",
            },
            {
                "name": "bits",
                "type": "integer",
                "required": False,
                "description": "the bit-length of the code, e.g. 24",
            },
            {
                "name": "rssi",
                "type": "integer",
                "required": False,
                "description": "the signal strength in dBm, e.g. -62",
            },
            {
                "name": "guess",
                "type": "string",
                "required": False,
                "description": "your plain-English guess at the source, used in the filename, e.g. 'a wireless doorbell remote'",
            },
        ],
    },
    "list_subghz": {
        "fn": list_subghz,
        "description": (
            "List the Sub-GHz captures already saved in apps_assets, newest first, each with "
            "its descriptive name, frequency, protocol, the guess at the source and its file "
            "path. Use it to see what has been harvested before, or to find a capture by name "
            "before replaying it. Reads files only."
        ),
        "args": [],
    },
}


def skill_specs():
    """The (name, description, args) of every skill, for describing a subagent's grant."""
    return {name: {"description": s["description"], "args": s.get("args", [])} for name, s in SKILLS.items()}


def dispatch_skill(name, call_args=None):
    """Runs a skill by name and returns its result, or an error for an unknown name.

    The single door a subagent's skill call passes through - mirroring dispatch_device for
    the CFP path - so a subagent can never reach a desktop function that was not granted to
    it as a skill.
    """
    skill = SKILLS.get(name)
    if skill is None:
        return {"status": "error", "error": f"unknown skill: {name}"}
    args = call_args or {}
    accepted = {arg["name"] for arg in skill.get("args", [])}
    kwargs = {key: value for key, value in args.items() if key in accepted}
    return skill["fn"](**kwargs)
