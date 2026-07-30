"""Flipper simulat, cu aceeasi interfata ca CFPClient.

Serveste la dezvoltarea si testarea agentului fara dispozitivul fizic conectat.
Raspunde ca firmware-ul real: reusesc doar comenzile pe care acesta le implementeaza
(ping, info, subghz.rssi), iar orice altceva primeste ERR unknown_command.

Valorile intoarse de subghz.rssi sunt fictive. Ele nu pot fi confundate cu masuratori
reale: clientul e marcat 'simulated', iar marcajul insoteste fiecare rezultat trimis
modelului (vezi CommandDispatcher._result).
"""

from protocol import CFPError

IMPLEMENTED = {
    "ping": ["pong"],
    "info": ["Flipper", "Zero", "(simulat)"],
}

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

    def __init__(self):
        self.calls = []

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
