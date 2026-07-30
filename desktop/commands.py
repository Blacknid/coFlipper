"""Puntea dintre catalogul de comenzi (commands.json) si uneltele expuse modelului Gemini.

Catalogul este singura sursa de adevar: o comanda noua se adauga acolo, iar modelul
o primeste automat ca unealta apelabila, fara modificari in codul agentului.
"""

import json
import pathlib

from google.genai import types

CATALOG_PATH = pathlib.Path(__file__).resolve().parent.parent / "commands.json"

# Comenzile marcate "planned" exista doar ca intentie de proiectare: firmware-ul
# nu le cunoaste inca, deci nu au ce sa caute in lista de unelte a modelului.
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
    """Comenzile pe care modelul le poate apela.

    Nu tot ce stie dispozitivul trebuie oferit modelului: comenzile marcate
    'agent_visible': false sunt de uz intern (de exemplu inchiderea aplicatiei)
    si nu au ce sa caute in lista lui de unelte.
    """
    return [
        cmd
        for cmd in catalog["commands"]
        if cmd.get("layer") == "device"
        and cmd.get("status") in statuses
        and cmd.get("agent_visible", True)
    ]


def tool_name(cfp_name):
    """'subghz.info' -> 'subghz_info' (numele de functii Gemini nu contin puncte)."""
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
            description += " (Atentie: momentan neimplementata in firmware, va raspunde ERR.)"
        declarations.append(
            types.FunctionDeclaration(
                name=tool_name(command["name"]),
                description=description,
                parameters=_parameters_schema(command),
            )
        )
    return types.Tool(function_declarations=declarations)


class CommandDispatcher:
    """Traduce un apel de unealta primit de la model intr-o cerere CFP catre Flipper."""

    def __init__(self, commands, client):
        self._by_tool_name = {tool_name(cmd["name"]): cmd for cmd in commands}
        self._client = client

    @property
    def commands(self):
        return list(self._by_tool_name.values())

    def _positional_args(self, command, call_args):
        # CFP v1 transmite argumentele pozitional, in ordinea din catalog.
        values = []
        for arg in command.get("args") or []:
            if arg["name"] in call_args:
                values.append(str(call_args[arg["name"]]))
        return values

    def dispatch(self, name, call_args):
        """Returneaza un dict, forma pe care Gemini o asteapta ca raspuns de unealta."""
        command = self._by_tool_name.get(name)
        if command is None:
            return {"status": "error", "error": f"comanda necunoscuta: {name}"}

        args = self._positional_args(command, call_args or {})
        try:
            data = self._client.request(command["name"], *args)
        except Exception as exc:  # eroare de protocol sau de port serial
            return {"status": "error", "error": str(exc)}
        return {"status": "ok", "data": data}
