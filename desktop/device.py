"""Conexiune care urmareste in timp real prezenta dispozitivului.

Expune aceeasi interfata ca CFPClient (metoda request), deci poate fi folosita direct
de CommandDispatcher: acesta nu trebuie sa stie daca dispozitivul e prezent sau nu.
Diferenta e ca dispozitivul poate fi conectat sau deconectat in timpul rularii, iar
starea se actualizeaza prin apeluri repetate la poll().
"""

import threading

from cfp_client import find_flipper_ports
from protocol import CFPClient, CFPError

# Portul serial exista doar cat timp Flipper-ul e conectat, dar prezenta portului nu
# garanteaza ca aplicatia CFP ruleaza pe dispozitiv: sunt doua stari distincte, iar
# utilizatorul are nevoie de indicatii diferite in fiecare caz.
DISCONNECTED = "disconnected"
NO_APP = "no_app"
CONNECTED = "connected"

STATE_ERRORS = {
    DISCONNECTED: "dispozitiv neconectat: niciun Flipper Zero pe portul serial",
    NO_APP: "aplicatia coFlipper CFP nu ruleaza pe dispozitiv",
}


class LiveDevice:
    simulated = False

    def __init__(self):
        # Blocheaza accesul concurent: firul care poarta conversatia trimite comenzi,
        # in timp ce firul de supraveghere poate inchide sau deschide portul.
        self._lock = threading.RLock()
        self._client = None
        self._port = None
        self.state = DISCONNECTED

    @property
    def port(self):
        return self._port

    def close(self):
        with self._lock:
            self._drop()

    def _drop(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        self._port = None
        self.state = DISCONNECTED

    def request(self, cmd, *args):
        with self._lock:
            if self.state != CONNECTED:
                raise CFPError(STATE_ERRORS[self.state])
            try:
                return self._client.request(cmd, *args)
            except CFPError:
                raise  # eroare raportata de dispozitiv, conexiunea e in regula
            except Exception:
                self._drop()
                raise CFPError("conexiunea cu dispozitivul s-a intrerupt")

    def poll(self):
        """Reevalueaza starea. Returneaza noua stare daca s-a schimbat, altfel None."""
        with self._lock:
            ports = find_flipper_ports()

            if self._port and self._port not in ports:
                self._drop()
                return self.state

            if self.state == CONNECTED:
                # Pierderea conexiunii se observa prin dispariția portului sau prin
                # eroarea unei comenzi, deci nu interogam dispozitivul la fiecare ciclu.
                return None

            if not ports:
                return None

            if self._client is None:
                try:
                    self._client = CFPClient(ports[0])
                    self._port = ports[0]
                except Exception:
                    self._drop()
                    return None

            try:
                self._client.request("ping")
                new_state = CONNECTED
            except CFPError:
                new_state = NO_APP
            except Exception:
                self._drop()
                return self.state

            if new_state == self.state:
                return None
            self.state = new_state
            return self.state
