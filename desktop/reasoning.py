"""The reasoning chain: the step-by-step trace of one conversation turn.

Between the user's request and the final answer lies a succession of decisions: which
tool is worth calling, what the device responded, what conclusion can be drawn from
that response, whether the work should be handed to a specialised subagent. This module
keeps that succession in the order it happened, so it can be shown to the user and
verified step by step.

The purpose is not debugging but verifiability. An agent that phrases sentences in
natural language sounds equally convincing whether it measured something or merely
assumed it; the chain shows concretely what each statement in the answer rests on.

The module contains no rendering code: the console and the window display the same
chain in different ways, and the formatting shared by both lives in Step's methods.
"""

import threading
import time
from dataclasses import dataclass, field

# The kinds of step a chain is made of.
REQUEST = "request"  # the user's request, the step that opens the chain
THOUGHT = "thought"  # a summary of the reasoning, received from the model
TOOL = "tool"  # a command actually executed on the device, with its result
SPAWN = "spawn"  # a subagent is summoned: who it is, what it may do, what it was asked
REPORT = "report"  # the findings a subagent hands back to the agent that summoned it
ANSWER = "answer"  # the final answer, phrased on the basis of the preceding steps


@dataclass
class Step:
    kind: str
    text: str = ""
    name: str = ""
    args: dict = field(default_factory=dict)
    outcome: dict = field(default_factory=dict)
    # Extra detail that only some kinds of step carry: for a spawn, the subagent's role,
    # model and permitted tools.
    meta: dict = field(default_factory=dict)
    # 0 for the main agent's own steps, 1 for those of a subagent it summoned. The
    # display indents by depth, so a subagent's work reads as nested inside the chain.
    depth: int = 0
    # Which subagent produced the step, empty for the main agent. Kept even though depth
    # already implies nesting: two subagents may work on the same chain, and without a
    # name their steps could not be told apart.
    source: str = ""
    # The round of dialogue with the model in which the step occurred. A turn can have
    # several: the model asks for a measurement, receives the result, then decides
    # whether it needs another.
    round: int = 0
    # Seconds elapsed since the start of the turn, so it is visible where the time went.
    at_s: float = 0.0

    @property
    def ok(self):
        return self.outcome.get("status") == "ok"

    @property
    def simulated(self):
        return bool(self.outcome.get("simulated"))

    def arg_line(self):
        return " ".join(f"{key}={value}" for key, value in self.args.items())

    def result_line(self):
        """The command's result on a single line, in the same form for every display."""
        if self.kind != TOOL:
            return ""
        if self.ok:
            # Device commands return 'data'; agent commands (the IR bruteforce, the app
            # builder) return a summary of their own, so we show whichever exists.
            if "data" in self.outcome:
                summary = " ".join(self.outcome.get("data") or [])
            else:
                summary = self.outcome.get("message") or self.outcome.get("outcome") or "done"
            return f"OK {summary}".strip()
        return f"ERR {self.outcome.get('error', 'unknown error')}"


class Trace:
    """The chain of a single turn: request, reasoning, commands, subagents, answer."""

    def __init__(self, request):
        self._start = time.monotonic()
        # Subagents run on their own threads and add steps to this same chain, so the
        # bookkeeping below is guarded.
        self._lock = threading.Lock()
        self.round = 0
        self.steps = []
        self._add(Step(kind=REQUEST, text=request))

    def _add(self, step):
        with self._lock:
            step.round = self.round
            step.at_s = time.monotonic() - self._start
            self.steps.append(step)
        return step

    def next_round(self):
        with self._lock:
            self.round += 1

    def add_thought(self, text, depth=0, source=""):
        return self._add(Step(kind=THOUGHT, text=plain_text(text), depth=depth, source=source))

    def add_tool(self, name, args, outcome, depth=0, source=""):
        return self._add(
            Step(
                kind=TOOL,
                name=name,
                args=dict(args or {}),
                outcome=outcome,
                depth=depth,
                source=source,
            )
        )

    def add_spawn(self, name, task, meta, depth=0):
        return self._add(Step(kind=SPAWN, name=name, text=task, meta=dict(meta), depth=depth))

    def add_report(self, name, text, meta=None, depth=0):
        return self._add(
            Step(
                kind=REPORT,
                name=name,
                text=plain_text(text),
                meta=dict(meta or {}),
                depth=depth,
                source=name,
            )
        )

    def add_answer(self, text):
        return self._add(Step(kind=ANSWER, text=text))

    @property
    def first(self):
        return self.steps[0]

    @property
    def evidence(self):
        """The steps that produced real data: what the final answer rests on."""
        return [step for step in self.steps if step.kind == TOOL and step.ok]

    @property
    def subagents(self):
        """The subagents summoned during this turn, in the order they were summoned."""
        return [step.name for step in self.steps if step.kind == SPAWN]


def plain_text(text):
    """Plain text out of text carrying Markdown markup.

    The model is instructed to answer without markup, and the reasoning summaries do not
    even pass through that instruction. The displays do not render Markdown, so a
    leftover asterisk shows up as exactly that: an asterisk.
    """
    cleaned = text.replace("**", "").replace("__", "").replace("`", "")
    lines = []
    for line in cleaned.splitlines():
        body = line.lstrip()
        indent = line[: len(line) - len(body)]
        if body.startswith(("* ", "- ")):
            body = "• " + body[2:].lstrip()
        elif body.startswith("#"):
            body = body.lstrip("#").lstrip()
        lines.append(indent + body)
    return "\n".join(lines).strip()
