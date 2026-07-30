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
import mimetypes
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog
from tkinter import font as tkfont
from tkinter import ttk

from dotenv import load_dotenv

from agent import (
    MODEL,
    SELECTABLE_MODELS,
    ModelOverloaded,
    TurnCancelled,
    build_chat,
    maybe_compact,
    rebuild_chat,
    run_turn,
)
from commands import CommandDispatcher, load_catalog, model_commands
from memory import MemoryStore
from reasoning import ANSWER, REPORT, REQUEST, SEARCH, SPAWN, THOUGHT, TOOL, plain_text
from settings import Settings
from subagents import SubagentRunner
import voice

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
# A darker red for the stop button's hover/pressed state (ERR_RED is the base).
ERR_RED_DARK = "#C25858"

# The one input button carries no label: an up-arrow to send, a filled square to stop the
# turn in progress - the same suggestive, text-free composer control Claude uses.
SEND_GLYPH = "↑"
STOP_GLYPH = "■"

# qFlipper's signature grid, drawn faintly behind the device card, and a slightly lifted
# surface used for button hover so controls react to the pointer instead of sitting inert.
GRID_LINE = "#232429"
HOVER_BG = "#26272C"
GRID_STEP = 15  # pixels between grid lines


def _blend(base, target, t):
    """A colour t of the way from base to target (both '#rrggbb'), for smooth pulsing/fading.
    t is clamped to 0..1; t=0 returns base, t=1 returns target."""
    t = max(0.0, min(1.0, t))
    b = (int(base[1:3], 16), int(base[3:5], 16), int(base[5:7], 16))
    g = (int(target[1:3], 16), int(target[3:5], 16), int(target[5:7], 16))
    return "#%02x%02x%02x" % tuple(round(b[i] + (g[i] - b[i]) * t) for i in range(3))

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
        # Set true when the user presses stop; the worker's turn checks it and aborts.
        self._cancel = False
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
        # Voice input: the push-to-talk recorder, and a flag for the timer that updates the
        # button's "recording…" label while the user is speaking.
        self._recorder = None
        self._mic_tick_pending = False
        # A file picked but not yet sent: it waits here (shown as a chip) so the user can add
        # instructions or send it alone. {bytes, mime_type, name}, or None when nothing is
        # attached. Voice is not staged this way - push-to-talk sends as soon as it stops.
        self._pending_attachment = None
        # The last answer and the commands behind it, kept for the merge.
        self.last_answer = None
        self.last_commands = []
        self._commands = []
        # Every request the user sent this chat. The subject-aware merge needs them to tell
        # what the chat is ABOUT, so it can group chats on the same subject and leave the
        # rest independent.
        self.requests = []

        # The model backing this chat: whatever the picker had selected when the tab opened.
        # Kept per-tab, so different chats can run on different models at the same time.
        self.model = window.selected_model
        # A fresh, independent conversation, seeded with the persistent memory. build_chat
        # also returns the client, which has to be kept alive for as long as the chat is
        # used (see build_chat).
        self.genai_client, self.chat = build_chat(
            window.api_key, window.commands, window.dispatcher.simulated, window.memory_prompt(),
            model=self.model,
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

        # A chip shown above the composer while a file is attached but not yet sent: it names
        # the file and offers an ✕ to drop it. Hidden (grid_remove) until a file is staged.
        self.attach_chip = tk.Frame(bar, bg=BG_CARD, highlightthickness=1, highlightbackground=BORDER)
        self.attach_chip_label = tk.Label(
            self.attach_chip, text="", bg=BG_CARD, fg=WEB_BLUE, font=self.window.font_small
        )
        self.attach_chip_label.pack(side="left", padx=(10, 6), pady=4)
        tk.Button(
            self.attach_chip, text="✕", command=self._clear_attachment, bg=BG_CARD, fg=FG_DIM,
            font=self.window.font_small, relief="flat", padx=6, pady=0, cursor="hand2",
            activebackground=BG_CARD, activeforeground=ERR_RED, borderwidth=0, highlightthickness=0,
        ).pack(side="left", padx=(0, 8))
        self.attach_chip.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.attach_chip.grid_remove()

        self.input_wrapper = tk.Frame(
            bar, bg=BG_PANEL, highlightthickness=1, highlightbackground=BORDER
        )
        self.input_wrapper.grid(row=1, column=0, sticky="ew", padx=(0, 12))

        self.entry = tk.Entry(
            self.input_wrapper,
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
        # The composer glows orange while it has focus, the way qFlipper's active fields do.
        self.entry.bind(
            "<FocusIn>", lambda _e: self.input_wrapper.configure(highlightbackground=ORANGE)
        )
        self.entry.bind(
            "<FocusOut>", lambda _e: self.input_wrapper.configure(highlightbackground=BORDER)
        )

        # Attach control: a single, text-free "+" that opens a small menu to choose what to
        # attach - a spoken message or a file (image, PDF, ...). Minimal, in Claude's style. It
        # is always present (files need no microphone); the voice option appears only when a mic
        # is actually usable. While recording, this same button becomes the stop control.
        self.attach_button = tk.Button(
            bar,
            text="＋",
            command=self._on_attach,
            bg=BG_CARD,
            fg=FG,
            font=self.window.font_bold,
            relief="flat",
            padx=16,
            pady=10,
            activebackground=BORDER,
            cursor="hand2",
            borderwidth=0,
            highlightthickness=0,
            width=2,
        )
        self.attach_button.grid(row=1, column=1, padx=(0, 8))
        # Hover feedback, but only in its idle "+" role - not while it is the red stop control.
        self.attach_button.bind(
            "<Enter>",
            lambda _e: self.attach_button.configure(bg=HOVER_BG)
            if not (self._recorder and self._recorder.recording) else None,
        )
        self.attach_button.bind(
            "<Leave>",
            lambda _e: self.attach_button.configure(bg=BG_CARD)
            if not (self._recorder and self._recorder.recording) else None,
        )

        # One text-free button that doubles as send and stop: an up-arrow to send, a square to
        # stop the turn in progress (like Claude's composer). It routes through one handler that
        # decides which it is from whether the chat is currently working.
        self.send_button = tk.Button(
            bar,
            text=SEND_GLYPH,
            command=self._on_send_or_stop,
            bg=ORANGE,
            fg="#141518",
            font=self.window.font_bold,
            relief="flat",
            padx=18,
            pady=8,
            activebackground=ORANGE_DARK,
            activeforeground="#141518",
            cursor="hand2",
            disabledforeground="#6A6A72",
            borderwidth=0,
            highlightthickness=0,
            width=2,
        )
        self.send_button.grid(row=1, column=2)
        self.send_button.bind("<Enter>", lambda _e: self._hover_send(True))
        self.send_button.bind("<Leave>", lambda _e: self._hover_send(False))

        self.set_input_enabled(True)

    def _on_send_or_stop(self):
        """The composer button: stop when a turn is running, send otherwise."""
        if self.busy:
            self.on_stop()
        else:
            self.on_send()

    def _refresh_send_button(self):
        """Puts the button in its current role: a square stop while the chat works, an up-arrow
        send when it is idle. Always clickable - the stop must stay live while everything else
        in the composer is disabled."""
        if self.busy:
            self.send_button.configure(
                text=STOP_GLYPH, bg=ERR_RED, activebackground=ERR_RED_DARK, state="normal"
            )
        else:
            self.send_button.configure(
                text=SEND_GLYPH, bg=ORANGE, activebackground=ORANGE_DARK, state="normal"
            )

    def _hover_send(self, hovering):
        if str(self.send_button["state"]) == "disabled":
            return
        if self.busy:  # stop role
            self.send_button.configure(bg=ERR_RED_DARK if hovering else ERR_RED)
        else:
            self.send_button.configure(bg=ORANGE_DARK if hovering else ORANGE)

    def set_input_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        self.entry.configure(state=state)
        # The send/stop button is managed by _refresh_send_button, not here: while the chat
        # works, the composer is disabled but that button stays live as the stop control.
        self._refresh_send_button()
        # The attach button is disabled while a turn runs too - unless a recording is in
        # progress, whose own Stop must stay clickable so the user can finish speaking.
        if not (self._recorder and self._recorder.recording):
            self.attach_button.configure(state=state)
        if enabled:
            self.entry.focus_set()

    def on_stop(self):
        """The user pressed stop. The worker's turn checks the flag at its next boundary and
        aborts; the button is parked until that lands as a 'stopped' event, so a second click
        does nothing in the meantime."""
        if not self.busy:
            return
        self._cancel = True
        self.set_local_status("se oprește...", WARN_YELLOW)
        self.send_button.configure(state="disabled")

    def _on_attach(self):
        """The composer's attach control. While recording it stops and sends; otherwise it
        opens a small menu to pick what to attach - a spoken message or a file. Minimal, in
        Claude's style: one button, no separate category on the bar."""
        if self._recorder and self._recorder.recording:
            self._stop_recording_and_send()
            return
        if self.busy:
            return
        menu = tk.Menu(
            self.frame,
            tearoff=0,
            bg=BG_CARD,
            fg=FG,
            activebackground=ORANGE,
            activeforeground="#141518",
            bd=0,
            relief="flat",
            font=self.window.font_small,
        )
        # Voice is offered only when a microphone is actually usable; a file always is.
        if voice.is_available():
            menu.add_command(label="  🎤   Mesaj vocal  ", command=self._start_recording)
        menu.add_command(label="  📎   Fișier  ", command=self._pick_file)
        self.attach_button.update_idletasks()
        x = self.attach_button.winfo_rootx()
        y = self.attach_button.winfo_rooty()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _start_recording(self):
        """Begins a push-to-talk voice message. The spoken audio is transcribed and acted on by
        the model in the same turn; the button becomes a stop that ends and sends it."""
        if self.busy:
            return
        try:
            self._recorder = voice.Recorder()
            self._recorder.start()
        except Exception as exc:  # noqa: BLE001 - a device grabbed by another app, etc.
            self._recorder = None
            self.set_local_status(f"microfonul nu a putut porni: {exc}", ERR_RED)
            return
        # Block the text path while recording, but keep the attach button (now a Stop) live.
        self.entry.configure(state="disabled")
        self.send_button.configure(state="disabled", bg=BG_CARD)
        self.attach_button.configure(text="⏹", bg=ERR_RED, fg="#141518")
        self._mic_tick_pending = True
        self._update_mic_label()

    def _pick_file(self):
        """Attaches a file (image, PDF, audio, text, ...) but does NOT send it. The file waits
        as a chip so the user can add instructions and then send, or send it alone to have the
        model analyse it. It reaches the model as an inline part on the next send."""
        if self.busy:
            return
        path = filedialog.askopenfilename(
            parent=self.window.root,
            title="Alege un fișier",
            filetypes=[
                ("Imagini", "*.png *.jpg *.jpeg *.webp *.gif *.bmp"),
                ("PDF", "*.pdf"),
                ("Audio", "*.wav *.mp3 *.m4a *.ogg *.flac"),
                ("Text", "*.txt *.md *.csv *.json"),
                ("Toate fișierele", "*.*"),
            ],
        )
        if not path:
            return
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            self.set_local_status(f"nu am putut citi fișierul: {exc}", ERR_RED)
            return
        if not data:
            self.set_local_status("fișierul este gol", FG_DIM)
            return
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        name = os.path.basename(path)
        # Stage it: shown as a chip, sent only when the user presses send (with or without text).
        self._pending_attachment = {"bytes": data, "mime_type": mime, "name": name}
        self.attach_chip_label.configure(text=f"📎 {name}")
        self.attach_chip.grid()
        self.set_local_status("fișier atașat — scrie instrucțiuni (opțional), apoi trimite", WEB_BLUE)
        self.entry.focus_set()

    def _clear_attachment(self):
        """Drops the staged file without sending it."""
        self._pending_attachment = None
        self.attach_chip.grid_remove()
        self.attach_chip_label.configure(text="")

    def _update_mic_label(self):
        if not (self._recorder and self._recorder.recording):
            self._mic_tick_pending = False
            return
        self.set_local_status(f"🎤 înregistrez… {self._recorder.duration:0.0f}s", ERR_RED)
        self.window.root.after(200, self._update_mic_label)

    def _stop_recording_and_send(self):
        recorder, self._recorder = self._recorder, None
        self._mic_tick_pending = False
        wav = recorder.stop()
        # Length from the WAV payload (44-byte header, 2 bytes/sample, 16 kHz mono) - the
        # recorder's own duration is 0 now that stop() has cleared its frames.
        seconds = max(0.0, (len(wav) - 44) / (voice.SAMPLE_RATE * voice.SAMPLE_WIDTH))
        self.attach_button.configure(text="＋", bg=BG_CARD, fg=FG)
        if len(wav) <= 44:  # nothing captured
            self.set_local_status("nu am înregistrat nimic", FG_DIM)
            self.set_input_enabled(True)
            return
        label = f"🎤 mesaj vocal ({seconds:0.0f}s)"
        self._send(message="", attachment={"bytes": wav, "mime_type": voice.MIME_TYPE},
                   display=label)

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
        elif kind == "stopped":
            # The turn was cancelled: stop revealing any buffered text, keep what is already on
            # screen, and mark the interruption so it is clear the answer is incomplete.
            self._reset_typers()
            self._append(self.transcript, "· oprit ·\n\n", "system")
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
        elif step.kind == SEARCH:
            # A web search the model ran itself: the queries it issued, then the sources it
            # read, styled like the visited-URL rows so a web-backed answer is transparent.
            self._append(self.chain, "căutare web\n", "call")
            if step.queries:
                qline = "   ".join(f"„{q}”" for q in step.queries)
                self._append(self.chain, f"{pad}{STEP_INDENT}{qline}\n", "dim")
            for src in step.sources:
                # Google returns an ugly redirect URI; the clean label is the domain or the
                # site title. Prefer those, fall back to the URI only if neither is present.
                domain, title, uri = src.get("domain"), src.get("title"), src.get("uri")
                label = domain or title or uri
                row = f"↗ {label}"
                if domain and title and title != domain:
                    row += f" — {title}"
                self._append(self.chain, f"{pad}{STEP_INDENT}{row}\n", "visit")
            self._append(self.chain, "\n")
            self.set_local_status("a căutat pe web…", WEB_BLUE)
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
        att = self._pending_attachment
        # Nothing to send unless there is text or a staged file. A staged file may go alone
        # (analyse the photo) or with instructions.
        if not message and not att:
            return
        self.entry.delete(0, "end")
        if att:
            self._clear_attachment()
            display = f"📎 {att['name']}" + (f" — {message}" if message else "")
            self._send(message, attachment={"bytes": att["bytes"], "mime_type": att["mime_type"]},
                       display=display)
        else:
            self._send(message)

    def _send(self, message, attachment=None, display=None):
        """Starts a turn, from typed text or a spoken (attachment) message.

        `display` is what the user's bubble and the merge should show when the request has no
        text of its own - a voice message shows "🎤 mesaj vocal (Ns)" rather than an empty
        line. The audio itself goes only to the worker, as the attachment."""
        if self.busy:
            return
        shown = display or message
        self.busy = True
        self._cancel = False
        self._commands = []
        self._answer_started = False
        self._live_thought = None
        self._reset_typers()
        self.requests.append(shown)
        self.set_input_enabled(False)
        self.window._emit("user", shown, session=self)
        self.window._emit("thinking", "lucrează...", session=self)
        self.window._on_session_started(self)
        threading.Thread(
            target=self._worker, args=(message,), kwargs={"attachment": attachment}, daemon=True
        ).start()

    def _worker(self, message, attachment=None):
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
            reply, _trace = run_turn(
                self.chat, self.window.dispatcher, message, on_step, on_delta,
                attachment=attachment, should_stop=lambda: self._cancel,
            )
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
                model=self.model,
            )
        except TurnCancelled:
            # The user pressed stop; end the turn quietly, keeping whatever was shown so far.
            self.window._emit("stopped", None, session=self)
        except SystemExit as exc:
            # send_with_retry exits via sys.exit when the model is unavailable on the plan.
            self.window._emit("error", str(exc), session=self)
        except ModelOverloaded as exc:
            # A passing Google-side 503 that outlasted the retries: show its own clear message,
            # not a raw "ModelOverloaded: ..." - the user did nothing wrong and can just retry.
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
        self.settings = Settings()
        self.closing = False
        self.sessions = []
        self._session_counter = 0
        # The model new chats are created with, chosen from the top-bar picker. A tab keeps
        # whatever model it was created with (shown in the picker when the tab is active), and
        # switching the picker moves the active tab onto the new model too.
        # The choice is remembered across restarts (settings.json). An explicit COFLIPPER_MODEL
        # in the environment is a deliberate override and still wins over the saved preference.
        env_model = os.environ.get("COFLIPPER_MODEL")
        self.selected_model = env_model or self.settings.get("model", MODEL)
        # A merge is a model request, so it is never spent twice on the same state: the
        # button stays disabled after a merge until at least one chat answers again, and
        # while a merge is in flight. _merge_signature() captures what was last merged.
        self._merging = False
        self._last_merged_signature = None
        self._pending_signature = None
        # The device-card status baseline, kept up to date by the monitor thread.
        self.ready_status = "se conecteaza..."
        self.ready_color = FG_DIM
        # Animation state: a phase advanced on a slow timer, driving the breathing connection
        # dot and the "working…" ellipsis, so the interface reads as alive rather than static.
        # _pulse_base is the dot's current colour; it steadies (stops breathing) on an error.
        self._anim_phase = 0.0
        self._pulse_base = FG_FAINT
        self._pulse_steady = False
        self._activity_base = ""

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
        root.after(90, self._anim_tick)
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
        # A clean, thumb-only scrollbar. clam's default draws two arrow buttons at the ends -
        # small dark squares that never light up on hover, which is exactly the "lines that
        # stay dark" the eye catches. Redefining the layout without the arrows removes them, so
        # only the thumb remains, and it brightens under the pointer.
        style.layout(
            "coFlipper.Vertical.TScrollbar",
            [
                (
                    "Vertical.Scrollbar.trough",
                    {
                        "sticky": "ns",
                        "children": [
                            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})
                        ],
                    },
                )
            ],
        )
        style.configure(
            "coFlipper.Vertical.TScrollbar",
            background=BORDER,          # the thumb
            troughcolor=BG_PANEL,       # the track it slides in
            bordercolor=BG_PANEL,
            darkcolor=BORDER,           # no bevel: match the thumb so there are no edge lines
            lightcolor=BORDER,
            relief="flat",
            width=10,
        )
        style.map(
            "coFlipper.Vertical.TScrollbar",
            # The thumb lifts to a light gray on hover and to the orange accent while dragged.
            background=[("pressed", ORANGE), ("active", FG_FAINT)],
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
            # 'active' is the hovered state: the tab lifts and brightens under the pointer.
            background=[("selected", BG_PANEL), ("active", HOVER_BG)],
            foreground=[("selected", ORANGE), ("active", FG)],
        )
        # The model picker, restyled for the dark theme (clam's default combobox is a bright
        # white field). The dropdown list is a Tk option, not a ttk one, so it is set through
        # the option database below.
        style.configure(
            "coFlipper.TCombobox",
            fieldbackground=BG_CARD,
            background=BG_CARD,
            foreground=FG,
            arrowcolor=FG_DIM,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            relief="flat",
            padding=(8, 4),
        )
        style.map(
            "coFlipper.TCombobox",
            fieldbackground=[("readonly", BG_CARD)],
            foreground=[("readonly", FG)],
            selectbackground=[("readonly", BG_CARD)],
            selectforeground=[("readonly", FG)],
        )
        self.root.option_add("*TCombobox*Listbox.background", BG_CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ORANGE)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#141518")

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

        # The model picker: which Gemini model the chats talk to. It shows the active tab's
        # model, and changing it moves that tab (and every new tab) onto the chosen model.
        self.model_var = tk.StringVar(value=self.selected_model)
        # The saved/overridden model is always offered, even if it is not one of the built-in
        # choices (a custom COFLIPPER_MODEL, say), so the picker can show what is actually in use.
        model_values = list(SELECTABLE_MODELS)
        if self.selected_model not in model_values:
            model_values.insert(0, self.selected_model)
        self.model_picker = ttk.Combobox(
            bar,
            textvariable=self.model_var,
            values=model_values,
            state="readonly",
            style="coFlipper.TCombobox",
            width=22,
            font=self.font_small,
        )
        self.model_picker.pack(side="right")
        self.model_picker.bind("<<ComboboxSelected>>", self._on_model_change)
        tk.Label(bar, text="model", bg=BG, fg=FG_FAINT, font=self.font_small).pack(
            side="right", padx=(0, 8)
        )

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

        # React to the pointer: lift the surface and brighten the border on hover, so the
        # controls feel live rather than painted on. A disabled button stays inert.
        def on_enter(_e):
            if str(button["state"]) != "disabled":
                button.configure(bg=HOVER_BG, highlightbackground=fg)

        def on_leave(_e):
            button.configure(bg=BG_CARD, highlightbackground=BORDER)

        button.bind("<Enter>", on_enter)
        button.bind("<Leave>", on_leave)
        return button

    def _set_controls_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        for button in (self.new_button, self.close_button, self.merge_button, self.memory_button):
            button.configure(state=state)
        # The picker is a readonly combobox when usable, fully disabled before the app is ready.
        self.model_picker.configure(state="readonly" if enabled else "disabled")

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
        # The device card is drawn on a canvas so qFlipper's faint grid can sit behind it. The
        # content stays ordinary widgets placed on the canvas, so everything that refers to the
        # status dot or the tools label keeps working unchanged.
        self.card_canvas = tk.Canvas(
            self.root, bg=BG_CARD, height=68, highlightthickness=1,
            highlightbackground=BORDER, bd=0,
        )
        self.card_canvas.grid(row=1, column=0, sticky="ew", padx=20)
        self.card_canvas.bind("<Configure>", self._draw_card_grid)

        left = tk.Frame(self.card_canvas, bg=BG_CARD)
        tk.Label(left, text="Flipper Zero", bg=BG_CARD, fg=FG, font=self.font_device).pack(anchor="w")

        state_row = tk.Frame(left, bg=BG_CARD)
        state_row.pack(anchor="w", pady=(4, 0))
        self.status_dot = tk.Label(state_row, text="●", bg=BG_CARD, fg=FG_FAINT, font=self.font_ui)
        self.status_dot.pack(side="left", padx=(0, 6))
        self.status_label = tk.Label(
            state_row, text="se conecteaza...", bg=BG_CARD, fg=FG_DIM, font=self.font_ui
        )
        self.status_label.pack(side="left")

        right = tk.Frame(self.card_canvas, bg=BG_CARD)
        tk.Label(
            right, text="UNELTE DISPONIBILE", bg=BG_CARD, fg=FG_FAINT, font=self.font_label
        ).pack(anchor="e")
        self.tools_label = tk.Label(
            right, text="—", bg=BG_CARD, fg=FG_DIM, font=self.font_small, justify="right"
        )
        self.tools_label.pack(anchor="e", pady=(3, 0))

        # Placed on the canvas; repositioned to the card's edges whenever it is resized.
        self._card_left_win = self.card_canvas.create_window(18, 34, window=left, anchor="w")
        self._card_right_win = self.card_canvas.create_window(0, 34, window=right, anchor="e")

    def _draw_card_grid(self, _event=None, width=None, height=None):
        """Repaints the faint grid behind the device card and pins the content to its edges.
        width/height default to the canvas's real size; they are passed explicitly only by the
        headless test, where an unmapped canvas reports no size of its own."""
        canvas = self.card_canvas
        canvas.delete("grid")
        width = width or canvas.winfo_width()
        height = height or canvas.winfo_height()
        for x in range(0, width, GRID_STEP):
            canvas.create_line(x, 0, x, height, fill=GRID_LINE, tags="grid")
        for y in range(0, height, GRID_STEP):
            canvas.create_line(0, y, width, y, fill=GRID_LINE, tags="grid")
        canvas.tag_lower("grid")  # keep the grid behind the content windows
        canvas.coords(self._card_left_win, 18, height // 2)
        canvas.coords(self._card_right_win, width - 18, height // 2)

    def _build_notebook(self):
        holder = tk.Frame(self.root, bg=BG, padx=20)
        holder.grid(row=2, column=0, sticky="nsew", pady=(16, 0))
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)

        self.notebook = ttk.Notebook(holder, style="coFlipper.TNotebook")
        self.notebook.grid(row=0, column=0, sticky="nsew")
        # When the user switches tab, the picker follows: it always shows the model of the
        # chat currently in front.
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

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
        # Feed the breathing dot: it pulses this colour, but stays steady on an error (red), so
        # a fault reads as a fixed alarm rather than a friendly heartbeat.
        self._pulse_base = color
        self._pulse_steady = color == ERR_RED

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

    def _on_tab_changed(self, _event=None):
        """Keeps the picker showing the front tab's model (the merge tab has none of its own,
        so the picker simply keeps its last value there)."""
        session = self._active_session()
        if session is not None and getattr(self, "model_var", None) is not None:
            self.model_var.set(session.model)

    def _on_model_change(self, _event=None):
        """The user picked a model. It becomes the default for new chats, and the active chat
        is moved onto it right away - carrying its whole history, so the conversation continues
        rather than restarting. A chat mid-turn is left alone (its worker is using the chat),
        and the picker snaps back until the turn is done."""
        chosen = self.model_var.get()
        session = self._active_session()
        if session is not None and session.busy:
            # Cannot swap the chat object out from under a running turn; revert and say so.
            self.model_var.set(session.model)
            session.set_local_status("schimbă modelul după ce se termină tura", WARN_YELLOW)
            return
        self.selected_model = chosen
        # Remember the choice so the next launch opens on the same model.
        self.settings.set("model", chosen)
        # Subagents are shared (one dispatcher); point them at the chosen model too.
        if self.dispatcher is not None and getattr(self.dispatcher, "subagents", None) is not None:
            self.dispatcher.subagents.model = chosen
        if session is not None and session.model != chosen:
            previous = session.model
            session.model = chosen
            session.chat = rebuild_chat(
                session.genai_client,
                session.chat,
                self.commands,
                self.dispatcher.simulated,
                self.memory_prompt(),
                model=chosen,
            )
            session._append(
                session.transcript,
                f"· model schimbat: {previous} → {chosen} (conversația continuă) ·\n\n",
                "system",
            )

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
            # Idle: a plain count, and the animated ellipsis is switched off.
            self._activity_base = ""
            self.activity_label.configure(text=f"{len(self.sessions)} chaturi")
        else:
            # Working: the ellipsis is animated by _anim_tick, so only the stem is set here.
            self._activity_base = (
                "1 chat lucrează" if working == 1 else f"{working} chaturi lucrează în paralel"
            )
            self.activity_label.configure(text=self._activity_base)
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

    def _anim_tick(self):
        """The slow heartbeat of the interface: it breathes the connection dot and animates the
        'working…' ellipsis, so a connected, thinking app looks alive rather than frozen."""
        import math

        if self.closing:
            return
        self._anim_phase += 0.12
        # The dot breathes between a dim and a full version of its status colour - unless it is
        # steady (an error), which should read as a fixed alarm, not a pulse.
        if self._pulse_steady:
            self.status_dot.configure(fg=self._pulse_base)
        else:
            level = 0.45 + 0.55 * (0.5 + 0.5 * math.sin(self._anim_phase))
            self.status_dot.configure(fg=_blend(BG_CARD, self._pulse_base, level))
        # The activity label, when a chat is working, gets a moving ellipsis (…, .·°, etc.).
        if self._activity_base:
            dots = "." * (1 + int(self._anim_phase * 1.5) % 3)
            self.activity_label.configure(text=self._activity_base + dots)
        self.root.after(90, self._anim_tick)

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

            # on_progress keeps the window alive while a long agent command runs (the IR
            # bruteforce, the app build): without it the window would look frozen.
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
