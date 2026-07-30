# The app builder — coFlipper writes, compiles and installs Flipper apps

This document explains a capability added to coFlipper: the agent can now **build actual
Flipper Zero applications** on request. The user asks in natural language — *"build me a
simple paint app"* — and the agent researches the task on the web, writes native C source
against the Flipper SDK, compiles it into a `.fap` with the Flipper toolchain, and, when a
device is connected, installs it. The source is kept and stays editable: a later request
such as *"add a bigger brush to the paint app"* reloads the existing code and changes it.

The engine of the feature is a **three-agent debate** — a proposer, a challenger and an
arbiter — whose argument is shown to the user in the same reasoning chain the rest of the
project uses. This is the highest-complexity part of coFlipper, and it was built to fit the
project's existing principles rather than beside them.

It also documents a second, unglamorous but essential piece of work done at the same time:
**stabilising the repository**, which was in a broken, un-runnable state when this began.

---

## 1. Stabilisation: resolving a broken merge

When this work started, the repository could not run at all. Eight files carried
unresolved Git merge-conflict markers (`<<<<<<< HEAD` / `=======` / `>>>>>>>`):

    desktop/agent.py   desktop/commands.py   desktop/gui.py
    desktop/reasoning.py   desktop/mock_flipper.py
    README.md   desktop/README.md   PROTOCOL.md

Python cannot even parse a file containing those markers, so `python gui.py` failed
immediately. The two sides of the merge were two feature branches that had never been
reconciled:

- **HEAD** — the subagents and the Wi-Fi/Marauder (ESP32) feature;
- **`655a80a`** — the infrared bruteforce feature.

`commands.json` referenced **both** feature sets, so neither could simply be discarded. The
resolution was, in every case, a **union**: the two branches touched disjoint state, so the
correct merge kept both. The one file that needed real thought was
`desktop/commands.py`'s `CommandDispatcher`, where both branches had rewritten the
constructor and the `dispatch` method. The merged dispatcher now:

- takes both `subagents=` (HEAD) and `on_progress=` (IR) as keyword-defaulted parameters,
  so existing positional construction in tests and entry points keeps working;
- keeps the device lock and session log (HEAD) **and** the progress callback (IR);
- routes an agent-layer command by a single discriminator — **does it name a subagent?**
  If yes, it delegates; if no, it runs desktop-side Python. That same discriminator is what
  the new app-builder commands reuse (see §4).

For `agent.py`'s system instruction the two sides were the same rules in different
languages (English vs Romanian) plus feature-specific additions. The merge kept **one
language — English** — and folded the IR-specific rules into it, so the shipped instruction
is a single coherent block rather than two half-blocks.

**A latent bug found and fixed while stabilising.** `model_commands()` is
`device_commands() + agent_commands()`. The old `device_commands()` filtered on
`layer in ("device", "agent")`, so every agent-layer command was returned twice — once by
each function — and the model received each such tool declared twice. It was harmless
enough to go unnoticed, but it is a defect, and the new tools would have inherited it.
`device_commands()` now returns device-layer commands only, and the duplication is gone.

**Gate.** After the merge, both existing test suites pass unchanged:

    python test_reasoning.py     # 22/22
    python test_subagents.py     # 38/38

That gate is what made it safe to build a new feature on top.

---

## 2. What the feature does, end to end

    User: build me a simple paint app

    1. build_flipper_app(request="a simple paint app", app_name="Paint")
    2. research   — a grounded web search: which furi/gui/view_port/input APIs a
                    drawing app needs, and example patterns
    3. proposer   — writes application.fam + the C source, explaining its choices
    4. challenger — argues against it: an SDK call that will not compile, a missing
                    exit loop, a stack too small, a feature the user did not ask for
    5. arbiter    — keeps only what survived the argument, emits the final files
    6. ufbt build — the REAL compiler runs; exit 0 means it built
       (on failure, the real compiler error is fed back to step 3 and they argue again)
    7. install    — if a Flipper is connected, ufbt installs and launches it;
                    with no device, the compile is real but installation is simulated
    8. the source and the whole debate are saved, so the app can be changed later

Every one of steps 2–7 appears in the **reasoning chain** on the right of the window,
nested one level deep in the subagent accent colour, so the argument that produced the app
is as visible as the app itself.

---

## 3. The three-agent debate

The debate lives in [`desktop/app_builder.py`](desktop/app_builder.py). The three agents
are:

| Agent | Role |
|---|---|
| **Proposer** | Researches the task and writes the first complete C source + `application.fam`. |
| **Challenger** | Denies that the design is right, with concrete technical objections — its whole job is to attack, never to rewrite. |
| **Arbiter** | The middle agent: reads both, decides which objections are right, and emits the final files that go to the compiler. |

Each is a **separate Gemini conversation** with its own system instruction. They are
defined as `Spec` instances — the same declarative shape the subagents use — but they are
deliberately **not** registered in `subagents.SPECS`: they are the internal machinery of
one command, not tools the main agent can summon on their own.

The agents exchange a structured artifact. The proposer and arbiter emit their files
between exact markers:

    ===FAM===
    App(appid="paint", name="Paint", apptype=FlipperAppType.EXTERNAL, entry_point="paint_main", ...)
    ===END FAM===
    ===SOURCE===
    #include <furi.h>
    ... the complete C file ...
    ===END SOURCE===

so the code can be extracted mechanically and handed to the compiler. The arbiter is
required to emit the **complete** file, never a diff, because that file is what gets built.

### Why a debate, not one model

A lone model is confidently wrong in ways a critic catches: it invents an SDK function that
does not exist, forgets the Back-button exit loop, or gilds the app with features nobody
asked for. The challenger exists to catch exactly that, and the arbiter exists so that the
critique actually changes the output rather than being ignored. The pattern mirrors the
project's existing conviction — visible elsewhere as the reasoning chain and the
"never claim a reading you did not take" rule — that an assistant acting on the real world
should be **checkable**, not merely fluent.

---

## 4. How it plugs into the existing system

Nothing about the feature is bolted on the side; it reuses the project's spine.

- **The catalog is still the single source of truth.** Two commands were added to
  `commands.json`, `layer: "agent"`, `category: "appgen"`: `agent.build_flipper_app` and
  `agent.edit_flipper_app`. Neither carries a `subagent` field, which is precisely how the
  dispatcher knows to run them as desktop Python rather than as a delegation — the same
  path the IR bruteforce takes. They became model tools automatically, with no change to
  the agent loop.

- **The reasoning chain renders the debate for free.** The builder reports progress through
  the *same* `emit(kind, **fields)` callback the subagents use (`spawn`, `thought`,
  `report`). `run_turn` already translates those events into chain steps, and the GUI
  already renders them nested and colour-coded. The builder wrote **zero** new rendering
  code.

- **Honesty is inherited and extended.** Each debating agent's instruction is the shared
  `BASE_INSTRUCTION` (the "never invent a reading" preamble) plus a build-specific rule:
  *never claim the app compiled, installed or runs unless a tool result says so.* See §6.

---

## 5. Compiling and installing: the ufbt bridge

[`desktop/ufbt_runner.py`](desktop/ufbt_runner.py) is the bridge to the Flipper toolchain.
Two facts about `ufbt` shaped it:

1. **`ufbt` runs its build through `os.system`**, which writes straight to the console and
   returns nothing a caller can read. To capture the compiler's output — the whole point,
   since those errors are fed back into the debate — `ufbt` must be run as a **child
   process** with its pipes captured (`subprocess.run([... "-m", "ufbt", ...],
   capture_output=True)`), never imported and called in-process.

2. **`ufbt` takes its working directory as the application directory** (`UFBT_APP_DIR =
   getcwd`); there is no per-app flag. Isolation between apps is therefore achieved by
   running each build with `cwd` set to that app's directory.

`ufbt` writes the finished `.fap` into the **shared** SDK build tree (`~/.ufbt/build`),
keyed by appid. On a successful build the runner copies that artifact back into the app's
own `dist/` directory, so each generated app owns its binary next to its source.

### The SDK / firmware target caveat (reported, never hidden)

The Flipper used in development runs **Momentum** firmware (target 7, API 87.1), but the
`ufbt` SDK currently installed is the **official** one (target f7, API 1.4.3). A `.fap`
built against one API may be refused by firmware built on another. The runner reads the
SDK's target/API and, on a real install, this mismatch is surfaced honestly rather than
papered over — the agent will not tell the user an app "installed and works" past a known
mismatch. To build against Momentum, point `ufbt` at the Momentum SDK
(`ufbt update --index-url=https://up.momentum-fw.dev/firmware/directory.json`), optionally
in a separate `UFBT_HOME` so the official SDK is left untouched.

---

## 6. The honesty principle, applied to building

coFlipper's founding rule is that the agent never claims something about the real world
that no tool result supports. Applied to building:

- **"The app compiled"** is a claim whose only truth-maker is `ufbt`'s exit code. The
  runner reports that exit code and the captured output verbatim; the builder never decides
  on its own that a build succeeded. A non-zero exit is reported as a failure, with the real
  error text, and the agent is instructed to relay it rather than invent success.
- **"The app installed"** requires a real device and a zero exit from `ufbt install`. With
  no Flipper attached the compile is still real, but installation is explicitly reported as
  **simulated** (`simulated: true`), exactly as the rest of the mock path is marked.
- The result handed back to the main agent carries the **real compiler output** alongside
  the verdict — the same way a subagent's report travels with its raw readings — so the
  agent can check the claim instead of trusting it.

---

## 7. The persistent, editable app store

The generated source is not thrown away. [`desktop/app_store.py`](desktop/app_store.py)
keeps every app on disk so the owner can change it later. The store lives **outside the
repository** (default `~/.coflipper/apps`, overridable with `COFLIPPER_APP_STORE`), because
the generated apps are the user's, not the project's.

    <store>/index.json                   the manifest: one entry per app
    <store>/<appid>/application.fam
    <store>/<appid>/<appid>.c            the generated C source — this is what stays editable
    <store>/<appid>/dist/<appid>.fap     the compiled artifact
    <store>/<appid>/build.log            the captured output of the last ufbt run
    <store>/<appid>/history/NNNN-*.json  one record per build or edit, with the full debate

Each manifest entry tracks the appid, name, source path, build status, last exit code,
whether it is installed, and a history of every build and edit. `edit_flipper_app` looks
the app up (by id or by human name), reads its current source, and seeds the debate with
that source **as the starting artifact** plus the change requested — so editing is just
"build, starting from what already exists." App ids are sanitised to valid lowercase C
identifiers and disambiguated on collision (a second `paint` becomes `paint_2`), so two
apps never clobber each other.

---

## 8. Cost and the budgets that bound it

Each agent is a model request, and the Gemini free tier allows roughly twenty requests per
day per model. A debate is therefore bounded at every level, in `app_builder.py`:

- `MAX_DEBATE_ROUNDS` — proposer→challenger→arbiter cycles before the first compile;
- `MAX_FIX_ITERATIONS` — compile→feed-the-error-back→re-debate cycles, on failure;
- `MAX_REQUESTS` — an overall ceiling across research and every agent, whatever the loops
  do. When it is hit the build stops and reports the last real compiler error, never a
  fabricated success.

The three agents run on `COFLIPPER_BUILDER_MODEL` (falling back to
`COFLIPPER_SUBAGENT_MODEL`, then the main model), so a heavy build can be pointed at a
separate per-model quota instead of competing with the conversation.

---

## 9. Web research

Research is a **separate, single-purpose Gemini conversation** configured with only the
`google_search` grounding tool and no function declarations. This is deliberate: it
sidesteps the question of whether a given model accepts grounding and function-calling in
the same request. Its cited summary is injected into the proposer's task — the same pattern
by which the analyst subagent receives the session log — and it degrades gracefully to
"no research" rather than failing the build if grounding is unavailable (as in mock mode).

---

## 10. Testing — with no API and no hardware

[`desktop/test_app_builder.py`](desktop/test_app_builder.py) exercises the whole pipeline
without spending a single model request or invoking the real compiler, mirroring the
existing `scripted_model.py` + `test_subagents.py` pattern. The three conversations are
scripted `ScriptedChat`s and `ufbt` is replaced by a fake build runner returning prepared
`(exit_code, output)` tuples. The 27 checks cover:

1. **a clean build** — the chain opens with a spawn, the debate steps are nested at depth 1
   and attributed to each agent by name, the compiler is really invoked, and its output is
   carried back;
2. **persistence** — the source and manifest are written, the history records the build,
   and an **edit** reloads the existing source into the debate and appends a second history
   record;
3. **fail-then-fix** — a first compile fails, the **real error text reaches the proposer's
   next prompt**, and the second attempt builds;
4. **honesty** — a build that never compiles is reported as failed, with a build error, and
   its report never claims success;
5. **budget** — a debate scripted to run forever is stopped by the request ceiling and
   still returns a result rather than an exception.

    python test_app_builder.py     # 27/27

Beyond the mocked suite, the pipeline was verified against the **real `ufbt` toolchain**:
scripted agents emitting a genuine minimal FAP were compiled to an actual `.fap` (exit 0),
which confirms that the compile path — the part the mocks cannot prove — works end to end.

---

## 11. Files added and changed

**New** (`desktop/`):

| File | Role |
|---|---|
| `app_builder.py` | the three-agent debate, the compile-error feedback loop, budgets, honesty |
| `app_store.py` | the persistent, editable store of generated apps |
| `ufbt_runner.py` | the subprocess wrapper around `ufbt`, capturing real compiler output |
| `test_app_builder.py` | the scripted-chat + fake-`ufbt` test suite (27 checks) |

**Changed:** the eight merge-conflicted files were resolved; `commands.json` gained the two
`appgen` commands and lost the agent-command duplication; `.gitignore` gained the app-store
paths.

---

## 12. Trying it

    cd desktop
    python gui.py --mock        # no device needed; the compile is still real

Then ask, in the input bar: **"build me a simple paint app"**. Watch the debate unfold in
the reasoning panel, and — if `ufbt` is installed — find the compiled `.fap` under
`~/.coflipper/apps/<appid>/dist/`. Afterwards, try **"add a bigger brush to the paint
app"** to see the edit path reload and change the same source.
