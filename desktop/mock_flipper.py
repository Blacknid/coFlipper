"""A simulated Flipper, with the same interface as CFPClient.

Used for developing and testing the agent without the physical device connected.
Responds like the real firmware: only the commands the firmware implements succeed
(ping, info, subghz.rssi, subghz.read, subghz.replay and the ir.* bruteforce commands),
commands marked as stubs answer ERR not_implemented, anything else gets ERR unknown_command.

The Wi-Fi/BLE commands (categories 'wifi' and 'ble' in commands.json) are served here by
a small Marauder simulator, so the whole Wi-Fi feature - scanning, listing, selecting a
target, attacking, sniffing, BLE - can be exercised end to end without the ESP32 board.
Its data is just as fictitious as subghz.rssi's, and carries the same 'simulated' marking.

The IR bruteforce commands are simulated with real state (a queue, a counter), because
the desktop side polls them in a loop: a stub that always answered the same would let
that loop spin until it timed out.

The values returned by subghz.rssi are fictitious. They cannot be mistaken for real
measurements: the client is marked 'simulated', and that marking accompanies every
result sent to the model (see CommandDispatcher._result).
"""

import random

from protocol import CFPError

IMPLEMENTED = {
    "ping": ["pong"],
    "info": ["Flipper", "Zero", "(simulated)"],
}

# Commands the firmware recognises but has not implemented yet: they answer
# not_implemented rather than unknown_command, so the agent sees the same distinction it
# would on the real device.
STUBS = ("subghz.info", "ir.info", "nfc.info")

# The IR bruteforce commands, served with simulated state by _ir_request.
IR_COMMANDS = ("ir.reset", "ir.queue", "ir.bruteforce", "ir.status")

# The bands the device's CC1101 transceiver is able to work in. The firmware rejects any
# frequency outside them, so the simulator has to do the same: otherwise the agent would
# be developed against a device more permissive than the real one.
SUBGHZ_BANDS = (
    (300_000_000, 348_000_000),
    (387_000_000, 464_000_000),
    (779_000_000, 928_000_000),
)

# The CC1101's synthesis step: the frequency actually used is a multiple of it, not
# exactly the one requested. The firmware reports the real value, and the simulator
# imitates that.
SUBGHZ_STEP_HZ = 397

# Tenths of a dBm added to successive readings of the same frequency. A real signal level
# never repeats exactly, and the scanner subagent judges precisely by that variation - how
# much the values move, what maximum they reach. A simulator returning one constant would
# make its whole line of reasoning vacuous, and would have let us ship a subagent whose
# premise was never exercised without hardware.
# The sequence is fixed rather than random, so two runs of the test suite still agree.
SUBGHZ_JITTER = (0, -12, 7, -4, 15, -9, 3, -6)

# The Sub-GHz "neighbourhood" subghz.read decodes, keyed by the ISM band the requested
# frequency falls in. Each tuple is one device the Flipper's CC1101 can pick up and decode:
# (protocol, key, bit-length, rssi dBm, a plain-English guess at the source). Deterministic,
# like the Wi-Fi fixtures, so the listener subagent harvests the same set every run and the
# feature is testable. The point of having SEVERAL per band is exactly the user's scenario:
# on 433.92 MHz an electric relay and a doorbell remote are both in the air, and successive
# reads surface them in turn - so the listener ends up with a LIST, not a single code.
#
# The keys are obviously invented (round hex), so no real remote's rolling code is implied;
# nothing here is transmitted, and every result reaches the model marked 'simulated'.
_SUBGHZ_SIGNALS = {
    # 300-348 MHz band: mostly older car fobs / tyre-pressure sensors.
    (300_000_000, 348_000_000): (
        ("CAME", "0x1A2B3C", 12, -58, "a gate/garage remote (CAME 12-bit)"),
        ("Princeton", "0x00C4D2", 24, -71, "a fixed-code remote (PT2262-style)"),
    ),
    # 387-464 MHz band: the busy 433.92 MHz ISM band - remotes, sensors, doorbells.
    (387_000_000, 464_000_000): (
        ("Nice_FloR_S", "0x3F7A11", 52, -49, "an electric relay / rolling-code actuator"),
        ("Princeton", "0x4E7B90", 24, -62, "a wireless doorbell remote"),
        ("KeeLoq", "0x9C1122AB", 66, -55, "a car key fob (KeeLoq rolling code)"),
        ("Weather_Station", "0xA1B2C3", 40, -78, "an outdoor weather-station sensor"),
    ),
    # 779-928 MHz band: 868/915 MHz - alarms, meters, industrial telemetry.
    (779_000_000, 928_000_000): (
        ("Somfy_Telis", "0x77E0FF", 56, -60, "a motorised-blind remote (Somfy 868 MHz)"),
        ("LoRa_Meter", "0x55AA33", 48, -83, "a smart utility meter (868 MHz telemetry)"),
    ),
}


# A fixed, fictitious Wi-Fi neighbourhood the Marauder simulator hands out. Deterministic
# on purpose: a scan that returned different networks each run would make the feature
# impossible to test or demonstrate reproducibly. Fields: SSID, BSSID, channel, RSSI dBm,
# encryption. The BSSIDs share an obviously invented prefix so no real device is implied.
_AP_FIXTURES = (
    ("Home_WiFi", "A2:14:6B:0C:11:01", 6, -41, "WPA2"),
    ("Office_WiFi", "A2:14:6B:0C:11:02", 11, -58, "WPA2-Enterprise"),
    ("CoffeeShop_Free", "A2:14:6B:0C:11:03", 1, -63, "OPEN"),
    ("NETGEAR_5A", "A2:14:6B:0C:11:04", 6, -72, "WPA2"),
    ("Guest", "A2:14:6B:0C:11:05", 3, -55, "WPA"),
    ("TP-LINK_2G", "A2:14:6B:0C:11:06", 9, -80, "WPA2"),
    ("HiddenNet", "A2:14:6B:0C:11:07", 6, -67, "WPA3"),
    ("AndroidAP_1234", "A2:14:6B:0C:11:08", 4, -49, "WPA2"),
    # A DIGI/RCS-RDS home router (a common ISP in Romania); stands in for "the user's own
    # network in range" so the authorized-deauth flow can be demonstrated end to end. As
    # fictitious as every other fixture, and reaches the model marked 'simulated'.
    ("DIGI_f7c1500", "A2:14:6B:0C:11:09", 1, -44, "WPA2"),
)

# Client stations, each associated with one access point above (by its index). MAC, the
# access-point index it is connected to, RSSI dBm.
_STA_FIXTURES = (
    ("F4:0F:24:3A:10:01", 0, -46),
    ("3C:5A:B4:9C:10:02", 0, -52),
    ("A4:83:E7:7B:10:03", 1, -60),
    ("D0:37:45:1E:10:04", 2, -65),
    ("B8:27:EB:22:10:05", 7, -50),
    # Two clients on DIGI_f7c1500 (AP index 8): a phone and a laptop, so the network can
    # be deauthenticated either as a whole (select_ap 8) or one client at a time.
    ("C8:3D:D4:5F:10:06", 8, -48),
    ("7A:11:2E:90:10:07", 8, -57),
)

# BLE devices the ble.scan simulator hands out. MAC, advertised name, RSSI dBm.
_BLE_FIXTURES = (
    ("4C:00:11:22:33:01", "AirPods", -55),
    ("54:2F:8A:11:22:02", "Mi_Band", -61),
    ("F8:1D:78:33:44:03", "unknown", -70),
    ("A0:6F:AA:55:66:04", "JBL_Flip", -48),
    ("E4:5F:01:77:88:05", "AirTag", -66),
)

# A fixed, fictitious set of NFC tags nfc.read hands out, one per read, cycling through the
# list. Deterministic on purpose, exactly like the Sub-GHz and Wi-Fi fixtures: a read that
# returned a different card each run could not be tested or demonstrated reproducibly.
#
# Each tuple is the RAW technical read the Flipper's NFC frontend produces - what the chip
# announces about itself - and NOTHING about what the card is FOR: the type, the UID, and the
# ISO14443 anticollision bytes (ATQA/SAK) or the ISO15693 marker. Deducing the likely USE (a
# hotel key, a transit pass, a bank card, an amiibo) and looking the tag up online is the
# agent's job, not the reader's - so the simulator hands over only the facts the hardware sees
# and leaves the interpretation to the nfc_identify subagent. The UIDs are obviously invented
# so no real card is implied; nothing here is emulated, and every result reaches the model
# marked 'simulated'. Fields: type, UID, ATQA, SAK, protocol, storage in bytes.
_NFC_FIXTURES = (
    ("Mifare Classic 1K", "04:A1:B2:C3", "0004", "08", "ISO14443-3A", 1024),
    ("NTAG215", "04:5F:2A:9C:71:80:00", "0044", "00", "ISO14443-3A", 504),
    ("Mifare DESFire EV1", "04:7C:11:E2:52:31:80", "0344", "20", "ISO14443-4", 4096),
    ("EMV bank card", "08:3D:9F:6A", "0004", "20", "ISO14443-4", 0),
    ("Mifare Ultralight", "04:E1:22:5B:63:41:80", "0044", "00", "ISO14443-3A", 64),
    ("ISO15693", "E0:04:01:50:8A:2B:3C:4D", "----", "--", "ISO15693", 256),
)

# Offensive/active operations always answer with an authorisation reminder appended, so the
# marking that they disrupt real devices reaches the model even if it skipped the catalog.
_BLE_SPAM = {
    "ble.spam_apple": "apple",
    "ble.spam_android": "android",
    "ble.spam_samsung": "samsung",
    "ble.spam_windows": "windows",
    "ble.spam_all": "all_vendors",
    "ble.spam_airtag": "airtag",
}


class MarauderSim:
    """A minimal Marauder firmware, enough to exercise the whole Wi-Fi/BLE feature.

    It keeps the same state the real firmware does across commands - the captured lists,
    the current target selection, the SSID list, whichever operation is running - so the
    agent's multi-step flows (scan, list, select, attack, stop) behave as they would on the
    board. Every value it returns is fictitious and, like all mock output, reaches the model
    marked 'simulated'; nothing here transmits anything or touches a real network.
    """

    def __init__(self):
        self.channel = None
        self.aps = []  # list of fixture tuples, in captured order
        self.stations = []  # (mac, ap_index, rssi)
        self.ble_devices = []  # (mac, name, rssi)
        self.selected_aps = set()
        self.selected_stations = set()
        self.ssid_list = []
        self.wifi_op = None  # the running Wi-Fi scan/sniff/attack, or None
        self.ble_op = None  # the running BLE operation, or None

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _parse_selection(selector, count):
        """'0', '0-3', '0,4,5' or 'all' -> a set of indices, validated against count."""
        if count == 0:
            raise CFPError("no_list_to_select_from")
        selector = str(selector).strip().lower()
        if selector == "all":
            return set(range(count))
        chosen = set()
        for part in selector.split(","):
            part = part.strip()
            if "-" in part:
                lo, hi = part.split("-", 1)
                span = range(int(lo), int(hi) + 1)
            else:
                span = (int(part),)
            for index in span:
                if not 0 <= index < count:
                    raise CFPError("invalid_selection")
                chosen.add(index)
        if not chosen:
            raise CFPError("invalid_selection")
        return chosen

    @staticmethod
    def _int(args, name):
        if not args:
            raise CFPError(f"missing_{name}")
        try:
            return int(args[0])
        except ValueError:
            raise CFPError(f"invalid_{name}")

    def _selected_target_count(self):
        return len(self.selected_aps) + len(self.selected_stations)

    # -- the command router ------------------------------------------------

    def handle(self, cmd, args):
        handler = getattr(self, "_cmd_" + cmd.replace(".", "_"), None)
        if handler is None:
            # A wifi.*/ble.* name the simulator does not implement is treated exactly as
            # the firmware would treat an unknown one.
            raise CFPError("unknown_command")
        return handler(args)

    # -- board / channel ---------------------------------------------------

    def _cmd_wifi_board_info(self, args):
        return ["board=present", "firmware=Marauder", "version=v0.13.9", "model=ESP32-WROOM-32"]

    def _cmd_marauder_reboot(self, args):
        self.wifi_op = None
        self.ble_op = None
        return ["rebooting", "board_back_in_~3s"]

    def _cmd_wifi_set_channel(self, args):
        channel = self._int(args, "channel")
        if not 1 <= channel <= 14:
            raise CFPError("invalid_channel")
        self.channel = channel
        return ["channel", str(channel)]

    # -- scanning / listing ------------------------------------------------

    def _cmd_wifi_scan_ap(self, args):
        self.aps = list(_AP_FIXTURES)
        self.wifi_op = None  # a bounded scan finishes on its own
        return ["captured", str(len(self.aps)), "access_points"]

    def _cmd_wifi_scan_station(self, args):
        if not self.aps:
            return ["no_access_points_yet", "run", "wifi.scan_ap", "first"]
        self.stations = [s for s in _STA_FIXTURES if s[1] < len(self.aps)]
        return ["captured", str(len(self.stations)), "stations"]

    def _cmd_wifi_list_ap(self, args):
        if not self.aps:
            return ["no_access_points_captured", "run", "wifi.scan_ap", "first"]
        return [
            f"{i};{ssid};{bssid};{ch};{rssi};{enc}"
            for i, (ssid, bssid, ch, rssi, enc) in enumerate(self.aps)
        ]

    def _cmd_wifi_list_station(self, args):
        if not self.stations:
            return ["no_stations_captured", "run", "wifi.scan_station", "first"]
        return [f"{i};{mac};{ap};{rssi}" for i, (mac, ap, rssi) in enumerate(self.stations)]

    def _cmd_wifi_select_ap(self, args):
        if not args:
            raise CFPError("missing_targets")
        self.selected_aps = self._parse_selection(args[0], len(self.aps))
        return ["selected", str(len(self.selected_aps)), "access_points"]

    def _cmd_wifi_select_station(self, args):
        if not args:
            raise CFPError("missing_targets")
        self.selected_stations = self._parse_selection(args[0], len(self.stations))
        return ["selected", str(len(self.selected_stations)), "stations"]

    def _cmd_wifi_clear_targets(self, args):
        self.aps = []
        self.stations = []
        self.selected_aps = set()
        self.selected_stations = set()
        return ["cleared", "lists_and_selection"]

    # -- passive sniffing --------------------------------------------------

    def _sniff(self, op, *extra):
        self.wifi_op = op
        return [op, "started"] + list(extra)

    def _cmd_wifi_sniff_beacon(self, args):
        return self._sniff("sniff_beacon")

    def _cmd_wifi_sniff_probe(self, args):
        return self._sniff("sniff_probe")

    def _cmd_wifi_sniff_deauth(self, args):
        return self._sniff("sniff_deauth", "listening_for_deauth_frames")

    def _cmd_wifi_sniff_pmkid(self, args):
        return self._sniff("sniff_pmkid", "handshakes_seen", "0")

    def _cmd_wifi_sniff_pwnagotchi(self, args):
        return self._sniff("sniff_pwnagotchi")

    def _cmd_wifi_sniff_raw(self, args):
        return self._sniff("sniff_raw")

    def _cmd_wifi_sniff_esp(self, args):
        return self._sniff("sniff_esp")

    # -- offensive / active ------------------------------------------------

    def _cmd_wifi_attack_deauth(self, args):
        targets = self._selected_target_count()
        if targets == 0:
            raise CFPError("no_target_selected")
        self.wifi_op = "attack_deauth"
        return ["deauth", "started", "targets", str(targets), "authorized_use_only"]

    def _cmd_wifi_attack_deauth_all(self, args):
        if not self.aps:
            raise CFPError("no_access_points_captured")
        self.wifi_op = "attack_deauth_all"
        return ["deauth_all", "started", "access_points", str(len(self.aps)), "authorized_use_only"]

    def _cmd_wifi_attack_beacon_random(self, args):
        count = self._int(args, "count") if args else 20
        self.wifi_op = "attack_beacon_random"
        return ["beacon_random", "started", "fake_ssids", str(count), "authorized_use_only"]

    def _cmd_wifi_attack_beacon_list(self, args):
        if not self.ssid_list:
            return ["ssid_list_empty", "add", "with", "wifi.ssid_add", "or", "wifi.ssid_generate"]
        self.wifi_op = "attack_beacon_list"
        return ["beacon_list", "started", "ssids", str(len(self.ssid_list)), "authorized_use_only"]

    def _cmd_wifi_attack_beacon_clone(self, args):
        if not self.selected_aps:
            raise CFPError("no_target_selected")
        cloned = self.aps[sorted(self.selected_aps)[0]][0]
        self.wifi_op = "attack_beacon_clone"
        return ["beacon_clone", "started", "ssid", cloned, "authorized_use_only"]

    def _cmd_wifi_attack_probe_flood(self, args):
        self.wifi_op = "attack_probe_flood"
        return ["probe_flood", "started", "authorized_use_only"]

    def _cmd_wifi_attack_rickroll(self, args):
        self.wifi_op = "attack_rickroll"
        return ["rickroll", "started", "authorized_use_only"]

    def _cmd_wifi_evil_portal(self, args):
        if args:
            ssid = str(args[0])
        elif self.selected_aps:
            ssid = self.aps[sorted(self.selected_aps)[0]][0]
        else:
            ssid = "Free_WiFi"
        self.wifi_op = "evil_portal"
        return ["evil_portal", "started", "ssid", ssid, "authorized_use_only"]

    def _cmd_wifi_karma(self, args):
        self.wifi_op = "karma"
        return ["karma", "started", "authorized_use_only"]

    def _cmd_wifi_wardrive(self, args):
        self.wifi_op = "wardrive"
        return ["wardrive", "started", "logging_to_sd"]

    # -- SSID list / GPS / capture ----------------------------------------

    def _cmd_wifi_ssid_add(self, args):
        if not args:
            raise CFPError("missing_ssid")
        self.ssid_list.append(str(args[0]))
        return ["added", str(args[0]), "total", str(len(self.ssid_list))]

    def _cmd_wifi_ssid_generate(self, args):
        count = self._int(args, "count")
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        for _ in range(max(0, count)):
            self.ssid_list.append("".join(random.choice(alphabet) for _ in range(8)))
        return ["generated", str(max(0, count)), "total", str(len(self.ssid_list))]

    def _cmd_wifi_ssid_list(self, args):
        if not self.ssid_list:
            return ["ssid_list_empty"]
        return list(self.ssid_list)

    def _cmd_wifi_gps_data(self, args):
        # A simulated fix, so wardrive and gps_data are demonstrable; real firmware returns
        # no_fix when no GPS module is attached.
        return ["fix", "lat=45.7538", "lon=21.2257", "satellites=7"]

    def _cmd_wifi_save_pcap(self, args):
        return ["saved", "/ext/apps_data/marauder/capture_001.pcap"]

    def _cmd_wifi_stop(self, args):
        stopped = self.wifi_op or "nothing"
        self.wifi_op = None
        return ["stopped", stopped]

    # -- BLE ---------------------------------------------------------------

    def _cmd_ble_scan(self, args):
        self.ble_devices = list(_BLE_FIXTURES)
        self.ble_op = None
        return ["captured", str(len(self.ble_devices)), "ble_devices"]

    def _cmd_ble_list(self, args):
        if not self.ble_devices:
            return ["no_ble_devices_captured", "run", "ble.scan", "first"]
        return [f"{i};{mac};{name};{rssi}" for i, (mac, name, rssi) in enumerate(self.ble_devices)]

    def _cmd_ble_sniff_airtag(self, args):
        seen = sum(1 for _, name, _ in _BLE_FIXTURES if "airtag" in name.lower())
        self.ble_op = "sniff_airtag"
        return ["sniff_airtag", "started", "trackers_seen", str(seen)]

    def _cmd_ble_detect_flipper(self, args):
        self.ble_op = "detect_flipper"
        return ["detect_flipper", "started", "flippers_seen", "0"]

    def _cmd_ble_wardrive(self, args):
        self.ble_op = "ble_wardrive"
        return ["ble_wardrive", "started", "logging_to_sd"]

    def _cmd_ble_stop(self, args):
        stopped = self.ble_op or "nothing"
        self.ble_op = None
        return ["stopped", stopped]

    def _ble_spam(self, cmd):
        self.ble_op = _BLE_SPAM[cmd]
        return ["ble_spam", _BLE_SPAM[cmd], "started", "authorized_use_only"]

    # The BLE-spam commands share one body; bind each name to it.
    def _cmd_ble_spam_apple(self, args):
        return self._ble_spam("ble.spam_apple")

    def _cmd_ble_spam_android(self, args):
        return self._ble_spam("ble.spam_android")

    def _cmd_ble_spam_samsung(self, args):
        return self._ble_spam("ble.spam_samsung")

    def _cmd_ble_spam_windows(self, args):
        return self._ble_spam("ble.spam_windows")

    def _cmd_ble_spam_all(self, args):
        return self._ble_spam("ble.spam_all")

    def _cmd_ble_spam_airtag(self, args):
        return self._ble_spam("ble.spam_airtag")


class NfcSim:
    """A minimal NFC front-end, enough to exercise the whole identification feature.

    It reports what the real Flipper's NFC reader reports when a tag is presented - the chip
    type, the UID, and the ISO14443 anticollision bytes - and holds a cursor so successive
    reads walk the fixture list, the way tapping several different cards would. Everything it
    returns is fictitious and, like all mock output, reaches the model marked 'simulated';
    nothing here reads or emulates a real card.

    Deliberately, it returns only the raw technical read and no interpretation: the type comes
    from the hardware, but 'what the card is used for' and any online detail are worked out by
    the nfc_identify subagent, never handed to it pre-chewed.
    """

    def __init__(self):
        self._cursor = 0
        self.nfc_op = None

    @staticmethod
    def _read_tokens(fixture):
        card_type, uid, atqa, sak, protocol, storage = fixture
        # 'key=value' tokens so the fields are self-describing on the wire, and a UID with an
        # unusual length or a missing ATQA (ISO15693 has none) cannot be misread positionally.
        tokens = [
            f"type={card_type}",
            f"uid={uid}",
            f"atqa={atqa}",
            f"sak={sak}",
            f"protocol={protocol}",
            f"bytes={storage}",
        ]
        return tokens

    def handle(self, cmd, args):
        if cmd == "nfc.read":
            return self._read(args)
        if cmd == "nfc.watch":
            return self._watch(args)
        if cmd == "nfc.emulate":
            return self._emulate(args)
        if cmd == "nfc.stop":
            stopped = self.nfc_op or "nothing"
            self.nfc_op = None
            return ["stopped", stopped]
        # An nfc.* name the simulator does not implement is treated as the firmware would.
        raise CFPError("unknown_command")

    def _read(self, args):
        """One tag, the next in the fixture list, wrapping round.

        A real read waits for a card and times out with 'no_card' if none arrives; the
        simulator always has a card to present (the demo/test taps one), so it returns the
        current fixture and advances the cursor, so a second read shows a different card
        rather than the same one forever.
        """
        fixture = _NFC_FIXTURES[self._cursor % len(_NFC_FIXTURES)]
        self._cursor += 1
        return self._read_tokens(fixture)

    def _watch(self, args):
        """A short monitoring session: the tags seen, in order, with a relative timestamp.

        Each entry is 'ms;type;uid', so agent.nfc_session_summary can count distinct cards
        and spot a repeat. The first tag is shown twice, at two moments, to stand in for the
        common case of one card tapped against a reader more than once.
        """
        order = (0, 1, 0)
        return [
            f"{(i + 1) * 800};{_NFC_FIXTURES[k][0]};{_NFC_FIXTURES[k][1]}"
            for i, k in enumerate(order)
        ]

    def _emulate(self, args):
        """Emulate a saved card. Offensive: on real hardware the Flipper answers a reader as
        if it were that card, so the reminder is appended exactly as the radio attacks do."""
        if not args:
            raise CFPError("missing_name")
        self.nfc_op = "emulate"
        return ["emulating", str(args[0]), "authorized_use_only"]


class MockCFPClient:
    # A marking read by CommandDispatcher and passed on to the model with every result,
    # so that it cannot present simulated data as real measurements.
    simulated = True

    def __init__(self, stop_after=None):
        self.calls = []
        # How many readings have been taken per frequency, which selects the jitter.
        self._readings = {}
        # How many signals subghz.read has already decoded per frequency, which selects
        # the next one from that band's fixture list. Each read advances the cursor, so a
        # subagent listening across a window harvests the band's devices one after another
        # instead of the same code over and over.
        self._read_cursor = {}
        # Serves every wifi.*/ble.* command, holding the same state the real board would.
        self._marauder = MarauderSim()
        # Serves the nfc.* commands, walking a fixture list of tags across successive reads.
        self._nfc = NfcSim()
        # IR bruteforce state, mirroring CfpIrState in the firmware.
        self._ir_queue = []
        self._ir_sent = 0
        self._ir_running = False
        self._ir_stopped = False
        # Which code the simulated user "reacts" to by pressing OK. None means no code
        # works and the run goes to exhaustion.
        self._stop_after = stop_after

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def _ir_request(self, cmd, args):
        if cmd == "ir.reset":
            self._ir_queue = []
            self._ir_sent = 0
            self._ir_running = False
            self._ir_stopped = False
            return ["cleared"]

        if cmd == "ir.queue":
            if len(args) < 3:
                raise CFPError("missing_code")
            self._ir_queue.append(tuple(args[:3]))
            return [str(len(self._ir_queue))]

        if cmd == "ir.bruteforce":
            if not self._ir_queue:
                raise CFPError("empty_queue")
            self._ir_running = True
            self._ir_stopped = False
            self._ir_sent = 0
            return ["started", str(len(self._ir_queue))]

        if cmd == "ir.status":
            # Each poll advances the run by one code, the way the device does between
            # two transmissions.
            if self._ir_running:
                if self._stop_after is not None and self._ir_sent >= self._stop_after:
                    self._ir_running = False
                    self._ir_stopped = True
                elif self._ir_sent < len(self._ir_queue):
                    self._ir_sent += 1
                else:
                    self._ir_running = False

            if self._ir_running:
                state = "running"
            elif self._ir_stopped:
                state = "stopped"
            else:
                state = "idle"
            return [state, str(self._ir_sent), str(len(self._ir_queue))]

        return None

    def request(self, cmd, *args, timeout=None):
        # Accepted for interface parity with CFPClient.request, which uses it to widen the
        # serial read timeout for a slow real command (nfc.read/nfc.watch). The mock answers
        # instantly regardless, so there is nothing to widen.
        del timeout
        self.calls.append((cmd, args))
        if cmd in IMPLEMENTED:
            return IMPLEMENTED[cmd]
        if cmd in IR_COMMANDS:
            return self._ir_request(cmd, args)
        if cmd == "subghz.rssi":
            return self._subghz_rssi(args)
        if cmd == "subghz.read":
            return self._subghz_read(args)
        if cmd == "subghz.replay":
            return self._subghz_replay(args)
        # The Wi-Fi dev board is served by its own simulator. On real hardware these travel
        # on to the ESP32 over UART; here they are answered locally, with the same state a
        # real board would carry across commands.
        if cmd.startswith(("wifi.", "ble.")) or cmd == "marauder.reboot":
            return self._marauder.handle(cmd, args)
        # Stubs first, so nfc.info still answers 'not_implemented' rather than being routed
        # into the NFC simulator, which knows only the working nfc.* commands.
        if cmd in STUBS:
            raise CFPError("not_implemented")
        # The NFC front-end has its own simulator, holding the read cursor across taps.
        if cmd.startswith("nfc."):
            return self._nfc.handle(cmd, args)
        raise CFPError("unknown_command")

    def _subghz_rssi(self, args):
        if not args:
            raise CFPError("missing_frequency")
        try:
            frequency = int(args[0])
        except ValueError:
            raise CFPError("invalid_frequency")
        if not any(low <= frequency <= high for low, high in SUBGHZ_BANDS):
            raise CFPError("invalid_frequency")

        actual = frequency - frequency % SUBGHZ_STEP_HZ
        # A fictitious value, but a reproducible one: the level for a given frequency is
        # derived from the frequency itself, and successive readings of it move by a fixed
        # sequence of small amounts rather than by chance.
        index = self._readings.get(frequency, 0)
        self._readings[frequency] = index + 1
        decidbm = 600 + (frequency // 100_000) % 400 + SUBGHZ_JITTER[index % len(SUBGHZ_JITTER)]
        # The division is done on the positive value: for negative numbers, // rounds
        # downwards and would turn -75.5 dBm into -76.5 dBm.
        return [str(actual), f"-{decidbm // 10}.{decidbm % 10}"]

    def _subghz_read(self, args):
        """One decoded Sub-GHz signal on a frequency, or a timeout if the air is quiet.

        args: frequency [, timeout_ms]. The timeout is accepted and ignored by the
        simulator (there is no real radio to wait on), but a subagent still passes it,
        so the real firmware and the mock take the same call.

        Successive reads of the same frequency walk that band's fixture list, one signal
        per call, so a listener collects the band's several devices in turn. When the list
        is spent the read returns 'no_signal' rather than repeating - the air has, for the
        purposes of this simulated run, gone quiet, which is the honest thing to report and
        also what makes the listener stop instead of looping to its budget.
        """
        if not args:
            raise CFPError("missing_frequency")
        try:
            frequency = int(args[0])
        except ValueError:
            raise CFPError("invalid_frequency")
        band = next(
            (b for b in SUBGHZ_BANDS if b[0] <= frequency <= b[1]), None
        )
        if band is None:
            raise CFPError("invalid_frequency")

        signals = _SUBGHZ_SIGNALS.get(band, ())
        index = self._read_cursor.get(band, 0)
        self._read_cursor[band] = index + 1
        if index >= len(signals):
            # Nothing left to decode in this band this run.
            return ["no_signal"]

        protocol, key, bits, rssi, _guess = signals[index]
        actual = frequency - frequency % SUBGHZ_STEP_HZ
        # 'signal' marks a decode, distinguishing it from the 'no_signal' timeout above; the
        # fields after it are protocol / key / bit-length / frequency actually synthesised /
        # RSSI, in the fixed order the client and the subagent agree on.
        return ["signal", protocol, key, str(bits), str(actual), str(rssi)]

    def _subghz_replay(self, args):
        """Retransmit a saved .sub file. Offensive: on real hardware this keys the radio.

        args: the path to the .sub file. The simulator transmits nothing (there is no radio),
        it only confirms which file it would have sent, echoing the path back so the agent can
        show the user exactly what was replayed. The firmware would read the file's Frequency
        and Key and re-emit it; here we only acknowledge, and the result is marked simulated
        upstream, so it can never be mistaken for a real transmission.
        """
        if not args:
            raise CFPError("missing_file")
        path = str(args[0])
        return ["replayed", path, "authorized_use_only"]
