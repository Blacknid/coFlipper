"""Runtime lookup of IR codes in the IRDB database, over a CDN.

The local table in `ir_codes` is small, hand-checked and instant, which is what the
first attempts should use. This module is the fallback for when those codes do not
work: it reaches for probonopd/irdb, a community database of many thousands of
remotes, and pulls the ones matching the brand and appliance at hand.

The database is deliberately *not* bundled. Its own guidance is to access it at
runtime so that updates arrive automatically, and it is far too large to ship
besides. Requests go through jsDelivr rather than raw GitHub: it is a CDN built for
this traffic, and raw.githubusercontent.com rate-limits quickly.

The CSV format is:

    functionname,protocol,device,subdevice,function
    KEY_POWER,NEC,131,241,10

`device`/`subdevice`/`function` are decimal, and the pair (device, subdevice) is what
the Flipper calls the address. A subdevice of -1 means the protocol carries no
subdevice, which is the difference between plain NEC and NECext - getting this wrong
produces a well-formed burst that no appliance answers.
"""

import csv
import io
import re
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://cdn.jsdelivr.net/gh/probonopd/irdb@master/codes"
INDEX_URL = f"{BASE}/index"

# The index is ~100 KB and the per-remote files are ~1 KB, so a short timeout is
# generous. A stubborn appliance is an interactive moment: the user is standing there
# pointing the Flipper, and a slow lookup is worse than none.
TIMEOUT_S = 20

# How many codes a fallback run may send. The database holds dozens of remotes per
# brand, and firing all of them would take minutes; the queue on the device is 32.
MAX_FALLBACK_CODES = 24

# IRDB protocol names mapped onto the ones the Flipper firmware knows. Anything absent
# is a protocol the device cannot transmit, and its codes are skipped rather than sent
# under a wrong name - the firmware would reject them, or worse, transmit nonsense.
PROTOCOL_MAP = {
    "NEC": "NEC",
    "NEC1": "NEC",
    "NEC2": "NEC",
    "NECx1": "NEC",
    "NECx2": "NEC",
    "Samsung": "Samsung32",
    "Samsung32": "Samsung32",
    "Samsung36": "Samsung32",
    "Sony12": "SIRC",
    "Sony15": "SIRC15",
    "Sony20": "SIRC20",
    "RC5": "RC5",
    "RC5x": "RC5X",
    "RC6": "RC6",
    "Kaseikyo": "Kaseikyo",
    "Panasonic": "Kaseikyo",
    "Panasonic2": "Kaseikyo",
    "RCA": "RCA",
    "Pioneer": "Pioneer",
}

# The function names IRDB uses vary by remote ("POWER", "KEY_POWER", "POWER TOGGLE").
# Matched case-insensitively, longest first, against the column.
FUNCTION_PATTERNS = {
    "power": (r"^key_power$", r"^power$", r"^power toggle$", r"^power on/off$",
              r"^powertoggle$", r"power"),
    "volume_up": (r"^key_volumeup$", r"^volume \+$", r"^volume up$", r"^vol\+$",
                  r"volume ?\+", r"volume ?up"),
    "volume_down": (r"^key_volumedown$", r"^volume -$", r"^volume down$", r"^vol-$",
                    r"volume ?-", r"volume ?down"),
    "mute": (r"^key_mute$", r"^mute$", r"mute"),
    "channel_up": (r"^key_channelup$", r"^channel \+$", r"^channel up$",
                   r"channel ?\+", r"channel ?up"),
    "channel_down": (r"^key_channeldown$", r"^channel -$", r"^channel down$",
                     r"channel ?-", r"channel ?down"),
    "input": (r"^key_input$", r"^input$", r"^input source$", r"^p\.input$",
              r"^source$", r"input", r"source"),
}

# Appliance types mapped onto the folder names IRDB uses. The database names the
# category per remote, and the spellings are not consistent, so several are accepted.
TYPE_FOLDERS = {
    "tv": ("TV", "Television"),
    "projector": ("Projector", "Video Projector"),
    "audio": ("Audio", "Receiver", "Amp", "Amplifier", "Soundbar", "Audio Amp",
              "Audio Receiver", "CD", "Stereo"),
    "ac": ("Air Conditioner", "AC", "Heater", "Air Purifier"),
}

_index_cache = None


class IrdbError(Exception):
    """A lookup could not be completed - network, or nothing matching found."""


def _fetch(url):
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise IrdbError(f"could not reach the IR database ({exc.reason})") from exc
    except OSError as exc:
        raise IrdbError(f"could not reach the IR database ({exc})") from exc


def load_index(refresh=False):
    """The list of every remote file in the database, fetched once per session."""
    global _index_cache
    if _index_cache is None or refresh:
        text = _fetch(INDEX_URL)
        _index_cache = [line.strip() for line in text.splitlines() if line.strip()]
    return _index_cache


def find_remotes(brand, device_type=None, limit=12):
    """Paths of the remote files matching a brand, most relevant first.

    Matching is deliberately loose on the brand (the database spells manufacturers
    inconsistently) but ordered so that entries of the right appliance type come
    first: a TV remote is a better guess for a TV than the same brand's DVD player.
    """
    index = load_index()
    wanted = brand.strip().lower()
    if not wanted:
        return []

    folders = TYPE_FOLDERS.get(device_type, ()) if device_type else ()
    folders_lower = tuple(f.lower() for f in folders)

    preferred, other = [], []
    for path in index:
        parts = path.split("/")
        if len(parts) < 3:
            continue
        if parts[0].strip().lower() != wanted:
            continue
        category = parts[1].strip().lower()
        if folders_lower and (
            category in folders_lower or any(f in category for f in folders_lower)
        ):
            preferred.append(path)
        else:
            other.append(path)

    # Without a type filter everything is equally relevant; with one, the right
    # category leads and the rest follow as a long shot.
    return (preferred + other)[:limit]


def brands_in_index(device_type=None):
    """Every manufacturer the database carries, optionally for one appliance type."""
    index = load_index()
    folders = tuple(f.lower() for f in TYPE_FOLDERS.get(device_type, ())) if device_type else ()

    names = set()
    for path in index:
        parts = path.split("/")
        if len(parts) < 3:
            continue
        if folders:
            category = parts[1].strip().lower()
            if not (category in folders or any(f in category for f in folders)):
                continue
        names.add(parts[0])
    return sorted(names)


def _matches_function(name, function):
    """Whether a CSV function name is the button we are after."""
    patterns = FUNCTION_PATTERNS.get(function)
    if not patterns:
        return False
    lowered = name.strip().lower()
    return any(re.search(p, lowered) for p in patterns)


def _to_flipper(protocol, device, subdevice, function):
    """One IRDB row as the (protocol, address, command) triple the firmware wants.

    Returns None when the protocol is not one the Flipper can transmit.

    The address is where the subtlety lives: IRDB splits it into device and
    subdevice, and a subdevice of -1 means there is none. For NEC that is exactly
    the plain/extended distinction - an extended remote addressed with only its
    device byte transmits cleanly and is ignored by the appliance.
    """
    flipper_protocol = PROTOCOL_MAP.get(protocol)
    if not flipper_protocol:
        return None

    try:
        device = int(device)
        subdevice = int(subdevice)
        command = int(function)
    except (TypeError, ValueError):
        return None

    if device < 0 or command < 0:
        return None

    # Some entries are placeholders rather than captured remotes: device 0 with no
    # subdevice addresses nothing, and sending those wastes slots in a queue that only
    # holds 32 and time the user spends waiting with the Flipper pointed at the room.
    if device == 0 and subdevice < 0:
        return None

    if subdevice >= 0:
        address = (subdevice << 8) | device
        # A NEC remote that carries a subdevice is an extended one; saying plain NEC
        # would truncate the address to its low byte.
        if flipper_protocol == "NEC":
            flipper_protocol = "NECext"
    else:
        address = device

    return (flipper_protocol, f"0x{address:02X}", f"0x{command:02X}")


def codes_from_remote(path, function):
    """Every code for one button, from one remote file."""
    # Many categories contain spaces ("Rear Projection DLP TV"); the path has to be
    # escaped or the request is rejected before it leaves the machine.
    text = _fetch(f"{BASE}/{urllib.parse.quote(path)}")
    found = []
    for row in csv.DictReader(io.StringIO(text)):
        name = (row.get("functionname") or "").strip()
        if not _matches_function(name, function):
            continue
        triple = _to_flipper(
            (row.get("protocol") or "").strip(),
            row.get("device"),
            row.get("subdevice"),
            row.get("function"),
        )
        if triple:
            found.append(triple)
    return found


def lookup(brand, device_type, function, limit=MAX_FALLBACK_CODES, max_remotes=12):
    """Codes for a brand/appliance/button, gathered from the online database.

    Returns (codes, report). The report says which remotes were consulted, so the
    agent can tell the user where the codes came from rather than presenting them as
    if they had been known all along.
    """
    remotes = find_remotes(brand, device_type, limit=max_remotes)
    if not remotes:
        available = brands_in_index(device_type)
        raise IrdbError(
            f"the database has no remote for brand {brand!r}"
            + (f" under {device_type}" if device_type else "")
            + f"; it knows {len(available)} brands for this appliance type"
        )

    codes, consulted, seen = [], [], set()
    for path in remotes:
        try:
            found = codes_from_remote(path, function)
        except IrdbError:
            # One unreadable file must not sink the whole lookup.
            continue
        if not found:
            continue

        consulted.append(path)
        for triple in found:
            if triple in seen:
                continue
            seen.add(triple)
            codes.append(triple)
            if len(codes) >= limit:
                break
        if len(codes) >= limit:
            break

    if not codes:
        raise IrdbError(
            f"found {len(remotes)} {brand} remote(s) but none carried a "
            f"{function.replace('_', ' ')} button the Flipper can transmit"
        )

    return codes, {
        "source": "irdb",
        "brand": brand,
        "device_type": device_type,
        "function": function,
        "remotes_consulted": consulted,
        "codes": len(codes),
    }
