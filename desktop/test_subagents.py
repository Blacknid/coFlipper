"""Checks delegation to subagents, without touching the Gemini API.

    python test_subagents.py

Both models are scripted - the main agent's and the subagent's - so a whole delegation can
be replayed as often as needed without consuming the daily quota. The dispatcher, the
session log, the round budget and the real device simulator all run unchanged.
"""

import sys
import time

import subagents
from agent import run_turn
from commands import CommandDispatcher, load_catalog, model_commands
from mock_flipper import MockCFPClient
from reasoning import ANSWER, REPORT, REQUEST, SPAWN
from scripted_model import Call, Checks, Part, Response, ScriptedChat


class ScriptedRunner(subagents.SubagentRunner):
    """The real runner with its conversations scripted.

    It deliberately subclasses rather than reimplements: the round budget, the evidence
    gathering and the report assembly under test are the ones that ship.
    """

    def __init__(self, dispatcher, chats):
        self._dispatcher = dispatcher
        self._simulated = True
        self.model = "scripted-model"
        self.chats = chats
        self.prompts = {}

    def _chat(self, spec):
        return self.chats[spec.key]

    def _prompt(self, spec, task):
        prompt = super()._prompt(spec, task)
        self.prompts[spec.key] = prompt
        return prompt


def measurement(frequency=433_920_000):
    return Call("subghz_rssi", {"frequency": frequency})


def main():
    checks = Checks("subagents")

    dispatcher = CommandDispatcher(model_commands(load_catalog()), MockCFPClient())
    chats = {
        "scanner": ScriptedChat(
            [
                Response([Part("First reading.", thought=True)], [measurement()]),
                Response([Part("A second one, to see the variation.", thought=True)], [measurement()]),
                Response([Part("Two identical readings of -93.9 dBm: background noise only.")]),
            ],
            "scanner",
        ),
        "analyst": ScriptedChat(
            [Response([Part("The log holds two readings on 433.92 MHz, both -93.9 dBm.")])],
            "analyst",
        ),
    }
    runner = ScriptedRunner(dispatcher, chats)
    dispatcher.subagents = runner

    main_script = [
        Response(
            [Part("This is about activity, not one value: I delegate.", thought=True)],
            [Call("agent_check_frequency_activity", {"frequency": 433_920_000, "readings": 2})],
        ),
        Response(
            [Part("Now I check what has been measured so far.", thought=True)],
            [Call("agent_research_session_log", {"question": "what was measured so far?"})],
        ),
        Response([Part("No activity on 433.92 MHz: two readings of -93.9 dBm, background noise.")]),
    ]
    steps = []
    reply, trace = run_turn(
        ScriptedChat(main_script, "main"), dispatcher, "Is there activity on 433.92 MHz?", steps.append
    )

    checks.section("1. the shape of a chain containing delegation")
    kinds = [s.kind for s in trace.steps]
    checks.check(f"steps: {kinds}", kinds[0] == REQUEST and kinds[-1] == ANSWER)
    checks.check("two subagents were summoned", kinds.count(SPAWN) == 2)
    checks.check("each one reported back", kinds.count(REPORT) == 2)
    checks.check("every step was reported as it happened", steps == trace.steps)
    checks.check(f"in order: {trace.subagents}", trace.subagents == ["scanner", "analyst"])

    checks.section("2. depth and attribution")
    spawns = [s for s in trace.steps if s.kind == SPAWN]
    nested = [s for s in trace.steps if s.depth == 1]
    checks.check("summoning belongs to the main agent", all(s.depth == 0 for s in spawns))
    checks.check("the subagents' own steps are nested", len(nested) >= 5)
    checks.check("every nested step names its author", all(s.source for s in nested))

    checks.section("3. what is announced when a subagent is summoned")
    meta = spawns[0].meta
    checks.check(f"its role: {meta['role'][:40]}...", "measures the radio spectrum" in meta["role"])
    checks.check(f"its model: {meta['model']}", meta["model"] == "scripted-model")
    checks.check(f"its permitted tools: {meta['tools']}", "subghz_rssi" in meta["tools"])
    checks.check(f"its budget: {meta['max_rounds']} rounds", meta["max_rounds"] > 0)
    checks.check(
        "the task carries the arguments the main agent chose",
        "frequency: 433920000" in spawns[0].text,
    )

    checks.section("4. the announced tools are the ones actually granted")
    # A name the catalog does not carry cannot be granted, so it must not be announced
    # either: the announcement is what the user is asked to trust.
    scanner_spec = subagents.SPECS["scanner"]
    granted = {tool["name"] for tool in runner._allowed(scanner_spec)}
    checks.check(
        f"announced {meta['tools']} == granted {sorted(granted)}",
        set(meta["tools"]) == {name.replace(".", "_") for name in granted},
    )
    narrowed = subagents.Spec(
        key="scanner", name="scanner", role="test", instruction="test",
        tools=("ping", "does.not.exist"),
    )
    checks.check(
        "a name absent from the catalog is dropped, not promised",
        [c["name"] for c in runner._allowed(narrowed)] == ["ping"],
    )

    checks.section("5. the analyst has no access to the device")
    checks.check("no tool is permitted to it", spawns[1].meta["tools"] == [])
    checks.check(
        "it receives the session log instead",
        "SESSION LOG:" in runner.prompts["analyst"],
    )
    checks.check(
        "the log holds the scanner's measurements",
        runner.prompts["analyst"].count("subghz.rssi frequency=433920000") == 2,
    )
    checks.check("the log marks simulated data", "[simulated]" in runner.prompts["analyst"])

    checks.section("6. the report handed back to the main agent")
    reports = [s for s in trace.steps if s.kind == REPORT]
    checks.check(f"the scanner's report: {reports[0].text[:36]}...", "background noise" in reports[0].text)
    checks.check("it states how many commands it ran", reports[0].meta["commands"] == 2)
    checks.check("it states how many rounds it used", reports[0].meta["rounds"] == 3)
    checks.check("the budget was not exhausted", reports[0].meta["truncated"] is False)

    checks.section("7. the raw evidence travels with the report")
    chats["analyst"] = ScriptedChat([Response([Part("Two readings in the log.")])], "analyst")
    outcome = dispatcher.dispatch("agent_research_session_log", {"question": "check"})
    checks.check("the result names the subagent behind it", outcome["subagent"] == "analyst")
    checks.check("the result is marked simulated", outcome.get("simulated") is True)
    checks.check("it carries the prose report", bool(outcome["report"]))
    checks.check("it carries the raw evidence too", "evidence" in outcome)

    checks.section("8. a subagent cannot summon another subagent")
    refused = dispatcher.dispatch_device("agent_check_frequency_activity", {})
    checks.check(f"the device path refuses the name: {refused['error']}", refused["status"] == "error")

    checks.section("9. the round budget stops a subagent that will not stop itself")
    budget = subagents.SPECS["scanner"].max_rounds
    # Keeps measuring until the budget runs out, then complies with the order to report:
    # the budget is reached on the last tool response, and the next answer is the report.
    looping = ScriptedChat(
        [Response([Part("One more.", thought=True)], [measurement()])] * budget
        + [Response([Part("Reporting what I have.")])],
        "looping",
    )
    chats["scanner"] = looping
    result = runner.run("scanner", "measure continuously")
    checks.check(f"it stopped at the budget ({result['rounds']} rounds)", result["rounds"] <= budget + 1)
    checks.check("the report flags the exhausted budget", result["budget_exhausted"] is True)
    checks.check(f"the number of requests is bounded ({len(looping.sent)})", len(looping.sent) <= budget + 1)
    checks.check("it still produced a usable report", "Reporting what I have" in result["report"])

    checks.section("10. a subagent that ignores the stop still reports something")
    chats["scanner"] = ScriptedChat(
        [Response([Part("One more.", thought=True)], [measurement()])] * (budget + 6), "stubborn"
    )
    stubborn = runner.run("scanner", "measure continuously")
    checks.check(
        f"the report explains what happened: {stubborn['report'][:40]}...",
        "No report" in stubborn["report"],
    )
    checks.check("the evidence is handed over regardless", len(stubborn["evidence"]) > 0)

    checks.section("11. the final answer")
    checks.check(f"phrased from the reports: {reply[:44]}...", "background noise" in reply)

    checks.section("12. a session without subagents still works")
    lone = CommandDispatcher(model_commands(load_catalog()), MockCFPClient())
    refused_delegation = lone.dispatch("agent_check_frequency_activity", {"frequency": 433_920_000})
    checks.check("delegation reports it is unavailable", refused_delegation["status"] == "error")
    checks.check("device commands keep working", lone.dispatch("ping")["status"] == "ok")

    checks.section("13. overlapping device commands are serialised")
    # The model does issue several tool calls at once - a real run had the scanner ask for
    # five readings in one round - and a single serial port serves the device, so requests
    # must not interleave on the wire.
    checks.check(*_lock_holds())

    return checks.finish()


def _lock_holds():
    """Runs commands from many threads and reports whether any two overlapped."""
    import threading

    overlaps = []
    inside = []

    class OverlapDetectingClient(MockCFPClient):
        def request(self, cmd, *args):
            inside.append(cmd)
            if len(inside) > 1:
                overlaps.append(tuple(inside))
            # Long enough that unsynchronised threads would certainly be caught here.
            time.sleep(0.01)
            result = super().request(cmd, *args)
            inside.pop()
            return result

    guarded = CommandDispatcher(model_commands(load_catalog()), OverlapDetectingClient())
    threads = [
        threading.Thread(target=guarded.dispatch_device, args=("ping", {})) for _ in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    executed = len(guarded.log)
    return (
        f"12 concurrent commands, {executed} executed, {len(overlaps)} overlaps",
        executed == 12 and not overlaps,
    )


if __name__ == "__main__":
    sys.exit(main())
