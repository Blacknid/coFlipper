"""Checks the Sub-GHz 'listen' feature: harvesting a frequency, saving captures, replay.

    python test_listen.py

Like test_subagents.py / test_audit.py, the models are scripted so no Gemini quota is
spent. What is NOT scripted is the real work behind the tools: subghz.read runs against the
MockCFPClient's actual band fixtures, and save_subghz / the replay resolution write and read
real .sub files in a temporary apps_assets folder. So the device primitive, the descriptive
naming, the store and the replay path are all exercised for real - only the model's choices
are canned.
"""

import os
import sys
import tempfile

import subagents
from commands import CommandDispatcher, load_catalog, model_commands
from mock_flipper import MockCFPClient
from scripted_model import Call, Checks, Part, Response, ScriptedChat
from subghz_store import SubGhzStore, descriptive_name
from test_subagents import ScriptedRunner

FREQ = 433_920_000


def main():
    checks = Checks("subghz listen")

    tmp = tempfile.mkdtemp(prefix="coflipper-assets-test-")
    os.environ["COFLIPPER_ASSETS"] = tmp

    dispatcher = CommandDispatcher(model_commands(load_catalog()), MockCFPClient())

    checks.section("1. subghz.read walks the band's devices, then reports the air quiet")
    reads = [dispatcher.dispatch_device("subghz_read", {"frequency": FREQ}) for _ in range(5)]
    decoded = [r["data"] for r in reads if r["data"][0] == "signal"]
    protocols = [d[1] for d in decoded]
    checks.check(f"several distinct signals decoded: {protocols}", len(set(protocols)) >= 3)
    checks.check("a decode carries protocol, key, bits, freq, rssi",
                 all(len(d) == 6 for d in decoded))
    checks.check("once the band is spent, read returns no_signal, not an invented code",
                 reads[-1]["data"] == ["no_signal"])
    checks.check("a frequency outside every CC1101 band is refused",
                 dispatcher.dispatch_device("subghz_read", {"frequency": 100_000_000})["status"] == "error")

    checks.section("2. the listener subagent's grant: passive tools + the save/list skills")
    meta = ScriptedRunner(dispatcher, {}).describe("listener")
    checks.check(f"its role mentions harvesting: {meta['role'][:40]}...", "harvest" in meta["role"])
    checks.check(f"it has subghz_read: {meta['tools']}", "subghz_read" in meta["tools"])
    checks.check("it was granted the save/list skills",
                 "save_subghz" in meta["tools"] and "list_subghz" in meta["tools"])
    checks.check("it has NO replay tool - it is passive, it cannot transmit",
                 "subghz_replay" not in meta["tools"])

    checks.section("3. the listener harvests, deduplicates and saves each distinct signal")
    # A fresh client so the read cursor starts at the top of the band again.
    fresh = CommandDispatcher(model_commands(load_catalog()), MockCFPClient())
    listener_chat = ScriptedChat(
        [
            Response([Part("Confirm activity first.", thought=True)],
                     [Call("subghz_rssi", {"frequency": FREQ})]),
            Response([Part("Now harvest across the window.", thought=True)],
                     [Call("subghz_read", {"frequency": FREQ, "timeout_ms": 3000})]),
            Response([Part("Another.", thought=True)],
                     [Call("subghz_read", {"frequency": FREQ, "timeout_ms": 3000})]),
            # A repeat of the relay: same protocol+key, must be deduplicated, saved once.
            Response([Part("The relay again - same code, do not double-count.", thought=True)],
                     [Call("save_subghz", {"frequency": FREQ, "protocol": "Nice_FloR_S",
                                           "key": "0x3F7A11", "bits": 52, "rssi": -49,
                                           "guess": "an electric relay / rolling-code actuator"}),
                      Call("save_subghz", {"frequency": FREQ, "protocol": "Princeton",
                                           "key": "0x4E7B90", "bits": 24, "rssi": -62,
                                           "guess": "a wireless doorbell remote"})]),
            Response([Part("Harvested two distinct devices on 433.92 MHz: an electric relay "
                           "(Nice_FloR_S) and a wireless doorbell remote (Princeton). Both saved.")]),
        ],
        "listener",
    )
    runner = ScriptedRunner(fresh, {"listener": listener_chat})
    fresh.subagents = runner

    result = runner.run("listener", "harvest 433.92 MHz for 20 seconds")
    checks.check(f"it reports a list, not one code: {result['report'][:50]}...",
                 "relay" in result["report"] and "doorbell" in result["report"])
    saved = [e for e in result["evidence"] if e["command"] == "save_subghz"]
    checks.check(f"it saved two distinct captures: {len(saved)}", len(saved) == 2)

    checks.section("4. the saved files are descriptively named and findable on disk")
    store = SubGhzStore()
    names = [c["stem"] for c in store.list_captures()]
    checks.check(f"the doorbell capture is named for what it is: {names}",
                 any("doorbell" in n for n in names))
    checks.check("the relay capture is named for what it is",
                 any("relay" in n for n in names))
    checks.check("the descriptive name carries the band",
                 all(n.startswith("433MHz") for n in names))
    doorbell = next(c for c in store.list_captures() if "doorbell" in c["stem"])
    sub_text = open(doorbell["sub_path"], encoding="utf-8").read()
    checks.check("the .sub file is real Flipper format with the right frequency",
                 "Filetype: Flipper SubGhz Key File" in sub_text and "Frequency: 433920000" in sub_text)

    checks.section("5. two captures that differ only by key do not clobber each other")
    a = store.save(frequency=FREQ, protocol="Princeton", key="0xAAA111", bits=24, guess="a doorbell")
    b = store.save(frequency=FREQ, protocol="Princeton", key="0xBBB222", bits=24, guess="a doorbell")
    checks.check("distinct stems for distinct keys", a["stem"] != b["stem"])

    checks.section("6. replay resolves a descriptive name to the right file and transmits it")
    replay = fresh.dispatch("agent_replay_subghz", {"name": "electric_relay"})
    checks.check(f"replay confirmed: {replay.get('data')}", replay["status"] == "ok")
    checks.check("it replayed the relay capture, resolved by name",
                 "relay" in replay.get("replayed", ""))
    checks.check("the device echoed the .sub path it transmitted",
                 replay.get("path", "").endswith(".sub"))

    checks.section("7. an ambiguous or unknown replay name is refused, not guessed")
    ambiguous = fresh.dispatch("agent_replay_subghz", {"name": "doorbell"})
    checks.check(f"ambiguous name refused with candidates: {ambiguous.get('error','')[:40]}...",
                 ambiguous["status"] == "error" and "several" in ambiguous["error"])
    unknown = fresh.dispatch("agent_replay_subghz", {"name": "no_such_capture_xyz"})
    checks.check("unknown name refused and lists what is available",
                 unknown["status"] == "error" and "Available" in unknown["error"])

    checks.section("8. the whole feature is reachable through the catalog, end to end")
    main_chat = ScriptedChat(
        [
            Response([Part("Open question about the frequency: I delegate to the listener.", thought=True)],
                     [Call("agent_listen", {"frequency": FREQ, "time": 20})]),
            Response([Part("On 433.92 MHz I harvested a relay and a doorbell remote, both saved.")]),
        ],
        "main",
    )
    runner.chats["listener"] = ScriptedChat(
        [
            Response([Part("Harvesting.", thought=True)],
                     [Call("subghz_read", {"frequency": FREQ})]),
            Response([Part("Saving the one I found.", thought=True)],
                     [Call("save_subghz", {"frequency": FREQ, "protocol": "Nice_FloR_S",
                                           "key": "0x3F7A11", "bits": 52,
                                           "guess": "an electric relay"})]),
            Response([Part("Harvested one device: an electric relay, saved.")]),
        ],
        "listener",
    )
    from agent import run_turn
    from reasoning import SPAWN, REPORT, ANSWER
    reply, trace = run_turn(main_chat, fresh, "what's transmitting on 433.92 MHz?", lambda s: None)
    checks.check("the listener was summoned via the catalog command agent.listen",
                 any(s.kind == SPAWN and s.name == "listener" for s in trace.steps))
    checks.check("it reported back into the reasoning chain",
                 any(s.kind == REPORT and s.name == "listener" for s in trace.steps))
    checks.check(f"the main agent answered from it: {reply[:40]}...",
                 "relay" in reply and trace.steps[-1].kind == ANSWER)

    checks.section("9. a listener cannot summon another subagent, and stays passive")
    refused = fresh.dispatch_device("agent_listen", {})
    checks.check(f"the device path refuses an agent name: {refused['error'][:30]}...",
                 refused["status"] == "error")

    return checks.finish()


if __name__ == "__main__":
    sys.exit(main())
