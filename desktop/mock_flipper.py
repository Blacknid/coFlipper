"""A simulated Flipper, with the same interface as CFPClient.

Used for developing and testing the agent without the physical device connected.
Responds like the real firmware: only the commands the firmware implements succeed
(ping, info, subghz.rssi), and anything else gets ERR unknown_command.

The Wi-Fi/BLE commands (categories 'wifi' and 'ble' in commands.json) are served here by
a small Marauder simulator, so the whole Wi-Fi feature - scanning, listing, selecting a
target, attacking, sniffing, BLE - can be exercised end to end without the ESP32 board.
Its data is just as fictitious as subghz.rssi's, and carries the same 'simulated' marking.

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
)

# Client stations, each associated with one access point above (by its index). MAC, the
# access-point index it is connected to, RSSI dBm.
_STA_FIXTURES = (
    ("F4:0F:24:3A:10:01", 0, -46),
    ("3C:5A:B4:9C:10:02", 0, -52),
    ("A4:83:E7:7B:10:03", 1, -60),
    ("D0:37:45:1E:10:04", 2, -65),
    ("B8:27:EB:22:10:05", 7, -50),
)

# BLE devices the ble.scan simulator hands out. MAC, advertised name, RSSI dBm.
_BLE_FIXTURES = (
    ("4C:00:11:22:33:01", "AirPods", -55),
    ("54:2F:8A:11:22:02", "Mi_Band", -61),
    ("F8:1D:78:33:44:03", "unknown", -70),
    ("A0:6F:AA:55:66:04", "JBL_Flip", -48),
    ("E4:5F:01:77:88:05", "AirTag", -66),
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


class MockCFPClient:
    # A marking read by CommandDispatcher and passed on to the model with every result,
    # so that it cannot present simulated data as real measurements.
    simulated = True

    def __init__(self):
        self.calls = []
        # How many readings have been taken per frequency, which selects the jitter.
        self._readings = {}
        # Serves every wifi.*/ble.* command, holding the same state the real board would.
        self._marauder = MarauderSim()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    def request(self, cmd, *args):
        self.calls.append((cmd, args))
        if cmd in IMPLEMENTED:
            return IMPLEMENTED[cmd]
        if cmd == "subghz.rssi":
            return self._subghz_rssi(args)
        # The Wi-Fi dev board is served by its own simulator. On real hardware these travel
        # on to the ESP32 over UART; here they are answered locally, with the same state a
        # real board would carry across commands.
        if cmd.startswith(("wifi.", "ble.")) or cmd == "marauder.reboot":
            return self._marauder.handle(cmd, args)
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
