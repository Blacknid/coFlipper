"""The bridge between the app builder and the Flipper toolchain (ufbt).

The builder writes C source; this module turns it into a .fap and puts it on the device.
Two facts about ufbt shape everything here:

1. ufbt runs its build through os.system internally, which writes straight to the console
   and returns nothing a caller can read. To capture the compiler's output - the whole
   point, since those errors are fed back into the debate - ufbt must be run as a CHILD
   PROCESS with its pipes captured, never imported and called in-process.

2. ufbt takes its working directory as the application directory (UFBT_APP_DIR = getcwd).
   There is no per-app flag. Isolation between apps is therefore achieved by running each
   build with cwd set to that app's directory; its build tree lands there and nowhere else.

The honesty rule the whole project rests on applies directly to building: 'the app
compiled' is a claim, and its only truth-maker is ufbt's exit code. This module reports
that exit code and the captured output verbatim. It never decides on its own that a build
succeeded, and the builder is forbidden from claiming success past a non-zero code.
"""

import os
import pathlib
import re
import shutil
import subprocess
import sys

# A build of a small app is quick, but a first-ever build may download the SDK; a
# generous ceiling keeps a genuinely stuck toolchain from hanging the whole application.
BUILD_TIMEOUT_S = 600
INSTALL_TIMEOUT_S = 180
STATUS_TIMEOUT_S = 60

# ufbt's output can be large. The slice fed back to the model is bounded so a wall of SCons
# noise does not swamp the actual error or blow the request size; the tail is kept because
# the compiler's error lines come at the end.
MAX_OUTPUT_CHARS = 6000


def _tail(text, limit=MAX_OUTPUT_CHARS):
    if len(text) <= limit:
        return text
    return "...(truncated)...\n" + text[-limit:]


def _run(args, cwd, timeout, env=None):
    """One ufbt invocation as a child process, output captured.

    Returns (exit_code, combined_output). A timeout or a failure to launch is reported as a
    non-zero result with the reason in the output, not raised: to the builder every failure
    is the same kind of thing - a build that did not succeed, with text explaining why.
    """
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "ufbt", *args],
            cwd=str(cwd),
            env=run_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") + (exc.stderr or "")
        return 1, f"ufbt {' '.join(args)} timed out after {timeout}s\n{partial}"
    except OSError as exc:
        return 1, f"could not launch ufbt: {exc}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def real_build_runner(app_dir, action, env=None):
    """The default build runner: really invokes ufbt. Injected so tests can replace it.

    action is 'build', 'install' or 'launch'. Returns (exit_code, output). The builder
    treats this as an opaque callable, so a test passes a fake one returning scripted
    tuples and never touches the toolchain.
    """
    timeout = BUILD_TIMEOUT_S if action == "build" else INSTALL_TIMEOUT_S
    return _run([action], app_dir, timeout, env=env)


def sdk_target_api(env=None):
    """The (target, api) the installed SDK builds against, or (None, None) if unreadable.

    Read from 'ufbt status', which prints the configured target and version. Used to warn
    when the SDK does not match the firmware on the connected device, rather than shipping
    a .fap that the device may silently refuse.
    """
    # ufbt status needs a cwd; the store root is irrelevant to it, so the current one serves.
    code, out = _run(["status"], os.getcwd(), STATUS_TIMEOUT_S, env=env)
    if code != 0:
        return None, None
    target = _match(out, r"Target\s+(\S+)")
    api = _match(out, r"(?:API|Version)\s+(\S+)")
    return target, api


def _match(text, pattern):
    m = re.search(pattern, text)
    return m.group(1) if m else None


class UfbtRunner:
    """Compiles and installs an app, reporting the real toolchain result.

    build_runner is the seam for testing: by default it is real_build_runner, which shells
    out to ufbt; a test injects a callable returning scripted (exit_code, output) tuples.
    """

    def __init__(self, build_runner=None, env=None):
        self._run = build_runner or real_build_runner
        self._env = env

    def build(self, app_dir):
        """Compiles the app in app_dir. Returns a result dict; 'built' is exit_code == 0.

        ufbt writes the compiled .fap into the shared SDK build tree keyed by appid, not
        into app_dir. On success the artifact is copied back into app_dir/dist, so each
        generated app owns its own binary next to its source rather than leaving it in a
        shared location where a same-named app could overwrite it.
        """
        code, output = self._run(str(app_dir), "build", self._env)
        result = {
            "action": "build",
            "exit_code": code,
            "built": code == 0,
            "output": _tail(output),
        }
        if code == 0:
            fap = self._collect_fap(app_dir)
            if fap:
                result["fap_path"] = str(fap)
        return result

    def _collect_fap(self, app_dir):
        """Copies the freshly built .fap from the SDK build tree into app_dir/dist.

        Best-effort: a failure to locate or copy the artifact does not turn a successful
        compile into a failure, since the exit code already established that it built.
        """
        app_dir = pathlib.Path(app_dir)
        appid = app_dir.name
        build_root = pathlib.Path(os.environ.get("UFBT_HOME", pathlib.Path.home() / ".ufbt")) / "build"
        source = build_root / f"{appid}.fap"
        if not source.exists():
            return None
        try:
            dist = app_dir / "dist"
            dist.mkdir(parents=True, exist_ok=True)
            target = dist / f"{appid}.fap"
            shutil.copy2(source, target)
            return target
        except OSError:
            return None

    def install(self, app_dir, launch=False):
        """Installs (and optionally launches) the app on a connected Flipper.

        'installed' is exit_code == 0. This needs a real device on USB; the builder only
        calls it when one is present, and reports installation as simulated otherwise.
        """
        action = "launch" if launch else "install"
        code, output = self._run(str(app_dir), action, self._env)
        return {
            "action": action,
            "exit_code": code,
            "installed": code == 0,
            "output": _tail(output),
        }
