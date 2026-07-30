"""Temporary scripts: short pieces of Python the agent writes and runs on the spot.

The catalog commands cover the operations the project anticipated. But an agent driving a
device meets situations no single command captures: poll a frequency until something
arrives, chain a handful of reads under a condition, time a sequence, retry until a value
settles. Rather than force every such case into a new catalog entry, the agent can write a
short script for exactly the case in front of it and run it once.

The script runs ENTIRELY ON THE HOST MACHINE - it is a subprocess of the desktop app, never
anything that runs on the Flipper. The Flipper is a resource-constrained microcontroller and
cannot host loops, timers or many concurrent operations; so all of that stays here, and the
device only ever receives individual CFP commands. And it receives them one at a time: every
flipper.request() goes through the same CommandDispatcher.dispatch_device whose device lock
serialises the serial port, so a script that fires reads in a tight loop still reaches the
Flipper as an orderly sequence of single commands, not a flood of overlapping ones.

The safety of that rests on two walls, because letting a model execute code it wrote is
otherwise the most dangerous thing in the whole system.

1. The script runs in a SEPARATE PROCESS, launched with Python's isolated mode (-I), with a
   hard wall-clock timeout the parent enforces by killing the child. A script that loops
   forever, or blocks, cannot hang the application: it is terminated and reported as timed
   out.

2. Inside that process the script reaches almost nothing. It gets a restricted set of
   builtins (no open, no __import__ of arbitrary modules, no eval/exec), a small allowlist
   of harmless stdlib modules (time, math, json, random, statistics), and ONE capability
   that matters: a `flipper` object with a single method, request(), which is the only way
   out of the sandbox. It cannot touch the filesystem, the network, os or subprocess.

The `flipper` object does not hold a device connection - the child process has none. Each
request() call is marshalled back to the PARENT over the pipe, executed there through the
real CommandDispatcher.dispatch_device (so it is logged, simulated-marked and confined to
device-layer commands exactly like a subagent's call), and the result marshalled back. The
child only ever sees JSON going out and JSON coming in; the parent stays in control of the
one door to the hardware. A transmitting command reached this way is still a device command
and still subject to the authorization the catalog demands - the model is instructed to run
the authorization gate before a script that transmits, just as it would before calling the
transmit command directly.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading

# The wall-clock ceiling for one script, in seconds. A script exists to do a bounded piece
# of work (poll for a few seconds, chain a handful of reads); it is not a place to park a
# long-running loop. The agent may ask for less, never more.
DEFAULT_TIMEOUT_S = 20
MAX_TIMEOUT_S = 60

# The stdlib modules a script may import. All are pure computation or timing - none can reach
# the filesystem, the network or the process table. `os`, `subprocess`, `socket`, `pathlib`
# and friends are deliberately absent.
ALLOWED_MODULES = ("time", "math", "json", "random", "statistics")

# Lines the child writes to stdout that begin with this marker are not script output: they
# are requests for a device command, to be served by the parent. Anything else the child
# prints is the script's own output, collected and returned to the model.
_REQ = "\x1e\x1eCFP_REQ\x1e\x1e"
_RESP = "\x1e\x1eCFP_RESP\x1e\x1e"

# The preamble exec'd in the child before the agent's code. It builds the restricted
# environment and the flipper proxy, then runs the user code inside it. It is deliberately
# self-contained: the child imports nothing of the project, only what is inlined here.
_CHILD_HARNESS = r'''
import sys, json, builtins

_REQ = "\x1e\x1eCFP_REQ\x1e\x1e"
_RESP = "\x1e\x1eCFP_RESP\x1e\x1e"
_ALLOWED = %(allowed)r

# The one door out of the sandbox: a device request, marshalled to the parent and back.
class _Flipper:
    def request(self, command, *args):
        """Send a CFP command to the device and return its result dict.

        command is a device-layer name (dotted 'subghz.read' or underscored 'subghz_read');
        args are its positional arguments. Returns the same {'status': 'ok'|'error', ...}
        dict the rest of the system uses, so a script checks result['status'] and reads
        result['data'] exactly as a tool result is read.
        """
        payload = json.dumps({"command": str(command), "args": [str(a) for a in args]})
        # Requests go on the real stdout with a marker; the parent picks them out of the
        # stream. Everything else printed is the script's output.
        sys.__stdout__.write(_REQ + payload + "\n")
        sys.__stdout__.flush()
        line = sys.__stdin__.readline()
        if not line:
            raise RuntimeError("device link closed")
        tag, _, body = line.partition(_RESP)
        if not body:
            raise RuntimeError("bad response from device link")
        return json.loads(body)

def _guarded_import(name, *a, **k):
    root = name.split(".")[0]
    if root not in _ALLOWED:
        raise ImportError(
            "module %%r is not available to a script (allowed: %%s)" %% (name, ", ".join(_ALLOWED))
        )
    return _real_import(name, *a, **k)

_real_import = builtins.__import__

# A restricted builtins map: the everyday names a script needs, minus everything that opens a
# file, reaches the interpreter internals, or runs more code. No open, eval, exec, compile,
# input, globals, vars, or __loader__ tricks.
_SAFE_BUILTIN_NAMES = (
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter", "float", "format",
    "frozenset", "hex", "int", "isinstance", "issubclass", "len", "list", "map", "max", "min",
    "oct", "ord", "chr", "pow", "print", "range", "repr", "reversed", "round", "set", "slice",
    "sorted", "str", "sum", "tuple", "type", "zip", "True", "False", "None",
    "Exception", "ValueError", "TypeError", "RuntimeError", "KeyError", "IndexError",
    "StopIteration", "ZeroDivisionError",
)
_safe_builtins = {n: getattr(builtins, n) for n in _SAFE_BUILTIN_NAMES if hasattr(builtins, n)}
_safe_builtins["__import__"] = _guarded_import

_SOURCE = json.loads(sys.__stdin__.readline())

# The script prints to a captured buffer routed to the same stdout, but the flipper proxy
# writes control lines to sys.__stdout__ directly, so the two never collide.
_env = {"__builtins__": _safe_builtins, "flipper": _Flipper()}
try:
    exec(compile(_SOURCE, "<script>", "exec"), _env)
except SystemExit:
    pass
except BaseException as exc:
    import traceback
    sys.__stdout__.write("\n[script error] " + "".join(
        traceback.format_exception_only(type(exc), exc)).strip() + "\n")
    sys.__stdout__.flush()
    sys.exit(1)
'''


def _clamp_timeout(timeout):
    try:
        t = int(timeout)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    return max(1, min(t, MAX_TIMEOUT_S))


def run_script(code, dispatcher, purpose="", timeout=DEFAULT_TIMEOUT_S, on_progress=None):
    """Run one temporary script in a sandboxed subprocess and return a result dict.

    code       the Python source the agent wrote.
    dispatcher the CommandDispatcher; the script's flipper.request() calls are served by its
               dispatch_device, so a script reaches the device on exactly the terms a
               subagent does - device-layer commands only, logged and simulated-marked.
    purpose    a one-line note from the agent on what the script is for, echoed back.
    timeout    wall-clock ceiling in seconds, clamped to [1, MAX_TIMEOUT_S].

    The result carries 'status', the captured 'output', the list of device 'commands' the
    script ran (so the model can check the script's claims against the real readings, the
    same guarantee a subagent's evidence gives), and whether it 'timed_out'.
    """
    code = (code or "").strip()
    if not code:
        return {"status": "error", "error": "the 'code' argument is required and cannot be empty"}

    timeout = _clamp_timeout(timeout)
    harness = _CHILD_HARNESS % {"allowed": list(ALLOWED_MODULES)}

    # The harness is written to a temp file the child runs; the agent's own code travels over
    # stdin as JSON, never touching disk, so nothing on disk is executable script text.
    with tempfile.NamedTemporaryFile(
        "w", suffix="_cfp_harness.py", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(harness)
        harness_path = handle.name

    evidence = []
    output_lines = []
    timed_out = False

    try:
        # -I: isolated mode - ignores environment variables and the user site directory, so
        # the child cannot be steered by PYTHON* env vars or a planted sitecustomize.
        proc = subprocess.Popen(
            [sys.executable, "-I", harness_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

        # The child blocks on its first readline for the source, so hand it over first.
        proc.stdin.write(json.dumps(code) + "\n")
        proc.stdin.flush()

        def pump():
            """Read the child's stdout: serve device requests, collect everything else."""
            for line in proc.stdout:
                if line.startswith(_REQ):
                    payload = json.loads(line[len(_REQ):])
                    name = payload["command"].replace(".", "_")
                    args = payload["args"]
                    # Positional args arrive as a list; dispatch_device takes a dict keyed by
                    # the command's parameter names, so map by position from the catalog.
                    call_args = _args_by_position(dispatcher, name, args)
                    outcome = dispatcher.dispatch_device(name, call_args)
                    evidence.append({"command": name, "args": call_args, "result": outcome})
                    if on_progress:
                        on_progress(name, outcome)
                    try:
                        proc.stdin.write(_RESP + json.dumps(outcome) + "\n")
                        proc.stdin.flush()
                    except (BrokenPipeError, ValueError):
                        break
                else:
                    output_lines.append(line.rstrip("\n"))

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        exit_code = None
        try:
            exit_code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            proc.wait()
        reader.join(timeout=1)

        stderr = (proc.stderr.read() or "").strip() if proc.stderr else ""
    finally:
        try:
            os.unlink(harness_path)
        except OSError:
            pass

    output = "\n".join(output_lines).strip()
    # The child exits non-zero when the script raised (the harness catches it, prints a
    # '[script error] ...' line and exits 1). That is a failed run, not a successful one, so
    # it is reported as an error - the model should see plainly that its script did not work.
    crashed = not timed_out and exit_code not in (0, None)
    result = {
        "status": "error" if (timed_out or crashed) else "ok",
        "purpose": purpose,
        "output": output or "(the script printed nothing)",
        # Every device command the script ran, with its result: the evidence the model checks
        # the script's printed claims against, so a script cannot become a source of an
        # unbacked measurement any more than a subagent can.
        "commands": evidence,
        "command_count": len(evidence),
        "timed_out": timed_out,
    }
    if timed_out:
        result["error"] = f"the script exceeded its {timeout}s time limit and was stopped"
    elif crashed:
        # The '[script error] ...' line the harness printed is already in output; point at it
        # rather than duplicating, so the model reads the actual exception once.
        result["error"] = "the script raised an error (see output)"
    if stderr:
        # Isolated-mode startup complaints or a hard crash; surfaced, not hidden.
        result["stderr"] = stderr[:2000]
    return result


def _args_by_position(dispatcher, name, args):
    """Turn a script's positional request args into the keyword dict dispatch_device wants.

    The script calls flipper.request('subghz.read', 433920000); the catalog says subghz.read's
    first parameter is 'frequency', so this produces {'frequency': 433920000}. A command with
    no declared parameters, or extra positional args, still passes them through under
    positional keys so nothing the script sent is silently dropped.
    """
    command = getattr(dispatcher, "_by_tool_name", {}).get(name)
    params = [a["name"] for a in (command or {}).get("args", [])] if command else []
    call_args = {}
    for i, value in enumerate(args):
        key = params[i] if i < len(params) else f"arg{i}"
        call_args[key] = value
    return call_args
