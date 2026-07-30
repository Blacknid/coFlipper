"""The app builder: three agents that argue an app into existence, then compile it.

The user asks for an app - 'a simple paint app' - and instead of a single model writing
some C and hoping, three separate agents debate it:

  - the PROPOSER researches the task and writes the C source and FAP manifest;
  - the CHALLENGER argues against that design, with concrete objections - what will not
    compile, what the SDK does not offer, what the user did not ask for;
  - the ARBITER reads both and decides, producing the version that goes to the compiler.

The debate is not decoration. A lone model is confidently wrong in ways a critic catches,
and the arbiter's job is precisely to keep only what survived the argument. When the
compiler then rejects the result, its real errors are fed back and the three argue again,
up to a bounded number of attempts.

This module reuses the project's existing machinery rather than inventing its own:

  - the honesty preamble and the Spec data shape come from subagents.py, because a
    separate conversation inherits nothing and would otherwise be the one component free
    to claim an app compiled when it did not;
  - progress is reported through the very same emit(kind, **fields) callback the subagents
    use, so the three agents appear nested in the reasoning chain, in the subagent accent
    colour, with zero new rendering code - the argument that produced an app is as visible
    as the app itself;
  - compiling and installing go through ufbt_runner.py, whose result - an exit code and the
    captured compiler output - is the only truth-maker for 'it built'. The builder never
    decides on its own that a build succeeded.

Cost is real: each agent is a model request, and the free tier allows about twenty a day
per model. Every loop is bounded (max_debate_rounds, max_fix_iterations) and an overall
request ceiling backstops the lot, so a build cannot silently consume the whole quota. The
three agents run on COFLIPPER_BUILDER_MODEL (falling back to the subagent model, then the
main one), so the builder can be pointed at a separate quota from the conversation.
"""

import os
import re

from google import genai
from google.genai import types

from agent import MODEL, answer_text, send_with_retry, thought_texts
from app_store import AppStore, sanitize_appid
from subagents import BASE_INSTRUCTION, SUBAGENT_MODEL, Spec
from ufbt_runner import UfbtRunner, sdk_target_api

# The builder's own model knob. It falls back to the subagent model and then the main one,
# so nothing has to be configured for it to work, but a heavy build can be isolated onto a
# separate per-model quota by setting it.
BUILDER_MODEL = os.environ.get("COFLIPPER_BUILDER_MODEL", SUBAGENT_MODEL)

# The debate is bounded at every level. These defaults were chosen against the free-tier
# quota: a clean build costs research + 3 agents = 4 requests; each fix iteration adds
# roughly 3 more. The ceiling stops a pathological loop from eating the day's allowance.
MAX_DEBATE_ROUNDS = 1  # proposer -> challenger -> arbiter cycles before the first compile
MAX_FIX_ITERATIONS = 3  # compile -> feed the error back -> re-debate, on failure
MAX_REQUESTS = 14  # hard ceiling across research + every agent, whatever the loops do

# The honesty rules specific to building. Appended to the shared BASE_INSTRUCTION so the
# builder's agents carry both the project-wide 'never invent a reading' rule and the
# build-specific 'never invent a successful compile' one.
BUILDER_HONESTY = """
You are helping build a real Flipper Zero application in C. Rules that bind you:
- You never claim the app compiled, installed or runs unless a tool result you were given
  says so. A compiler error is reported and fixed, never hidden or wished away.
- You write real C against the Flipper firmware API (furi, gui, view_port, input). You do
  not invent SDK functions; if unsure whether one exists, you say so and choose a safer one.
- You answer in plain text, no Markdown. When you are asked for source code, you return the
  complete file between the exact markers requested, and nothing outside them matters.
"""

PROPOSER = Spec(
    key="proposer",
    name="proposer",
    role="researches the app and writes the first version of its C source",
    instruction="""You are the PROPOSER. Given a request for a Flipper Zero app, and any
web research handed to you, you design the app and write it in full.

You produce two files, each complete and each between its markers, with nothing else that
matters outside them:

===FAM===
<the application.fam manifest: App(appid=..., name=..., apptype=FlipperAppType.EXTERNAL,
entry_point=..., stack_size=..., fap_category=..., fap_version=...)>
===END FAM===

===SOURCE===
<the complete C source: includes, an entry point named exactly as in the manifest, a
view_port and gui, an input loop that exits on the Back button, and whatever the app does>
===END SOURCE===

Keep it small and correct over large and broken. A paint app is a canvas you move a cursor
on with the arrow keys and draw with OK; it does not need saving to disk to be a first
version. Explain your key choices briefly before the markers, since the arbiter reads them.""",
)

CHALLENGER = Spec(
    key="challenger",
    name="challenger",
    role="argues against the proposed design, with concrete technical objections",
    instruction="""You are the CHALLENGER. Your job is to deny that the proposer's design
is right, and to say why, concretely. You do not write the app; you attack it.

Look for: SDK functions that may not exist or are misused; an entry point that does not
match the manifest; a missing or wrong input/exit loop; a stack too small; things the user
asked for that are absent; things present that the user did not ask for and that add risk.
If a compiler error from a previous attempt is included, center your critique on its real
cause rather than guessing.

Be specific and technical - 'furi_record_open expects the record name RECORD_GUI, not a
literal' beats 'the GUI code looks off'. If, after genuinely trying, you find the design
sound, say so plainly and name the one thing most likely to still go wrong. You never
rewrite the code yourself; you argue.""",
)

ARBITER = Spec(
    key="arbiter",
    name="arbiter",
    role="weighs the proposal against the critique and produces the version that is compiled",
    instruction="""You are the ARBITER, the middle agent. You read the proposer's design
and the challenger's objections and you decide: which objections are right and must be
fixed, which are wrong and why, and what the resulting app actually is.

You output the FINAL, complete files that will be sent to the compiler, each between its
markers exactly as the proposer used them:

===FAM===
<final application.fam>
===END FAM===

===SOURCE===
<final complete C source>
===END SOURCE===

You keep only what survives the argument. If the challenger was right, you fix it; if the
challenger was wrong, you keep the proposer's version and note briefly why. The code you
emit is the code that gets built, so it must be complete and self-contained - never a diff,
never 'the same but with X changed'. Before the markers, state in one or two sentences what
you kept, what you changed, and why.""",
)

# The three are defined as Specs for the same reason everything else is - one declarative
# shape - but they are NOT registered in subagents.SPECS: they are not delegatable tools the
# main agent can summon, they are the internal machinery of one command.
DEBATE = (PROPOSER, CHALLENGER, ARBITER)

_FAM_RE = re.compile(r"===FAM===\s*(.*?)\s*===END FAM===", re.DOTALL)
_SOURCE_RE = re.compile(r"===SOURCE===\s*(.*?)\s*===END SOURCE===", re.DOTALL)


def _extract(text, regex):
    m = regex.search(text or "")
    return m.group(1).strip() if m else None


class BudgetError(Exception):
    """Raised when the overall request ceiling is hit, so the loop can stop cleanly."""


class AppBuilder:
    """Runs the three-agent debate, compiles the result, and (when possible) installs it.

    The seams for testing mirror SubagentRunner exactly: _chat(spec) creates a model
    conversation and can be overridden to return a scripted one; the build runner is
    injected through UfbtRunner(build_runner=...). With both replaced, the whole pipeline -
    debate, compile-error feedback, persistence - runs with no API and no toolchain.
    """

    def __init__(self, dispatcher, simulated=False, store=None, ufbt=None,
                 model=None, api_key=None):
        self._dispatcher = dispatcher
        self._simulated = simulated
        self._store = store or AppStore()
        self._ufbt = ufbt or UfbtRunner()
        self.model = model or BUILDER_MODEL
        # The client is created lazily and only if a real chat is needed, so a fully
        # scripted test never requires an API key.
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self._genai = None
        self._requests = 0

    # -- model plumbing (the test seam) -----------------------------------

    def _client(self):
        if self._genai is None:
            self._genai = genai.Client(api_key=self._api_key)
        return self._genai

    def _chat(self, spec):
        """A fresh conversation for one debating agent. Overridden in tests."""
        instruction = BASE_INSTRUCTION + "\n" + BUILDER_HONESTY + "\n" + spec.instruction
        return self._client().chats.create(
            model=self.model,
            config=types.GenerateContentConfig(system_instruction=instruction),
        )

    def _ask(self, spec, message, emit, round_no):
        """One request to one agent, its reasoning and reply emitted into the chain.

        Counts against the overall ceiling and raises BudgetError when it is exceeded, so a
        runaway debate stops instead of draining the quota.
        """
        if self._requests >= MAX_REQUESTS:
            raise BudgetError(f"the build's request budget of {MAX_REQUESTS} was reached")
        self._requests += 1
        chat = self._chats[spec.key]
        response = send_with_retry(chat, message)
        for thought in thought_texts(response):
            _emit(emit, "thought", text=thought, source=spec.name)
        reply = answer_text(response)
        _emit(emit, "report", name=spec.name,
              text=_summary(reply), meta={"round": round_no, "role": spec.role})
        return reply

    def _research(self, request, emit):
        """A grounded web search for how to build this app, injected into the proposer.

        Deliberately its own single-purpose conversation with ONLY the google_search tool
        and no function declarations, which sidesteps the question of whether the model
        accepts grounding and function-calling together. In simulated mode, or when no
        client is available, it degrades to no research rather than failing the build.
        Overridable in tests.
        """
        try:
            chat = self._client().chats.create(
                model=self.model,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
            self._requests += 1
            response = send_with_retry(
                chat,
                f"Research how to build this Flipper Zero app in C against the current "
                f"firmware SDK: {request}. Summarise the relevant furi/gui/view_port/input "
                f"APIs and any example patterns, concisely, for another agent to use.",
            )
            summary = answer_text(response) or "(no research returned)"
        except Exception as exc:  # noqa: BLE001 - research is best-effort, never fatal
            summary = f"(web research unavailable: {exc})"
        _emit(emit, "thought", text=_summary(summary), source="research")
        return summary

    # -- the debate --------------------------------------------------------

    def _debate(self, request, prior_source, prior_fam, research, compiler_error, emit, round_no):
        """One full proposer -> challenger -> arbiter cycle. Returns (fam, source, arbiter_text)."""
        starting = ""
        if prior_source:
            starting = (
                "\n\nThe app already exists. Start from its current files and apply the "
                f"change.\n\n===CURRENT FAM===\n{prior_fam}\n===CURRENT SOURCE===\n{prior_source}\n"
            )
        error_note = ""
        if compiler_error:
            error_note = (
                "\n\nThe previous version FAILED to compile. The real compiler output was:\n"
                f"{compiler_error}\nFix the actual cause."
            )

        proposal = self._ask(
            PROPOSER,
            f"Request: {request}\n\nWeb research:\n{research}{starting}{error_note}",
            emit, round_no,
        )
        critique = self._ask(
            CHALLENGER,
            f"Request: {request}\n\nThe proposer's design and code:\n{proposal}{error_note}",
            emit, round_no,
        )
        decision = self._ask(
            ARBITER,
            f"Request: {request}\n\nProposer:\n{proposal}\n\nChallenger:\n{critique}{error_note}",
            emit, round_no,
        )
        fam = _extract(decision, _FAM_RE) or prior_fam or _extract(proposal, _FAM_RE)
        source = _extract(decision, _SOURCE_RE) or _extract(proposal, _SOURCE_RE) or prior_source
        return fam, source, decision

    # -- the public entry points ------------------------------------------

    def build(self, request, app_name=None, emit=None):
        if not request:
            return {"status": "error", "error": "the 'request' argument is required"}
        appid = self._store.allocate_appid(app_name or request)
        name = app_name or appid
        return self._run(request, appid, name, kind="build",
                         prior_source=None, prior_fam=None, emit=emit)

    def edit(self, app_name, change_request, emit=None):
        if not app_name or not change_request:
            return {"status": "error", "error": "both 'app_name' and 'change_request' are required"}
        entry = self._store.get(app_name)
        if entry is None:
            return {"status": "error",
                    "error": f"no app called '{app_name}' has been built yet; build it first"}
        appid = entry["appid"]
        prior_source, prior_fam = self._store.read_source(appid)
        return self._run(change_request, appid, entry.get("name", appid), kind="edit",
                         prior_source=prior_source, prior_fam=prior_fam, emit=emit)

    def _run(self, request, appid, name, kind, prior_source, prior_fam, emit):
        """The whole pipeline: debate, compile, feed errors back, install, persist.

        The result dict is what the main agent receives. It carries the real compiler exit
        code and output, so the agent can neither claim a failed build succeeded nor be
        asked to take the builder's word for it.
        """
        _emit(emit, "spawn", name="app_builder", task=f"{kind}: {request}",
              meta={"role": f"three-agent app {kind}", "model": self.model,
                    "tools": ["proposer", "challenger", "arbiter", "ufbt"],
                    "max_rounds": MAX_DEBATE_ROUNDS + MAX_FIX_ITERATIONS})

        self._chats = {spec.key: self._chat(spec) for spec in DEBATE}
        transcript = {"request": request, "kind": kind, "rounds": []}

        try:
            research = self._research(request, emit)
        except BudgetError as exc:
            return self._finish(appid, name, kind, request, None, None, transcript,
                                emit, error=str(exc))

        fam = prior_fam
        source = prior_source
        compiler_error = None
        build_result = None
        total_rounds = MAX_DEBATE_ROUNDS + MAX_FIX_ITERATIONS

        for round_no in range(1, total_rounds + 1):
            try:
                fam, source, decision = self._debate(
                    request, source, fam, research, compiler_error, emit, round_no)
            except BudgetError as exc:
                return self._finish(appid, name, kind, request, source, fam, transcript,
                                    emit, build_result=build_result, error=str(exc))
            transcript["rounds"].append({"round": round_no, "arbiter": decision})

            if not source:
                return self._finish(appid, name, kind, request, source, fam, transcript,
                                    emit, error="the debate produced no source code")

            self._store.write_source(appid, name, source, fam or _default_fam(appid, name))
            build_result = self._ufbt.build(self._store.app_dir(appid))
            _emit(emit, "thought",
                  text=f"compiler exit {build_result['exit_code']} "
                       f"({'built' if build_result['built'] else 'failed'})",
                  source="ufbt")

            if build_result["built"]:
                return self._finish(appid, name, kind, request, source, fam, transcript,
                                    emit, build_result=build_result, install=True)
            # Failed: feed the real error into the next round, if any remain.
            compiler_error = build_result["output"]

        # Every attempt failed. Report the last real error; never claim success.
        return self._finish(appid, name, kind, request, source, fam, transcript,
                            emit, build_result=build_result,
                            error="could not reach a clean build within the attempt budget")

    def _finish(self, appid, name, kind, request, source, fam, transcript, emit,
                build_result=None, install=False, error=None):
        built = bool(build_result and build_result.get("built"))
        installed = False
        install_note = None
        target_match = None

        if built and install:
            if self._simulated:
                # No physical device: the compile was real, the install is not. Say so.
                install_note = "installation simulated: no physical Flipper is connected"
            else:
                target, api = sdk_target_api()
                install_result = self._ufbt.install(self._store.app_dir(appid), launch=True)
                installed = install_result["installed"]
                target_match = None  # the honest default: unknown unless a device reported
                if not installed:
                    install_note = "the app compiled but installation failed: " + \
                        install_result["output"][-400:]

        # Persist the outcome and the debate behind it.
        self._store.record_build(
            appid, kind=kind, request=request,
            exit_code=(build_result or {}).get("exit_code"),
            built=built, installed=installed,
            transcript=transcript,
            build_log=(build_result or {}).get("output"),
        )

        report = _final_report(kind, name, built, installed, install_note, error, self._simulated)
        _emit(emit, "report", name="app_builder", text=report,
              meta={"commands": self._requests, "rounds": len(transcript["rounds"]),
                    "truncated": bool(error)})

        result = {
            "status": "ok",
            "app_builder": kind,
            "appid": appid,
            "name": name,
            "built": built,
            "installed": installed,
            "exit_code": (build_result or {}).get("exit_code"),
            # The real compiler output travels with the verdict, like a subagent's evidence,
            # so the main agent can check the claim instead of trusting it.
            "compiler_output": (build_result or {}).get("output"),
            "report": report,
            "requests_used": self._requests,
        }
        if build_result and build_result.get("fap_path"):
            result["fap_path"] = build_result["fap_path"]
        if install_note:
            result["install_note"] = install_note
        if target_match is not None:
            result["target_match"] = target_match
        if error:
            result["build_error"] = error
        if self._simulated:
            result["simulated"] = True
        return result


# -- small helpers, kept free of state --------------------------------------


def _emit(emit, kind, **fields):
    if emit:
        emit(kind, **fields)


def _summary(text, limit=1400):
    """A debate turn can be a whole C file; the chain shows a bounded summary of it."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...(truncated in the chain; full text saved to history)"


def _default_fam(appid, name):
    """A minimal manifest, used only if the arbiter somehow emitted source but no manifest."""
    return (
        "App(\n"
        f'    appid="{appid}",\n'
        f'    name="{name}",\n'
        "    apptype=FlipperAppType.EXTERNAL,\n"
        f'    entry_point="{appid}_main",\n'
        "    stack_size=2 * 1024,\n"
        '    fap_category="Misc",\n'
        '    fap_version="1.0",\n'
        ")\n"
    )


def _final_report(kind, name, built, installed, install_note, error, simulated):
    """The prose report handed back to the main agent, honest about what really happened."""
    verb = "built" if kind == "build" else "changed and rebuilt"
    if not built:
        detail = error or "the compiler rejected it"
        return (f"The app '{name}' was NOT {verb}: {detail}. The real compiler output is in "
                "compiler_output; relay the error to the user rather than claiming success.")
    parts = [f"The app '{name}' compiled successfully (compiler exit 0)."]
    if installed:
        parts.append("It was installed and launched on the connected Flipper.")
    elif install_note:
        parts.append(install_note + ".")
    if simulated:
        parts.append("This ran in simulated mode: the compile was real, the device was not.")
    parts.append("The source is saved and can be changed later with edit_flipper_app.")
    return " ".join(parts)
