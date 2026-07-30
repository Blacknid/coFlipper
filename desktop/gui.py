"""The graphical interface of the coFlipper agent.

Several chats at once, in a row of tabs. Each tab is an independent conversation with the
model - its own history, its own reasoning chain, its own input - and they run in parallel
on their own worker threads. They all share ONE dispatcher, because there is only one
Flipper Zero: its serial port is a single resource, and the dispatcher's device lock
serialises the tabs' access to it so two parallel chats never interleave on the wire.

Inside a tab, the layout is the original one: the conversation on the left, the reasoning
chain on the right - the model's thoughts and the commands actually sent to the device, in
the order they happened. So the final answer does not show up as a verdict, but as the end
of a path the user was able to follow.

When several chats have answered, the Merge button hands their results to a separate
synthesiser (merge.py), which combines them into one consolidated result shown in its own
tab: the reduce step to the parallel chats' map.

The look follows the visual language of the official qFlipper application: a very dark
background, the Flipper orange as the only accent color, flat panels with a thin outline,
and a device card across the top. Subagents keep a second accent, cyan.

Running it:
    python gui.py           # with a Flipper connected over USB
    python gui.py --mock    # without a device, for development
"""

import argparse
import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from dotenv import load_dotenv

from agent import MODEL, build_chat, maybe_compact, run_turn
from commands import CommandDispatcher, load_catalog, model_commands
from memory import MemoryStore
from reasoning import ANSWER, REPORT, REQUEST, SPAWN, THOUGHT, TOOL, plain_text
from subagents import SubagentRunner

# The qFlipper palette: an almost black background, a single orange accent (#FF8200 is
# the orange from the Flipper Zero identity), everything else in shades of gray.
BG = "#141518"
BG_CARD = "#1D1E22"
BG_PANEL = "#1A1B1F"
BORDER = "#2C2E34"
FG = "#E8E9ED"
FG_DIM = "#8A8C94"
FG_FAINT = "#5C5E66"
ORANGE = "#FF8200"
ORANGE_DARK = "#D96E00"
OK_GREEN = "#5FBF7F"
ERR_RED = "#E06C6C"
WARN_YELLOW = "#E0B84C"
# A second accent, used exclusively for subagents. The orange stays with the main agent,
# so delegation stands out at a glance, without having to read anything.
CYAN = "#4FB8C4"
# A third accent, for the merge: the synthesis of several chats is neither the main agent
# nor a subagent, so it gets a colour of its own.
MERGE_PURPLE = "#B08CE0"
# For addresses the agent reached over the network (the online IR database): a link blue,
# so a visited source reads as a link would.
WEB_BLUE = "#6FA8DC"

# How often the presence of the device is checked. Checking means no more than reading
# the list of serial ports, so a short interval does not load the system.
DEVICE_POLL_S = 1.5

# The typewriter: how often streamed text is revealed, and how much each time. The model
# streams in coarse chunks (a sentence at a time) and often finishes long before the text
# has been read, so the chunks are buffered and revealed a few characters at a time - a
# deliberately slow, semi-live transition. The work itself runs at full speed on the worker
# thread; only the display lags behind, on purpose.
#
# The pace is steady (TYPE_MIN_CHARS per tick) up to a large backlog, so a whole answer that
# arrived at once still types out slowly rather than jumping to the end. Only past
# TYPE_BACKLOG_LIMIT does it reveal faster, and even then never all at once - a safety valve
# so a very long answer does not take minutes, not the normal path.
TYPE_INTERVAL_MS = 26
TYPE_MIN_CHARS = 1
TYPE_BACKLOG_LIMIT = 600
TYPE_CATCHUP_DIVISOR = 40

# The step number takes up the first line; the rest of the text lines up underneath it.
STEP_INDENT = "   "


def _indent(text, level=1):
    """Indents a text that may span several paragraphs to the given depth."""
    pad = STEP_INDENT * level
    return "\n".join(pad + line if line else "" for line in text.splitlines())


# The longest a collapsed reasoning header may be before it is cut with an ellipsis.
THOUGHT_TITLE_MAX = 72


def _thought_summary(text):
    """Splits a reasoning step into a one-line title and the body underneath it.

    The models return their reasoning already led by a short title line ('Listing the
    access points', 'Analyzing the deauth request'), so the collapsed header can say what
    the step is about rather than a generic label - the title is simply that first line.
    The body keeps the rest; when the step is a single line, the body repeats it in full so
    a title cut short by the ellipsis is still readable once expanded.
    """
    cleaned = plain_text(text).strip()
    if not cleaned:
        return "raționament", ""
    lines = cleaned.splitlines()
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    first = lines[index].strip() if index < len(lines) else "raționament"
    title = first if len(first) <= THOUGHT_TITLE_MAX else first[: THOUGHT_TITLE_MAX - 1].rstrip() + "…"
    rest = "\n".join(lines[index + 1 :]).strip()
    return title, (rest or cleaned)


class ChatSession:
    """One tab: a whole independent conversation with the model.

    It owns everything that is per-conversation - the Gemini chat, the transcript, the
    reasoning chain, the input, the busy flag and the step numbering - and reaches the
    device through the window's shared dispatcher. Several of these run at once, each on
    its own worker thread; the window shell holds what they share.
    """

    def __init__(self, window, name):
        self.window = window
        self.name = name
        self.busy = False
        # Step numbering restarts with every request: the chain is read per current turn.
        self.step_no = 0
        self.chain_used = False
        # Each reasoning step is shown collapsed behind a clickable header (like Claude's
        # thinking): a running id gives every collapsible its own pair of tags, and the map
        # tracks whether each is currently collapsed.
        self._collapse_id = 0
        self._collapsibles = {}
        # Streaming state: whether the answer block has been opened (so the streamed,
        # possibly-raw text can be replaced by the cleaned version at the end), and the
        # reasoning step currently typing itself out live in the chain, if any.
        self._answer_started = False
        self._live_thought = None
        # The typewriter buffers: streamed text waiting to be revealed a little at a time.
        # 'complete' means every fragment has arrived and 'data' finalises the channel once
        # the buffer drains (the cleaned reply for 'answer', the THOUGHT step for 'thought').
        self._typers = {
            "answer": {"buf": "", "complete": False, "data": None},
            "thought": {"buf": "", "complete": False, "data": None},
        }
        # The last answer and the commands behind it, kept for the merge.
        self.last_answer = None
        self.last_commands = []
        self._commands = []
        # Every request the user sent this chat. The subject-aware merge needs them to tell
        # what the chat is ABOUT, so it can group chats on the same subject and leave the
        # rest independent.
        self.requests = []

        # A fresh, independent conversation, seeded with the persistent memory. build_chat
        # also returns the client, which has to be kept alive for as long as the chat is
        # used (see build_chat).
        self.genai_client, self.chat = build_chat(
            window.api_key, window.commands, window.dispatcher.simulated, window.memory_prompt()
        )

        self.frame = tk.Frame(window.notebook, bg=BG)
        self._build()

    # ------------------------------------------------------------------ layout

    def _build(self):
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=1)

        panels = tk.Frame(self.frame, bg=BG, padx=16)
        panels.grid(row=0, column=0, sticky="nsew", pady=(14, 0))
        panels.rowconfigure(1, weight=1)
        panels.columnconfigure(0, weight=3)
        panels.columnconfigure(1, weight=2)

        self.window._panel_label(panels, "CONVERSAȚIE", column=0)
        self.window._panel_label(panels, "LANȚ DE RAȚIONAMENT", column=1, padx=(14, 0))

        self.transcript = self.window._make_text(panels, self.window.font_body, "word", column=0)
        self.window._configure_transcript_tags(self.transcript)

        self.chain = self.window._make_text(
            panels, self.window.font_mono, "word", column=1, padx=(14, 0)
        )
        self.window._configure_chain_tags(self.chain)

        # A per-tab status line: what THIS chat is doing right now. The device card up top
        # is shared, so it cannot show each tab's state; this can.
        self.status_label = tk.Label(
            self.frame, text="", bg=BG, fg=FG_DIM, font=self.window.font_small, anchor="w"
        )
        self.status_label.grid(row=1, column=0, sticky="ew", padx=18, pady=(6, 0))

        self._build_input()

    def _build_input(self):
        bar = tk.Frame(self.frame, bg=BG, padx=16, pady=14)
        bar.grid(row=2, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)

        wrapper = tk.Frame(bar, bg=BG_PANEL, highlightthickness=1, highlightbackground=BORDER)
        wrapper.grid(row=0, column=0, sticky="ew", padx=(0, 12))

        self.entry = tk.Entry(
            wrapper,
            bg=BG_PANEL,
            fg=FG,
            font=self.window.font_body,
            relief="flat",
            insertbackground=ORANGE,
            selectbackground=ORANGE_DARK,
            highlightthickness=0,
            disabledbackground=BG_PANEL,
            disabledforeground=FG_FAINT,
        )
        self.entry.pack(fill="x", padx=14, pady=11)
        self.entry.bind("<Return>", lambda _event: self.on_send())

        self.send_button = tk.Button(
            bar,
            text="TRIMITE",
            command=self.on_send,
            bg=ORANGE,
            fg="#141518",
            font=self.window.font_bold,
            relief="flat",
            padx=26,
            pady=10,
            activebackground=ORANGE_DARK,
            activeforeground="#141518",
            cursor="hand2",
            disabledforeground="#6A6A72",
            borderwidth=0,
            highlightthickness=0,
        )
        self.send_button.grid(row=0, column=1)
        self.send_button.bind("<Enter>", lambda _e: self._hover_send(True))
        self.send_button.bind("<Leave>", lambda _e: self._hover_send(False))

        self.set_input_enabled(True)

    def _hover_send(self, hovering):
        if str(self.send_button["state"]) == "disabled":
            return
        self.send_button.configure(bg=ORANGE_DARK if hovering else ORANGE)

    def set_input_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        self.entry.configure(state=state)
        self.send_button.configure(state=state, bg=ORANGE if enabled else BG_CARD)
        if enabled:
            self.entry.focus_set()

    def set_local_status(self, text, color=FG_DIM):
        self.status_label.configure(text=text, fg=color)

    def focus(self):
        self.entry.focus_set()

    # --------------------------------------------------------------- rendering

    def _append(self, widget, text, tag=None):
        widget.configure(state="normal")
        widget.insert("end", text, tag)
        widget.configure(state="disabled")
        widget.see("end")

    def handle_event(self, kind, payload):
        """A session-scoped event, arriving on the main thread from the drain loop."""
        if kind == "user":
            self._append(self.transcript, "Tu\n", "speaker")
            self._append(self.transcript, payload + "\n\n", "agent")
        elif kind == "thinking":
            self.set_local_status(payload, ORANGE)
        elif kind == "thought_delta":
            # Buffered, not shown at once: the typewriter reveals it smoothly in the chain.
            self._typers["thought"]["buf"] += payload
        elif kind == "answer_delta":
            self._typers["answer"]["buf"] += payload
        elif kind == "agent":
            # The reply is complete; the typewriter finishes revealing it, then finalises
            # (cleans the markup and closes the turn). It is not shown whole here.
            self._typers["answer"]["complete"] = True
            self._typers["answer"]["data"] = payload
        elif kind == "error":
            self._reset_typers()
            self._append(self.transcript, payload + "\n\n", "error")
            self._finish()
        elif kind == "compacted":
            self._append(
                self.transcript,
                f"· context compactat: {payload} mesaje mai vechi au fost rezumate ·\n\n",
                "system",
            )
        elif kind == "step":
            self.render_step(payload)

    def _finalize_answer(self, reply):
        """Settles the streamed reply into its final, cleaned form.

        The stream shows raw text as it arrives (the typing effect); once complete, the
        markup the model may have slipped in is stripped by replacing the streamed body with
        plain_text. When nothing streamed - a model that does not stream, or an empty reply -
        the answer is simply written out."""
        clean = plain_text(reply)
        if self._answer_started:
            self.transcript.configure(state="normal")
            self.transcript.delete("astart", "end-1c")
            self.transcript.insert("astart", clean + "\n\n", "agent")
            self.transcript.configure(state="disabled")
            self.transcript.see("end")
            self._answer_started = False
        else:
            self._append(self.transcript, "Agent\n", "speaker")
            self._append(self.transcript, clean + "\n\n", "agent")

    def _finish(self):
        self.busy = False
        self.set_local_status("gata", FG_DIM)
        self.set_input_enabled(True)
        self.window._on_session_finished(self)

    def render_step(self, step):
        """One step of the reasoning chain, appended to this tab's chain panel."""
        if step.kind == REQUEST:
            self.step_no = 0
            if self.chain_used:
                self._append(self.chain, "\n")
            self.chain_used = True
            self._append(self.chain, f"CERERE: {step.text}\n\n", "head")
            return

        # The main agent's reasoning streams and types itself out; this step only marks it
        # complete, so the typewriter folds it away once it has finished typing. A thought
        # that produced no streamed text, and subagent thoughts (depth > 0), fall through to
        # the immediate path below.
        if step.kind == THOUGHT and step.depth == 0 and (
            self._live_thought is not None or self._typers["thought"]["buf"]
        ):
            self._typers["thought"]["complete"] = True
            self._typers["thought"]["data"] = step
            self.set_local_status("raționează...", ORANGE)
            return

        pad = STEP_INDENT * step.depth
        if step.depth:
            # Delegated steps are not numbered: the numbering follows the main agent's
            # decisions, and a subagent may take any number of steps inside one of those.
            self._append(self.chain, f"{pad}{step.source or 'subagent'} · ", "agentname")
        else:
            self.step_no += 1
            self._append(self.chain, f"{self.step_no}. ", "num")

        if step.kind == THOUGHT:
            self._add_collapsible_thought(step.text, step.depth)
            self.set_local_status("raționează...", ORANGE)
        elif step.kind == TOOL:
            self._append(self.chain, f"{step.name}\n", "call")
            self._append(
                self.chain, f"{pad}{STEP_INDENT}{step.arg_line() or '(fara argumente)'}\n", "dim"
            )
            self._append(
                self.chain, f"{pad}{STEP_INDENT}{step.result_line()}\n", "ok" if step.ok else "err"
            )
            if step.simulated:
                self._append(self.chain, f"{pad}{STEP_INDENT}(rezultat simulat)\n", "warn")
            if step.visited:
                self._append(self.chain, f"{pad}{STEP_INDENT}a vizitat baza IRDB:\n", "label")
                for url in step.visited:
                    self._append(self.chain, f"{pad}{STEP_INDENT}↗ {url}\n", "visit")
            self._append(self.chain, "\n")
            self.set_local_status(f"execută {step.name}...", ORANGE)
        elif step.kind == SPAWN:
            self._on_spawn(step)
        elif step.kind == REPORT:
            self._on_report(step)
        elif step.kind == ANSWER:
            self._append(self.chain, f"răspuns formulat ({step.at_s:.1f} s)\n", "label")

    # -------------------------------------------------------------- typewriter

    def _reset_typers(self):
        for channel in self._typers.values():
            channel["buf"] = ""
            channel["complete"] = False
            channel["data"] = None

    def type_step(self):
        """One tick of the typewriter: reveal a little more of each streaming channel.

        Called on a timer from the window. The answer types into the transcript, the
        reasoning into the chain; each finalises itself once its buffer has drained and its
        end has been signalled, so nothing is folded away before it has finished typing."""
        self._advance("answer", self._emit_answer_text, self._finalize_answer_typed)
        self._advance("thought", self._emit_thought_text, self._finalize_thought_typed)

    def _advance(self, channel, show, finalize):
        state = self._typers[channel]
        buf = state["buf"]
        if buf:
            # Steady slow pace until the backlog is large, then a bounded catch-up, so the
            # animation lags the finished work smoothly instead of ever snapping to the end.
            count = TYPE_MIN_CHARS
            if len(buf) > TYPE_BACKLOG_LIMIT:
                count = max(TYPE_MIN_CHARS, len(buf) // TYPE_CATCHUP_DIVISOR)
            show(buf[:count])
            state["buf"] = buf[count:]
        elif state["complete"]:
            state["complete"] = False
            data = state["data"]
            state["data"] = None
            finalize(data)

    def _emit_answer_text(self, chunk):
        """Reveals a fragment of the reply in the transcript, opening the block on the first."""
        if not self._answer_started:
            self._append(self.transcript, "Agent\n", "speaker")
            self.transcript.configure(state="normal")
            self.transcript.mark_set("astart", "end-1c")
            self.transcript.mark_gravity("astart", "left")
            self.transcript.configure(state="disabled")
            self._answer_started = True
        self._append(self.transcript, chunk, "agent")

    def _finalize_answer_typed(self, reply):
        self._finalize_answer(reply or "")
        self._finish()

    def _emit_thought_text(self, chunk):
        """Reveals a fragment of the reasoning in the chain, opening the live step on the first."""
        if self._live_thought is None:
            self._open_live_thought()
        self._append(self.chain, chunk, self._live_thought["btag"])
        self.set_local_status("raționează...", ORANGE)

    def _finalize_thought_typed(self, step):
        if self._live_thought is not None:
            self._finalize_live_thought(step)
        else:
            # The thought carried no streamed text, so nothing opened; show it collapsed.
            self._add_collapsible_thought(step.text, step.depth)

    def _open_live_thought(self):
        """Opens a reasoning step in the expanded, still-typing state: its number, a header
        with a placeholder title, and an empty body that the streamed text fills."""
        self._collapse_id += 1
        htag = f"thead{self._collapse_id}"
        btag = f"tbody{self._collapse_id}"
        self.chain.configure(state="normal")
        self.step_no += 1
        self.chain.insert("end", f"{self.step_no}. ", "num")
        self.chain.insert("end", "▾ raționează…\n", (htag, "thought_header"))
        self.chain.tag_configure(btag, elide=False)  # visible while it types
        self.chain.configure(state="disabled")
        self._collapsibles[btag] = {"htag": htag, "collapsed": False}
        self.chain.tag_bind(htag, "<Button-1>", lambda _e, b=btag: self._toggle_thought(b))
        self.chain.tag_bind(htag, "<Enter>", lambda _e: self.chain.configure(cursor="hand2"))
        self.chain.tag_bind(htag, "<Leave>", lambda _e: self.chain.configure(cursor=""))
        self._live_thought = {"htag": htag, "btag": btag}

    def _finalize_live_thought(self, step):
        """Settles a live-typed reasoning step: titles its header from the step, replaces the
        raw streamed body with the cleaned text, and folds it away (collapsed by default)."""
        info = self._live_thought
        self._live_thought = None
        htag, btag = info["htag"], info["btag"]
        title, body = _thought_summary(step.text)

        self.chain.configure(state="normal")
        header = self.chain.tag_ranges(htag)
        if header:
            self.chain.delete(header[0], header[1])
            self.chain.insert(header[0], f"▸ {title}\n", (htag, "thought_header"))
        body_range = self.chain.tag_ranges(btag)
        if body_range:
            self.chain.delete(body_range[0], body_range[1])
            self.chain.insert(body_range[0], _indent(body, step.depth + 1) + "\n\n", btag)
        else:
            self.chain.insert("end", _indent(body, step.depth + 1) + "\n\n", btag)
        self.chain.tag_configure(btag, elide=True)
        self.chain.configure(state="disabled")
        self._collapsibles[btag]["collapsed"] = True

    def _add_collapsible_thought(self, text, depth):
        """A reasoning step shown like Claude's thinking: a gray, clickable header with the
        detail hidden underneath, revealed on click.

        Collapsed by default, so the chain reads as a short outline and the full reasoning is
        one click away. The body is an elided text range (Tkinter hides an 'elide' tag), and
        clicking the header toggles that tag and flips the arrow. The commands stay visible;
        only the prose reasoning is folded away.
        """
        title, body = _thought_summary(text)
        self._collapse_id += 1
        htag = f"thead{self._collapse_id}"
        btag = f"tbody{self._collapse_id}"

        self.chain.configure(state="normal")
        self.chain.insert("end", f"▸ {title}\n", (htag, "thought_header"))
        self.chain.insert("end", _indent(body, depth + 1) + "\n\n", (btag, "thought"))
        self.chain.tag_configure(btag, elide=True)
        self.chain.configure(state="disabled")

        self._collapsibles[btag] = {"htag": htag, "collapsed": True}
        # The click lands anywhere on the header; the cursor becomes a hand over it, so it
        # reads as something to press.
        self.chain.tag_bind(htag, "<Button-1>", lambda _e, b=btag: self._toggle_thought(b))
        self.chain.tag_bind(htag, "<Enter>", lambda _e: self.chain.configure(cursor="hand2"))
        self.chain.tag_bind(htag, "<Leave>", lambda _e: self.chain.configure(cursor=""))

    def _toggle_thought(self, btag):
        """Shows or hides one reasoning step, flipping its header arrow to match."""
        info = self._collapsibles[btag]
        info["collapsed"] = not info["collapsed"]
        self.chain.tag_configure(btag, elide=info["collapsed"])
        ranges = self.chain.tag_ranges(info["htag"])
        if ranges:
            start = ranges[0]
            self.chain.configure(state="normal")
            self.chain.delete(start, f"{start}+1c")
            self.chain.insert(
                start, "▸" if info["collapsed"] else "▾", (info["htag"], "thought_header")
            )
            self.chain.configure(state="disabled")

    def _on_spawn(self, step):
        """Announces the summoning of a subagent: who it is, what it can do, what was asked."""
        meta = step.meta
        tools = ", ".join(meta.get("tools") or []) or "niciuna (fara acces la dispozitiv)"
        self._append(self.chain, f"subagent convocat: {step.name}\n", "spawn")
        self._append(self.chain, f"{STEP_INDENT}rol: {meta.get('role', '—')}\n", "dim")
        self._append(self.chain, f"{STEP_INDENT}model: {meta.get('model', '—')}\n", "dim")
        self._append(self.chain, f"{STEP_INDENT}unelte permise: {tools}\n", "dim")
        self._append(
            self.chain, f"{STEP_INDENT}buget: maximum {meta.get('max_rounds', '—')} runde\n", "dim"
        )
        self._append(self.chain, f"{STEP_INDENT}sarcina primită:\n", "dim")
        self._append(self.chain, _indent(step.text, 2) + "\n\n", "task")
        self.set_local_status(f"{step.name} lucrează...", ORANGE)

    def _on_report(self, step):
        """The report with which the subagent comes back to the main agent."""
        meta = step.meta
        detail = f"{meta.get('commands', 0)} comenzi, {meta.get('rounds', 0)} runde"
        if meta.get("truncated"):
            detail += ", buget epuizat"
        self._append(self.chain, f"raportează agentului principal ({detail})\n", "report")
        self._append(self.chain, _indent(step.text, step.depth + 1) + "\n\n", "thought")
        self.set_local_status("agentul principal preia raportul...", ORANGE)

    # ------------------------------------------------------------------ worker

    def on_send(self):
        if self.busy:
            return
        message = self.entry.get().strip()
        if not message:
            return
        self.entry.delete(0, "end")
        self.busy = True
        self._commands = []
        self._answer_started = False
        self._live_thought = None
        self._reset_typers()
        self.requests.append(message)
        self.set_input_enabled(False)
        self.window._emit("user", message, session=self)
        self.window._emit("thinking", "lucrează...", session=self)
        self.window._on_session_started(self)
        threading.Thread(target=self._worker, args=(message,), daemon=True).start()

    def _worker(self, message):
        def on_step(step):
            # The command names this chat ran, collected for the merge. Only the main
            # agent's own device calls (depth 0), not the subagents' internal ones.
            if step.kind == TOOL and step.depth == 0:
                self._commands.append(step.name)
            self.window._emit("step", step, session=self)

        def on_delta(channel, text):
            # Streamed text fragments: 'answer' types out the reply live, 'thought' feeds the
            # rolling thinking preview. Both cross to the main thread through the event queue.
            self.window._emit(channel + "_delta", text, session=self)

        try:
            reply, _trace = run_turn(self.chat, self.window.dispatcher, message, on_step, on_delta)
            self.last_answer = reply or "(raspuns gol)"
            self.last_commands = list(self._commands)
            self.window._emit("agent", reply or "(raspuns gol)", session=self)
            # Context management: after the turn, compact this chat if its history has grown
            # past the budget. The memory is re-read, so a fact remembered during the turn is
            # folded into the rebuilt chat.
            self.chat, compacted = maybe_compact(
                self.genai_client,
                self.chat,
                self.window.commands,
                self.window.dispatcher.simulated,
                self.window.memory_prompt(),
                on_summary=lambda _s, dropped: self.window._emit("compacted", dropped, session=self),
            )
        except SystemExit as exc:
            # send_with_retry exits via sys.exit when the model is unavailable on the plan.
            self.window._emit("error", str(exc), session=self)
        except Exception as exc:
            self.window._emit("error", f"{type(exc).__name__}: {exc}", session=self)


class CoFlipperWindow:
    def __init__(self, root, mock=False):
        self.root = root
        self.mock = mock
        self.events = queue.Queue()
        self.dispatcher = None
        self.flipper = None
        self.api_key = None
        self.commands = None
        self.memory = MemoryStore()
        self.closing = False
        self.sessions = []
        self._session_counter = 0
        # A merge is a model request, so it is never spent twice on the same state: the
        # button stays disabled after a merge until at least one chat answers again, and
        # while a merge is in flight. _merge_signature() captures what was last merged.
        self._merging = False
        self._last_merged_signature = None
        self._pending_signature = None
        # The device-card status baseline, kept up to date by the monitor thread.
        self.ready_status = "se conecteaza..."
        self.ready_color = FG_DIM

        root.title("coFlipper")
        root.minsize(860, 520)
        root.configure(bg=BG)

        self._build_fonts()
        self._build_styles()
        self._build_layout()
        # The geometry is fixed after the widgets are built: otherwise the window resizes
        # itself to fit the content and ends up running past the edge of the screen.
        self._center_window(1080, 700)

        root.after(100, self._drain_events)
        root.after(TYPE_INTERVAL_MS, self._type_tick)
        # Connecting opens the serial port and reads the catalog, so it happens on a
        # separate thread: otherwise the window would look frozen until it finished.
        threading.Thread(target=self._connect, daemon=True).start()

    # ---------------------------------------------------------------- geometry

    def _center_window(self, width, height):
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(width, screen_w - 80)
        height = min(height, screen_h - 140)
        x = (screen_w - width) // 2
        y = (screen_h - height) // 2
        self.root.geometry(f"{width}x{height}+{x}+{max(y, 20)}")

    # ------------------------------------------------------------------ styles

    def _build_fonts(self):
        self.font_ui = tkfont.Font(family="Segoe UI", size=10)
        self.font_small = tkfont.Font(family="Segoe UI", size=9)
        self.font_body = tkfont.Font(family="Segoe UI", size=11)
        self.font_bold = tkfont.Font(family="Segoe UI", size=11, weight="bold")
        self.font_device = tkfont.Font(family="Segoe UI Semibold", size=15)
        self.font_brand = tkfont.Font(family="Segoe UI", size=13, weight="bold")
        self.font_mono = tkfont.Font(family="Cascadia Code", size=9)
        self.font_label = tkfont.Font(family="Segoe UI", size=8, weight="bold")
        self.font_thought = tkfont.Font(family="Segoe UI", size=9, slant="italic")
        self.font_step = tkfont.Font(family="Segoe UI", size=8, weight="bold")

    def _build_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "coFlipper.Vertical.TScrollbar",
            background=BORDER,
            troughcolor=BG_PANEL,
            bordercolor=BG_PANEL,
            arrowcolor=FG_DIM,
            darkcolor=BORDER,
            lightcolor=BORDER,
            relief="flat",
            width=10,
        )
        style.map(
            "coFlipper.Vertical.TScrollbar",
            background=[("active", FG_FAINT), ("pressed", ORANGE)],
        )
        # The tab strip, restyled for the dark theme: the system default is a light,
        # raised ribbon that clashes with everything else.
        style.configure("coFlipper.TNotebook", background=BG, borderwidth=0, tabmargins=(0, 4, 0, 0))
        style.configure(
            "coFlipper.TNotebook.Tab",
            background=BG_CARD,
            foreground=FG_DIM,
            padding=(16, 7),
            borderwidth=0,
            font=self.font_ui,
        )
        style.map(
            "coFlipper.TNotebook.Tab",
            background=[("selected", BG_PANEL)],
            foreground=[("selected", ORANGE)],
        )

    def _build_layout(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        self._build_top_bar()
        self._build_device_card()
        self._build_notebook()

    def _build_top_bar(self):
        bar = tk.Frame(self.root, bg=BG, padx=20, pady=14)
        bar.grid(row=0, column=0, sticky="ew")

        tk.Label(bar, text="co", bg=BG, fg=FG, font=self.font_brand).pack(side="left")
        tk.Label(bar, text="Flipper", bg=BG, fg=ORANGE, font=self.font_brand).pack(side="left")

        tk.Label(bar, text=MODEL, bg=BG, fg=FG_FAINT, font=self.font_small).pack(side="right")

        # The controls for the parallel chats live here, to the right of the brand.
        controls = tk.Frame(bar, bg=BG)
        controls.pack(side="left", padx=(24, 0))

        self.new_button = self._toolbar_button(controls, "+ chat nou", self._add_session_clicked)
        self.new_button.pack(side="left", padx=(0, 8))
        self.close_button = self._toolbar_button(controls, "✕ închide", self._close_active_session)
        self.close_button.pack(side="left", padx=(0, 8))
        self.merge_button = self._toolbar_button(
            controls, "⧉ merge", self._on_merge, accent=MERGE_PURPLE
        )
        self.merge_button.pack(side="left")
        self.memory_button = self._toolbar_button(
            controls, "🧠 memorie", self._show_memory_dialog, accent=OK_GREEN
        )
        self.memory_button.pack(side="left", padx=(8, 0))

        self.activity_label = tk.Label(bar, text="", bg=BG, fg=FG_FAINT, font=self.font_small)
        self.activity_label.pack(side="right", padx=(0, 18))

        self._set_controls_enabled(False)

    def _toolbar_button(self, parent, text, command, accent=None):
        fg = accent or FG_DIM
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=BG_CARD,
            fg=fg,
            font=self.font_small,
            relief="flat",
            padx=12,
            pady=6,
            activebackground=BORDER,
            activeforeground=FG,
            cursor="hand2",
            disabledforeground=FG_FAINT,
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=BORDER,
        )
        return button

    def _set_controls_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        for button in (self.new_button, self.close_button, self.merge_button, self.memory_button):
            button.configure(state=state)

    def memory_prompt(self):
        return self.memory.as_prompt() if self.memory else ""

    def _refresh_memory_label(self):
        self.memory_button.configure(text=f"🧠 memorie ({self.memory.count})")

    def _show_memory_dialog(self):
        """A small window listing the persistent memories, with the option to clear them.

        Memory a user cannot inspect is memory they cannot trust, so it is shown in full and
        can be wiped - the same transparency the reasoning chain gives the rest of the agent.
        """
        dialog = tk.Toplevel(self.root, bg=BG)
        dialog.title("Memorie persistentă")
        dialog.configure(padx=18, pady=16)
        dialog.transient(self.root)
        tk.Label(
            dialog, text="MEMORIE PERSISTENTĂ", bg=BG, fg=FG_FAINT, font=self.font_label
        ).pack(anchor="w", pady=(0, 8))

        items = self.memory.all()
        if not items:
            tk.Label(
                dialog,
                text="Nimic memorat încă.\nAgentul salvează singur ce merită ținut minte "
                "între sesiuni.",
                bg=BG,
                fg=FG_DIM,
                font=self.font_ui,
                justify="left",
            ).pack(anchor="w")
        else:
            box = tk.Frame(dialog, bg=BG_PANEL, highlightthickness=1, highlightbackground=BORDER)
            box.pack(fill="both", expand=True)
            for item in items:
                row = tk.Label(
                    box,
                    text=f"• {item['text']}   ({item.get('at', '')})",
                    bg=BG_PANEL,
                    fg=FG,
                    font=self.font_ui,
                    justify="left",
                    wraplength=440,
                    anchor="w",
                )
                row.pack(fill="x", padx=12, pady=4)

        buttons = tk.Frame(dialog, bg=BG)
        buttons.pack(fill="x", pady=(12, 0))

        def clear_all():
            self.memory.clear()
            self._refresh_memory_label()
            dialog.destroy()

        if items:
            tk.Button(
                buttons,
                text="Șterge tot",
                command=clear_all,
                bg=BG_CARD,
                fg=ERR_RED,
                font=self.font_small,
                relief="flat",
                padx=12,
                pady=6,
                activebackground=BORDER,
                activeforeground=FG,
                cursor="hand2",
                borderwidth=0,
                highlightthickness=1,
                highlightbackground=BORDER,
            ).pack(side="left")
        tk.Button(
            buttons,
            text="Închide",
            command=dialog.destroy,
            bg=BG_CARD,
            fg=FG_DIM,
            font=self.font_small,
            relief="flat",
            padx=12,
            pady=6,
            activebackground=BORDER,
            activeforeground=FG,
            cursor="hand2",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
        ).pack(side="right")

    def _build_device_card(self):
        card = tk.Frame(
            self.root, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER, padx=18, pady=14
        )
        card.grid(row=1, column=0, sticky="ew", padx=20)
        card.columnconfigure(1, weight=1)

        left = tk.Frame(card, bg=BG_CARD)
        left.grid(row=0, column=0, sticky="w")

        tk.Label(left, text="Flipper Zero", bg=BG_CARD, fg=FG, font=self.font_device).pack(anchor="w")

        state_row = tk.Frame(left, bg=BG_CARD)
        state_row.pack(anchor="w", pady=(4, 0))
        self.status_dot = tk.Label(state_row, text="●", bg=BG_CARD, fg=FG_FAINT, font=self.font_ui)
        self.status_dot.pack(side="left", padx=(0, 6))
        self.status_label = tk.Label(
            state_row, text="se conecteaza...", bg=BG_CARD, fg=FG_DIM, font=self.font_ui
        )
        self.status_label.pack(side="left")

        right = tk.Frame(card, bg=BG_CARD)
        right.grid(row=0, column=1, sticky="e")
        tk.Label(
            right, text="UNELTE DISPONIBILE", bg=BG_CARD, fg=FG_FAINT, font=self.font_label
        ).pack(anchor="e")
        self.tools_label = tk.Label(
            right, text="—", bg=BG_CARD, fg=FG_DIM, font=self.font_small, justify="right"
        )
        self.tools_label.pack(anchor="e", pady=(3, 0))

    def _build_notebook(self):
        holder = tk.Frame(self.root, bg=BG, padx=20)
        holder.grid(row=2, column=0, sticky="nsew", pady=(16, 0))
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)

        self.notebook = ttk.Notebook(holder, style="coFlipper.TNotebook")
        self.notebook.grid(row=0, column=0, sticky="nsew")

        # The merge tab: always the last one, read-only, populated when the user merges.
        self.merge_frame = tk.Frame(self.notebook, bg=BG)
        self._build_merge_tab()
        self.notebook.add(self.merge_frame, text="  ⧉ Merge  ")

    def _build_merge_tab(self):
        self.merge_frame.columnconfigure(0, weight=1)
        self.merge_frame.rowconfigure(1, weight=1)

        header = tk.Frame(self.merge_frame, bg=BG, padx=16)
        header.grid(row=0, column=0, sticky="ew", pady=(14, 0))
        tk.Label(
            header, text="REZULTAT COMBINAT", bg=BG, fg=FG_FAINT, font=self.font_label
        ).pack(side="left")
        self.merge_status = tk.Label(header, text="", bg=BG, fg=FG_DIM, font=self.font_small)
        self.merge_status.pack(side="right")

        body = tk.Frame(self.merge_frame, bg=BG, padx=16)
        body.grid(row=1, column=0, sticky="nsew", pady=(7, 16))
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.merge_text = self._make_text(body, self.font_body, "word", column=0)
        self.merge_text.tag_configure("merge", foreground=FG, spacing1=2, spacing3=8)
        self.merge_text.tag_configure("head", foreground=MERGE_PURPLE, font=self.font_bold, spacing3=8)
        self.merge_text.tag_configure("error", foreground=ERR_RED, font=self.font_ui)
        self.merge_text.tag_configure("system", foreground=FG_DIM, font=self.font_ui)
        self._append(
            self.merge_text,
            "Aici apare rezultatul combinat al chaturilor.\n"
            "Deschide cel puțin două chaturi, pune-le să răspundă, apoi apasă „⧉ merge”.\n"
            "Chaturile pe același subiect sunt combinate într-o concluzie; cele pe subiecte "
            "diferite rămân independente, fiecare cu rezultatul lui.\n\n",
            "system",
        )

    # ------------------------------------------------------ shared text helpers

    def _panel_label(self, parent, text, column, padx=0):
        tk.Label(
            parent, text=text, bg=BG, fg=FG_FAINT, font=self.font_label, anchor="w"
        ).grid(row=0, column=column, sticky="ew", padx=padx, pady=(0, 7))

    def _make_text(self, parent, font, wrap, column, padx=0):
        """A text area with a scrollbar, placed in the parent grid. Returns the text."""
        frame = tk.Frame(parent, bg=BG_PANEL, highlightthickness=1, highlightbackground=BORDER)
        frame.grid(row=1, column=column, sticky="nsew", padx=padx)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        text = tk.Text(
            frame,
            bg=BG_PANEL,
            fg=FG,
            font=font,
            wrap=wrap,
            relief="flat",
            padx=16,
            pady=14,
            insertbackground=ORANGE,
            selectbackground=ORANGE_DARK,
            state="disabled",
            highlightthickness=0,
            width=1,
            height=1,
            spacing3=2,
        )
        text.grid(row=0, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(
            frame, orient="vertical", command=text.yview, style="coFlipper.Vertical.TScrollbar"
        )
        scroll.grid(row=0, column=1, sticky="ns", padx=(0, 3), pady=3)
        text.configure(yscrollcommand=scroll.set)
        return text

    def _configure_transcript_tags(self, widget):
        widget.tag_configure("speaker", foreground=ORANGE, font=self.font_bold)
        widget.tag_configure("agent", foreground=FG, spacing1=2, spacing3=8)
        widget.tag_configure("system", foreground=FG_DIM, font=self.font_ui)
        widget.tag_configure("error", foreground=ERR_RED, font=self.font_ui)
        widget.tag_configure("warning", foreground=WARN_YELLOW, font=self.font_bold)

    def _configure_chain_tags(self, widget):
        widget.tag_configure("head", foreground=ORANGE, font=self.font_step, spacing3=6)
        widget.tag_configure("num", foreground=FG_FAINT, font=self.font_step)
        widget.tag_configure("label", foreground=FG_DIM, font=self.font_step)
        widget.tag_configure("thought", foreground=FG_DIM, font=self.font_thought, spacing1=1)
        # The gray, clickable title that reveals a reasoning step, in Claude's thinking style.
        widget.tag_configure("thought_header", foreground=FG_DIM, font=self.font_step)
        widget.tag_configure("call", foreground=FG)
        widget.tag_configure("ok", foreground=OK_GREEN)
        widget.tag_configure("err", foreground=ERR_RED)
        widget.tag_configure("dim", foreground=FG_FAINT)
        widget.tag_configure("warn", foreground=WARN_YELLOW)
        widget.tag_configure("spawn", foreground=CYAN, font=self.font_step)
        widget.tag_configure("report", foreground=CYAN, font=self.font_step)
        widget.tag_configure("agentname", foreground=CYAN, font=self.font_small)
        widget.tag_configure("task", foreground=FG_DIM, font=self.font_thought)
        # Addresses visited over the network (the online IR database), shown like links.
        widget.tag_configure("visit", foreground=WEB_BLUE, font=self.font_small)

    def _append(self, widget, text, tag=None):
        widget.configure(state="normal")
        widget.insert("end", text, tag)
        widget.configure(state="disabled")
        widget.see("end")

    def _set_status(self, text, color=FG_DIM):
        self.status_label.configure(text=text, fg=color)
        self.status_dot.configure(fg=color)

    # ------------------------------------------------------------- session mgmt

    def _add_session(self, select=True):
        self._session_counter += 1
        session = ChatSession(self, f"Chat {self._session_counter}")
        self.sessions.append(session)
        # Insert before the merge tab, so merge stays rightmost.
        index = self.notebook.index(self.merge_frame)
        self.notebook.insert(index, session.frame, text=f"  {session.name}  ")
        if select:
            self.notebook.select(session.frame)
            session.focus()
        self._refresh_activity()
        return session

    def _add_session_clicked(self):
        if self.dispatcher is None:
            return
        self._add_session()

    def _active_session(self):
        try:
            current = self.notebook.select()
        except tk.TclError:
            return None
        for session in self.sessions:
            if str(session.frame) == current:
                return session
        return None

    def _close_active_session(self):
        if len(self.sessions) <= 1:
            return  # always keep at least one chat open
        session = self._active_session()
        if session is None or session.busy:
            return  # do not close a chat that is mid-turn
        self.notebook.forget(session.frame)
        self.sessions.remove(session)
        self._refresh_activity()

    def _on_session_started(self, session):
        self._refresh_activity()

    def _on_session_finished(self, session):
        # A finished turn may have remembered something, so the count is refreshed here.
        self._refresh_memory_label()
        self._refresh_activity()

    def _refresh_activity(self):
        """The shared indicators that depend on how many chats exist or are working."""
        working = sum(1 for s in self.sessions if s.busy)
        if working == 0:
            self.activity_label.configure(text=f"{len(self.sessions)} chaturi")
        elif working == 1:
            self.activity_label.configure(text="1 chat lucrează…")
        else:
            self.activity_label.configure(text=f"{working} chaturi lucrează în paralel…")
        # Merge needs at least two answers and nothing still running.
        answered = sum(1 for s in self.sessions if s.last_answer)
        # Merge needs at least two answers, nothing running, no merge already in flight, and
        # a state that differs from the one last merged - so pressing it never re-spends a
        # request on inputs that have not changed.
        can_merge = (
            answered >= 2
            and working == 0
            and not self._merging
            and self._merge_signature() != self._last_merged_signature
        )
        self.merge_button.configure(state="normal" if can_merge else "disabled")
        self.close_button.configure(
            state="normal" if len(self.sessions) > 1 else "disabled"
        )

    def _merge_signature(self):
        """What would be merged right now: each answered chat and its current answer.

        Two calls compare equal when nothing a merge depends on has changed, which is how
        the button knows a fresh merge would only repeat the last one.
        """
        return tuple(sorted((s.name, s.last_answer) for s in self.sessions if s.last_answer))

    # ------------------------------------------------------------------- merge

    def _on_merge(self):
        if self._merging:
            return
        answered = [s for s in self.sessions if s.last_answer]
        if len(answered) < 2:
            self._show_merge_message(
                "Merge-ul are nevoie de cel puțin două chaturi cu răspuns.", "error"
            )
            self.notebook.select(self.merge_frame)
            return
        if any(s.busy for s in self.sessions):
            self._show_merge_message("Așteaptă să termine chaturile active înainte de merge.", "error")
            return

        self._merging = True
        self._pending_signature = self._merge_signature()
        data = [
            {
                "name": s.name,
                "request": "; ".join(s.requests),
                "answer": s.last_answer,
                "commands": s.last_commands,
            }
            for s in answered
        ]
        self.notebook.select(self.merge_frame)
        self._emit("merge_thinking", "sintetizează rezultatele…")
        self.merge_button.configure(state="disabled")
        threading.Thread(target=self._merge_worker, args=(data,), daemon=True).start()

    def _merge_worker(self, data):
        try:
            from merge import synthesize

            text = synthesize(
                self.api_key, data, model=MODEL, simulated=self.dispatcher.simulated
            )
            self._emit("merge_result", {"text": text, "count": len(data)})
        except SystemExit as exc:
            self._emit("merge_error", str(exc))
        except Exception as exc:
            self._emit("merge_error", f"{type(exc).__name__}: {exc}")

    def _show_merge_message(self, text, tag):
        self._append(self.merge_text, text + "\n\n", tag)

    # ------------------------------------------------------------------ events

    def _drain_events(self):
        """The only place from which the interface is modified.

        Tkinter cannot be called from another thread, so the worker threads only put events
        on the queue, tagged with their session (or None for the shared ones), and the main
        loop consumes them here.
        """
        try:
            while True:
                kind, payload, session = self.events.get_nowait()
                if session is not None:
                    session.handle_event(kind, payload)
                else:
                    getattr(self, f"_on_event_{kind}")(payload)
        except queue.Empty:
            pass
        self.root.after(80, self._drain_events)

    def _emit(self, kind, payload, session=None):
        self.events.put((kind, payload, session))

    def _type_tick(self):
        """Drives every tab's typewriter, so streamed text reveals smoothly rather than in
        blocks. Runs on the main thread, like the rest of the interface updates."""
        for session in self.sessions:
            session.type_step()
        self.root.after(TYPE_INTERVAL_MS, self._type_tick)

    def _on_event_ready(self, payload):
        simulated = payload["simulated"]
        self.ready_status = payload["status"]
        self.ready_color = WARN_YELLOW if simulated else FG_DIM
        self._set_status(self.ready_status, self.ready_color)

        summary = f"{payload['tool_count']} comenzi · {', '.join(payload['categories'])}"
        if payload.get("subagents"):
            summary += "\nsubagenți: " + ", ".join(payload["subagents"])
        self.tools_label.configure(text=summary)

        # The first chat opens now that the shared resources are ready.
        self._set_controls_enabled(True)
        self._refresh_memory_label()
        first = self._add_session()
        if simulated:
            self._append(
                first.transcript,
                "MOD SIMULAT - niciun Flipper Zero fizic nu este conectat.\n"
                "Rezultatele comenzilor sunt fictive, produse de un simulator.\n\n",
                "warning",
            )
        self._refresh_activity()

    def _on_event_fatal(self, payload):
        self._set_status(payload["status"], ERR_RED)
        # No session exists yet on a fatal startup error, so this goes to the merge tab,
        # which is the one panel present from the start.
        self.notebook.select(self.merge_frame)
        self._show_merge_message(payload["message"], "error")

    def _on_event_device(self, state):
        from device import CONNECTED, DISCONNECTED

        if state == CONNECTED:
            self.ready_status, self.ready_color = f"conectat pe {self.flipper.port}", OK_GREEN
        elif state == DISCONNECTED:
            self.ready_status, self.ready_color = "neconectat", ERR_RED
        else:
            self.ready_status, self.ready_color = "aplicatia CFP nu ruleaza", WARN_YELLOW
        self._set_status(self.ready_status, self.ready_color)

    def _on_event_ir_progress(self, payload):
        sent, total = payload
        # The IR bruteforce holds the single device, so its progress belongs on the shared
        # device card rather than in any one tab.
        self._set_status(f"bruteforce IR: {sent}/{total} coduri trimise", WARN_YELLOW)

    def _on_event_merge_thinking(self, payload):
        self.merge_status.configure(text=payload, fg=MERGE_PURPLE)

    def _on_event_merge_result(self, payload):
        self._merging = False
        # Remember exactly what was merged, so the button will not re-run on this same state.
        self._last_merged_signature = self._pending_signature
        self.merge_status.configure(text="gata", fg=FG_DIM)
        self._append(
            self.merge_text, f"Rezultat pe subiecte ({payload['count']} chaturi)\n", "head"
        )
        self._append(self.merge_text, plain_text(payload["text"]) + "\n\n", "merge")
        self._refresh_activity()

    def _on_event_merge_error(self, payload):
        # The merge did not produce a result, so the state is not marked as merged: the
        # button stays available to retry.
        self._merging = False
        self.merge_status.configure(text="eroare", fg=ERR_RED)
        self._show_merge_message(payload, "error")
        self._refresh_activity()

    # --------------------------------------------------------- connect / worker

    def _on_ir_progress(self, sent, total):
        """Called from a worker thread during an IR bruteforce; only queues an event."""
        self._emit("ir_progress", (sent, total))

    def _connect(self):
        load_dotenv()
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            self._emit(
                "fatal",
                {
                    "status": "cheie API lipsa",
                    "message": "GEMINI_API_KEY nu este setat. Completeaza-l in desktop/.env.",
                },
            )
            return

        commands = model_commands(load_catalog())
        if not commands:
            self._emit(
                "fatal",
                {"status": "catalog gol", "message": "Nicio comanda disponibila in commands.json."},
            )
            return

        try:
            if self.mock:
                from mock_flipper import MockCFPClient

                self.flipper = MockCFPClient()
                status = "dispozitiv simulat"
            else:
                from device import LiveDevice

                self.flipper = LiveDevice()
                status = "se caută dispozitivul..."

            self.dispatcher = CommandDispatcher(
                commands, self.flipper, on_progress=self._on_ir_progress, memory=self.memory
            )
            self.dispatcher.subagents = SubagentRunner(
                api_key, self.dispatcher, self.dispatcher.simulated
            )
        except Exception as exc:
            self._emit("fatal", {"status": "eroare la pornire", "message": str(exc)})
            return

        # Stored for the sessions and the merge, which build their own chats from these.
        self.api_key = api_key
        self.commands = commands

        device = [c for c in commands if c.get("layer") == "device"]
        self._emit(
            "ready",
            {
                "status": status,
                "tool_count": len(device),
                "categories": sorted({c.get("category", "other") for c in device}),
                "subagents": sorted({c["subagent"] for c in commands if c.get("subagent")}),
                "simulated": self.dispatcher.simulated,
            },
        )

        if not self.mock:
            threading.Thread(target=self._monitor_device, daemon=True).start()

    def _monitor_device(self):
        """Watches the device being plugged in and out for as long as the app is running."""
        while not self.closing:
            state = self.flipper.poll()
            if state:
                self._emit("device", state)
            time.sleep(DEVICE_POLL_S)

    def close(self):
        self.closing = True  # stops the thread that monitors the device
        if self.flipper:
            try:
                self.flipper.close()
            except Exception:
                pass


def enable_dpi_awareness():
    """Without this, Windows scales the window like an image and the text looks blurry."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Interfata grafica a agentului coFlipper")
    parser.add_argument(
        "--mock", action="store_true", help="foloseste un Flipper simulat, fara dispozitiv fizic"
    )
    args = parser.parse_args()

    enable_dpi_awareness()
    root = tk.Tk()
    window = CoFlipperWindow(root, mock=args.mock)

    def on_close():
        window.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
