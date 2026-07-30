# desktop/ — the coFlipper agent

The component that runs on the computer: it interprets user requests with the help of the Gemini model and translates them into CFP commands sent to the Flipper Zero. The protocol is documented in [/PROTOCOL.md](../PROTOCOL.md), the command catalog in [/commands.json](../commands.json).

## Installation

    pip install -r requirements.txt

Then copy `.env.example` to `.env` and fill in the key obtained from [Google AI Studio](https://aistudio.google.com/apikey):

    GEMINI_API_KEY=your_key

The `.env` file is excluded from git and must not be published.

### Choosing the model

The model is set explicitly in `agent.py` and can be changed, without modifying the code, through the `COFLIPPER_MODEL` environment variable. We deliberately avoided aliases of the `gemini-flash-latest` kind: these always track the most recent generation, and usage limits differ substantially from one generation to the next.

Practical findings from during development, on the free plan:

- the limit is approximately **20 requests per day for each model** among the recent generations (verified on `gemini-3.6-flash` and `gemini-3.5-flash`). A single exchange of messages can consume two or three requests, since every round of tool calls needs an additional request, so the limit is reached quickly;
- the quota is counted separately for each model, so switching to another model through `COFLIPPER_MODEL` provides a fresh quota;
- the `gemini-2.0-flash` and `gemini-2.0-flash-lite` models are not available on the free plan (they respond with `limit: 0`), and `gemini-2.5-flash` and `gemini-2.5-flash-lite` are no longer accessible to new keys (404 error). `list_models.py` shows the models accessible to the configured key.

Consequence for development: work on the interface is done in `--mock` mode wherever possible, and requests to the model are reserved for the checks that actually need them. For a public demonstration it is worth checking the remaining quota ahead of time.

Changing the model for the current session, when one model's quota has been exhausted:

    set COFLIPPER_MODEL=gemini-3.5-flash-lite
    python gui.py

## Running

The graphical application, the usual way to use the project:

    python gui.py             # with a Flipper Zero connected over USB
    python gui.py --mock      # without a physical device, for development

The same functionality is available in the console as well, useful when debugging:

    python agent.py
    python agent.py --mock

In `--mock` mode, commands do not reach a real device: they are served by a simulated Flipper that responds like the firmware. The three commands the firmware implements — `ping`, `info` and `subghz.rssi` — succeed, and anything else returns `unknown_command`. The simulator also enforces the frequency bands the CC1101 transceiver can actually reach, so a frequency the hardware would refuse is refused here too. This mode is useful for working on the agent side when the device is not at hand.

Two properties of the simulated measurements are deliberate. The level for a given frequency is derived from the frequency itself, so it is the same in every run and two test runs can be compared. But successive readings of the *same* frequency move slightly, by a fixed sequence of small amounts, because a real signal level never repeats exactly. The second property was added after a real run made its absence obvious: the scanner subagent judges by how much the values vary, and against a simulator returning one constant its entire line of reasoning was vacuous — we would have shipped a subagent whose premise had never been exercised without hardware.

### Signalling simulated mode

Simulated mode raises an honesty problem that we only discovered while using the application: the simulator returns plausible responses, and the model, having no way to know they come from a simulator, would inform the user that the device is connected and working normally. The statement was false, even though no element of the code was, formally, wrong.

The solution has three components that complement one another:

- every tool result produced in simulated mode contains the `simulated` field, which the model receives directly;
- the system instruction receives an additional section that explicitly forbids stating that a device is connected, and requires signalling the provenance of the data in every response;
- the interface uses yellow instead of green for the connection status, shows a warning at startup, and marks every result with "(simulated result)".

Without the first measure, the restriction in the instruction would have remained a mere recommendation, one the model had no way to apply: nothing in the data it received indicated that it was inside a simulation.

## The reasoning chain

Between the user's request and the final answer lies a succession of decisions: which tool is worth calling, what the device responded, what conclusion can be drawn from that response, whether another measurement is needed. The application keeps this succession and displays it step by step, in the order in which it happened. A turn may contain several rounds of dialogue with the model, and every round contributes steps to the chain.

A typical chain, exactly as it appears in the right-hand panel:

    REQUEST: Check whether the device responds and measure the level on 433.92 MHz

    1. reasoning
       The request has two parts. I first check whether the device responds,
       because a measurement on a mute device would make no sense.

    2. ping
       (no arguments)
       OK pong

    3. reasoning
       It responds. I move on to the measurement, on the requested frequency.

    4. subghz_rssi
       frequency=433920000
       OK 433919809 -93.9

    5. reasoning
       The level is low, so nothing powerful is transmitting nearby.

    6. answer phrased (2.4 s)

The reasoning steps are not reconstructed by us from the executed commands: they are summaries of the model's own reasoning, produced by the model and requested explicitly through `thinking_config`. Models that do not offer such summaries remain perfectly usable — the chain then contains only the request, the executed commands and the answer. The request can be disabled with `COFLIPPER_THOUGHTS=0`.

The number of rounds is neither fixed nor predictable: for the same request, in one run the model called both measurements within the same round, and in another it split them across two successive rounds. The chain reflects what actually happened, not a predetermined template.

One limitation we could not eliminate entirely: the language of the reasoning summaries. The system instruction asks the model to reason in the language in which it received the request, and in practice the first summary usually obeys, but the later ones frequently revert to English. The summaries are not written directly by the model, but by an internal mechanism that condenses its reasoning, and that mechanism cannot be reached from the instruction. We preferred to leave the summaries as they arrive rather than translate them: a translation would consume additional requests from the daily quota and, more importantly, would interpose one more step between the model's real reasoning and what the user sees — precisely what the chain is trying to avoid.

Markdown markup was a related practical problem. By default the model answers with asterisks, backticks and headings, which the window displays literally, as pointless punctuation. The solution has two parts: the system instruction asks for plain text, and the display cleans up the remaining markup, since the model obeys the request only for the most part.

One consequence of this separation deserves mention: the reasoning summaries are the model's internal notes and have no place in the answer addressed to the user. For this reason the answer text is rebuilt from the fragments that are not marked as reasoning, instead of being taken directly from the response's `text` field, which would include them as well.

The reason the chain is displayed permanently, rather than hidden in a debugging log, is the same one behind the restriction against inventing data: an agent that phrases sentences in natural language sounds equally convincing whether it measured something or merely assumed it. The chain shows concretely what each statement rests on — and when a command fails, it is visible exactly what failed and at which moment. The final answer thus stops being a verdict and becomes the conclusion of a path the user can walk through and verify.

The same principle covers the one place the agent reaches the network. It does not browse the web — it drives a device — but the online IR search (`agent.ir_control` with `search_online`) fetches remote files from the IR-code database (probonopd/irdb, over the jsDelivr CDN). When it does, the chain lists the exact files it visited, as links, under the command that fetched them: the codes are shown to have come from a named source rather than presented as if they had always been known. Reaching the database is the whole of the agent's web access; there are no other sites to show.

## Subagents

Not every job fits comfortably inside the main conversation. Two cases came up often enough to be worth handling separately.

The first is long work. Measuring one frequency is a single command, but establishing whether anything is actually *transmitting* on it is not: a single reading cannot tell an empty frequency from a transmitter that happens to be silent at that instant. It takes several readings, spaced out, and a judgement over the whole set. Done inline, those readings pile up in the main conversation — every round carries the entire history to the model again, the context grows, and the user watches a window that appears to be doing nothing.

The second is research over material already gathered. Every command the session has executed on the device is recorded in a log, and questions about it ("what have we measured so far?", "which commands failed?") need no hardware access at all, only reading.

So the agent can delegate. A subagent is a separate conversation with the model: its own system instruction, its own tool list, its own round budget. It carries out one bounded task and reports back to the agent that summoned it. Two exist:

| Subagent | Role | Tools |
|---|---|---|
| scanner | measures the radio spectrum over several successive readings | `ping`, `info`, `subghz.rssi` |
| analyst | researches the session log | none |

The analyst having no tools at all is deliberate, not an omission. A subagent that can only read cannot disturb the radio, and it cannot be the source of a fabricated measurement either: everything it says has to be traceable to a line of the log it was given.

Delegation is described in `commands.json` like everything else, under `"layer": "agent"`, with a `subagent` field naming the specialist and a `task` field phrasing the job in words. The catalog therefore remains the single source of truth: adding a delegated capability means describing it there, not changing the agent's code.

### What the interface shows

Delegation must not pass unnoticed. If only the result appeared in the chain, the user would have no way of knowing that part of the answer was produced by a second model, with different tools and a different instruction. So the moment a subagent is summoned, the chain announces who it is — its role, the model it runs on, the tools it is permitted, its round budget — and the task it was given, including the arguments the main agent chose. Its own reasoning and commands then appear nested one level deeper, each carrying its author's name, and it ends by reporting back:

    2. subagent summoned: scanner
       role: measures the radio spectrum on the device, over several successive readings
       model: gemini-3.5-flash
       permitted tools: ping, info, subghz_rssi
       budget: at most 6 rounds
       task received:
          Establish whether there is real radio activity on the requested frequency...
          Parameters given by the main agent: frequency: 433920000, readings: 2

       scanner · reasoning
          First reading on the requested frequency.

       scanner · subghz_rssi
          frequency=433920000
          OK 433919809 -93.9

       scanner · reports to the main agent (2 readings, 3 rounds)
          Two identical readings of -93.9 dBm. Below -90 dBm means background noise.

Subagent steps are drawn in a second accent colour, so the change of author is visible without reading. Only the main agent's steps are numbered: numbering follows *its* decisions, and a subagent may take any number of steps inside a single one of them.

### The report, and why it carries the raw data

The report handed back is not prose alone. It travels together with the evidence behind it — every command the subagent ran, with its arguments and the device's answer — and the main agent is instructed that if the two disagree, the readings win. Without this, delegation would introduce exactly the weakness the project set out to avoid: a claim about the hardware that nothing in the data supports, this time laundered through a second model.

### Cost, and the round budget

Subagents consume requests from the daily quota exactly like the main agent, and a delegated turn can easily cost twice what an inline one would. Two mechanisms keep that bounded.

The first is a round budget per subagent. When it is spent, the subagent is not cut off but asked to report on the basis of what it already has, so the work is not wasted. Should it ignore that order and call another tool anyway, it is stopped there — testing this revealed a real defect in the first implementation, which kept re-issuing the request and would have burned the whole daily quota on a single stubborn subagent. If it never produces a conclusion, the report says so explicitly rather than arriving empty, which the main agent could otherwise mistake for "nothing found".

The second is the choice of model. The free-tier quota is counted separately per model, so pointing the subagents at a different one gives them their own allowance instead of competing with the conversation:

    set COFLIPPER_SUBAGENT_MODEL=gemini-3.5-flash-lite
    python gui.py

Subagents run one at a time, deliberately. Running them in parallel would buy little here — there is a single radio in the device, so measurements have to be serialised anyway — while multiplying the risk of hitting the per-minute rate limit. The device access they do share is guarded by a lock, since a single serial port serves the Flipper and two overlapping requests would desynchronise the protocol, matching responses to the wrong command.

## The graphical interface

The window is organised as a row of tabs above a shared device card. Each tab is split into two panels: on the left the conversation proper, on the right the reasoning chain described above. The top bar carries the project name and the active language model, the controls for the parallel chats, and — in the device card below it — the connection status (green for connected, red for error, yellow for simulated mode), the serial port in use and the available tools.

Each tab has its own status line under the panels: while a chat is working it shows whether the model is reasoning at that moment or executing a particular command, not merely the fact that it is busy. The shared device card, by contrast, shows the state of the one device — and, during an IR bruteforce, its progress — since that is common to every tab.

The reply is streamed, not shown whole at the end: the answer types itself out as the model produces it, the commands appear in the chain the moment they run, and the status line previews the reasoning as it streams. The turn is built round by round from `send_message_stream`, so a long answer no longer looks like a frozen window followed by a wall of text — it arrives the way it is written.

## Parallel chats and merge

A chat is not the whole window; it is one tab. The `+ chat nou` button opens another, and each tab is a fully independent conversation with the model: its own history, its own reasoning chain, its own Gemini chat session. They run at the same time, each on its own worker thread, so several lines of work advance in parallel while the user reads or types in any one of them. The top bar shows how many are working at once.

What they share is the device, because there is only one. Every tab reaches the Flipper through the same `CommandDispatcher`, whose device lock serialises access to the single serial port: two chats measuring at the same moment take turns on the wire rather than interleaving and desynchronising the protocol. Sharing the dispatcher also means they share one Marauder simulator and one session log — which is correct, since on real hardware they would be sharing one physical board and one device.

The parallel chats are a *map*; the `⧉ merge` button is the *reduce*, and it is subject-aware. It hands each answered chat — its request, its final answer, and the commands it actually ran — to a separate synthesiser conversation (`merge.py`), which first groups the chats by subject (a frequency, a network, a device, a capability) and only then merges within each group. Chats on the SAME subject, even from different angles, are combined into one consolidated conclusion that names where they agreed and diverged; a chat alone on its subject is kept independent, with its own result, rather than forced together with unrelated ones. So two chats studying 433.92 MHz merge into a single conclusion, while a third scanning Wi-Fi stays on its own.

The merged, subject-grouped result appears in its own read-only tab. The synthesiser obeys the same honesty rule as everything else: it may not invent a reading none of the chats took, and in simulated mode it says so. Merge is enabled only once at least two chats have answered and none is still working.

## Memory and context management

A conversation has two kinds of memory, and coFlipper handles them separately.

The short-term memory is the conversation's own history: the model sees every earlier turn of the current chat. That history cannot grow without bound, though — each turn resends the whole of it, so the cost per turn climbs and the model's context limit eventually looms. Past a soft budget of turns (`COFLIPPER_CONTEXT_TURNS`, 16 by default), the chat is *compacted*: the older turns are replaced by a model-written summary that keeps the real readings and decisions, the most recent turns are kept verbatim, and the conversation continues on a much shorter context. The compaction is shown, not hidden — a line marks it in the tab — because a summary that silently drops something the user said would be exactly the kind of invisible loss the reasoning chain exists to prevent.

The long-term memory is `memory.py`: a small set of durable facts the agent chose to keep, written to disk (`memory.json`) so they survive a restart and are loaded back into every new conversation. The model writes to it through the `agent_remember` tool — for things worth keeping across sessions, like the brand of a device the user owns, a preference, or a lasting finding — and reads from it automatically, since each session is built with the current memories folded into its system instruction. It is told not to store secrets or one-off values. The interface shows exactly what is remembered (the "🧠 memorie" button) and lets the user wipe it, because memory a user cannot inspect is memory they cannot trust. The memory is shared by every tab, since it belongs to the user, not to one conversation; a compaction re-reads it, so a fact remembered mid-session is folded into the rebuilt chat.

Together with the stopping conditions already in place — a turn ends when the model asks for no further command, and every subagent runs under a hard round budget — these give the agentic loop its memory, its context management and its halting.

## Bringing up the connection

Both the agent and the manual console go through `connect()` in `cfp_client.py`, which handles the two steps a CFP session needs:

1. Finding the serial port, by USB VID/PID rather than by description (on Windows the Flipper shows up as a nondescript "USB Serial Device").
2. Launching the coFlipper application on the device, if it is not already open.

The second step matters more than it looks: the `cfp` command exists only while our application is running, because it is that application which registers the command into the Flipper's CLI. With it closed, every request fails with `could not find command cfp`. To launch it, the client briefly speaks the Flipper's *native* CLI (`loader info` to see what is open, `loader open` to start our application), then hands the port over to the CFP session proper.

If the application is already open, it is detected and left alone. Use `--no-launch` to skip this step entirely and assume the application is running.

Two device-side details worth knowing: `loader open` needs the full `.fap` path, since it resolves plain names only for built-in applications; and `loader close` does not work on our application, which exits only on a Back event — the `cfp <id> exit` command is what closes it remotely.

### Manual console

    python cfp_client.py                      # interactive, auto-detected port
    python cfp_client.py --port COM12         # explicit port
    python cfp_client.py -c "ping" -c "info"  # run commands and exit
    python cfp_client.py --list-ports         # list serial ports and exit
    python cfp_client.py --no-launch          # assume the application is already open

It can also be used as a module, which is how `agent.py` obtains its client:

    from cfp_client import connect

    with connect() as flipper:
        print(flipper.request("ping"))

## Files

| File | Role |
|---|---|
| gui.py | the graphical application (Tkinter) |
| agent.py | the agent proper: the conversation loop and orchestration of tool calls |
| reasoning.py | the reasoning chain: the steps of a turn, in the order they happened |
| subagents.py | the subagents: specialised assistants the main agent delegates to |
| merge.py | the synthesiser that merges the results of several parallel chats into one |
| memory.py | the agent's persistent memory: durable facts kept across sessions on disk |
| commands.py | conversion of the commands.json catalog into Gemini tools, and dispatching of calls |
| ir_bruteforce.py | the IR control/bruteforce orchestration behind the agent.ir_control tool |
| ir_codes.py | the built-in table of infrared codes, by appliance type and brand |
| irdb.py | lookup in the online IRDB database of real remotes, used as a fallback |
| device.py | the device connection used by the interface, on a background thread |
| protocol.py | implementation of the CFP client over the serial port |
| cfp_client.py | connection setup (port detection + launching the application) and a console for sending CFP commands manually, without a language model |
| mock_flipper.py | a simulated Flipper, with the same interface as the real client |
| scripted_model.py | a scripted stand-in for the model, so the tests need no API requests |
| test_reasoning.py | checks the construction of the reasoning chain |
| test_subagents.py | checks delegation, the session log and the round budget |
| test_gemini.py | a minimal check of the connection to the Gemini API |
| list_models.py | lists the models available to the configured key |

## Verification status

The full loop model → tool → CFP command → response → final phrasing has been tested against the real Gemini API, both in simulated mode and on a physical Flipper Zero (Momentum firmware `mntm-012`, USB serial port).

Scenarios verified on the physical device:

- querying device state (`ping`, `info`), with two tools called within a single conversation turn;
- measuring the signal level on a frequency indicated by the user, with the value interpreted in natural language;
- comparing two frequencies, with the agent deciding on its own to perform two successive measurements and formulate a conclusion;
- requesting a physically impossible frequency (2.4 GHz), in which case the agent reported the error returned by the device and correctly explained the hardware limitation, without inventing a measurement.

The graphical interface was verified separately, with the simulated device: connecting, correct enabling of the controls, sending a request, displaying the executed commands and the final response, as well as handling errors without freezing the window. The parallel chats were checked the same way: opening and closing tabs, each tab holding its own independent conversation, all of them sharing the one dispatcher, and the merge becoming available only once at least two chats have answered. The subject-aware merge itself was run against the real Gemini API with three chats — two studying the same frequency and one scanning Wi-Fi — and it correctly merged the first two into a single conclusion while keeping the third independent, each finding kept tied to the chat that produced it.

Memory and context management were verified against the real Gemini API too. Persistent memory: the agent, told a durable fact in one chat, saved it through `agent_remember`, and a brand-new chat — a separate conversation — recalled it from the loaded memory. Context compaction: a chat driven past a lowered turn budget was compacted, its history shrinking to a summary plus the recent turns while it kept answering coherently, and the summary preserved the facts rather than dropping them.

Two test suites cover the parts that can be checked without hardware and without the API. Both run in under a second and can be repeated freely, since the model is replaced by a scripted set of responses:

    python test_reasoning.py     # 22 checks
    python test_subagents.py     # 38 checks

`test_reasoning.py` covers the order of the steps across several rounds, the fact that every step reaches the display immediately, the separation of reasoning summaries from the answer text, the marking of simulated results, a turn in which the command fails, the case of a model that produces no reasoning summaries, and the cleaning up of Markdown markup.

`test_subagents.py` covers a full delegation: what is announced when a subagent is summoned, the nesting and attribution of its steps, the fact that the analyst receives the session log and no tools, the report and the raw evidence handed back, the refusal of a subagent's attempt to summon another subagent, and the round budget — both when the subagent complies with the order to report and when it ignores it. Two further checks guard the edges: a session with no subagent runner attached must still execute device commands normally, and twelve commands issued from twelve threads at once must reach the device one at a time. That last one is not hypothetical — in a real run the scanner asked for five readings within a single round, and the model is free to do so.

The suites are worth more than the count of checks suggests: writing them found two real defects. The round budget originally kept re-issuing its request to a subagent that ignored it, which would have consumed the whole daily quota on a single stubborn subagent; and one test double initially let reasoning text leak into the response's `text` field, which sent us to read the SDK's own implementation and confirm that it skips reasoning parts — the behaviour our answer-rebuilding relies on.

That the real model does return reasoning summaries while also using tools was confirmed separately, against the Gemini API: asked to compare the signal level on two frequencies, the agent reasoned, performed the two measurements and formulated the conclusion, and the chain contained every step in order. This check cannot be replaced by the one with fixed responses, because the uncertainty lay precisely here: whether the model accepts the request for summaries at the same time as tool calling.

Delegation was confirmed the same way, and for the same reason: a scripted model cannot show whether a real one *chooses* to delegate. Asked whether there was real activity on 433.92 MHz — with the request stating explicitly that a single instantaneous reading was not what was wanted — the main agent summoned the scanner of its own accord rather than calling `subghz.rssi` itself. The scanner checked the device with `info`, took five readings, reported back that all five sat at -93.9 dBm with no variation, and the main agent phrased the conclusion from that report while stating that the data came from a simulator. The whole turn took 22 seconds, which is itself the argument for delegating work of this kind rather than running it inside the conversation.

The simulator reproduces `subghz.rssi` as well, with the same frequency bands the device's CC1101 transceiver accepts. Without that restriction, the agent would have been developed against a device more permissive than the real one, and a frequency rejected by the hardware would have gone unnoticed during development.
