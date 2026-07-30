"""Checks the NFC feature: identifying a tag, guessing what it is used for, and the
web lookup that backs it - plus the boundary that emulation is offensive and gated.

    python test_nfc.py

Like test_wifi.py / test_listen.py, the model is scripted so no Gemini quota is spent, but
the work behind the tools is real: nfc.read runs against the MockCFPClient's NfcSim, which
hands out a fixed neighbourhood of tags as the raw technical read a real frontend produces -
type, UID, ATQA, SAK, protocol, storage - and nothing about what the card is FOR. Deducing
the use and looking the chip up online is the nfc_identify subagent's job, so what this suite
proves is that the read carries only facts, that the subagent is passive (it can read a tag
but has NO emulate/transmit tool), that it is granted web search to corroborate, and that the
whole identification is reachable end to end through the catalog.

The one part a scripted model cannot exercise is the live web search itself (the grounding is
served by Google, not by a fixture); that is verified against the real API separately, exactly
as the reasoning-summaries and delegation behaviours are. Here the search is represented by the
subagent's scripted report, and what is checked around it is the plumbing: that the subagent is
actually given the tool, and that a radio subagent is not.
"""

import sys

import subagents
from commands import CommandDispatcher, load_catalog, model_commands
from mock_flipper import MockCFPClient
from scripted_model import Call, Checks, Part, Response, ScriptedChat
from test_subagents import ScriptedRunner


def _fresh():
    return CommandDispatcher(model_commands(load_catalog()), MockCFPClient())


def _tokens(result):
    """The key=value read tokens as a dict, so a test reads a field by name."""
    return dict(tok.split("=", 1) for tok in result["data"])


def main():
    checks = Checks("nfc")

    catalog = load_catalog()["commands"]
    by_name = {c["name"]: c for c in catalog}

    checks.section("1. the nfc_identify subagent's grant: read + web search, no emulate")
    meta = ScriptedRunner(_fresh(), {}).describe("nfc_identify")
    checks.check(f"its role is identify-and-look-up, not emulate: {meta['role'][:45]}...",
                 "identifies" in meta["role"].lower() and "used for" in meta["role"].lower())
    checks.check(f"it can read a tag: {meta['tools']}", "nfc_read" in meta["tools"])
    checks.check("it was given web search, to corroborate the tag online",
                 "web_search" in meta["tools"])
    checks.check("it has NO emulate tool - it can never impersonate the card",
                 "nfc_emulate" not in meta["tools"])
    checks.check("web search is opt-in per subagent: a radio subagent does not get it",
                 "web_search" not in ScriptedRunner(_fresh(), {}).describe("wifi_recon")["tools"])

    checks.section("2. a read returns the raw technical facts, not an interpretation")
    dispatcher = _fresh()
    first = dispatcher.dispatch_device("nfc_read", {})
    fields = _tokens(first)
    checks.check(f"the read carries the chip type: {fields.get('type')}",
                 fields.get("type") == "Mifare Classic 1K")
    checks.check(f"it carries the UID and the anticollision bytes: {sorted(fields)}",
                 {"uid", "atqa", "sak", "protocol", "bytes"} <= set(fields))
    checks.check("it hands over NO guess about the card's use - that is the agent's job",
                 not any(k in fields for k in ("use", "guess", "purpose")))
    second = _tokens(dispatcher.dispatch_device("nfc_read", {}))
    checks.check(f"a second tap reads a different tag, not the same one forever: {second.get('type')}",
                 second.get("type") != fields.get("type"))
    watch = _fresh().dispatch_device("nfc_watch", {"duration_ms": 3000})["data"]
    checks.check(f"a watch session lists tags chronologically as 'ms;type;uid': {len(watch)} events",
                 all(len(ev.split(";")) == 3 for ev in watch) and len(watch) >= 2)

    checks.section("3. the subagent reads, names the chip, guesses the use and cites a source")
    survey = _fresh()
    # Two turns, mirroring the real flow: the subagent reads the tag, then answers - and in
    # that answering turn the web search happens server-side (grounding), so it is one round,
    # not a separate empty one. The scripted report stands in for what the search corroborated.
    identify_chat = ScriptedChat(
        [
            Response([Part("Read the tag the user presented.", thought=True)],
                     [Call("nfc_read", {})]),
            Response([Part("Corroborate the chip online, then report.", thought=True),
                      Part("This is a Mifare Classic 1K (ATQA 0004, SAK 08, ISO14443-3A, "
                           "1 KB). Most plausibly an access credential - an office or hotel "
                           "key card, or a transit/loyalty card - which is what a 1K Classic "
                           "is overwhelmingly used for; it is an informed guess, not a "
                           "certainty, and the UID alone identifies no person. Corroborated "
                           "online: per nxp.com and the Wikipedia MIFARE page, Classic 1K "
                           "holds 1024 bytes in 16 sectors and is a legacy access/ticketing "
                           "chip with known crypto weaknesses.")]),
        ],
        "nfc_identify",
    )
    runner = ScriptedRunner(survey, {"nfc_identify": identify_chat})
    survey.subagents = runner
    result = runner.run("nfc_identify", "what is this card? it's my office badge")
    ran = [e["command"] for e in result["evidence"]]
    checks.check(f"it actually read the tag: {ran}", "nfc_read" in ran)
    checks.check("it never emulated or transmitted anything - passive by construction",
                 not any("emulate" in c for c in ran))
    checks.check(f"the report names the chip type: {result['report'][:30]}...",
                 "Mifare Classic 1K" in result["report"])
    checks.check("the report gives a reasoned guess at the use, flagged as a guess",
                 ("access" in result["report"] or "key card" in result["report"])
                 and "guess" in result["report"])
    checks.check("the report cites where the online detail came from",
                 "nxp.com" in result["report"] or "Wikipedia" in result["report"])

    checks.section("4. the identify feature is reachable through the catalog, end to end")
    end = _fresh()
    main_chat = ScriptedChat(
        [
            Response([Part("The user wants this tag identified: I delegate.", thought=True)],
                     [Call("agent_identify_nfc", {"goal": "found this sticker on a poster"})]),
            Response([Part("It's an NTAG-style sticker, the kind used on smart posters.")]),
        ],
        "main",
    )
    end.subagents = ScriptedRunner(end, {"nfc_identify": ScriptedChat(
        [
            Response([Part("Reading.", thought=True)], [Call("nfc_read", {})]),
            Response([Part("A Mifare Classic 1K - most likely an access/ticketing card. "
                           "Corroborated via nxp.com.")]),
        ],
        "nfc_identify",
    )})
    from agent import run_turn
    from reasoning import SPAWN, REPORT, ANSWER
    reply, trace = run_turn(main_chat, end, "read this tag and tell me what it is", lambda s: None)
    checks.check("the subagent was summoned via agent.identify_nfc",
                 any(s.kind == SPAWN and s.name == "nfc_identify" for s in trace.steps))
    checks.check("it reported back into the reasoning chain",
                 any(s.kind == REPORT and s.name == "nfc_identify" for s in trace.steps))
    checks.check(f"the main agent answered from it: {reply[:40]}...",
                 trace.steps[-1].kind == ANSWER and reply)

    checks.section("5. reading is passive; emulation is offensive and behind the auth gate")
    gate = by_name["agent.confirm_authorized_action"]
    checks.check("nfc.read and nfc.watch are marked passive - free to use to identify a tag",
                 by_name["nfc.read"]["impact"] == "passive"
                 and by_name["nfc.watch"]["impact"] == "passive")
    checks.check("nfc.emulate is marked offensive - it impersonates the card to a reader",
                 by_name["nfc.emulate"]["impact"] == "offensive")
    checks.check("nfc.emulate is enumerated behind the mandatory authorization gate",
                 "nfc.emulate" in gate["uses"])
    checks.check("no passive nfc read/watch command is behind the gate - only emulate is",
                 "nfc.read" not in gate["uses"] and "nfc.watch" not in gate["uses"])
    emulate = _fresh().dispatch_device("nfc_emulate", {"name": "my_badge"})
    checks.check(f"the emulate command itself carries the authorized-use reminder: {emulate.get('data')}",
                 emulate["status"] == "ok" and "authorized_use_only" in emulate["data"])

    return checks.finish()


if __name__ == "__main__":
    sys.exit(main())
