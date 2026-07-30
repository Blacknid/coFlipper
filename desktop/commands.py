"""The bridge between the command catalog (commands.json) and the tools exposed to Gemini.

The catalog is the single source of truth: a new command is added there, and the model
receives it automatically as a callable tool, with no changes to the agent's code.
"""

import json
import pathlib

from google.genai import types

CATALOG_PATH = pathlib.Path(__file__).resolve().parent.parent / "commands.json"

# Commands marked "planned" exist only as design intent: the firmware does not know
# them yet, so they have no business being in the model's tool list.
AVAILABLE_STATUSES = ("implemented", "stub")

_JSON_TYPE_TO_SCHEMA = {
    "string": types.Type.STRING,
    "integer": types.Type.INTEGER,
    "number": types.Type.NUMBER,
    "boolean": types.Type.BOOLEAN,
}


def load_catalog(path=CATALOG_PATH):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def device_commands(catalog, statuses=AVAILABLE_STATUSES):
    """The commands the model is allowed to call.

    Not everything the device knows should be offered to the model: commands marked
    'agent_visible': false are for internal use (for example closing the application)
    and have no business being in its tool list.
    """
    return [
        cmd
        for cmd in catalog["commands"]
        if cmd.get("layer") == "device"
        and cmd.get("status") in statuses
        and cmd.get("agent_visible", True)
    ]


def tool_name(cfp_name):
    """'subghz.info' -> 'subghz_info' (Gemini function names cannot contain dots)."""
    return cfp_name.replace(".", "_")


def _parameters_schema(command):
    args = command.get("args") or []
    if not args:
        return None

    properties = {}
    required = []
    for arg in args:
        properties[arg["name"]] = types.Schema(
            type=_JSON_TYPE_TO_SCHEMA[arg["type"]],
            description=arg.get("description", ""),
        )
        if arg.get("required"):
            required.append(arg["name"])

    return types.Schema(type=types.Type.OBJECT, properties=properties, required=required)


def build_tool(commands):
    declarations = []
    for command in commands:
        description = command["description"]
        if command.get("status") == "stub":
            description += " (Warning: currently not implemented in firmware, will respond ERR.)"
        declarations.append(
            types.FunctionDeclaration(
                name=tool_name(command["name"]),
                description=description,
                parameters=_parameters_schema(command),
            )
        )
    return types.Tool(function_declarations=declarations)


class CommandDispatcher:
    """Translates a tool call received from the model into a CFP request to the Flipper."""

    def __init__(self, commands, client):
        self._by_tool_name = {tool_name(cmd["name"]): cmd for cmd in commands}
        self._client = client

    @property
    def commands(self):
        return list(self._by_tool_name.values())

    def _positional_args(self, command, call_args):
        # CFP v1 passes arguments positionally, in the order given in the catalog.
        values = []
        for arg in command.get("args") or []:
            if arg["name"] in call_args:
                values.append(str(call_args[arg["name"]]))
        return values

    @property
    def simulated(self):
        return getattr(self._client, "simulated", False)

    def dispatch(self, name, call_args):
        """Returns a dict, the shape Gemini expects as a tool response."""
        command = self._by_tool_name.get(name)
        if command is None:
            return self._result({"status": "error", "error": f"comanda necunoscuta: {name}"})

        args = self._positional_args(command, call_args or {})
        try:
            data = self._client.request(command["name"], *args)
        except Exception as exc:  # eroare de protocol sau de port serial
            return self._result({"status": "error", "error": str(exc)})
        return self._result({"status": "ok", "data": data})

    def _result(self, outcome):
        # Cand datele vin de la simulator, marcajul insoteste fiecare rezultat: modelul
        # trebuie sa poata distinge o masuratoare reala de una inventata de simulator.
        if self.simulated:
            outcome["simulated"] = True
        return outcome
