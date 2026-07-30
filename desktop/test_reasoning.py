"""Checks the construction of the reasoning chain, without touching the Gemini API.

    python test_reasoning.py

The model is replaced by a scripted set of responses (see scripted_model.py); the
dispatcher, the tool loop and the simulated device are the real ones.
"""

import sys

from agent import run_turn
from commands import CommandDispatcher, load_catalog, model_commands
from mock_flipper import MockCFPClient
from reasoning import ANSWER, REQUEST, THOUGHT, TOOL
from scripted_model import Call, Checks, Part, Response, ScriptedChat


def dispatcher():
    return CommandDispatcher(model_commands(load_catalog()), MockCFPClient())


def main():
    checks = Checks("reasoning chain")

    checks.section("1. the shape of a chain spanning several rounds")
    script = [
        Response(
            [Part("I first check whether the device responds.", thought=True)],
            [Call("ping")],
        ),
        Response(
            [Part("It responds, so I can read the firmware version.", thought=True)],
            [Call("info")],
        ),
        Response(
            [
                Part("I have both pieces, I phrase the answer.", thought=True),
                Part("The simulated device responds and runs firmware 1.0.0."),
            ]
        ),
    ]
    chat = ScriptedChat(script)
    seen = []
    reply, trace = run_turn(chat, dispatcher(), "check the device", seen.append)

    kinds = [step.kind for step in trace.steps]
    checks.check(
        f"steps in order: {kinds}",
        kinds == [REQUEST, THOUGHT, TOOL, THOUGHT, TOOL, THOUGHT, ANSWER],
    )
    checks.check("every step was reported as it happened", seen == trace.steps)
    checks.check("the request opens the chain", trace.first.text == "check the device")
    checks.check("three exchanges with the model", len(chat.sent) == 3)
    checks.check("rounds are numbered", [s.round for s in trace.steps] == [0, 1, 1, 2, 2, 3, 3])
    checks.check(
        "elapsed time never goes backwards",
        [s.at_s for s in trace.steps] == sorted(s.at_s for s in trace.steps),
    )

    checks.section("2. reasoning stays out of the answer")
    checks.check(
        f"answer is clean: {reply!r}",
        reply == "The simulated device responds and runs firmware 1.0.0.",
    )
    checks.check("reasoning went to the chain instead", "I phrase the answer" not in reply)

    checks.section("3. evidence and markings")
    tools = [s for s in trace.steps if s.kind == TOOL]
    checks.check("both commands succeeded", all(s.ok for s in tools))
    checks.check("both are marked as simulated", all(s.simulated for s in tools))
    checks.check(f"result line: {tools[0].result_line()!r}", tools[0].result_line() == "OK pong")
    checks.check("the chain exposes two pieces of evidence", len(trace.evidence) == 2)

    checks.section("4. a turn in which the command fails")
    # 2.4 GHz lies in no CC1101 band, so the device refuses the measurement.
    failing = [
        Response(
            [Part("I measure the level on the requested frequency.", thought=True)],
            [Call("subghz_rssi", {"frequency": 2_400_000_000})],
        ),
        Response(
            [Part("It failed, I report the error.", thought=True)],
            # No text part: what the model returns when it only reasons.
        ),
    ]
    _, failed_trace = run_turn(ScriptedChat(failing), dispatcher(), "measure 2.4 GHz")
    failed = [s for s in failed_trace.steps if s.kind == TOOL]
    checks.check("the command failed", failed and not failed[0].ok)
    checks.check(
        f"the error is visible in the chain: {failed[0].result_line()!r}",
        failed[0].result_line() == "ERR invalid_frequency",
    )
    checks.check("a failed command is not counted as evidence", failed_trace.evidence == [])

    checks.section("5. the simulator mirrors the real measurement command")
    measuring = [
        Response(
            [Part("I measure on 433.92 MHz.", thought=True)],
            [Call("subghz_rssi", {"frequency": 433_920_000})],
        ),
        Response([Part("Low level.")]),
    ]
    _, rssi_trace = run_turn(ScriptedChat(measuring), dispatcher(), "how much signal on 433.92?")
    measured = [s for s in rssi_trace.steps if s.kind == TOOL][0]
    checks.check(f"the measurement succeeds: {measured.result_line()!r}", measured.ok)
    checks.check("it reports frequency and level", len(measured.outcome["data"]) == 2)
    checks.check("the level is negative, in dBm", measured.outcome["data"][1].startswith("-"))

    checks.section("6. models that produce no reasoning summaries")
    _, plain_trace = run_turn(ScriptedChat([Response([Part("Done.")])]), dispatcher(), "hello")
    checks.check(
        f"the chain holds request and answer only: {[s.kind for s in plain_trace.steps]}",
        [s.kind for s in plain_trace.steps] == [REQUEST, ANSWER],
    )

    checks.section("7. Markdown markup is cleaned up for display")
    from reasoning import plain_text

    cleaned = plain_text("## Conclusion\nOn **433.92 MHz** the level is `-93.9 dBm`:\n*   low")
    checks.check(f"no markup left: {cleaned.splitlines()[0]!r}", "#" not in cleaned)
    checks.check("no asterisks or backticks", "*" not in cleaned and "`" not in cleaned)
    checks.check("list items become bullets", "• low" in cleaned)

    return checks.finish()


if __name__ == "__main__":
    sys.exit(main())
