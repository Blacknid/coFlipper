"""The coFlipper agent: connects the Gemini model to the Flipper Zero device.

The user writes in natural language, the model decides which CFP commands are needed,
the agent executes them on the device and returns the real results to the model, on
whose basis it formulates the final answer.

Running:
    python agent.py           # with a Flipper connected over USB
    python agent.py --mock    # without a device, for development
"""

import argparse
import itertools
import os
import sys
import time

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

from commands import CommandDispatcher, build_tool, load_catalog, model_commands
from reasoning import ANSWER, REPORT, SPAWN, THOUGHT, TOOL, Trace

# A deliberately pinned model, not a "latest" alias: the alias always tracks the most
# recent generation, and that generation comes with different usage limits.
# On the free plan each model has roughly 20 requests per day, counted separately, so
# switching model through the COFLIPPER_MODEL environment variable grants a fresh quota.
MODEL = os.environ.get("COFLIPPER_MODEL", "gemini-3.5-flash")

# The models offered in the interface's picker: the chat-capable Gemini models (function
# calling + reasoning), not the specialised ones (image, TTS, music, robotics). Ordered
# newest-first; whatever MODEL is set to is guaranteed to appear, so a custom COFLIPPER_MODEL
# is still selectable. list_models.py shows everything the key can reach.
SELECTABLE_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3-flash-preview",
    "gemini-3.1-pro-preview",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-flash-latest",
]
if MODEL not in SELECTABLE_MODELS:
    SELECTABLE_MODELS.insert(0, MODEL)

# The API occasionally returns 503 when overloaded. Without a retry, such a transient
# error would interrupt the conversation in progress.
SEND_RETRIES = 3
RETRY_DELAY_S = 2.0

# A 503 (the model is momentarily overloaded on Google's side) is transient and usually
# clears within tens of seconds - so it is worth riding out with several attempts and a
# growing wait, rather than surfacing to the user after a couple of quick tries. The waits
# grow (2, 4, 8, 16, 16... seconds, capped) so a longer spike is still caught without
# hammering the API. Overridable in case a demo needs to fail fast or wait even longer.
SERVER_RETRIES = int(os.environ.get("COFLIPPER_SERVER_RETRIES", "6"))
SERVER_BACKOFF_CAP_S = 16.0

# The most attempts any single send makes: server errors get the larger budget, everything
# else stops sooner (the per-error logic in _retry_after_error decides when to give up).
MAX_SEND_ATTEMPTS = max(SEND_RETRIES, SERVER_RETRIES)


class TurnCancelled(Exception):
    """The user stopped the turn while it was still running. Raised cooperatively at the turn's
    checkpoints (between rounds, between streamed chunks, before each tool call), since a
    running thread cannot be killed safely - so a stop takes effect at the next checkpoint, not
    necessarily the instant it is pressed."""


class ModelOverloaded(Exception):
    """The model stayed overloaded (503) after every retry. Carries a message the interface
    can show as-is: it is a passing Google-side spike, not a fault in the request, so the
    remedy is to try again shortly or switch model - not to change anything."""

    def __init__(self, model):
        self.model = model
        super().__init__(
            f"Modelul {model} este momentan supraîncărcat (503) și a rămas așa după mai multe "
            f"reîncercări. Este o supraîncărcare temporară la Google, nu o eroare a cererii. "
            f"Reîncearcă în câteva momente, sau pornește cu alt model prin COFLIPPER_MODEL "
            f"(de ex. gemini-3.6-flash)."
        )

# Recent models reason before answering and can return a summary of that reasoning. We
# request it explicitly: without it, the only thing visible between the user's request and
# the final answer would be the list of executed commands, not the reason the agent chose
# exactly those commands.
# COFLIPPER_THOUGHTS=0 disables the request, for models that do not accept it.
INCLUDE_THOUGHTS = os.environ.get("COFLIPPER_THOUGHTS", "1") != "0"

SYSTEM_INSTRUCTION = """You are the assistant of the coFlipper project. You control a
Flipper Zero device connected over USB, using the tools placed at your disposal.

Rules you follow strictly:
1. Any information about the state of the device or about surrounding signals comes
   EXCLUSIVELY from the result of a tool you called. You never invent frequencies, UIDs,
   protocols or hardware readings.
2. If a tool answers with an error, you tell the user openly what failed, and you do not
   compensate for the error with a plausible invented answer. The errors the device
   returns are 'unknown_command' (the firmware does not know that command), 'not_implemented'
   (the firmware recognises the command but has not implemented it yet), 'bad_frame',
   'missing_frequency' and 'invalid_frequency' (the frequency is outside the bands the
   CC1101 transceiver can reach: roughly 300-348, 387-464 and 779-928 MHz). The
   connection itself adds two: 'device not connected' (the Flipper Zero is not attached
   over USB - you ask the user to connect it) and 'the coFlipper CFP application is not
   running on the device' (you ask them to start it from the Flipper's menu). The device
   can be connected or disconnected at any moment during the conversation, so one command
   may fail even though an earlier one succeeded.
3. You may explain general technical notions from your own knowledge, but you mark
   clearly the difference between a general explanation and data measured by the device.
4. Answer in the language in which you received the prompt, and reason in that same
   language: the steps of your reasoning are shown to the user, alongside the executed
   commands.
5. You answer in plain text, without Markdown markup - no asterisks, hashes or backticks.
   The answer is displayed in a window that does not interpret such markup, so it would
   appear as pointless punctuation.
6. Some tools do not execute a command themselves but delegate the work to a specialised
   subagent, which carries it out and reports back to you. Their descriptions say so.
   Delegate when the job needs several successive measurements, or when the question is
   about what the session has already done rather than about the present moment. A
   subagent's report reaches you together with the raw readings behind it: if the two
   disagree, you trust the readings and say so.
7. For infrared commands ('turn off the TV', 'volume up', 'change the channel') you use
   the agent_ir_control tool. You pass it the user's request as they phrased it, plus the
   brand of the appliance if they mentioned one. For follow-up requests ('louder', 'next
   channel') you pass device_type explicitly, since the appliance is no longer named but
   you know it from the conversation. You never invent IR codes and you do not call the
   individual ir.* commands yourself: the tool manages them on its own.
8. When you start an IR bruteforce, you tell the user to press the middle button (OK) on
   the Flipper the moment the appliance reacts - that is what stops the emission. If the
   result has 'worked': false, the codes were exhausted without confirmation: you say so
   openly and suggest trying again with the brand specified, instead of claiming success.
9. If the user says it did not work ('still not working', 'nothing happened'), you call
   agent_ir_control again with search_online=true, to search the online database of real
   remotes (thousands of models), not just the built-in table. You need the appliance's
   brand: if the user has not given it, ask first. Warn them it takes longer, since it is
   downloaded from the internet. After five unsuccessful attempts on the same appliance,
   the online search starts automatically. In the answer you say where the codes came
   from: 'code_source': 'builtin' means the internal table, 'irdb' means the online
   database. If the result contains 'next_step', you use it to tell the user what follows.
10. An optional Wi-Fi dev board - an ESP32 flashed with Marauder firmware - may be attached
   to the Flipper's GPIO/UART header. It unlocks the wifi.* and ble.* tools: scanning for
   networks and Bluetooth devices, sniffing frames, capturing handshakes, and active
   operations such as deauthentication, beacon spam, evil-portal and BLE pairing spam. How
   you use them:
   - Each device tool is either passive (only listens or reads: scans, lists, sniffs, GPS,
     wardrive, board_info) or offensive (transmits and disrupts other devices: wifi.attack_*,
     wifi.evil_portal, wifi.karma, ble.spam_*). Passive tools you may use freely to survey
     the environment.
   - Before ANY offensive tool, you FIRST ask the user to confirm, in one sentence, that
     they own the target network/device or have explicit written authorization to test it.
     You send the command only after they confirm. If they decline or cannot confirm, you
     refuse and briefly explain that deauthenticating, impersonating or spamming networks
     and devices you are not authorized to touch is both harmful and, in most places,
     illegal. You never turn a refusal into instructions for doing it anyway.
   - Most attacks act on a target chosen beforehand with wifi.select_ap / wifi.select_station.
     When the user names a network, first scan (wifi.scan_ap) and list (wifi.list_ap), match
     the name, and select just that one - do not attack everything in range unless the user
     explicitly asks for that and confirms authorization for the whole environment. For an
     open question like 'what is around', prefer delegating to the wifi_recon subagent.
   - Marauder operations run continuously until stopped. Send wifi.stop / ble.stop as soon
     as the user's goal is met, and always before starting a different operation.
   - Wi-Fi/BLE tools add their own errors: 'wifi_board_not_connected' (the ESP32 Marauder
     board is not attached or powered - tell the user to connect it to the GPIO header),
     'no_target_selected' (an attack was requested before selecting a target - scan, list
     and select first), 'invalid_channel', 'invalid_selection' and 'unknown_command' (the
     board's firmware does not know that command).
11. You can build actual Flipper Zero applications on request. When the user asks for an
   app ('build me a paint app', 'make a stopwatch app'), you use build_flipper_app, passing
   their request as they phrased it; suggest a short app_name and confirm it with them if
   they did not give one. To change an app that already exists ('add a bigger brush to the
   paint app'), you use edit_flipper_app with the app's name and the change requested. These
   tools run a three-way design debate, compile the C source with the Flipper toolchain and,
   if a device is connected, install it. They report the REAL compiler result: if the build
   failed you say so and relay the error, and you never claim an app was built, installed or
   is running unless the tool result says it was. The build takes a while and costs several
   model requests, so warn the user before starting one.
12. When answering means searching through the apps you have already built - their C source,
   their build logs, their history, their name or their location on disk - rather than
   reading the device, you use the audit_files tool. You pass it the user's question as they
   phrased it, and an app id if it is clear which app is meant. Treat these as trigger words
   that call for this tool: 'search', 'find', 'look for', 'which app', 'what apps', 'do I
   have', 'is there an app', 'where is', 'location of', 'path to', 'the file for', 'the
   source of', 'the code for', 'the name of', 'list my apps', 'what have I built', together
   with any mention of an app or script by name in a question that asks you to locate,
   identify or inspect it rather than build or run it. Examples of the shape, not an
   exhaustive list: 'find the app that does X', 'give me the name of the <something> app',
   'where is the <something> app', 'what's the path to my <something> script', 'which of my
   apps uses the OK button', 'find the app that failed to compile', 'what have I built so
   far'. When the user asks where an app or script is located, answer with the app id and
   the file path the search returns - that is the location of the generated app on disk.
   This delegates to an audit subagent that only reads files and has no device tools, so it
   can never touch the radio; you answer from what its search returned, and you never guess
   what a generated app contains, is called, or where it lives without searching first.
   One boundary: audit_files searches the apps you generated and saved on this computer. It
   does NOT list arbitrary files on the Flipper's own SD card - saved signals, other apps
   installed outside this project - which are a device matter, not a file-audit one; if the
   user clearly means files on the device's SD card rather than the apps built here, say so
   plainly instead of pretending the audit found them.
13. For Sub-GHz, distinguish three tools. subghz.rssi measures the raw signal level on a
   frequency; agent_check_frequency_activity delegates several such readings to decide whether
   anything is transmitting. subghz.read decodes ONE packet - rarely what you want on its own.
   When the user asks what is transmitting on a frequency, or asks to capture/record a signal,
   you use agent_listen: it delegates to the listener subagent, which listens across a time
   window, harvests the LIST of distinct devices sharing the frequency (a busy band like
   433.92 MHz commonly carries a relay, a doorbell, a car fob and a sensor at once), guesses
   what each is, and saves each distinct signal as a descriptively named .sub file in the
   apps_assets folder so it can be found and replayed later. Pass the frequency and, if the
   user gave one, how long to listen; if no frequency is given it defaults to 433.92 MHz. The
   listener is passive - it only receives - so harvesting never transmits. When instead the
   user tells you a signal is ABOUT TO BE PLAYED or triggered - 'a Sub-GHz signal is going to
   be played shortly', 'I'm about to press the fob, watch for it', 'catch the next signal on
   433.92' - you use agent_watch_subghz: it delegates to the watcher subagent, which waits on
   the frequency for a bounded window and reports the FIRST signal that arrives, stopping the
   instant it hears one. Tell these three apart by intent: agent_check_frequency_activity asks
   'is anything transmitting right now', agent_listen harvests the whole list of what is
   ALREADY on the band, and agent_watch_subghz WAITS for a single upcoming signal you have
   been warned about. Pass the frequency and, if the user gave one, how long to wait; the
   defaults are 433.92 MHz and 15 seconds. The watcher is passive - it only receives. If the
   window passes with nothing heard, you relay that plainly and do not pretend a signal came.
14. To replay a captured Sub-GHz signal, you use agent_replay_subghz, naming the capture the
   way the user refers to it ('the doorbell one', 'the relay signal') - it resolves that to
   the saved file. Replaying TRANSMITS and can actuate the real device the signal belongs to,
   so it is offensive: exactly like the offensive Wi-Fi/BLE tools, you FIRST ask the user to
   confirm in one sentence that they own the device or are authorized to test it, and you send
   it only after they confirm. If the name is ambiguous or unknown the tool lists the available
   captures rather than guessing; you relay that list and ask which one they mean.
14a. There is a second, RAW way to capture and replay a Sub-GHz signal, for when the goal is to
   record a signal and play it back later rather than to identify its protocol - a relay remote,
   a garage fob, anything whose protocol may be unknown. When the user says they are about to
   play or trigger a signal they want to keep ('I'm going to play a relay remote', 'record my
   garage fob'), use agent_capture_raw: derive a short descriptive name from their words
   ('a relay remote' -> 'raw_relay_remote', 'the garage fob' -> 'raw_garage_fob') and pass it as
   name. It records the waveform onto the Flipper for a 5-second window (default 433.92 MHz) and
   returns the exact name it saved under. After it captures, TELL THE USER the exact saved name
   and that they can replay it later by saying "play <that name>", then STOP - capturing does not
   replay. Capturing is passive. Later, when the user says 'play <name>' (or 'replay the relay
   one'), use agent_replay_raw with that name: it plays for about 5 seconds. Replaying a raw
   capture TRANSMITS and can actuate the real device, so it is offensive exactly like
   agent_replay_subghz - FIRST get the user's one-sentence confirmation that they own the device
   or are authorized, and only then send it. An ambiguous or unknown name lists the candidates
   instead of guessing; relay that and ask which they mean. Use the raw pair (capture_raw /
   replay_raw) when the intent is record-and-replay; use agent_listen + agent_replay_subghz when
   the intent is to identify what is on a frequency and save it decoded.
15. You have a persistent memory across sessions. When the user tells you something durable
    and worth keeping - a device they own and its brand, a preference, a lasting finding -
    you save it with the agent_remember tool, in one short sentence, so you still know it
    the next time they open the application. You do not store secrets, passwords or one-off
    values, and you do not announce that you are saving something unless asked. Whatever you
    already remember is listed below under PERSISTENT MEMORY, when there is anything.
16. When a job needs custom logic that no single command captures - polling a frequency
    until a value settles or a signal appears, chaining several reads under a condition,
    timing a sequence, retrying until something happens, computing a result across many
    readings - you may write a short, temporary script and run it with agent_run_script,
    passing a one-line 'purpose' and the Python in 'code'. Reach for this only when no
    dedicated command fits: if agent_listen, agent_watch_subghz, agent_check_frequency_activity
    or an IR tool already does the job, use that instead. The script runs sandboxed - it can
    import only time, math, json, random and statistics, and it reaches the device solely
    through flipper.request('subghz.read', 433920000), which returns the usual result dict;
    it has no filesystem, network or shell access, and it is killed if it runs past its time
    limit, so keep the logic bounded. Whatever the script prints is returned to you, together
    with the real results of every device command it ran: base your answer on those recorded
    results, not on the printed text alone, exactly as you would with a subagent's evidence.
    A script that sends a TRANSMITTING command (subghz.send, ir.send, nfc.emulate, offensive
    Wi-Fi/BLE) is offensive just as a direct call would be: you run the authorization gate
    first and keep scripts passive unless the user has authorized the transmission.
"""

# Appended to the system instruction when working without a physical device. Without it,
# the model receives plausible responses from the simulator and tells the user the Flipper
# is connected and working - exactly the confusion this project set out to avoid.
SIMULATED_NOTICE = """
WARNING - SIMULATED MODE: no physical Flipper Zero is connected. Every tool is served by
a simulator, and their results are fictitious. They carry the field 'simulated': true.
You never state that the device is connected or that a value was measured. In every
answer that refers to the state of the device or to signals, you state explicitly that
the data comes from a simulator.
"""


def build_client_for_device():
    from cfp_client import pick_port
    from protocol import CFPClient

    return CFPClient(pick_port())


def _system_instruction(simulated=False, memory_prompt=""):
    """The full system instruction: the rules, then the persistent memory, then, in
    simulated mode, the warning. Memory comes before the simulated notice so the warning
    stays the last thing the model reads."""
    instruction = SYSTEM_INSTRUCTION
    if memory_prompt:
        instruction += memory_prompt
    if simulated:
        instruction += SIMULATED_NOTICE
    return instruction


def _chat_config(commands, simulated=False, memory_prompt=""):
    """The GenerateContentConfig shared by a fresh chat and by a compacted one, so a
    conversation keeps exactly the same tools and instruction after it is compacted."""
    return types.GenerateContentConfig(
        system_instruction=_system_instruction(simulated, memory_prompt),
        tools=[build_tool(commands)],
        thinking_config=(
            types.ThinkingConfig(include_thoughts=True) if INCLUDE_THOUGHTS else None
        ),
    )


def build_chat(api_key, commands, simulated=False, memory_prompt="", model=None):
    """The conversation session, with the tools derived from the command catalog.

    Returns the client as well, not only the chat: the caller has to keep a reference to
    it, otherwise the garbage collector destroys it and closes the HTTP connection the
    conversation relies on.

    memory_prompt is the persistent memory (memory.py) folded into the system instruction,
    so every new conversation starts knowing the facts the agent chose to remember.

    model chooses which Gemini model backs the chat; the interface lets the user pick it per
    chat. When omitted it falls back to the process default (MODEL / COFLIPPER_MODEL).
    """
    client = genai.Client(api_key=api_key)
    chat = client.chats.create(
        model=model or MODEL, config=_chat_config(commands, simulated, memory_prompt)
    )
    return client, chat


def rebuild_chat(client, old_chat, commands, simulated=False, memory_prompt="", model=None):
    """The same conversation, moved onto a different model. Its whole history carries over, so
    switching model mid-chat continues where it left off rather than starting blank. Used by
    the interface's model picker."""
    return client.chats.create(
        model=model or MODEL,
        config=_chat_config(commands, simulated, memory_prompt),
        history=old_chat.get_history(),
    )


# --- Context management ---------------------------------------------------------
# A conversation's history is its short-term memory, but it cannot grow without bound: every
# turn resends the whole of it, so the cost per turn rises and the model's context limit
# looms. Past a soft budget of turns, the older part is replaced by a summary and only the
# recent turns are kept verbatim - the conversation continues seamlessly on a short context.
CONTEXT_TURN_LIMIT = int(os.environ.get("COFLIPPER_CONTEXT_TURNS", "16"))
CONTEXT_KEEP_RECENT = 6

SUMMARY_INSTRUCTION = """You compress a coFlipper conversation so it can continue without
carrying its whole history. Write a short, factual summary in the conversation's own
language: what the user asked, what was actually measured or done on the device (with the
concrete values and the command results), what failed, and any decision or preference that
should carry forward. Keep every real reading; drop the small talk. Plain text, no Markdown."""


def _history_text(contents):
    """The chat history flattened to plain text, for the summariser to compress."""
    lines = []
    for content in contents:
        role = getattr(content, "role", "?")
        for part in getattr(content, "parts", None) or []:
            if getattr(part, "text", None):
                lines.append(f"{role}: {part.text}")
            elif getattr(part, "function_call", None):
                lines.append(f"{role}: called {part.function_call.name}")
            elif getattr(part, "function_response", None):
                fr = part.function_response
                lines.append(f"{role}: result of {fr.name} -> {fr.response}")
    return "\n".join(lines)


def _summarize_history(client, contents, model=None):
    chat = client.chats.create(
        model=model or MODEL,
        config=types.GenerateContentConfig(system_instruction=SUMMARY_INSTRUCTION),
    )
    response = send_with_retry(chat, "Summarise the conversation so far:\n\n" + _history_text(contents))
    return answer_text(response)


def maybe_compact(client, chat, commands, simulated=False, memory_prompt="", on_summary=None,
                  model=None):
    """Compacts an over-long chat. Returns (chat, compacted): the same chat when it is still
    within budget, or a fresh one seeded with a summary plus the recent turns when it is not.

    on_summary(summary, dropped_count) is called when a compaction happens, so the interface
    can show that the context was compressed instead of it happening invisibly. model keeps
    the compacted chat on the same model the tab was using.
    """
    history = chat.get_history()
    if len(history) <= CONTEXT_TURN_LIMIT:
        return chat, False

    keep = list(history[-CONTEXT_KEEP_RECENT:])
    older = history[:-CONTEXT_KEEP_RECENT]
    if not older:
        # Nothing old enough to summarise (only possible if the budget is set below the
        # number of turns kept); compacting would grow the history, not shrink it.
        return chat, False
    summary = _summarize_history(client, older, model)

    seeded = [
        types.Content(
            role="user",
            parts=[types.Part.from_text(text="[Summary of the earlier conversation]\n" + summary)],
        ),
        types.Content(
            role="model", parts=[types.Part.from_text(text="Understood, continuing from here.")]
        ),
    ] + keep
    new_chat = client.chats.create(
        model=model or MODEL, config=_chat_config(commands, simulated, memory_prompt), history=seeded
    )
    if on_summary:
        on_summary(summary, len(older))
    return new_chat, True


def _response_parts(response):
    for candidate in response.candidates or []:
        content = getattr(candidate, "content", None)
        for part in (getattr(content, "parts", None) or []) if content else []:
            yield part


def thought_texts(response):
    """The reasoning summaries in the response, in the order the model produced them.

    A summary arrives as an ordinary piece of text, distinguished only by the 'thought'
    flag. Models that offer no summaries simply return zero pieces of this kind, and the
    resulting chain contains only the executed commands.
    """
    return [
        part.text for part in _response_parts(response) if getattr(part, "thought", False) and part.text
    ]


def answer_text(response):
    """The text addressed to the user, without the reasoning summaries.

    We do not use response.text directly: it can include the reasoning parts as well,
    which are the model's internal notes and belong in the chain, not in the answer.
    """
    chunks = [
        part.text
        for part in _response_parts(response)
        if part.text and not getattr(part, "thought", False)
    ]
    return "".join(chunks).strip() or (response.text or "").strip()


def _retry_after_error(exc, attempt):
    """Handles a transient send error: sleeps before a retry, or re-raises / exits when the
    error is fatal or the retries are spent. Shared by the blocking and streaming senders."""
    if isinstance(exc, errors.ServerError):
        # 503 and friends are Google-side overload, not our fault and not permanent. Ride
        # them out with a growing wait; give up only after SERVER_RETRIES with a message that
        # says what it was and what the user can do about it.
        if attempt >= SERVER_RETRIES:
            raise ModelOverloaded(MODEL) from exc
        wait = min(SERVER_BACKOFF_CAP_S, RETRY_DELAY_S * (2 ** (attempt - 1)))
        print(
            f"  [gemini] {MODEL} overloaded ({exc.code}); retry {attempt}/{SERVER_RETRIES - 1} "
            f"in {wait:.0f}s..."
        )
        time.sleep(wait)
        return
    if isinstance(exc, errors.ClientError):
        # Not all models accept the request for reasoning summaries. The API's raw message
        # does not say what needs changing, so we translate it.
        if exc.code == 400 and "thinking" in str(exc).lower():
            sys.exit(
                f"The model {MODEL} does not accept reasoning summaries.\n"
                "Start again with COFLIPPER_THOUGHTS=0: the chain will show the "
                "executed commands, but without the model's explanations."
            )
        if exc.code != 429:
            raise exc
        # A 429 with 'limit: 0' does not mean a quota we consumed ourselves, but a model
        # that is not available at all on the current plan: retrying is pointless.
        if "limit: 0" in str(exc):
            sys.exit(
                f"The model {MODEL} is not available on this API key's plan.\n"
                "Choose another one through the COFLIPPER_MODEL environment variable "
                "(list_models.py shows what exists)."
            )
        if attempt == SEND_RETRIES:
            raise exc
        print("  [gemini] request limit reached, waiting...")
        time.sleep(RETRY_DELAY_S * attempt * 5)
        return
    raise exc


def send_with_retry(chat, message):
    """A blocking send, retrying transient errors. Used by the subagents and the summariser,
    which consume a whole response at once rather than streaming it."""
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            return chat.send_message(message)
        except errors.APIError as exc:
            _retry_after_error(exc, attempt)


# A stream that yielded nothing (the request produced no content at all).
_STREAM_END = object()


def _open_stream_with_retry(chat, message):
    """Opens a streaming response and pulls its first chunk, retrying transient errors the
    same way send_with_retry does - they surface as the request is made and first read."""
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            stream = chat.send_message_stream(message)
            first = next(stream, _STREAM_END)
            return stream, first
        except errors.APIError as exc:
            _retry_after_error(exc, attempt)


def _consume_stream(stream, first, on_thought_delta=None, on_answer_delta=None, should_stop=None):
    """Reads a streamed round, emitting text as it arrives, and returns what it amounts to.

    Returns (thoughts, answer, calls): the reasoning summaries (each a full string), the
    answer text, and the tool calls requested. The deltas are forwarded live through the two
    callbacks, which is what gives the interface its typing feel; the return value is what the
    turn loop acts on, exactly as if the response had arrived whole.

    should_stop, when it returns True, aborts the stream between chunks with TurnCancelled - so
    a stop pressed while the model is still streaming its answer takes effect promptly.
    """
    thoughts, current, answer, calls = [], [], [], []

    def flush_thought():
        if current:
            thoughts.append("".join(current))
            current.clear()

    chunks = [] if first is _STREAM_END else itertools.chain([first], stream)
    for chunk in chunks:
        if should_stop and should_stop():
            stream.close()
            raise TurnCancelled()
        for part in _response_parts(chunk):
            if getattr(part, "thought", False) and getattr(part, "text", None):
                current.append(part.text)
                if on_thought_delta:
                    on_thought_delta(part.text)
            elif getattr(part, "function_call", None):
                continue  # collected from chunk.function_calls, below
            elif getattr(part, "text", None):
                # A switch from reasoning to answer ends the current thought.
                flush_thought()
                answer.append(part.text)
                if on_answer_delta:
                    on_answer_delta(part.text)
        for call in getattr(chunk, "function_calls", None) or []:
            flush_thought()
            calls.append(call)
    flush_thought()
    return thoughts, "".join(answer), calls


# The language a voice message is assumed to be in. Left to guess, the model sometimes mis-
# detects a short phrase (a Romanian "Cum te cheamă" heard as Italian, since they sound alike)
# and then answers in the wrong language. So a spoken turn pins the language explicitly, and
# asks the model to say back what it heard - a mishearing then shows on screen instead of
# passing silently. Override the assumed language with COFLIPPER_VOICE_LANG (e.g. "English").
VOICE_LANGUAGE = os.environ.get("COFLIPPER_VOICE_LANG", "Romanian")
VOICE_HINT = (
    "[VOICE MESSAGE] The user spoke this audio instead of typing it. Transcribe it faithfully, "
    "then act on it. Reply in the SAME language the user actually spoke: unless the audio is "
    f"clearly in another language, treat it as {VOICE_LANGUAGE} and never answer in a different "
    "language (in particular, do not confuse it with a similar-sounding one). Begin your reply "
    "with one short line, in that language, restating what you understood - e.g. 'Am auzit: "
    "\"...\"' - so a mishearing is visible, then answer normally."
)


def _first_message(message, attachment):
    """The payload for the opening round: plain text, or text-plus-media when the user spoke
    or attached something. Only the first round carries the attachment - the later rounds
    answer the model's tool calls and must stay text, or the model re-hears the same audio.

    A voice message also carries a language hint, so the model does not mis-detect the spoken
    language and answer in the wrong one (see VOICE_HINT)."""
    if not attachment:
        return message
    parts = []
    if attachment.get("mime_type", "").startswith("audio/"):
        parts.append(types.Part.from_text(text=VOICE_HINT))
    if message and message.strip():
        parts.append(types.Part.from_text(text=message))
    parts.append(
        types.Part.from_bytes(data=attachment["bytes"], mime_type=attachment["mime_type"])
    )
    return parts


def run_turn(chat, dispatcher, message, on_step=None, on_delta=None, attachment=None,
             should_stop=None):
    """One conversation turn, building the reasoning chain as it goes.

    A turn is not a single exchange of messages: the model may ask for a measurement,
    interpret it, then decide it needs another one before answering. Every round
    contributes steps to the chain - first the reasoning, then the commands that reasoning
    motivated - and at the end the answer phrased on the basis of them.

    A round may also summon a subagent. Its steps enter the same chain, one level deeper,
    so the delegated work stays visible instead of collapsing into a single opaque result.

    on_step(step) is called for every step as soon as it happens, so the display grows in
    real time rather than only at the end of the turn. on_delta(channel, text) is called
    with each streamed fragment of text - channel 'thought' while the model reasons, channel
    'answer' as it phrases the reply - which is what lets the interface type the answer out
    live instead of showing it whole at the end.

    attachment, when given, is a spoken (or otherwise non-text) message: {"bytes", "mime_type"}.
    It rides along with the opening request so the model hears it and answers in the same turn.

    should_stop, when it returns True, aborts the turn with TurnCancelled - this is how the
    interface's stop button cancels work in progress. It is checked at the natural boundaries
    (between rounds, between streamed chunks, before each tool call), so a stop takes effect
    within a moment rather than instantly.

    Returns (answer, chain).
    """
    trace = Trace(message or "🎤 mesaj vocal")

    def record(step):
        if on_step:
            on_step(step)
        return step

    def check_stop():
        if should_stop and should_stop():
            raise TurnCancelled()

    def subagent_event(kind, **fields):
        """Turns a subagent's progress into steps of this chain, nested one level.

        The subagent knows nothing about the chain; it only announces what it is doing.
        Translating those announcements here keeps ownership of the chain in one place.
        """
        if kind == "spawn":
            record(trace.add_spawn(fields["name"], fields["task"], fields["meta"]))
        elif kind == "thought":
            record(trace.add_thought(fields["text"], depth=1, source=fields["source"]))
        elif kind == "tool":
            record(
                trace.add_tool(
                    fields["name"],
                    fields["args"],
                    fields["outcome"],
                    depth=1,
                    source=fields["source"],
                )
            )
        elif kind == "report":
            record(trace.add_report(fields["name"], fields["text"], fields["meta"], depth=1))

    def thought_delta(text):
        if on_delta:
            on_delta("thought", text)

    def answer_delta(text):
        if on_delta:
            on_delta("answer", text)

    record(trace.first)
    pending = _first_message(message, attachment)
    reply = ""

    while True:
        check_stop()
        trace.next_round()
        stream, first = _open_stream_with_retry(chat, pending)
        thoughts, answer, calls = _consume_stream(
            stream, first, thought_delta, answer_delta, should_stop
        )

        for thought in thoughts:
            record(trace.add_thought(thought))

        if not calls:
            reply = answer.strip()
            break

        results = []
        for call in calls:
            check_stop()
            args = dict(call.args or {})
            outcome = dispatcher.dispatch(call.name, call.args, on_subagent_event=subagent_event)
            # A delegated call has already reported itself through subagent_event, spawn
            # step included; adding a tool step as well would duplicate it in the chain.
            if not outcome.get("subagent"):
                record(trace.add_tool(call.name, args, outcome))
            results.append(
                types.Part.from_function_response(name=call.name, response=outcome)
            )
        pending = results

    record(trace.add_answer(reply))
    return reply, trace


def main():
    parser = argparse.ArgumentParser(description="The coFlipper agent (Gemini + Flipper Zero)")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="use a simulated Flipper, without a physical device",
    )
    args = parser.parse_args()

    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY is not set. Put it in desktop/.env (see .env.example).")

    catalog = load_catalog()
    commands = model_commands(catalog)
    if not commands:
        sys.exit("No command available in commands.json.")

    if args.mock:
        from mock_flipper import MockCFPClient

        flipper = MockCFPClient()
        print("Simulated mode: no physical device is used.")
    else:
        flipper = build_client_for_device()

    from memory import MemoryStore

    memory = MemoryStore()
    dispatcher = CommandDispatcher(commands, flipper, memory=memory)
    # Imported here rather than at module level: subagents.py imports this module, so a
    # top-level import in both directions would be circular.
    from subagents import SubagentRunner

    dispatcher.subagents = SubagentRunner(api_key, dispatcher, dispatcher.simulated)
    # genai_client is not used directly, but the reference has to be kept for as long as
    # the conversation lasts (see build_chat).
    genai_client, chat = build_chat(  # noqa: F841
        api_key, commands, dispatcher.simulated, memory.as_prompt()
    )
    if memory.count:
        print(f"Persistent memory: {memory.count} fact(s) remembered from earlier sessions.")

    names = ", ".join(cmd["name"] for cmd in commands)
    print(f"Tools available to the model: {names}")
    print("Write a request in natural language. Ctrl+C to finish.\n")

    def log_step(step):
        """The reasoning chain, printed as it is built."""
        indent = "    " * step.depth
        if step.kind == THOUGHT:
            print(f"  {indent}[thought] {step.text}")
        elif step.kind == TOOL:
            print(f"  {indent}[flipper] {step.name} {step.arg_line()}".rstrip())
            print(f"  {indent}[flipper] -> {step.result_line()}")
            for url in step.visited:
                print(f"  {indent}[web] visited {url}")
        elif step.kind == SPAWN:
            print(f"  [subagent] {step.name} summoned: {step.meta.get('role', '')}")
            print(f"  [subagent] model {step.meta.get('model')}, "
                  f"tools: {', '.join(step.meta.get('tools') or ['none'])}")
        elif step.kind == REPORT:
            print(f"  {indent}[subagent] {step.name} reports to the agent "
                  f"({step.meta.get('commands', 0)} commands): {step.text}")
        elif step.kind == ANSWER:
            print(f"  [agent] answer phrased after {step.at_s:.1f} s")

    def on_summary(_summary, dropped):
        print(f"  [context] conversation compacted: {dropped} older entries summarised")

    try:
        while True:
            message = input("> ").strip()
            if not message:
                continue
            try:
                reply, _trace = run_turn(chat, dispatcher, message, log_step)
            except ModelOverloaded as exc:
                # A passing Google-side overload; keep the session alive so the user can just
                # ask again rather than losing the whole conversation.
                print(f"  [gemini] {exc}")
                continue
            print(reply)
            # After each turn, compact if the history has grown past the budget. The memory
            # is re-read so a fact remembered mid-session is folded into the rebuilt chat.
            chat, _compacted = maybe_compact(
                genai_client, chat, commands, dispatcher.simulated, memory.as_prompt(), on_summary
            )
    except KeyboardInterrupt:
        print("\nSession stopped.")
    finally:
        flipper.close()


if __name__ == "__main__":
    main()
