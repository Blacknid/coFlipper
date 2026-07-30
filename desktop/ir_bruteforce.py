"""Drives an IR bruteforce run on the Flipper, from a natural-language request.

This is the agent-layer half of the feature: it turns "turn off my samsung tv" into a
short, filtered list of codes, queues them on the device, starts the run and follows it
until either the codes are exhausted or the user presses OK on the Flipper to say the
appliance reacted.

The device side is deliberately dumb - it transmits whatever triples it is given and
reports progress. All the guessing (which appliance, which brand, which function, which
codes are worth sending) happens here, where there is a language model to help.

The run is asynchronous by necessity: CFPClient's read timeout is 2 s, while a run of
a dozen codes takes several seconds. So `ir.bruteforce` returns as soon as it has
started, and progress is polled through `ir.status`.
"""

import time

from ir_codes import (
    DEFAULT_FUNCTION,
    detect_function,
    detect_type,
    known_brands,
    select_codes,
)
from protocol import CFPError

# How often the desktop asks the device how far it has got. Frequent enough that the
# user pressing OK is noticed promptly, sparse enough not to flood the serial port.
POLL_INTERVAL_S = 0.3

# A run cannot legitimately outlast this; it guards against a device that stops
# answering mid-run, so the agent does not wait forever.
MAX_RUN_S = 120.0


def plan_from_text(text, brand=None, function=None, device_type=None):
    """Works out what to send, from the user's own words.

    Everything is optional except the text: the appliance type, brand and function are
    inferred from it when not given explicitly. This is what lets a single tool call
    handle "turn off my tv", "turn off this projector" and "volume up on the sony".
    """
    resolved_function = function or detect_function(text) or DEFAULT_FUNCTION

    resolved_type = device_type or detect_type(text)
    if not resolved_type:
        # A follow-up like "volume down" names no appliance: the model knows which one
        # from the conversation and should pass device_type explicitly.
        return [], {
            "function": resolved_function,
            "error": (
                "could not tell what kind of appliance this is; pass device_type "
                "explicitly (tv, projector, audio, ac) or ask the user which one"
            ),
            "known_types": sorted(["tv", "projector", "audio", "ac"]),
        }

    # The brand is not guessed from the text here: the model is far better at spotting
    # it ("my old Sammy in the bedroom") than a substring match would be, so it passes
    # the brand explicitly when it recognizes one.
    codes, plan = select_codes(resolved_type, brand, resolved_function)
    plan.setdefault("known_brands", known_brands(resolved_type))
    return codes, plan


def run(client, codes, label="device", poll_interval=POLL_INTERVAL_S, max_run_s=MAX_RUN_S,
        on_progress=None):
    """Queues the codes, starts the run, and follows it to the end.

    Returns a dict describing the outcome: whether the user stopped it (which means a
    code worked), how many codes were sent, and how many were queued.

    `on_progress(sent, total)` is called as the run advances, so the interface can show
    the same progress the Flipper's screen is showing.
    """
    if not codes:
        return {"status": "error", "error": "no codes to send"}

    # A previous run may have left codes queued; start from a clean slate.
    client.request("ir.reset")

    # A rejected code must not pass silently: the device would then bruteforce a
    # shorter list than intended and report an honest-looking "none of them worked".
    # The usual cause is a protocol name the firmware does not carry.
    rejected = []
    for protocol, address, command in codes:
        try:
            client.request("ir.queue", protocol, address, command)
        except CFPError as exc:
            rejected.append(f"{protocol} {address} {command} ({exc.message})")

    if rejected:
        return {
            "status": "error",
            "error": "the device refused some IR codes: " + "; ".join(rejected),
            "rejected": rejected,
        }

    # The label appears on the Flipper's screen during the run. CFP v1 arguments cannot
    # contain spaces, so it travels as a single token.
    client.request("ir.bruteforce", label.replace(" ", "_")[:23])

    deadline = time.monotonic() + max_run_s
    last_sent = -1

    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        state, sent, total = _status(client)

        if sent != last_sent:
            last_sent = sent
            if on_progress:
                on_progress(sent, total)

        if state == "stopped":
            # The user pressed OK: the appliance reacted, so the code just sent is the
            # one that worked.
            return {
                "status": "ok",
                "outcome": "stopped_by_user",
                "worked": True,
                "sent": sent,
                "total": total,
                "message": (
                    f"stopped after {sent} of {total} codes - the user confirmed on the "
                    "device that the appliance responded"
                ),
            }

        if state == "idle":
            # The queue ran out without the user stopping it.
            return {
                "status": "ok",
                "outcome": "exhausted",
                "worked": False,
                "sent": sent,
                "total": total,
                "message": (
                    f"all {total} codes were sent and the user did not confirm a "
                    "reaction; the appliance may use a brand or protocol the database "
                    "does not carry"
                ),
            }

    return {
        "status": "error",
        "error": f"the run did not finish within {max_run_s:.0f}s",
        "sent": last_sent if last_sent >= 0 else 0,
    }


def _status(client):
    """(state, sent, total) from the device, tolerating a malformed reply."""
    try:
        data = client.request("ir.status")
    except CFPError as exc:
        raise CFPError(f"could not read the bruteforce status: {exc.message}") from exc

    state = data[0] if data else "idle"
    sent = int(data[1]) if len(data) > 1 and data[1].isdigit() else 0
    total = int(data[2]) if len(data) > 2 and data[2].isdigit() else 0
    return state, sent, total


# How many times one appliance+button may fail on the built-in codes before the online
# database is consulted. Counted per target rather than globally: five different
# appliances failing once each is five ordinary misses, not one stubborn device.
FAILURES_BEFORE_ONLINE = 5

# target -> number of unsuccessful attempts on the local table so far.
_failures = {}


def _target_key(plan):
    return (plan.get("device_type"), plan.get("brand") or "any", plan.get("function"))


def failure_count(device_type, brand, function):
    """Attempts made on the built-in codes for one target, without success."""
    return _failures.get((device_type, brand or "any", function), 0)


def reset_failures():
    """Forgets the history - a new session should not inherit the last one's."""
    _failures.clear()


def _online_codes(plan, limit=None):
    """Codes for this target from the online database, or None if unavailable.

    Returns (codes, report). Never raises: a failed lookup must degrade to the local
    behaviour rather than sinking the request the user actually made.
    """
    import irdb

    brand = plan.get("brand")
    if not brand:
        # Without a brand the database offers thousands of remotes and no way to rank
        # them; the local all-brands sweep is the better use of the queue.
        return None, {"skipped": "no brand to search for"}

    try:
        codes, report = irdb.lookup(
            brand,
            plan.get("device_type"),
            plan.get("function"),
            limit=limit or irdb.MAX_FALLBACK_CODES,
        )
        return codes, report
    except irdb.IrdbError as exc:
        return None, {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the network offers many failure modes
        return None, {"error": f"the online lookup failed: {exc}"}


def bruteforce(client, text, brand=None, function=None, device_type=None,
               on_progress=None, force_online=False):
    """The whole operation: work out the codes, send them, report what happened.

    This is what the agent layer calls; the return value is shaped for the model, with
    the selection reasoning included so it can explain what it did rather than just
    announcing a result.

    Codes come from the built-in table first: it is small, hand-checked and instant.
    Once the same appliance and button have failed FAILURES_BEFORE_ONLINE times, the
    online database is consulted instead - by then the built-in guess is demonstrably
    wrong, and the wider net is worth the wait.
    """
    codes, plan = plan_from_text(text, brand, function, device_type)

    if not codes and not force_online:
        return {"status": "error", "plan": plan, "error": plan.get("error", "no codes selected")}

    key = _target_key(plan)
    failures = _failures.get(key, 0)
    escalate = force_online or failures >= FAILURES_BEFORE_ONLINE

    source = "builtin"
    online_report = None
    if escalate:
        online, online_report = _online_codes(plan)
        if online:
            codes, source = online, "irdb"

    if not codes:
        return {
            "status": "error",
            "plan": plan,
            "error": (online_report or {}).get("error", plan.get("error", "no codes selected")),
        }

    label = f"{plan.get('brand') or 'any'}-{plan.get('device_type')}"
    outcome = run(client, codes, label=label, on_progress=on_progress)

    # A run only counts as a failure when it actually ran and nothing worked; a device
    # error says nothing about whether the codes were right.
    if outcome.get("status") == "ok":
        if outcome.get("worked"):
            _failures.pop(key, None)
        else:
            _failures[key] = failures + 1

    outcome["plan"] = plan
    outcome["code_source"] = source
    outcome["attempts_on_builtin"] = _failures.get(key, 0)
    if online_report:
        outcome["online_lookup"] = online_report
        # The files actually fetched from the IR database, as full URLs, so the reasoning
        # chain can show what the online search visited - the only online access the agent
        # has, since it drives a device rather than browsing the web.
        consulted = online_report.get("remotes_consulted")
        if consulted:
            import irdb

            outcome["visited"] = [irdb.remote_url(path) for path in consulted]
    if source == "builtin" and not outcome.get("worked"):
        remaining = FAILURES_BEFORE_ONLINE - _failures.get(key, 0)
        if remaining > 0:
            outcome["next_step"] = (
                f"{remaining} more unsuccessful attempt(s) on this appliance and the "
                "online IR database will be searched automatically; the user can also "
                "ask for it now"
            )
        else:
            outcome["next_step"] = (
                "the next attempt on this appliance will search the online IR database"
            )
    return outcome
