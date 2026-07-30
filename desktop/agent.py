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

# The API occasionally returns 503 when overloaded. Without a retry, such a transient
# error would interrupt the conversation in progress.
SEND_RETRIES = 3
RETRY_DELAY_S = 2.0

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
   returns are 'unknown_command' (the firmware does not know that command), 'bad_frame',
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
7. An optional Wi-Fi dev board - an ESP32 flashed with Marauder firmware - may be attached
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
8. For infrared commands ('turn off the TV', 'turn the volume up', 'change the channel')
   you use the agent_ir_control tool. You pass it the user's request as they phrased it,
   plus the device brand if they mentioned it. For follow-up requests ('louder', 'next
   channel') you pass device_type explicitly, because the device is no longer named but
   you know it from the conversation. You do not invent IR codes and you do not call the
   individual ir.* commands yourself: the tool handles them on its own.
9. When you start an IR bruteforce, you tell the user to press the middle button (OK) on
   the Flipper the moment the device reacts - that is how the transmission is stopped. If
   the result has 'worked': false, it means the codes were exhausted without confirmation:
   you say so openly and suggest they retry specifying the brand, rather than claiming
   success.
10. If the user says it did not work ('still not working', 'nothing happened'), you call
    agent_ir_control again with search_online=true, so it searches the online database of
    real remotes (thousands of models), not only the internal table. You need the device
    brand: if the user did not give it, you ask for it first. You warn them it takes
    longer, because it downloads from the internet. After five failed attempts on the same
    device, the online search starts automatically.
11. In the answer you say where the codes came from: 'code_source': 'builtin' means the
    internal table, 'irdb' means the online database. If the result contains 'next_step',
    you use it to tell the user what comes next.
12. You have a persistent memory across sessions. When the user tells you something durable
    and worth keeping - a device they own and its brand, a preference, a lasting finding -
    you save it with the agent_remember tool, in one short sentence, so you still know it
    the next time they open the application. You do not store secrets, passwords or one-off
    values, and you do not announce that you are saving something unless asked. Whatever you
    already remember is listed below under PERSISTENT MEMORY, when there is anything.
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


def build_chat(api_key, commands, simulated=False, memory_prompt=""):
    """The conversation session, with the tools derived from the command catalog.

    Returns the client as well, not only the chat: the caller has to keep a reference to
    it, otherwise the garbage collector destroys it and closes the HTTP connection the
    conversation relies on.

    memory_prompt is the persistent memory (memory.py) folded into the system instruction,
    so every new conversation starts knowing the facts the agent chose to remember.
    """
    client = genai.Client(api_key=api_key)
    chat = client.chats.create(model=MODEL, config=_chat_config(commands, simulated, memory_prompt))
    return client, chat


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


def _summarize_history(client, contents):
    chat = client.chats.create(
        model=MODEL,
        config=types.GenerateContentConfig(system_instruction=SUMMARY_INSTRUCTION),
    )
    response = send_with_retry(chat, "Summarise the conversation so far:\n\n" + _history_text(contents))
    return answer_text(response)


def maybe_compact(client, chat, commands, simulated=False, memory_prompt="", on_summary=None):
    """Compacts an over-long chat. Returns (chat, compacted): the same chat when it is still
    within budget, or a fresh one seeded with a summary plus the recent turns when it is not.

    on_summary(summary, dropped_count) is called when a compaction happens, so the interface
    can show that the context was compressed instead of it happening invisibly.
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
    summary = _summarize_history(client, older)

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
        model=MODEL, config=_chat_config(commands, simulated, memory_prompt), history=seeded
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
        if attempt == SEND_RETRIES:
            raise exc
        print(f"  [gemini] service unavailable ({exc.code}), retrying...")
        time.sleep(RETRY_DELAY_S * attempt)
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
    for attempt in range(1, SEND_RETRIES + 1):
        try:
            return chat.send_message(message)
        except errors.APIError as exc:
            _retry_after_error(exc, attempt)


# A stream that yielded nothing (the request produced no content at all).
_STREAM_END = object()


def _open_stream_with_retry(chat, message):
    """Opens a streaming response and pulls its first chunk, retrying transient errors the
    same way send_with_retry does - they surface as the request is made and first read."""
    for attempt in range(1, SEND_RETRIES + 1):
        try:
            stream = chat.send_message_stream(message)
            first = next(stream, _STREAM_END)
            return stream, first
        except errors.APIError as exc:
            _retry_after_error(exc, attempt)


def _consume_stream(stream, first, on_thought_delta=None, on_answer_delta=None):
    """Reads a streamed round, emitting text as it arrives, and returns what it amounts to.

    Returns (thoughts, answer, calls): the reasoning summaries (each a full string), the
    answer text, and the tool calls requested. The deltas are forwarded live through the two
    callbacks, which is what gives the interface its typing feel; the return value is what the
    turn loop acts on, exactly as if the response had arrived whole.
    """
    thoughts, current, answer, calls = [], [], [], []

    def flush_thought():
        if current:
            thoughts.append("".join(current))
            current.clear()

    chunks = [] if first is _STREAM_END else itertools.chain([first], stream)
    for chunk in chunks:
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


def run_turn(chat, dispatcher, message, on_step=None, on_delta=None):
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

    Returns (answer, chain).
    """
    trace = Trace(message)

    def record(step):
        if on_step:
            on_step(step)
        return step

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
    pending = message
    reply = ""

    while True:
        trace.next_round()
        stream, first = _open_stream_with_retry(chat, pending)
        thoughts, answer, calls = _consume_stream(stream, first, thought_delta, answer_delta)

        for thought in thoughts:
            record(trace.add_thought(thought))

        if not calls:
            reply = answer.strip()
            break

        results = []
        for call in calls:
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
            reply, _trace = run_turn(chat, dispatcher, message, log_step)
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
