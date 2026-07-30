"""A connection that keeps track of the device's presence in real time.

It exposes the same interface as CFPClient (the request method), so it can be used
directly by CommandDispatcher: that one does not have to know whether the device is
present or not. The difference is that the device can be plugged in or unplugged while
the program is running, and the state is refreshed by repeated calls to poll().
"""

import threading

from cfp_client import find_flipper_ports
from protocol import CFPClient, CFPError

# The serial port exists only while the Flipper is plugged in, but the presence of the
# port does not guarantee that the CFP application is running on the device: these are
# two distinct states, and the user needs different guidance in each of them.
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
        # Locks out concurrent access: the thread that carries the conversation sends
        # commands, while the monitoring thread may close or open the port underneath it.
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
                raise  # error reported by the device, the connection itself is fine
            except Exception:
                self._drop()
                raise CFPError("conexiunea cu dispozitivul s-a intrerupt")

    def poll(self):
        """Re-evaluates the state. Returns the new state if it changed, otherwise None."""
        with self._lock:
            ports = find_flipper_ports()

            if self._port and self._port not in ports:
                self._drop()
                return self.state

            if self.state == CONNECTED:
                # Losing the connection shows up either as the port disappearing or as a
                # failing command, so we do not interrogate the device on every cycle.
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
