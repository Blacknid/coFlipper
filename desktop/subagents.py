"""Subagents: specialised assistants the main agent delegates work to.

Two situations justify handing work over instead of doing it inline.

The first is long work. Measuring one frequency is a single command, but watching one
over time, or comparing a whole band, means many commands and a long wait. Done inline,
those commands pile up in the main conversation: every round carries the full history
again, the context grows, and the user watches an idle window. Delegated, the work
happens in a separate session with its own history, and the main agent receives only the
conclusion.

The second is research over material already gathered. Everything the session executed
on the device is recorded in a log, and questions about it ('what did we measure so
far?') need no new hardware access at all - only reading. That is a different job from
driving the radio, so it gets a different specialist, one deliberately given no device
tools whatsoever.

A subagent is therefore a separate conversation with the model: its own system
instruction, its own tool list, its own round budget. It reports back to the agent that
summoned it, and the report carries the raw evidence alongside the prose, so the main
agent is never asked to take a subagent's word for a measurement.

Subagents cost requests from the daily quota, exactly like the main agent. Two
mechanisms keep that in check: a round budget per subagent (max_rounds), and the option
of running them on a different model through COFLIPPER_SUBAGENT_MODEL - the free-tier
quota is counted separately per model, so a subagent on another model draws from its own
allowance instead of competing with the conversation.
"""

import os
from dataclasses import dataclass

from google import genai
from google.genai import types

from agent import (
    INCLUDE_THOUGHTS,
    MODEL,
    SIMULATED_NOTICE,
    answer_text,
    send_with_retry,
    thought_texts,
)
from commands import build_tool, tool_name

# By default a subagent runs on the same model as the conversation. Pointing it at
# another one gives it a separate free-tier quota, which is worth doing for a public
# demonstration: see desktop/README.md.
SUBAGENT_MODEL = os.environ.get("COFLIPPER_SUBAGENT_MODEL", MODEL)

# Shared by every subagent. The honesty rules are repeated here on purpose: a subagent is
# a separate conversation and inherits nothing from the main agent's instruction, so
# without this it would be the one component of the system free to invent measurements.
BASE_INSTRUCTION = """You are a specialised subagent of the coFlipper project. You were
summoned by the main agent to carry out one bounded task, and you report back to it -
not to the user.

Rules you follow strictly:
1. Every piece of information about the device or about surrounding signals comes
   EXCLUSIVELY from the result of a tool you called, or from the log you were given. You
   never invent frequencies, signal levels, identifiers or readings.
2. If a tool answers with an error, you report the error as it is. You do not replace it
   with a plausible value.
3. Your report is read by another agent, not by a person: it must be short and factual.
   State what you did, what came out of it, and what conclusion follows - in plain text,
   without Markdown markup.
4. You do not ask questions and you do not wait for confirmations. If the task cannot be
   carried out, you say so in the report and stop.
"""


@dataclass(frozen=True)
class Spec:
    key: str
    name: str
    # One line, shown in the interface the moment the subagent is summoned, so the user
    # knows who started working and why.
    role: str
    instruction: str
    # CFP commands this subagent may call. An empty tuple means no device access at all,
    # which is the point for the analyst: a subagent that only reads cannot disturb the
    # radio, and it cannot be the source of a fabricated measurement either.
    tools: tuple = ()
    # Upper bound on rounds of dialogue with the model. Reached, the subagent stops and
    # reports what it has. Without it, a subagent that keeps deciding it needs one more
    # measurement could consume the whole daily quota on its own.
    max_rounds: int = 4
    # Whether the session log is appended to its task.
    needs_log: bool = False


SCANNER = Spec(
    key="scanner",
    name="scanner",
    role="measures the radio spectrum on the device, over several successive readings",
    tools=("ping", "info", "subghz.rssi"),
    max_rounds=6,
    instruction="""Your speciality is measurement on the Flipper Zero's radio.

A single reading of the signal level says little: the level fluctuates, and a transmitter
that is not transmitting at that exact instant is indistinguishable from an empty
frequency. So you take several readings of the same frequency, spaced out, and you judge
by the whole set: the maximum reached, how much the values vary, whether anything stands
out above the background noise.

Useful reference points: below about -90 dBm means background noise, an empty frequency;
-80 to -60 dBm means something is transmitting nearby; above -50 dBm means a strong
signal, very close. These are guides for interpretation, not certainties - state them as
such.

Take as many readings as the task requires, but no more than you need: every reading is
a command on a physical device, and every round of measurements costs a request.""",
)

ANALYST = Spec(
    key="analyst",
    name="analyst",
    role="researches the session log, without touching the device",
    tools=(),
    max_rounds=1,
    needs_log=True,
    instruction="""Your speciality is researching what the session has already done.

You receive the log of every command executed on the device up to this point: the
command, its arguments, the result or the error, and the moment it happened. You have no
device tools - you cannot measure anything new, and you must not pretend otherwise. If
the answer to the task is not in the log, you say exactly that.

What you look for: what was actually measured and on which frequencies, which commands
failed and why, whether the values repeat or differ between readings, and what the log
does NOT contain but would be needed in order to answer.""",
)

WIFI_RECON = Spec(
    key="wifi_recon",
    name="wifi_recon",
    role="surveys the Wi-Fi and BLE environment on the Marauder board, without attacking anything",
    tools=(
        "wifi.board_info",
        "wifi.set_channel",
        "wifi.scan_ap",
        "wifi.scan_station",
        "wifi.list_ap",
        "wifi.list_station",
        "ble.scan",
        "ble.list",
    ),
    max_rounds=8,
    instruction="""Your speciality is reconnaissance of the Wi-Fi (and, when asked, the
Bluetooth) environment using the ESP32 Marauder board attached to the Flipper.

You have PASSIVE tools only: confirm the board (wifi.board_info), scan for access points
and their client stations, read the lists, and - if the task mentions Bluetooth - scan
BLE. You have no attack tools of any kind, on purpose: you can never deauthenticate, spam
or impersonate anything, and you must not claim to. Any offensive action is the main
agent's decision, taken with the user, after your report.

Work in order: board_info first, then scan_ap, then list_ap; add scan_station/list_station
when clients matter, and ble.scan/ble.list only if the task is about Bluetooth. Read the
list tokens carefully - each access point is 'index;SSID;BSSID;channel;RSSI;encryption' -
and build an interpreted picture: how many networks, which are OPEN versus encrypted (an
open network is the notable one), which channels are crowded, which access points are
strong (close, RSSI near -40) versus weak (far, below -80), and which have active clients.

If the task named a specific network, say plainly whether it appears in the scan and give
its index, channel and encryption; if it does not appear, say so rather than guessing. End
with the few access points or stations that best match what the main agent asked for.""",
)

SPECS = {spec.key: spec for spec in (SCANNER, ANALYST, WIFI_RECON)}


class SubagentRunner:
    """Runs subagents, each in its own conversation with the model.

    Reports progress through an `emit(kind, **fields)` callback rather than building the
    reasoning chain itself: the chain belongs to the turn that summoned the subagent, and
    the runner has no business knowing how it is displayed.
    """

    def __init__(self, api_key, dispatcher, simulated=False, model=None):
        self._genai = genai.Client(api_key=api_key)
        self._dispatcher = dispatcher
        self._simulated = simulated
        self.model = model or SUBAGENT_MODEL

    def _allowed(self, spec):
        """The catalog entries this subagent actually receives as tools.

        A name in spec.tools that the catalog does not carry cannot be granted, so it is
        dropped here. Both the announcement and the chat session are derived from this one
        list on purpose: if they were computed separately, the interface could promise the
        user a tool the subagent never got.
        """
        return [
            command
            for command in self._dispatcher.device_catalog
            if command["name"] in spec.tools
        ]

    def describe(self, key):
        """What the interface announces when this subagent is summoned."""
        spec = SPECS[key]
        return {
            "role": spec.role,
            "model": self.model,
            "tools": [tool_name(command["name"]) for command in self._allowed(spec)],
            "max_rounds": spec.max_rounds,
        }

    def _chat(self, spec):
        instruction = BASE_INSTRUCTION + "\n" + spec.instruction
        if self._simulated:
            instruction += SIMULATED_NOTICE

        allowed = self._allowed(spec)
        return self._genai.chats.create(
            model=self.model,
            config=types.GenerateContentConfig(
                system_instruction=instruction,
                tools=[build_tool(allowed)] if allowed else None,
                thinking_config=(
                    types.ThinkingConfig(include_thoughts=True) if INCLUDE_THOUGHTS else None
                ),
            ),
        )

    def _prompt(self, spec, task):
        if not spec.needs_log:
            return task
        log = self._dispatcher.log_text()
        return f"{task}\n\nSESSION LOG:\n{log or '(the log is empty: nothing has been executed on the device yet)'}"

    def run(self, key, task, emit=None):
        """Summons the subagent, runs it to completion and returns its report.

        The returned dictionary is what the main agent receives as the tool result, so it
        carries both the prose report and the raw evidence behind it.
        """
        spec = SPECS.get(key)
        if spec is None:
            return {"status": "error", "error": f"unknown subagent: {key}"}

        def announce(kind, **fields):
            if emit:
                emit(kind, **fields)

        announce("spawn", name=spec.name, task=task, meta=self.describe(key))

        chat = self._chat(spec)
        evidence = []
        rounds = 0
        truncated = False
        final_requested = False

        response = send_with_retry(chat, self._prompt(spec, task))
        while True:
            rounds += 1
            for thought in thought_texts(response):
                announce("thought", text=thought, source=spec.name)

            if not response.function_calls:
                break

            if rounds >= spec.max_rounds:
                truncated = True
                if final_requested:
                    # It was already asked to stop and called a tool regardless. Asking
                    # again would be an unbounded loop of requests, which is precisely
                    # what the budget exists to prevent, so the loop ends here.
                    break
                # The budget is spent. The subagent is asked for its conclusion rather
                # than cut off, so the work already done still produces a usable report.
                final_requested = True
                response = send_with_retry(
                    chat,
                    "Your round budget is spent. Do not call any more tools: report now, "
                    "on the basis of what you have measured so far.",
                )
                continue

            results = []
            for call in response.function_calls:
                args = dict(call.args or {})
                outcome = self._dispatcher.dispatch_device(call.name, call.args)
                announce("tool", name=call.name, args=args, outcome=outcome, source=spec.name)
                evidence.append({"command": call.name, "args": args, "result": outcome})
                results.append(
                    types.Part.from_function_response(name=call.name, response=outcome)
                )
            response = send_with_retry(chat, results)

        report = answer_text(response)
        if not report:
            # Possible when the budget ran out mid-tool-call: there is no prose to hand
            # back. Saying so plainly is better than returning an empty report, which the
            # main agent could mistake for 'nothing found'.
            report = (
                f"No report: the subagent used up its budget of {spec.max_rounds} rounds "
                f"without drawing a conclusion. It took {len(evidence)} readings, listed "
                "in the evidence."
            )
        # 'commands', not 'measurements': the evidence holds every command the subagent
        # ran, and not all of them measure anything - a real run began with 'info' to check
        # the device was answering. Counting that as a measurement would overstate, by one,
        # how much was actually observed.
        meta = {"rounds": rounds, "commands": len(evidence), "truncated": truncated}
        announce("report", name=spec.name, text=report, meta=meta)

        return {
            "status": "ok",
            "subagent": spec.name,
            "role": spec.role,
            "model": self.model,
            "report": report,
            # The raw evidence travels with the report on purpose: the main agent should
            # be able to check the conclusion against the readings instead of trusting it.
            "evidence": evidence,
            "rounds": rounds,
            "budget_exhausted": truncated,
        }
