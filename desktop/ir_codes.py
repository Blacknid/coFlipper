"""IR remote codes, grouped by appliance type, brand and function.

The database is deliberately small and hand-checked rather than a dump of a public
universal-remote list: the point of the command is to send *few* codes, the ones that
have a real chance of working, not to spray everything at the room. Each entry is a
code in the Flipper's IR format (protocol, address, command), the same triple the
infrared HAL needs to transmit.

Several brands share a protocol (NEC is near-universal); what distinguishes them is the
address, which is why filtering by brand is worth doing at all - the address of a
Samsung TV means nothing to an LG one. Within a brand, the command byte selects the
function: power, volume, channel, mute and input all share the brand's address.

The type/brand/function vocabulary is matched against free text by `detect_type`,
`match_brand` and `detect_function`, which is what lets the agent turn "turn off my
samsung tv" or "volume up on the sony" into exactly the right short code list.
"""

# Appliance types the database knows. The aliases are what users actually write; the
# model does not have to normalize anything before calling the tool.
DEVICE_TYPES = {
    # Interactive flat panels (classroom and meeting-room "smart boards") are driven as
    # televisions - same protocols, same button set - so they are aliases of tv rather
    # than a type of their own.
    "tv": (
        "tv",
        "television",
        "televizor",
        "telly",
        "smart tv",
        "smart board",
        "smartboard",
        "interactive display",
        "interactive panel",
        "interactive whiteboard",
        "tabla interactiva",
        "tabla smart",
        # Bare "board" is how these get referred to once the conversation has
        # established what is in the room ("turn the board off").
        "board",
        "tabla",
    ),
    "projector": ("projector", "proiector", "beamer", "video projector"),
    "audio": ("audio", "stereo", "soundbar", "hifi", "amplifier", "receiver", "speaker"),
    # Romanian aliases are listed in their inflected forms too ("aerul"), since the
    # match is a plain substring test and users write the article, not the lemma.
    "ac": (
        "ac",
        "air conditioner",
        "aircon",
        "air conditioning",
        "climate",
        "aer conditionat",
        "aerul conditionat",
        "aer condition",
    ),
}

# The functions the database carries, with the free-text aliases users write for them.
# Ordered longest-alias-first at match time, so "volume down" is not decided by "volume".
FUNCTIONS = {
    "power": ("power", "turn off", "turn on", "switch off", "switch on", "shut down",
              "standby", "close", "opreste", "porneste", "inchide"),
    "volume_up": ("volume up", "louder", "turn it up", "vol up", "increase volume",
                  "mai tare", "volum sus"),
    "volume_down": ("volume down", "quieter", "turn it down", "vol down",
                    "decrease volume", "lower the volume", "mai incet", "volum jos"),
    "mute": ("mute", "silence", "unmute", "fara sunet"),
    "channel_up": ("channel up", "next channel", "ch up", "canal urmator"),
    "channel_down": ("channel down", "previous channel", "ch down", "canal anterior"),
    "input": ("input", "source", "hdmi", "change input", "switch source", "sursa"),
}

# Alternative spellings and second names for brands already in the database, mapped to
# the entry that carries the codes. Consulted before fuzzy matching, which cannot be
# relied on here: some of these are a single character away from an unrelated
# manufacturer ("viewsense" vs "hisense") and would silently resolve to the wrong one.
BRAND_ALIASES = {
    "viewsense": "viewsonic",
    "view sonic": "viewsonic",
    "view sense": "viewsonic",
}

# Each brand carries its protocol and address once, plus a command byte per function.
# A missing function simply means the database does not have it for that brand; the
# selection code reports that rather than substituting a wrong code.
#
# Kept as strings because CFP v1 passes arguments as space-separated text.
IR_CODES = {
    "tv": {
        "samsung": ("Samsung32", "0x07", {
            "power": "0x02", "volume_up": "0x07", "volume_down": "0x0B",
            "mute": "0x0F", "channel_up": "0x12", "channel_down": "0x10",
            "input": "0x01",
        }),
        "lg": ("NEC", "0x04", {
            "power": "0x08", "volume_up": "0x02", "volume_down": "0x03",
            "mute": "0x09", "channel_up": "0x00", "channel_down": "0x01",
            "input": "0x0B",
        }),
        "sony": ("SIRC", "0x01", {
            "power": "0x15", "volume_up": "0x12", "volume_down": "0x13",
            "mute": "0x14", "channel_up": "0x10", "channel_down": "0x11",
            "input": "0x25",
        }),
        "philips": ("RC5", "0x00", {
            "power": "0x0C", "volume_up": "0x10", "volume_down": "0x11",
            "mute": "0x0D", "channel_up": "0x20", "channel_down": "0x21",
        }),
        "panasonic": ("Kaseikyo", "0x2002", {
            "power": "0x3D", "volume_up": "0x20", "volume_down": "0x21",
            "mute": "0x32", "channel_up": "0x34", "channel_down": "0x35",
        }),
        "toshiba": ("NEC", "0x40", {
            "power": "0x12", "volume_up": "0x1A", "volume_down": "0x1E",
            "mute": "0x10", "channel_up": "0x1B", "channel_down": "0x1F",
        }),
        "sharp": ("NEC", "0x01", {
            "power": "0x16", "volume_up": "0x0A", "volume_down": "0x0E",
            "mute": "0x12", "channel_up": "0x10", "channel_down": "0x14",
        }),
        "hisense": ("NEC", "0x00", {
            "power": "0x15", "volume_up": "0x07", "volume_down": "0x0B",
            "mute": "0x0F", "channel_up": "0x12", "channel_down": "0x10",
        }),
        "tcl": ("NEC", "0x04", {
            "power": "0x08", "volume_up": "0x02", "volume_down": "0x03",
            "mute": "0x09",
        }),
        "vizio": ("NEC", "0x04", {
            "power": "0x08", "volume_up": "0x02", "volume_down": "0x03",
            "mute": "0x09",
        }),
        "grundig": ("RC5", "0x00", {
            "power": "0x0C", "volume_up": "0x10", "volume_down": "0x11",
        }),
        # JVC sets are usually driven as plain NEC with address 0x03; the firmware
        # carries no separate JVC protocol, and naming one would be rejected as
        # unknown_protocol.
        "jvc": ("NEC", "0x03", {
            "power": "0x17", "volume_up": "0x1A", "volume_down": "0x1B",
        }),
        # ViewSonic interactive panels ("smart boards") share the address of their
        # projector line. Input matters more than channel on these: they are fed from
        # a PC or a laptop, and most have no tuner at all.
        "viewsonic": ("NEC", "0x83", {
            "power": "0x0C", "volume_up": "0x10", "volume_down": "0x11",
            "mute": "0x0E", "input": "0x14",
        }),
    },
    "projector": {
        "epson": ("NEC", "0x83", {"power": "0x90", "input": "0x9D"}),
        "benq": ("NEC", "0x00", {"power": "0x4E", "input": "0x18"}),
        "acer": ("NEC", "0x10", {"power": "0x03", "input": "0x12"}),
        "optoma": ("NEC", "0x32", {"power": "0x02", "input": "0x13"}),
        "viewsonic": ("NEC", "0x83", {"power": "0x0C", "input": "0x14"}),
        "nec": ("NEC", "0x00", {"power": "0x02", "input": "0x18"}),
    },
    "audio": {
        "sony": ("SIRC", "0x10", {
            "power": "0x15", "volume_up": "0x12", "volume_down": "0x13", "mute": "0x14",
        }),
        "yamaha": ("NEC", "0x7A", {
            "power": "0x1E", "volume_up": "0x1A", "volume_down": "0x1B", "mute": "0x1C",
        }),
        "denon": ("NEC", "0x02", {
            "power": "0x2A", "volume_up": "0x00", "volume_down": "0x01", "mute": "0x02",
        }),
        "onkyo": ("NEC", "0xD2", {
            "power": "0x1B", "volume_up": "0x40", "volume_down": "0x41", "mute": "0x42",
        }),
        "bose": ("NEC", "0x02", {
            "power": "0x0C", "volume_up": "0x03", "volume_down": "0x04", "mute": "0x05",
        }),
        "samsung": ("Samsung32", "0x07", {
            "power": "0x02", "volume_up": "0x07", "volume_down": "0x0B", "mute": "0x0F",
        }),
    },
    "ac": {
        # Air conditioners mostly use long stateful frames rather than short button
        # codes; only power is carried here, and even that is brand-approximate.
        "daikin": ("NEC", "0x88", {"power": "0x5A"}),
        "mitsubishi": ("NEC", "0x23", {"power": "0xCB"}),
        "lg": ("NEC", "0x81", {"power": "0x66"}),
        "samsung": ("Samsung32", "0x07", {"power": "0x02"}),
        "gree": ("NEC", "0x09", {"power": "0x08"}),
    },
}

DEFAULT_FUNCTION = "power"


def _normalize(text):
    return (text or "").strip().lower()


def known_brands(device_type):
    return sorted(IR_CODES.get(device_type, {}))


def known_functions(device_type, brand=None):
    """Which functions the database actually carries, so the agent can say what it can do."""
    brands = IR_CODES.get(device_type, {})
    if brand and brand in brands:
        return sorted(brands[brand][2])
    available = set()
    for _protocol, _address, commands in brands.values():
        available.update(commands)
    return sorted(available)


def detect_function(text):
    """The function requested in free text, or None.

    Longest alias wins, so 'volume down' is not swallowed by the 'volume up' alias
    sharing the word 'volume', and 'turn it down' beats a bare 'down'.
    """
    import re

    lowered = _normalize(text)
    best = None
    best_len = 0
    for function, aliases in FUNCTIONS.items():
        for alias in aliases:
            if re.search(rf"\b{re.escape(alias)}\b", lowered) and len(alias) > best_len:
                best, best_len = function, len(alias)
    return best


def detect_type(text):
    """The appliance type mentioned in free text, or None.

    Longer aliases win: 'smart tv' should not be decided by the 'tv' substring before
    the more specific alias has been considered. Matching is on word boundaries, so
    short aliases like 'ac' and 'tv' do not fire inside 'black' or 'network'.
    """
    import re

    lowered = _normalize(text)
    best = None
    best_len = 0
    for device_type, aliases in DEVICE_TYPES.items():
        for alias in aliases:
            if re.search(rf"\b{re.escape(alias)}\b", lowered) and len(alias) > best_len:
                best, best_len = device_type, len(alias)
    return best


def _similarity(a, b):
    """How close two brand names are, 0.0 to 1.0, without pulling in a dependency.

    difflib is in the standard library and good enough here: the input is a single
    short word, and we only need to tell 'samsng' from 'sony'.
    """
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a, b).ratio()


def match_brand(brand, device_type, cutoff=0.6):
    """Resolves a brand name to one present in the database.

    Returns (resolved_brand, kind) where kind is:
      'exact'   - the brand is in the database as written;
      'closest' - not found, but a sufficiently similar name exists (typo, or a brand
                  the database does not carry) - the caller should say so to the user;
      None      - nothing close enough, the caller should fall back to every brand.
    """
    brands = IR_CODES.get(device_type, {})
    if not brands:
        return None, None

    wanted = _normalize(brand)
    if not wanted:
        return None, None

    # Alternative spellings are resolved before anything else. Fuzzy matching cannot be
    # trusted with these: "viewsense" is one edit away from "hisense", so scoring would
    # confidently pick a different manufacturer's codes.
    wanted = BRAND_ALIASES.get(wanted, wanted)

    if wanted in brands:
        return wanted, "exact"

    # A brand written as part of a longer string ("samsung ue40") still counts as exact:
    # the user named it, we just received extra words around it.
    for candidate in brands:
        if candidate in wanted or wanted in candidate:
            return candidate, "exact"

    scored = sorted(
        ((_similarity(wanted, candidate), candidate) for candidate in brands),
        reverse=True,
    )
    score, candidate = scored[0]
    if score >= cutoff:
        return candidate, "closest"
    return None, None


def _codes_for(brand_entry, function):
    """The (protocol, address, command) triple for one brand and function, or None."""
    protocol, address, commands = brand_entry
    command = commands.get(function)
    if command is None:
        return None
    return (protocol, address, command)


def select_codes(device_type, brand=None, function=DEFAULT_FUNCTION):
    """The codes to try, plus an explanation of how they were chosen.

    Returns (codes, plan) where plan describes the selection for the user: which brand
    and function were used, whether the brand was an exact or approximate match, and
    how many codes resulted. Sorting out useless codes happens here - a Samsung TV
    never receives LG addresses, and a volume request never sends power codes.
    """
    if device_type not in IR_CODES:
        return [], {
            "device_type": device_type,
            "error": f"unknown appliance type: {device_type}",
            "known_types": sorted(IR_CODES),
        }

    function = _normalize(function) or DEFAULT_FUNCTION
    if function not in FUNCTIONS:
        return [], {
            "device_type": device_type,
            "error": f"unknown function: {function}",
            "known_functions": sorted(FUNCTIONS),
        }

    brands = IR_CODES[device_type]
    resolved, kind = match_brand(brand, device_type) if brand else (None, None)

    if resolved:
        code = _codes_for(brands[resolved], function)
        if code is None:
            return [], {
                "device_type": device_type,
                "brand": resolved,
                "function": function,
                "error": (
                    f"the database has no '{function}' code for {resolved} "
                    f"{device_type}"
                ),
                "available_functions": known_functions(device_type, resolved),
            }
        plan = {
            "device_type": device_type,
            "brand": resolved,
            "function": function,
            "match": kind,
            "codes": 1,
        }
        if kind == "closest":
            plan["requested_brand"] = _normalize(brand)
            plan["note"] = (
                f"brand '{_normalize(brand)}' is not in the database; "
                f"'{resolved}' is the closest match and was used instead"
            )
        return [code], plan

    # No brand, or nothing close enough: every brand for this appliance type that has
    # the requested function. This is the only case where it behaves like a classic
    # bruteforce, and it is still filtered - brands lacking the function are skipped.
    codes = []
    covered = []
    for name, entry in brands.items():
        code = _codes_for(entry, function)
        if code is not None:
            codes.append(code)
            covered.append(name)

    plan = {
        "device_type": device_type,
        "brand": None,
        "function": function,
        "match": "all_brands",
        "codes": len(codes),
        "brands_tried": sorted(covered),
    }
    if brand:
        plan["requested_brand"] = _normalize(brand)
        plan["note"] = (
            f"brand '{_normalize(brand)}' did not match anything in the database, "
            "so codes for every known brand of this appliance type will be tried"
        )
    if not codes:
        plan["error"] = f"no brand of {device_type} has a '{function}' code in the database"
    return codes, plan
