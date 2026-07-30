"""Checks the Sub-GHz 'watch' feature: waiting for an upcoming signal and reacting to it.

    python test_watch.py

Like test_listen.py, the models are scripted so no Gemini quota is spent, but the work
behind the tools is real: subghz.read runs against the MockCFPClient's actual band fixtures.
What this suite exercises is the WATCHER's distinct behaviour - it waits for the first
signal and stops, rather than harvesting a list like the listener - and its honesty when
the window passes with nothing on the air.
"""

import sys

import subagents
from commands import CommandDispatcher, load_catalog, model_commands
from mock_flipper import MockCFPClient
from scripted_model import Call, Checks, Part, Response, ScriptedChat
from test_subagents import ScriptedRunner

FREQ = 433_920_000
# The 300-348 MHz band carries exactly two fixtures in the mock; draining them lets a later
# read return no_signal, which is how the "nothing was played in the window" case is staged.
QUIET_FREQ = 315_000_000


def _drain_band(dispatcher, frequency):
    """Read a band until it reports no_signal, so subsequent reads stay quiet."""
    for _ in range(20):
        if dispatcher.dispatch_device("subghz_read", {"frequency": frequency})["data"] == ["no_signal"]:
            return


def main():
    checks = Checks("subghz watch")

    checks.section("1. the watcher subagent's grant: passive read tools, no transmit")
    dispatcher = CommandDispatcher(model_commands(load_catalog()), MockCFPClient())
    meta = ScriptedRunner(dispatcher, {}).describe("watcher")
    checks.check(f"its role is about waiting for a signal: {meta['role'][:45]}...",
                 "wait" in meta["role"].lower())
    checks.check(f"it has subghz_read to catch the signal: {meta['tools']}",
                 "subghz_read" in meta["tools"])
    checks.check("it has subghz_rssi to note the background level",
                 "subghz_rssi" in meta["tools"])
    checks.check("it has NO save skill - watching is not harvesting to disk",
                 "save_subghz" not in meta["tools"])
    checks.check("it has NO transmit tool - it is passive, it cannot transmit",
                 "subghz_send" not in meta["tools"] and "subghz_replay" not in meta["tools"])

    checks.section("2. the watcher stops the instant a signal arrives")
    fresh = CommandDispatcher(model_commands(load_catalog()), MockCFPClient())
    # The watcher waits through two quiet reads, then catches the first real signal and stops.
    watcher_chat = ScriptedChat(
        [
            Response([Part("Note the resting level first.", thought=True)],
                     [Call("subghz_rssi", {"frequency": FREQ})]),
            Response([Part("Now wait for the signal.", thought=True)],
                     [Call("subghz_read", {"frequency": FREQ, "timeout_ms": 2000})]),
            Response([Part("Caught it - a rolling-code signal arrived. I stop here.")]),
        ],
        "watcher",
    )
    runner = ScriptedRunner(fresh, {"watcher": watcher_chat})
    fresh.subagents = runner

    result = runner.run("watcher", "a signal is about to be played on 433.92, watch for it")
    reads = [e for e in result["evidence"] if e["command"] == "subghz_read"]
    checks.check(f"it reacted after a single read, it did not keep harvesting: {len(reads)} read(s)",
                 len(reads) == 1)
    decoded = reads[0]["result"]["data"]
    checks.check(f"the read really decoded a signal: {decoded[:2]}", decoded[0] == "signal")
    checks.check(f"it reports the signal WAS heard: {result['report'][:50]}...",
                 "caught" in result["report"].lower() or "arrived" in result["report"].lower())
    checks.check("it saved nothing - watching reports, it does not write captures to disk",
                 not any(e["command"] == "save_subghz" for e in result["evidence"]))

    checks.section("3. when the window passes with nothing on the air, it says so honestly")
    quiet = CommandDispatcher(model_commands(load_catalog()), MockCFPClient())
    _drain_band(quiet, QUIET_FREQ)
    quiet_chat = ScriptedChat(
        [
            Response([Part("Waiting for the signal.", thought=True)],
                     [Call("subghz_read", {"frequency": QUIET_FREQ, "timeout_ms": 2000})]),
            Response([Part("Still nothing.", thought=True)],
                     [Call("subghz_read", {"frequency": QUIET_FREQ, "timeout_ms": 2000})]),
            Response([Part("Nothing was heard on 315 MHz in the window - the sender may not "
                           "have played it in time, or the frequency may be wrong.")]),
        ],
        "watcher",
    )
    quiet_runner = ScriptedRunner(quiet, {"watcher": quiet_chat})
    quiet.subagents = quiet_runner
    quiet_result = quiet_runner.run("watcher", "watch 315 MHz for a signal")
    quiet_reads = [e for e in quiet_result["evidence"] if e["command"] == "subghz_read"]
    checks.check("every read genuinely returned no_signal against the drained band",
                 all(e["result"]["data"] == ["no_signal"] for e in quiet_reads))
    checks.check(f"it reports nothing heard, without inventing a signal: {quiet_result['report'][:45]}...",
                 "nothing" in quiet_result["report"].lower())
    checks.check("no fabricated protocol/key leaked into the report",
                 "0x" not in quiet_result["report"])

    checks.section("4. the whole feature is reachable through the catalog, end to end")
    end = CommandDispatcher(model_commands(load_catalog()), MockCFPClient())
    main_chat = ScriptedChat(
        [
            Response([Part("The user warned a signal is coming: I arm the watcher.", thought=True)],
                     [Call("agent_watch_subghz", {"frequency": FREQ, "time": 15})]),
            Response([Part("The watcher caught a rolling-code signal on 433.92 MHz.")]),
        ],
        "main",
    )
    end_runner = ScriptedRunner(end, {"watcher": ScriptedChat(
        [
            Response([Part("Waiting.", thought=True)],
                     [Call("subghz_read", {"frequency": FREQ})]),
            Response([Part("Caught a Nice_FloR_S signal - a rolling-code actuator. Heard it, stopping.")]),
        ],
        "watcher",
    )})
    end.subagents = end_runner

    from agent import run_turn
    from reasoning import SPAWN, REPORT, ANSWER
    reply, trace = run_turn(main_chat, end, "a subghz signal is going to be played shortly, listen for it", lambda s: None)
    checks.check("the watcher was summoned via the catalog command agent.watch_subghz",
                 any(s.kind == SPAWN and s.name == "watcher" for s in trace.steps))
    checks.check("it reported back into the reasoning chain",
                 any(s.kind == REPORT and s.name == "watcher" for s in trace.steps))
    checks.check(f"the main agent answered from it: {reply[:45]}...",
                 "signal" in reply.lower() and trace.steps[-1].kind == ANSWER)

    checks.section("5. watch and listen are distinct specialists, not the same one")
    checks.check("the catalog carries a watch command backed by the watcher subagent",
                 any(c.get("name") == "agent.watch_subghz" and c.get("subagent") == "watcher"
                     for c in load_catalog()["commands"]))
    checks.check("watch is marked passive, exactly like listen - it never transmits",
                 next(c for c in load_catalog()["commands"] if c.get("name") == "agent.watch_subghz")
                 .get("impact") == "passive")

    return checks.finish()


if __name__ == "__main__":
    sys.exit(main())
