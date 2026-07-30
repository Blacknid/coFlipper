"""Checks the RAW Sub-GHz capture/replay feature: record a waveform under a name, report it,
and replay it later by that name.

    python test_raw.py

No Gemini quota is spent - the work behind the tools runs against the MockCFPClient. What this
suite exercises is the raw pair's distinct shape from the decoded one: subghz.read_raw stores a
waveform ON THE DEVICE under a caller-chosen name and the desktop keeps only a name-level record,
subghz.send_raw replays by that name, and the agent flow derives the name from the user's words,
reports it, and gates replay on authorization.
"""

import os
import sys
import tempfile

from commands import CommandDispatcher, load_catalog, model_commands
from mock_flipper import MockCFPClient
from scripted_model import Checks
from subghz_store import SubGhzStore

FREQ = 433_920_000


def _fresh_dispatcher():
    return CommandDispatcher(model_commands(load_catalog()), MockCFPClient())


def main():
    checks = Checks("subghz raw capture/replay")

    checks.section("1. the catalog carries the raw commands with the right layers and impact")
    catalog = load_catalog()["commands"]
    by_name = {c.get("name"): c for c in catalog}
    checks.check("subghz.read_raw is an implemented passive device command",
                 by_name.get("subghz.read_raw", {}).get("layer") == "device"
                 and by_name["subghz.read_raw"].get("impact") == "passive"
                 and by_name["subghz.read_raw"].get("status") == "implemented")
    checks.check("subghz.send_raw is an implemented offensive device command",
                 by_name.get("subghz.send_raw", {}).get("layer") == "device"
                 and by_name["subghz.send_raw"].get("impact") == "offensive")
    checks.check("agent.capture_raw is a passive agent command using subghz.read_raw",
                 by_name.get("agent.capture_raw", {}).get("impact") == "passive"
                 and "subghz.read_raw" in by_name["agent.capture_raw"].get("uses", []))
    checks.check("agent.replay_raw is an offensive agent command using subghz.send_raw",
                 by_name.get("agent.replay_raw", {}).get("impact") == "offensive"
                 and "subghz.send_raw" in by_name["agent.replay_raw"].get("uses", []))
    gate = by_name.get("agent.confirm_authorized_action", {}).get("uses", [])
    checks.check("replay is behind the authorization gate (both the device and agent forms)",
                 "subghz.send_raw" in gate and "agent.replay_raw" in gate)

    checks.section("2. the device round-trip: read_raw captures under a name, send_raw replays it")
    d = _fresh_dispatcher()
    cap = d.dispatch_device("subghz_read_raw",
                            {"frequency": FREQ, "name": "raw_relay_remote", "timeout_ms": 5000})
    checks.check(f"read_raw captured a waveform: {cap.get('data')}",
                 cap.get("status") == "ok" and cap["data"][0] == "captured")
    checks.check("it reports a sample count and the saved name",
                 cap["data"][1].isdigit() and cap["data"][2] == "raw_relay_remote")
    checks.check("the capture is marked simulated, so it is never mistaken for a real one",
                 cap.get("simulated") is True)
    snd = d.dispatch_device("subghz_send_raw", {"name": "raw_relay_remote"})
    checks.check(f"send_raw replays by name: {snd.get('data')}",
                 snd.get("status") == "ok" and snd["data"][0] == "transmitted")

    checks.section("3. an out-of-band frequency is refused, not silently saved")
    bad_band = d.dispatch_device("subghz_read_raw",
                                 {"frequency": 100_000_000, "name": "raw_bad", "timeout_ms": 5000})
    checks.check(f"a frequency outside every CC1101 band is rejected: {bad_band.get('error')}",
                 bad_band.get("status") == "error" and "invalid_frequency" in bad_band.get("error", ""))
    checks.check("read_raw refuses a call with no name (nothing to save under)",
                 d.dispatch_device("subghz_read_raw", {"frequency": FREQ}).get("status") == "error")

    checks.section("4. the agent flow: capture derives+reports the name and saves a record")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["COFLIPPER_ASSETS"] = tmp
        try:
            a = _fresh_dispatcher()
            cap = a._dispatch_agent({"name": "agent.capture_raw"},
                                    {"name": "raw_relay_remote"}, lambda e: None)
            checks.check(f"capture_raw saved under the derived name: {cap.get('captured')}",
                         cap.get("status") == "ok" and cap.get("captured") == "raw_relay_remote")
            checks.check("it reports a sample count so the user knows something was recorded",
                         isinstance(cap.get("samples"), int) and cap["samples"] > 0)
            recorded = [r["name"] for r in SubGhzStore().list_raw()]
            checks.check(f"the capture is recorded on the desktop for later replay: {recorded}",
                         "raw_relay_remote" in recorded)

            checks.section("5. replay resolves the name the user speaks, and refuses the unknown")
            rep = a._dispatch_agent({"name": "agent.replay_raw"},
                                    {"name": "relay"}, lambda e: None)
            checks.check("replay_raw resolves a substring to the exact capture and transmits",
                         rep.get("status") == "ok" and rep.get("replayed") == "raw_relay_remote")
            bad = a._dispatch_agent({"name": "agent.replay_raw"},
                                    {"name": "does_not_exist"}, lambda e: None)
            checks.check("an unknown name is refused with the candidates listed, not guessed",
                         bad.get("status") == "error" and "raw_relay_remote" in bad.get("error", ""))

            checks.section("6. a failed capture records nothing to replay")
            empty = a._dispatch_agent({"name": "agent.capture_raw"},
                                      {"name": "raw_ghost", "frequency": 100_000_000},
                                      lambda e: None)
            checks.check("capture_raw on an out-of-band frequency saves no record and errors",
                         empty.get("status") == "error"
                         and "raw_ghost" not in [r["name"] for r in SubGhzStore().list_raw()])
        finally:
            os.environ.pop("COFLIPPER_ASSETS", None)

    checks.section("7. raw capture is distinct from decoded: it stores a waveform, not params")
    with tempfile.TemporaryDirectory() as tmp:
        store = SubGhzStore(root=tmp)
        record = store.save_raw(name="raw_relay_remote", frequency=FREQ, samples=2839)
        checks.check("the raw record points at a device-side .sub path (the file lives on the Flipper)",
                     record.get("device_path") == "/ext/subghz/raw_relay_remote.sub"
                     and record.get("raw") is True)
        checks.check("it carries no decoded protocol or key - a raw capture is an opaque waveform",
                     "protocol" not in record and "key" not in record)

    return checks.finish()


if __name__ == "__main__":
    sys.exit(main())
