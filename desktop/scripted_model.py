"""A scripted stand-in for the Gemini model, used by the tests.

The daily free-tier quota is about 20 requests per model, and a single conversation turn
consumes several of them - one for the initial request and one more for every round of
tool calls. A test suite that spoke to the real API would therefore exhaust the quota in a
handful of runs, and would not be repeatable anyway: the model is free to answer the same
question with a different number of rounds each time.

So the tests replace the conversation, not the code around it. A ScriptedChat returns a
fixed list of responses in order, exactly where `chats.create(...)` would have returned a
real session, and everything else - the tool loop, the dispatcher, the reasoning chain,
the subagents, the interface - runs unchanged.

The doubles below mimic the shape the SDK actually produces. One detail matters and was
verified against the installed SDK rather than assumed: GenerateContentResponse.text
skips parts marked as reasoning (`_get_text` does `if part.thought: continue`). The double
does the same, because a test that let reasoning leak into `.text` would have hidden a
real defect in how the answer is rebuilt.
"""


class Part:
    """One piece of a response: ordinary text, or a summary of the model's reasoning."""

    def __init__(self, text=None, thought=False):
        self.text = text
        self.thought = thought


class Call:
    """A tool call requested by the model."""

    def __init__(self, name, args=None):
        self.name = name
        self.args = args or {}


class Response:
    def __init__(self, parts, calls=None):
        self.candidates = [
            type("Candidate", (), {"content": type("Content", (), {"parts": parts})()})()
        ]
        self.function_calls = calls or []
        self.text = "".join(p.text or "" for p in parts if not p.thought) or None


class ScriptedChat:
    """Hands out prepared responses in order, and records what was sent to it."""

    def __init__(self, responses, label="model"):
        self._responses = list(responses)
        self.label = label
        self.sent = []

    def send_message(self, message):
        self.sent.append(message)
        if not self._responses:
            raise AssertionError(
                f"{self.label}: more responses were requested than the script provides"
            )
        return self._responses.pop(0)

    def send_message_stream(self, message):
        """The streaming counterpart run_turn now uses: one prepared response, delivered as
        a single chunk. A real stream would split it into many, but a stream of one whole
        chunk exercises the same accumulation the multi-chunk case does."""
        yield self.send_message(message)


class Checks:
    """Minimal test bookkeeping, so the suite needs no external dependency."""

    def __init__(self, title):
        self.results = []
        print(f"== {title}")

    def section(self, title):
        print(f"\n{title}")

    def check(self, label, condition):
        self.results.append(bool(condition))
        print(f"  {'OK  ' if condition else 'FAIL'} {label}")

    def finish(self):
        passed = sum(self.results)
        total = len(self.results)
        print(f"\n{passed}/{total} checks passed")
        return 0 if passed == total else 1
