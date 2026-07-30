"""A simulated Flipper, with the same interface as CFPClient.

Serveste la dezvoltarea si testarea agentului fara dispozitivul fizic conectat.
Raspunde ca firmware-ul real: reusesc doar comenzile pe care acesta le implementeaza
(ping, info, subghz.rssi si comenzile ir.*), comenzile marcate ca stub raspund ERR
not_implemented, iar orice altceva primeste ERR unknown_command.

Valorile intoarse de subghz.rssi sunt fictive. Ele nu pot fi confundate cu masuratori
reale: clientul e marcat 'simulated', iar marcajul insoteste fiecare rezultat trimis
modelului (vezi CommandDispatcher._result).

Comenzile de bruteforce IR sunt simulate cu stare reala (o coada, un contor), fiindca
partea de desktop le interogheaza in bucla: un stub care ar raspunde mereu la fel ar
lasa acea bucla sa se roteasca pana la expirarea timpului.
"""

from protocol import CFPError

IMPLEMENTED = {
    "ping": ["pong"],
    "info": ["Flipper", "Zero", "(simulated)"],
}

# Comenzi pe care firmware-ul le recunoaste, dar nu le-a implementat inca: raspund
# not_implemented, nu unknown_command, ca agentul sa vada aceeasi diferenta ca pe
# dispozitivul real.
STUBS = ("subghz.info", "ir.info", "nfc.info")

# Comenzile de bruteforce IR servite cu stare simulata de _ir_request.
IR_COMMANDS = ("ir.reset", "ir.queue", "ir.bruteforce", "ir.status")

# Benzile in care emitatorul-receptor CC1101 al dispozitivului poate lucra. Firmware-ul
# respinge orice frecventa din afara lor, deci simulatorul trebuie sa faca la fel:
# altfel agentul ar fi dezvoltat pe un dispozitiv mai permisiv decat cel real.
SUBGHZ_BANDS = (
    (300_000_000, 348_000_000),
    (387_000_000, 464_000_000),
    (779_000_000, 928_000_000),
)

# Pasul de sinteza al CC1101: frecventa efectiv folosita e un multiplu al lui, nu exact
# cea ceruta. Firmware-ul raporteaza valoarea reala, iar simulatorul o imita.
SUBGHZ_STEP_HZ = 397


class MockCFPClient:
    # Marcaj citit de CommandDispatcher si transmis modelului la fiecare rezultat, ca
    # sa nu poata prezenta date simulate drept masuratori reale.
    simulated = True

    def __init__(self, stop_after=None):
        self.calls = []
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

    def request(self, cmd, *args):
        self.calls.append((cmd, args))
        if cmd in IMPLEMENTED:
            return IMPLEMENTED[cmd]
        if cmd in IR_COMMANDS:
            return self._ir_request(cmd, args)
        if cmd == "subghz.rssi":
            return self._subghz_rssi(args)
        if cmd in STUBS:
            raise CFPError("not_implemented")
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
        # Valoare fictiva, dar stabila pentru aceeasi frecventa: o masuratoare care se
        # schimba la fiecare apel ar face imposibila compararea a doua rulari de test.
        decidbm = 600 + (frequency // 100_000) % 400
        # Impartirea se face pe valoarea pozitiva: pentru numere negative, // rotunjeste
        # in jos si ar transforma -75.5 dBm in -76.5 dBm.
        return [str(actual), f"-{decidbm // 10}.{decidbm % 10}"]
