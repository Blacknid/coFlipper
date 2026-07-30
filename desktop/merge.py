"""Merging the results of several independent chat sessions, by subject.

The graphical interface can run several chats at once, each its own conversation with the
model, each reaching the single Flipper Zero through the shared dispatcher (whose device
lock serialises their access to the one serial port). The chats are the 'map': several
independent lines of work, running in parallel.

This module is the 'reduce', and it is subject-aware. Not every pair of chats should be
merged: two chats studying the SAME subject from different angles should be combined into
one conclusion, but two chats on unrelated subjects should stay separate, each with its
own result. So the synthesiser first groups the chats by subject and only then merges,
within each group. It is a model call like any other, so it obeys the same honesty rules:
it may not invent a reading none of the chats took.
"""

from google import genai
from google.genai import types

from agent import INCLUDE_THOUGHTS, MODEL, SIMULATED_NOTICE, answer_text, send_with_retry

MERGE_INSTRUCTION = """You are the synthesiser of the coFlipper project. You receive several
chat sessions. Each is its own separate conversation with the model, with its own request,
its own final answer, and its own commands sent to the Flipper Zero.

Your first job is to decide which sessions are about the SAME subject and which are on
different subjects. The subject is what a session is about: a particular frequency, a
particular network or device, a particular capability. Two sessions share a subject if they
investigate the same target, even from different angles; they are on different subjects if
they investigate unrelated things.

Then produce the result, grouped by subject:
- For a subject that has TWO OR MORE sessions: give ONE consolidated conclusion that merges
  what those sessions found, noting briefly where they agreed and where they diverged and
  naming the session behind each point. This is the merge.
- For a session alone on its subject: present its result on its own, marked clearly as
  independent. Do NOT force it together with sessions on unrelated subjects.

Head each group with a short line naming its subject.

Rules you follow strictly:
1. Every claim about the device or about signals stays tied to the session and the command
   that produced it. You never invent a frequency, identifier or reading that no session
   actually obtained; if the sessions did not measure something, you say so rather than
   filling the gap.
2. Answer in the language the sessions used. Plain text only, no Markdown markup - no
   asterisks, hashes or backticks. Be concise: a summary of summaries, not a repeat of each
   one in full."""


def _session_block(session):
    """One session, rendered for the synthesiser's prompt."""
    lines = [f"SESSION \"{session['name']}\":"]
    request = (session.get("request") or "").strip()
    lines.append(f"asked: {request or '(not recorded)'}")
    answer = (session.get("answer") or "").strip() or "(this session produced no answer)"
    lines.append(f"final answer:\n{answer}")
    commands = session.get("commands") or []
    lines.append("commands it ran: " + (", ".join(commands) if commands else "(none)"))
    return "\n".join(lines)


def build_merge_prompt(sessions):
    """The prompt handed to the synthesiser: each session's subject, answer and commands."""
    blocks = "\n\n".join(_session_block(s) for s in sessions)
    return (
        f"Group the following {len(sessions)} sessions by subject and produce the combined "
        f"result: merge the sessions that share a subject, keep the rest independent.\n\n{blocks}"
    )

def synthesize(api_key, sessions, model=None, simulated=False):
    """Runs the synthesiser conversation over the sessions and returns its merged text.

    'sessions' is a list of dicts, each with 'name', 'answer' and 'commands' (a list of
    command names). Blocks until the model answers, so callers run it on a worker thread.
    """
    instruction = MERGE_INSTRUCTION
    if simulated:
        instruction += SIMULATED_NOTICE

    client = genai.Client(api_key=api_key)
    chat = client.chats.create(
        model=model or MODEL,
        config=types.GenerateContentConfig(
            system_instruction=instruction,
            thinking_config=(
                types.ThinkingConfig(include_thoughts=True) if INCLUDE_THOUGHTS else None
            ),
        ),
    )
    response = send_with_retry(chat, build_merge_prompt(sessions))
    return answer_text(response)
