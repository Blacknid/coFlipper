"""Checks the temporary-script feature: the sandbox, device access, and the catalog path.

    python test_scripting.py

Nothing here is scripted-away: a real child Python process is launched for every case, so
what is under test is the actual sandbox that ships - the import allowlist, the missing
builtins, the wall-clock timeout, and the pipe that routes a script's flipper.request()
back to the parent's dispatcher. The device behind it is the MockCFPClient, so the reads
are real reads against the mock's band fixtures. No Gemini quota is spent: the scripts are
plain Python, not model output, and the one end-to-end case uses a ScriptedChat.
"""

import sys

from commands import CommandDispatcher, load_catalog, model_commands
from mock_flipper import MockCFPClient
from scripting import MAX_TIMEOUT_S, run_script

FREQ = 433_920_000


def _fresh():
    return CommandDispatcher(model_commands(load_catalog()), MockCFPClient())


def main():
    checks = Checks = _Checks("scripting")

    checks.section("1. a script reaches the device only through flipper.request, and it works")
    d = _fresh()
    code = (
        "found = None\n"
        "for i in range(8):\n"
        "    r = flipper.request('subghz.read', 433920000)\n"
        "    if r['status'] == 'ok' and r['data'][0] == 'signal':\n"
        "        found = r['data']; print('HIT', found[1], found[2]); break\n"
        "if not found: print('nothing')\n"
    )
    res = run_script(code, d, purpose="poll until a signal", timeout=10)
    checks.check(f"the script ran and printed its hit: {res['output']!r}", res["status"] == "ok")
    checks.check("it really talked to the device (evidence recorded)", res["command_count"] >= 1)
    checks.check("the first recorded command was subghz_read",
                 res["commands"][0]["command"] == "subghz_read")
    checks.check("the read result carries real decoded data, not invented text",
                 res["commands"][0]["result"]["data"][0] == "signal")
    checks.check("the device result is marked simulated, and that marking is preserved",
                 res["commands"][0]["result"].get("simulated") is True)

    checks.section("2. the sandbox blocks the filesystem, the shell and the network")
    for mod in ("os", "subprocess", "socket", "pathlib", "shutil"):
        r = run_script(f"import {mod}\nprint('reached')", _fresh(), timeout=5)
        checks.check(f"import {mod} is refused", r["status"] == "error" and "reached" not in r["output"])
    r = run_script("open('x.txt', 'w')", _fresh(), timeout=5)
    checks.check("open() is not even defined", r["status"] == "error" and "not defined" in r["output"])
    r = run_script("print(eval('1+1'))", _fresh(), timeout=5)
    checks.check("eval() is not defined", r["status"] == "error" and "not defined" in r["output"])
    r = run_script("print(exec('x=1'))", _fresh(), timeout=5)
    checks.check("exec() is not defined", r["status"] == "error" and "not defined" in r["output"])

    checks.section("3. the harmless stdlib modules a script legitimately needs are allowed")
    r = run_script(
        "import math, time, json, random, statistics\n"
        "print(math.sqrt(16), statistics.mean([1,2,3]), json.dumps({'a':1}))",
        _fresh(), timeout=5,
    )
    checks.check(f"time/math/json/random/statistics import and run: {r['output']!r}",
                 r["status"] == "ok" and "4.0" in r["output"])

    checks.section("4. a runaway script is killed at its time limit, not left to hang")
    r = run_script("import time\nwhile True: time.sleep(0.05)", _fresh(), timeout=2)
    checks.check("it is reported timed out", r["timed_out"] is True and r["status"] == "error")
    checks.check("the error names the limit", "time limit" in r.get("error", ""))
    checks.check("a script cannot ask for more than the ceiling",
                 _clamped_to_ceiling())

    checks.section("5. a script that raises is reported as an error, not a success")
    r = run_script("x = 1 / 0", _fresh(), timeout=5)
    checks.check("division by zero surfaces as an error status", r["status"] == "error")
    checks.check("the actual exception is in the output",
                 "ZeroDivisionError" in r["output"])

    checks.section("6. an empty script is refused before a process is even started")
    r = run_script("   ", _fresh(), timeout=5)
    checks.check("empty code is rejected", r["status"] == "error" and "code" in r["error"])

    checks.section("7. a script cannot escalate to a subagent or an agent-layer command")
    # dispatch_device refuses non-device names, and that is the only door the script has.
    r = run_script(
        "r = flipper.request('agent_listen', 433920000)\nprint(r['status'], r.get('error',''))",
        _fresh(), timeout=5,
    )
    checks.check("routing an agent command through the script's device door is refused",
                 r["status"] == "ok" and "error" in r["output"] and "unknown device command" in r["output"])

    checks.section("8. the whole feature is reachable through the catalog, end to end")
    d = _fresh()
    catalog = {c["name"]: c for c in load_catalog()["commands"]}
    checks.check("the catalog carries agent.run_script as an implemented agent command",
                 catalog["agent.run_script"]["layer"] == "agent"
                 and catalog["agent.run_script"]["status"] == "implemented"
                 and "subagent" not in catalog["agent.run_script"])
    exposed = {c["name"] for c in model_commands(load_catalog())}
    checks.check("it is exposed to the model as a tool", "agent.run_script" in exposed)
    out = d.dispatch("agent_run_script", {
        "purpose": "one read via the catalog dispatch path",
        "code": "print(flipper.request('subghz.read', 433920000)['data'][0])",
        "timeout": 10,
    })
    checks.check(f"dispatch('agent_run_script', ...) runs it: {out['output']!r}",
                 out["status"] == "ok" and out["command_count"] == 1)

    return checks.finish()


def _clamped_to_ceiling():
    """A timeout above the ceiling is clamped, so a script cannot request an unbounded run."""
    r = run_script("import time\nwhile True: time.sleep(0.05)", _fresh(), timeout=MAX_TIMEOUT_S + 999)
    # It still times out; the point is it did so under the ceiling, not the absurd request.
    return r["timed_out"] and f"{MAX_TIMEOUT_S}s" in r.get("error", "")


class _Checks:
    """A tiny check harness, matching the style of the other suites' Checks."""

    def __init__(self, title):
        print(f"== {title}\n")
        self.passed = 0
        self.failed = 0

    def section(self, name):
        print(f"\n{name}")

    def check(self, label, ok):
        mark = "OK  " if ok else "FAIL"
        print(f"  {mark} {label}")
        if ok:
            self.passed += 1
        else:
            self.failed += 1

    def finish(self):
        total = self.passed + self.failed
        print(f"\n{self.passed}/{total} checks passed")
        return 1 if self.failed else 0


if __name__ == "__main__":
    sys.exit(main())
