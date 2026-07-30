"""Checks the app builder, without the Gemini API and without the Flipper toolchain.

    python test_app_builder.py

The three debating agents are scripted, and ufbt is replaced by a fake build runner that
returns prepared (exit_code, output) tuples. This makes the whole pipeline - the debate,
the compile-error feedback loop, the persistence and the honesty guarantees - replayable
with no quota spent and no compiler invoked. What ships runs unchanged; only the two seams
that reach the outside world (the model conversations and ufbt) are substituted.
"""

import sys
import tempfile

import app_builder
from app_builder import AppBuilder
from app_store import AppStore
from reasoning import REPORT, SPAWN, Trace
from scripted_model import Checks, Part, Response, ScriptedChat
from ufbt_runner import UfbtRunner


def files(fam, source, note="looks fine"):
    """A scripted agent reply carrying a FAM and a SOURCE block, as the real agents emit."""
    return (
        f"{note}\n===FAM===\n{fam}\n===END FAM===\n"
        f"===SOURCE===\n{source}\n===END SOURCE===\n"
    )


FAM = 'App(appid="paint", name="Paint", apptype=FlipperAppType.EXTERNAL, entry_point="paint_main")'
GOOD_SOURCE = "int paint_main() { return 0; }"
BAD_SOURCE = "int paint_main( { return 0; }"  # a deliberate syntax error for the fail case


class FakeUfbt:
    """A stand-in build runner. Returns the next scripted (exit_code, output) per action.

    Records every build directory it was asked to compile, so a test can assert that the
    real source was written to disk before the compiler was invoked.
    """

    def __init__(self, results):
        self._results = list(results)
        self.builds = []

    def __call__(self, app_dir, action, env=None):
        self.builds.append((app_dir, action))
        if action in ("install", "launch"):
            return 0, "installed"
        if not self._results:
            return 0, "built"
        return self._results.pop(0)


class ScriptedAppBuilder(AppBuilder):
    """The real builder with its three conversations and its research scripted."""

    def __init__(self, dispatcher, chats, research="(scripted research)", **kw):
        super().__init__(dispatcher, **kw)
        self._scripted_chats = chats
        self._scripted_research = research

    def _chat(self, spec):
        return self._scripted_chats[spec.key]

    def _research(self, request, emit):
        app_builder._emit(emit, "thought", text="scripted research", source="research")
        return self._scripted_research


def make_builder(tmp, chats, ufbt_results, simulated=True):
    store = AppStore(root=tmp)
    fake = FakeUfbt(ufbt_results)
    builder = ScriptedAppBuilder(
        dispatcher=None,
        chats=chats,
        store=store,
        ufbt=UfbtRunner(build_runner=fake),
        simulated=simulated,
        api_key="not-used",
    )
    return builder, store, fake


def collect(emit_steps):
    """Turn emitted (kind, fields) events into a Trace, the way run_turn does."""
    trace = Trace("build a paint app")
    for kind, fields in emit_steps:
        if kind == "spawn":
            trace.add_spawn(fields["name"], fields["task"], fields["meta"])
        elif kind == "thought":
            trace.add_thought(fields["text"], depth=1, source=fields.get("source", ""))
        elif kind == "report":
            trace.add_report(fields["name"], fields["text"], fields.get("meta"), depth=1)
    return trace


def main():
    checks = Checks("app_builder")

    # -- 1. a clean build: one debate round, compiler succeeds --------------
    checks.section("1. a clean build in a single debate round")
    with tempfile.TemporaryDirectory() as tmp:
        chats = {
            "proposer": ScriptedChat([Response([Part(files(FAM, GOOD_SOURCE, "my design"))])], "proposer"),
            "challenger": ScriptedChat([Response([Part("I object to nothing major.")])], "challenger"),
            "arbiter": ScriptedChat([Response([Part(files(FAM, GOOD_SOURCE, "kept the design"))])], "arbiter"),
        }
        builder, store, fake = make_builder(tmp, chats, ufbt_results=[(0, "Compiling... done")])
        events = []
        result = builder.build("a simple paint app", app_name="Paint", emit=lambda k, **f: events.append((k, f)))

        checks.check(f"status ok: {result['status']}", result["status"] == "ok")
        checks.check("the app was built (exit 0)", result["built"] is True)
        checks.check("the compiler was actually invoked", ("build" in [a for _, a in fake.builds]))
        checks.check("appid derived from the name", result["appid"] == "paint")
        checks.check("real compiler output is carried back", "done" in (result["compiler_output"] or ""))

        trace = collect(events)
        kinds = [s.kind for s in trace.steps]
        checks.check(f"chain opens with a spawn: {kinds[1] if len(kinds)>1 else None}", SPAWN in kinds)
        checks.check("chain ends by reporting back", trace.steps[-1].kind == REPORT)
        depths = {s.depth for s in trace.steps if s.kind in (REPORT,) and s.source in ("proposer", "challenger", "arbiter")}
        checks.check("debate steps are nested at depth 1", depths == {1} or depths == set())
        sources = {s.source for s in trace.steps if s.kind == REPORT}
        checks.check(f"each agent is attributed by name: {sources}",
                     {"proposer", "challenger", "arbiter"}.issubset(sources))

    # -- 2. persistence: source and manifest written, editable -------------
    checks.section("2. the built app is persisted and editable")
    with tempfile.TemporaryDirectory() as tmp:
        chats = {
            "proposer": ScriptedChat([Response([Part(files(FAM, GOOD_SOURCE))])], "proposer"),
            "challenger": ScriptedChat([Response([Part("fine")])], "challenger"),
            "arbiter": ScriptedChat([Response([Part(files(FAM, GOOD_SOURCE))])], "arbiter"),
        }
        builder, store, fake = make_builder(tmp, chats, ufbt_results=[(0, "done")])
        builder.build("a paint app", app_name="Paint", emit=None)

        checks.check("source file exists on disk", store.source_path("paint").exists())
        checks.check("manifest file exists on disk", store.fam_path("paint").exists())
        entry = store.get("paint")
        checks.check("manifest entry recorded", entry is not None and entry["build_status"] == "built")
        checks.check("history has one record", len(entry["history"]) == 1)
        src, fam = store.read_source("paint")
        checks.check("the saved source is the arbiter's", "paint_main" in src)

        # Now edit it, starting from the saved source.
        edit_chats = {
            "proposer": ScriptedChat([Response([Part(files(FAM, GOOD_SOURCE + " // brush"))])], "proposer"),
            "challenger": ScriptedChat([Response([Part("fine")])], "challenger"),
            "arbiter": ScriptedChat([Response([Part(files(FAM, GOOD_SOURCE + " // brush"))])], "arbiter"),
        }
        edit_builder = ScriptedAppBuilder(
            dispatcher=None, chats=edit_chats, store=store,
            ufbt=UfbtRunner(build_runner=FakeUfbt([(0, "done")])),
            simulated=True, api_key="x")
        prompts = {}
        orig_debate = edit_builder._debate

        def spy(*a, **k):
            prompts["prior_source"] = a[1]  # prior_source argument
            return orig_debate(*a, **k)

        edit_builder._debate = spy
        edit_result = edit_builder.edit("Paint", "add a bigger brush", emit=None)
        checks.check("edit found the existing app", edit_result["status"] == "ok")
        checks.check("edit fed the existing source into the debate",
                     prompts.get("prior_source") is not None and "paint_main" in prompts["prior_source"])
        entry = store.get("paint")
        checks.check("history grew to two records", len(entry["history"]) == 2)
        checks.check("second record is an edit", entry["history"][1]["kind"] == "edit")

    # -- 3. fail then fix: compiler error reaches the next debate ----------
    checks.section("3. a compile failure feeds the real error back into the debate")
    with tempfile.TemporaryDirectory() as tmp:
        chats = {
            "proposer": ScriptedChat([
                Response([Part(files(FAM, BAD_SOURCE, "first try"))]),
                Response([Part(files(FAM, GOOD_SOURCE, "fixed it"))]),
            ], "proposer"),
            "challenger": ScriptedChat([
                Response([Part("this will not compile")]),
                Response([Part("better now")]),
            ], "challenger"),
            "arbiter": ScriptedChat([
                Response([Part(files(FAM, BAD_SOURCE, "ship it"))]),
                Response([Part(files(FAM, GOOD_SOURCE, "corrected"))]),
            ], "arbiter"),
        }
        builder, store, fake = make_builder(
            tmp, chats,
            ufbt_results=[(1, "error: expected ')' before '{' token"), (0, "done")])
        result = builder.build("a paint app", app_name="Paint", emit=None)

        checks.check("the second attempt built successfully", result["built"] is True)
        checks.check("it took two compiler invocations",
                     [a for _, a in fake.builds].count("build") == 2)
        # The real error text must have reached the proposer's second prompt.
        second_prompt = builder._scripted_chats["proposer"].sent[-1]
        checks.check("the real compiler error reached the next round",
                     "expected ')'" in second_prompt)

    # -- 4. honesty: a build that never compiles is never reported built ---
    checks.section("4. a build that never compiles is reported as failed, never as built")
    with tempfile.TemporaryDirectory() as tmp:
        chats = {
            "proposer": ScriptedChat([Response([Part(files(FAM, BAD_SOURCE))]) for _ in range(6)], "proposer"),
            "challenger": ScriptedChat([Response([Part("nope")]) for _ in range(6)], "challenger"),
            "arbiter": ScriptedChat([Response([Part(files(FAM, BAD_SOURCE))]) for _ in range(6)], "arbiter"),
        }
        # Every compile fails.
        builder, store, fake = make_builder(
            tmp, chats, ufbt_results=[(1, "error") for _ in range(6)])
        result = builder.build("a paint app", app_name="Paint", emit=None)

        checks.check("built is False", result["built"] is False)
        checks.check("a build error is reported", "build_error" in result)
        checks.check("the report does not claim success",
                     "NOT" in result["report"] or "not" in result["report"])
        entry = store.get("paint")
        checks.check("persisted status is failed", entry["build_status"] == "failed")

    # -- 5. budget: the request ceiling stops a runaway debate -------------
    checks.section("5. the overall request budget bounds the number of model calls")
    with tempfile.TemporaryDirectory() as tmp:
        # Enough scripted responses that the script itself would never stop the loop;
        # only the budget can.
        many = lambda label: ScriptedChat(  # noqa: E731
            [Response([Part(files(FAM, BAD_SOURCE))]) for _ in range(30)], label)
        chats = {"proposer": many("p"), "challenger": many("c"), "arbiter": many("a")}
        builder, store, fake = make_builder(
            tmp, chats, ufbt_results=[(1, "error") for _ in range(30)])
        result = builder.build("a paint app", app_name="Paint", emit=None)
        checks.check(f"requests stayed within the ceiling ({app_builder.MAX_REQUESTS})",
                     result["requests_used"] <= app_builder.MAX_REQUESTS)
        checks.check("it still returned a result, not an exception", result["status"] == "ok")

    return checks.finish()


if __name__ == "__main__":
    sys.exit(main())
