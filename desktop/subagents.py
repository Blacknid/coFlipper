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
from commands import build_skill_tool, build_tool, tool_name
from skills import SKILLS, skill_specs

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
    # Skills this subagent may call: desktop capabilities (defined in skills.py), granted
    # the same way device tools are. The audit subagent is given the file-search skills and
    # NO device tools, so it can search the project's own files without ever touching the
    # radio. A device subagent leaves this empty, and vice versa.
    skills: tuple = ()
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

LISTENER = Spec(
    key="listener",
    name="listener",
    role="listens on one frequency over a window and harvests every distinct Sub-GHz signal it decodes, saving each one",
    tools=("ping", "info", "subghz.rssi", "subghz.read"),
    skills=("save_subghz", "list_subghz"),
    max_rounds=12,
    instruction="""Your speciality is harvesting Sub-GHz signals from the air on one
frequency, over a stretch of time, and reporting the DISTINCT devices you heard.

The main agent gives you a frequency (in Hz) and a listening window (in seconds). The air
on a busy ISM frequency like 433.92 MHz is shared: several unrelated devices transmit on
it - a gate remote, an electric relay, a doorbell, a weather sensor - and a single capture
catches whichever happened to fire at that instant. Your job is to listen repeatedly across
the whole window and build the LIST of everything that showed up, not to stop at the first
code.

How you work:
1. Optionally take one subghz.rssi reading first to confirm there is activity at all; if the
   frequency is plainly dead (well below about -90 dBm), say so and stop - there is nothing
   to harvest.
2. Call subghz.read on the frequency, in a loop, until the window is used up or reads stop
   returning new signals. Each read decodes at most one signal.
   - A result beginning 'signal' carries: protocol, key, bit-length, the frequency actually
     synthesised, and RSSI, in that order.
   - A result of 'no_signal' means that read timed out with nothing decoded. A couple of
     consecutive 'no_signal's means the air has gone quiet: stop, do not keep burning reads.
3. DEDUPLICATE. The same device often repeats; treat two captures with the same protocol
   AND the same key as one device, not two. What you report is the set of DISTINCT signals.
4. For each distinct signal, name what it plausibly is from its protocol and frequency - a
   fixed-code doorbell, a rolling-code car fob, a relay/actuator, a weather sensor - and say
   plainly it is a guess, not a certainty. The protocol narrows it but rarely pins the brand.
5. SAVE each distinct signal with save_subghz, passing the frequency, protocol, key, bits,
   rssi and - importantly - your plain-English guess as 'guess'. The guess goes into the
   file's name, which is what makes the capture findable later, so make it specific ('a
   wireless doorbell remote', not just 'a remote'). Save once per distinct device, not once
   per read.

Read every field from a tool result - never invent a protocol, key or RSSI. Every read is a
command on a physical radio and costs part of your round budget, so listen as long as the
window and the traffic warrant, and no longer. End with the harvested list: for each device,
its protocol, key, RSSI, your one-line guess at what it is, and the name it was saved
under.""",
)

WATCHER = Spec(
    key="watcher",
    name="watcher",
    role="waits on one frequency for a bounded window and reports the first Sub-GHz signal it decodes",
    tools=("ping", "info", "subghz.rssi", "subghz.read"),
    max_rounds=10,
    instruction="""Your speciality is WAITING for a Sub-GHz signal that has not been sent
yet. The main agent summons you when the user is about to play or trigger a signal ('a
signal is going to be played shortly', 'watch for the fob'), and your job is to catch the
first real transmission and report it at once.

This is not harvesting. You are not building a list of everything on the band - you are
waiting for ONE thing to happen and reacting to it. Stop the moment you have it.

The main agent gives you a frequency (in Hz) and a window (in seconds). How you work:
1. Optionally take one subghz.rssi reading to note the resting background level, so the
   arriving signal can be told apart from it. This is not required - do not spend more than
   one reading on it.
2. Call subghz.read on the frequency, in a loop. Each read waits a short moment for a decode.
   - The instant a read returns a result beginning 'signal' - carrying protocol, key,
     bit-length, the frequency actually synthesised and RSSI - you STOP. Do not read again:
     the thing you were waiting for has arrived, and continuing only burns budget.
   - A result of 'no_signal' means that read window passed with nothing on the air. That is
     the EXPECTED case while you wait, not a reason to stop: keep reading until either a
     signal arrives or your round budget is spent.
3. When a signal arrives, report it: the protocol, key, bit-length and RSSI exactly as the
   read returned them, plus a plain-English guess at what the device plausibly is (a doorbell,
   a car fob, a relay, a sensor), stated as a guess. Say clearly that the signal WAS heard.
4. If the whole window passes with only no_signal, report plainly that NOTHING was heard in
   the window - do not invent a signal to fill the silence, and do not claim the frequency is
   dead (the sender may simply not have played it in time). Suggest the window may have been
   too short, or the wrong frequency.

Read every field from a real tool result - never invent a protocol, key or RSSI. Every read
is a command on a physical radio and costs part of your budget, so keep reading while the
window lasts, and stop the instant a signal lands.""",
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

AUDIT = Spec(
    key="audit",
    name="audit",
    role="searches the project's generated-app files on disk, without touching the device",
    tools=(),
    skills=("list_app_store", "search_app_store"),
    max_rounds=6,
    instruction="""Your speciality is searching the coFlipper project's own files - the
Flipper apps that have been generated and saved to disk - to answer a question about them.

You have file-search skills only, and NO device tools of any kind. You cannot measure a
frequency, scan for networks or transmit anything, and you must not claim to: your entire
world is the files on disk. Everything you report comes from a skill result, never from
guessing what an app probably contains.

The generated apps live in a store, one directory per app, each with: the editable C
source, the FAP manifest, the last build log, and one history record per build or edit.
Your skills:
- list_app_store: the apps that exist, with their build status. Start here when the
  question is open ('what have I built?', 'which apps are there?') or when you do not yet
  know which app to search into.
- search_app_store: a case-insensitive substring search across those files. Each hit names
  the app, which file it was found in (source, manifest, build_log, history), the line
  number and a snippet. Pass an appid to confine the search to one app.

Work in order: if you already know the app, search it directly; otherwise list first, then
search. Choose search terms that actually appear in the files - a Flipper C source uses
identifiers like InputKeyOk, furi_, canvas_draw_, view_port_, and a failed build log
contains 'error:'. If a search returns nothing, say so plainly rather than inventing a
match. If a result is marked truncated, narrow the query or scope it to one app and search
again. End with a short, factual answer to the question, citing the app ids and files the
finding rests on.""",
)

SPECS = {spec.key: spec for spec in (SCANNER, LISTENER, WATCHER, ANALYST, WIFI_RECON, AUDIT)}


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

    def _allowed_skills(self, spec):
        """The skill names this subagent actually receives.

        A name in spec.skills that skills.py does not define cannot be granted, so it is
        dropped here - the same guarantee _allowed() gives for device tools: the interface
        never promises a capability the subagent did not get.
        """
        return [name for name in spec.skills if name in SKILLS]

    def describe(self, key):
        """What the interface announces when this subagent is summoned."""
        spec = SPECS[key]
        device_tools = [tool_name(command["name"]) for command in self._allowed(spec)]
        return {
            "role": spec.role,
            "model": self.model,
            # Device tools and skills are announced together: to the user they are both
            # 'the tools this subagent was given', regardless of which layer serves them.
            "tools": device_tools + self._allowed_skills(spec),
            "max_rounds": spec.max_rounds,
        }

    def _chat(self, spec):
        instruction = BASE_INSTRUCTION + "\n" + spec.instruction
        if self._simulated:
            instruction += SIMULATED_NOTICE

        allowed = self._allowed(spec)
        skills = self._allowed_skills(spec)
        tools = []
        if allowed:
            tools.append(build_tool(allowed))
        if skills:
            tools.append(build_skill_tool({name: SKILLS[name] for name in skills}))
        return self._genai.chats.create(
            model=self.model,
            config=types.GenerateContentConfig(
                system_instruction=instruction,
                tools=tools or None,
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

        skill_names = set(self._allowed_skills(spec))
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
                # A granted skill is a desktop capability, routed to dispatch_skill; anything
                # else is a device command. Routing by the granted set, not by guessing from
                # the name, is what keeps a subagent to exactly the tools it was announced.
                if call.name in skill_names:
                    outcome = self._dispatcher.dispatch_skill(call.name, args)
                else:
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
