"""Checks the audit subagent: delegation that searches files on disk, not the device.

    python test_audit.py

The subagent's conversation is scripted, exactly as in test_subagents.py, so no Gemini
quota is spent. What is NOT scripted is the file search itself: a real app store is built
in a temporary directory and the skills read it for real, so the test exercises the actual
search_app_store / list_app_store code the subagent calls.
"""

import os
import sys
import tempfile

import subagents
from app_store import AppStore
from commands import CommandDispatcher, load_catalog, model_commands
from mock_flipper import MockCFPClient
from scripted_model import Call, Checks, Part, Response, ScriptedChat
from test_subagents import ScriptedRunner


def _seed_store(root):
    """A store with two apps: one that uses the OK button and built, one that failed."""
    store = AppStore(root=root)
    store.write_source(
        "paint",
        "Paint",
        c_source=(
            "#include <furi.h>\n"
            "// draw with the arrow keys, toggle brush with OK\n"
            "if(event.key == InputKeyOk) { thick_brush = !thick_brush; }\n"
            "canvas_draw_dot(canvas, x, y);\n"
        ),
        fam_source='App(appid="paint", name="Paint")\n',
    )
    store.record_build(
        "paint", kind="build", request="a paint app", exit_code=0, built=True, installed=True,
        build_log="CC paint.c\nLinking paint.fap\nBuilt successfully\n",
    )
    store.write_source(
        "stopwatch",
        "Stopwatch",
        c_source="#include <furi.h>\nint main() { retrun 0; }\n",  # deliberate typo
        fam_source='App(appid="stopwatch", name="Stopwatch")\n',
    )
    store.record_build(
        "stopwatch", kind="build", request="a stopwatch", exit_code=1, built=False, installed=False,
        build_log="CC stopwatch.c\nstopwatch.c:2:20: error: expected ';' before 'return'\n",
    )
    return store


def main():
    checks = Checks("audit subagent")

    tmp = tempfile.mkdtemp(prefix="coflipper-audit-test-")
    os.environ["COFLIPPER_APP_STORE"] = tmp
    _seed_store(tmp)

    dispatcher = CommandDispatcher(model_commands(load_catalog()), MockCFPClient())

    # The subagent lists the apps, searches for the OK button, and reports what it found.
    audit_chat = ScriptedChat(
        [
            Response([Part("Open question: I list what exists first.", thought=True)],
                     [Call("list_app_store", {})]),
            Response([Part("Now I search for the OK button across the source.", thought=True)],
                     [Call("search_app_store", {"query": "InputKeyOk"})]),
            Response([Part("The Paint app uses InputKeyOk in its source, at line 3.")]),
        ],
        "audit",
    )
    runner = ScriptedRunner(dispatcher, {"audit": audit_chat})
    dispatcher.subagents = runner

    checks.section("1. what the audit subagent is granted")
    meta = runner.describe("audit")
    checks.check(f"its role: {meta['role'][:40]}...", "searches the project's" in meta["role"])
    checks.check(f"its tools are skills, not device: {meta['tools']}",
                 meta["tools"] == ["list_app_store", "search_app_store"])
    checks.check("it has no device tools at all",
                 all(t not in meta["tools"] for t in ("subghz_rssi", "ping", "info")))

    checks.section("2. a granted skill absent from the registry is dropped, not promised")
    narrowed = subagents.Spec(key="audit", name="audit", role="t", instruction="t",
                              skills=("search_app_store", "no_such_skill"))
    checks.check("unknown skill name is not granted",
                 runner._allowed_skills(narrowed) == ["search_app_store"])

    checks.section("3. the subagent really searches the files on disk")
    result = runner.run("audit", "which of my apps uses the OK button?")
    checks.check(f"it reports the finding: {result['report'][:40]}...",
                 "Paint" in result["report"])
    ran = [e["command"] for e in result["evidence"]]
    checks.check(f"it called the skills: {ran}",
                 ran == ["list_app_store", "search_app_store"])
    search = next(e for e in result["evidence"] if e["command"] == "search_app_store")
    hits = search["result"]["hits"]
    checks.check(f"the search really hit the source ({search['result']['match_count']} hits)",
                 any(h["appid"] == "paint" and h["file"] == "source" for h in hits))
    checks.check("the hit carries a line number and snippet",
                 all("line" in h and "snippet" in h for h in hits))
    checks.check("the hit carries the file path so 'where is it?' can be answered",
                 all(h.get("path") and h["path"].endswith(".c") for h in hits))
    listing = next(e for e in result["evidence"] if e["command"] == "list_app_store")
    checks.check("list_app_store saw both seeded apps",
                 {a["appid"] for a in listing["result"]["apps"]} == {"paint", "stopwatch"})
    paint_entry = next(a for a in listing["result"]["apps"] if a["appid"] == "paint")
    checks.check("the listing gives each app's location on disk",
                 bool(paint_entry.get("dir")) and paint_entry.get("source_path", "").endswith("paint.c"))

    checks.section("4. searching the build logs finds the failure")
    err = dispatcher.dispatch_skill("search_app_store", {"query": "error:"})
    apps_with_error = {h["appid"] for h in err["hits"]}
    checks.check(f"the failed build is found in a build_log: {sorted(apps_with_error)}",
                 "stopwatch" in apps_with_error
                 and any(h["file"] == "build_log" for h in err["hits"]))

    checks.section("5. scoping to one app confines the search")
    scoped = dispatcher.dispatch_skill("search_app_store", {"query": "furi", "appid": "paint"})
    checks.check("every hit is from the scoped app",
                 scoped["hits"] and all(h["appid"] == "paint" for h in scoped["hits"]))
    checks.check("it records what it was scoped to", scoped.get("scoped_to_app") == "paint")

    checks.section("6. an empty query is refused, a miss is honest")
    checks.check("empty query rejected",
                 dispatcher.dispatch_skill("search_app_store", {"query": " "})["status"] == "error")
    miss = dispatcher.dispatch_skill("search_app_store", {"query": "zzz_nothing_matches_zzz"})
    checks.check("a search that matches nothing returns zero hits, not an invented one",
                 miss["status"] == "ok" and miss["hits"] == [])

    checks.section("7. the skill call is recorded in the session log")
    checks.check("the search shows up in the log the way a device command would",
                 "search_app_store" in dispatcher.log_text())

    checks.section("8. delegation is reachable through the catalog, end to end")
    catalog_chat = ScriptedChat(
        [
            Response([Part("This is about my files: I delegate to audit.", thought=True)],
                     [Call("agent_audit_files", {"question": "which app uses OK"})]),
            Response([Part("Your Paint app uses the OK button.")]),
        ],
        "main",
    )
    from agent import run_turn
    runner.chats["audit"] = ScriptedChat(
        [
            Response([Part("Searching.", thought=True)],
                     [Call("search_app_store", {"query": "InputKeyOk"})]),
            Response([Part("Paint uses InputKeyOk.")]),
        ],
        "audit",
    )
    reply, trace = run_turn(catalog_chat, dispatcher, "which app uses the OK button?", lambda s: None)
    from reasoning import SPAWN, REPORT
    checks.check("the audit subagent was summoned via the catalog command",
                 any(s.kind == SPAWN and s.name == "audit" for s in trace.steps))
    checks.check("it reported back into the chain",
                 any(s.kind == REPORT and s.name == "audit" for s in trace.steps))
    checks.check(f"the main agent answered from it: {reply[:36]}...", "Paint" in reply)

    return checks.finish()


if __name__ == "__main__":
    sys.exit(main())
