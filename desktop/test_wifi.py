"""Checks the Wi-Fi feature: surveying the neighbourhood, reporting it, and the authorized
deauthentication flow that may follow.

    python test_wifi.py

Like test_listen.py / test_watch.py, the models are scripted so no Gemini quota is spent,
but the work behind the tools is real: every wifi.* command runs against the MockCFPClient's
MarauderSim, which holds the same state the ESP32 board's firmware would - the captured AP
and station lists, the current target selection, whichever operation is running - so the
multi-step flows (scan, list, select, attack, stop) behave as they would on hardware.

Two things this suite is built to prove. First, that the recon half is strictly passive: the
wifi_recon subagent is granted scanning and listing tools and NO attack tool of any kind, so
a survey can never, by itself, start a deauth. Second, that the offensive half is gated and
targeted: a deauth is refused until a target is selected, it acts on exactly the network the
user picked rather than the whole band, and the catalog marks it offensive and lists it
behind the mandatory authorization gate - the deauth of everything in range is a separate,
deliberately distinct command.
"""

import sys

import subagents
from commands import CommandDispatcher, load_catalog, model_commands
from mock_flipper import MockCFPClient
from scripted_model import Call, Checks, Part, Response, ScriptedChat
from test_subagents import ScriptedRunner


def _fresh():
    return CommandDispatcher(model_commands(load_catalog()), MockCFPClient())


def main():
    checks = Checks("wifi")

    catalog = load_catalog()["commands"]
    by_name = {c["name"]: c for c in catalog}

    checks.section("1. the wifi_recon subagent's grant: passive scan/list only, no attack")
    meta = ScriptedRunner(_fresh(), {}).describe("wifi_recon")
    checks.check(f"its role is about surveying, not attacking: {meta['role'][:45]}...",
                 "surveys" in meta["role"].lower() and "without attacking" in meta["role"].lower())
    checks.check(f"it can scan for access points: {meta['tools']}",
                 "wifi_scan_ap" in meta["tools"] and "wifi_list_ap" in meta["tools"])
    checks.check("it can scan for client stations too",
                 "wifi_scan_station" in meta["tools"] and "wifi_list_station" in meta["tools"])
    checks.check("it was granted NO attack tool of any kind - it can never deauth on its own",
                 not any("attack" in t or "evil" in t or "karma" in t for t in meta["tools"]))
    checks.check("it has no select tool either - selecting a target is the attacker's job",
                 "wifi_select_ap" not in meta["tools"])

    checks.section("2. a scan harvests the neighbourhood, listed in the board's token format")
    dispatcher = _fresh()
    scan = dispatcher.dispatch_device("wifi_scan_ap", {})
    checks.check(f"the scan reports how many networks it captured: {scan['data']}",
                 scan["status"] == "ok" and scan["data"][0] == "captured")
    listing = dispatcher.dispatch_device("wifi_list_ap", {})["data"]
    checks.check(f"every network is one 'index;SSID;BSSID;channel;rssi;enc' token: {len(listing)} of them",
                 all(len(tok.split(";")) == 6 for tok in listing))
    ssids = [tok.split(";")[1] for tok in listing]
    checks.check(f"the SSIDs are discovered, not invented: {ssids[:3]}...",
                 "Home_WiFi" in ssids and "DIGI_f7c1500" in ssids)
    encs = {tok.split(";")[5] for tok in listing}
    checks.check(f"encryption is read per network, and an OPEN one stands out: {sorted(encs)}",
                 "OPEN" in encs and "WPA2" in encs)
    checks.check("listing before scanning is refused, not faked",
                 _fresh().dispatch_device("wifi_list_ap", {})["data"][0] == "no_access_points_captured")

    checks.section("3. the recon subagent surveys and reports an interpreted picture")
    survey = _fresh()
    recon_chat = ScriptedChat(
        [
            Response([Part("Confirm the board is attached before anything.", thought=True)],
                     [Call("wifi_board_info", {})]),
            Response([Part("Scan the access points, then read the list.", thought=True)],
                     [Call("wifi_scan_ap", {}), Call("wifi_list_ap", {})]),
            Response([Part("Attribute the clients to their networks.", thought=True)],
                     [Call("wifi_scan_station", {}), Call("wifi_list_station", {})]),
            Response([Part("Surveyed 9 networks on 2.4 GHz. One is OPEN (CoffeeShop_Free, "
                           "channel 1); the rest are WPA/WPA2/WPA3. DIGI_f7c1500 (index 8, "
                           "channel 1, WPA2) is the strongest at -44 dBm and has two active "
                           "clients. The network you named, DIGI_f7c1500, was found.")]),
        ],
        "wifi_recon",
    )
    runner = ScriptedRunner(survey, {"wifi_recon": recon_chat})
    survey.subagents = runner
    result = runner.run("wifi_recon", "survey the Wi-Fi and find the network DIGI_f7c1500")
    ran = [e["command"] for e in result["evidence"]]
    checks.check(f"it confirmed the board before scanning: {ran[:2]}", ran[0] == "wifi_board_info")
    checks.check("it never called an attack command - passive by construction",
                 not any("attack" in c for c in ran))
    checks.check(f"the report names the found network with its index: {result['report'][-40:]}",
                 "DIGI_f7c1500" in result["report"] and "found" in result["report"].lower())
    checks.check("the report flags the open network the user should notice",
                 "OPEN" in result["report"] and "CoffeeShop_Free" in result["report"])

    checks.section("4. the recon feature is reachable through the catalog, end to end")
    end = _fresh()
    main_chat = ScriptedChat(
        [
            Response([Part("An open 'what is around' question: I delegate the survey.", thought=True)],
                     [Call("agent_wifi_recon", {"goal": "general overview"})]),
            Response([Part("Nine networks nearby; one open (CoffeeShop_Free), the rest encrypted.")]),
        ],
        "main",
    )
    end.subagents = ScriptedRunner(end, {"wifi_recon": ScriptedChat(
        [
            Response([Part("Scanning.", thought=True)],
                     [Call("wifi_board_info", {}), Call("wifi_scan_ap", {}), Call("wifi_list_ap", {})]),
            Response([Part("Nine networks: one OPEN (CoffeeShop_Free), the others WPA/WPA2/WPA3.")]),
        ],
        "wifi_recon",
    )})
    from agent import run_turn
    from reasoning import SPAWN, REPORT, ANSWER
    reply, trace = run_turn(main_chat, end, "what wifi networks are around me?", lambda s: None)
    checks.check("the recon subagent was summoned via agent.wifi_recon",
                 any(s.kind == SPAWN and s.name == "wifi_recon" for s in trace.steps))
    checks.check("it reported back into the reasoning chain",
                 any(s.kind == REPORT and s.name == "wifi_recon" for s in trace.steps))
    checks.check(f"the main agent answered from it: {reply[:45]}...",
                 "network" in reply.lower() and trace.steps[-1].kind == ANSWER)

    checks.section("5. a deauth is refused until a target is chosen, then hits exactly it")
    attack = _fresh()
    attack.dispatch_device("wifi_scan_ap", {})
    refused = attack.dispatch_device("wifi_attack_deauth", {})
    checks.check(f"deauth with nothing selected is refused: {refused.get('error')}",
                 refused["status"] == "error" and refused["error"] == "no_target_selected")
    selected = attack.dispatch_device("wifi_select_ap", {"targets": "8"})
    checks.check(f"selecting the one network the user owns picks exactly one: {selected['data']}",
                 selected["status"] == "ok" and selected["data"][:2] == ["selected", "1"])
    started = attack.dispatch_device("wifi_attack_deauth", {})
    checks.check(f"the deauth then starts against that single target: {started['data']}",
                 started["status"] == "ok" and started["data"][:4] == ["deauth", "started", "targets", "1"])
    checks.check("the board's answer carries the authorized-use-only reminder",
                 "authorized_use_only" in started["data"])
    stopped = attack.dispatch_device("wifi_stop", {})
    checks.check(f"the attack stops on command: {stopped['data']}",
                 stopped["status"] == "ok" and stopped["data"] == ["stopped", "attack_deauth"])

    checks.section("6. targeted deauth is distinct from the indiscriminate deauth-of-everything")
    checks.check("a single selection acts on one network, not the nine in range",
                 started["data"][3] == "1")
    broad = _fresh()
    broad.dispatch_device("wifi_scan_ap", {})
    all_ap = broad.dispatch_device("wifi_attack_deauth_all", {})
    checks.check(f"deauth_all is a separate command that hits every AP captured: {all_ap['data']}",
                 all_ap["data"][:2] == ["deauth_all", "started"] and all_ap["data"][3] == "9")

    checks.section("7. offensive Wi-Fi commands are marked and sit behind the authorization gate")
    gate = by_name["agent.confirm_authorized_action"]
    checks.check("scan and list are marked passive - free to use to survey",
                 by_name["wifi.scan_ap"]["impact"] == "passive"
                 and by_name["wifi.list_ap"]["impact"] == "passive")
    checks.check("the deauth commands are marked offensive",
                 by_name["wifi.attack_deauth"]["impact"] == "offensive"
                 and by_name["wifi.attack_deauth_all"]["impact"] == "offensive")
    checks.check("both deauths are enumerated behind the mandatory authorization gate",
                 "wifi.attack_deauth" in gate["uses"] and "wifi.attack_deauth_all" in gate["uses"])
    checks.check("no passive scan/list command is behind the gate - only the offensive ones are",
                 "wifi.scan_ap" not in gate["uses"] and "wifi.list_ap" not in gate["uses"])

    return checks.finish()


if __name__ == "__main__":
    sys.exit(main())
