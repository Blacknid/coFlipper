"""The bridge between the command catalog (commands.json) and the tools exposed to Gemini.

The catalog is the single source of truth: a new command is added there, and the model
receives it automatically as a callable tool, with no changes to the agent's code.
"""

import json
import pathlib
import threading
import time

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
    """The device-layer commands the model is allowed to call.

    Not everything the device knows should be offered to the model: commands marked
    'agent_visible': false are for internal use (for example closing the application,
    or the individual steps of an IR bruteforce, which the agent layer drives itself)
    and have no business being in its tool list.

    Only the 'device' layer is returned here; the desktop-side agent commands come from
    agent_commands(), and model_commands() joins the two. Folding both into one filter
    would double-count the agent commands, since model_commands() adds them again.
    """
    return [
        cmd
        for cmd in catalog["commands"]
        if cmd.get("layer") == "device"
        and cmd.get("status") in statuses
        and cmd.get("agent_visible", True)
    ]


def agent_commands(catalog):
    """The commands carried out by a subagent rather than by the device.

    They live in the same catalog as the device commands, under 'layer': 'agent', and
    each names the subagent that carries it out in its 'subagent' field. Keeping them
    here rather than hard-coding them in the code preserves the catalog's role as the
    single source of truth: delegation is described in the same place as everything else
    the model is allowed to do.
    """
    return [
        cmd
        for cmd in catalog["commands"]
        if cmd.get("layer") == "agent"
        and cmd.get("status") == "implemented"
        and cmd.get("agent_visible", True)
    ]


def model_commands(catalog):
    """Everything the main agent may call: device commands plus delegation to subagents."""
    return device_commands(catalog) + agent_commands(catalog)


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
    """Routes a tool call received from the model to whoever carries it out.

    Three destinations: device-layer commands become CFP requests to the Flipper;
    agent-layer commands with a 'subagent' field summon that subagent; the remaining
    agent-layer commands (currently the IR control/bruteforce) run as a Python
    orchestration on the desktop. The dispatcher is also where the session log is kept,
    since every device command passes through here anyway - and that log is the material
    the analyst subagent researches.
    """

    def __init__(self, commands, client, subagents=None, on_progress=None):
        self._by_tool_name = {tool_name(cmd["name"]): cmd for cmd in commands}
        self._client = client
        # Set after construction in practice: the runner needs the dispatcher in order to
        # execute device commands, so one of the two has to be wired up second.
        self.subagents = subagents
        # Called as a long-running agent command advances (currently the IR bruteforce),
        # so the interface can show progress instead of appearing frozen.
        self._on_progress = on_progress
        # A single serial port serves the device, and subagents run on their own threads.
        # Without this lock two overlapping requests would interleave on the wire and the
        # protocol would desynchronise - responses would be matched to the wrong command.
        self._device_lock = threading.Lock()
        self._log = []
        self._log_start = time.monotonic()

    @property
    def commands(self):
        return list(self._by_tool_name.values())

    @property
    def device_catalog(self):
        """The device commands, as the subagents' tool lists are drawn from these."""
        return [cmd for cmd in self._by_tool_name.values() if cmd.get("layer") == "device"]

    @property
    def log(self):
        return list(self._log)

    def log_text(self):
        """The session log as plain text, the form the analyst subagent receives it in."""
        lines = []
        for entry in self._log:
            args = " ".join(f"{k}={v}" for k, v in entry["args"].items()) or "(no arguments)"
            result = entry["result"]
            if result.get("status") == "ok":
                shown = f"OK {' '.join(result.get('data') or [])}".strip()
            else:
                shown = f"ERR {result.get('error', 'unknown error')}"
            simulated = "  [simulated]" if result.get("simulated") else ""
            lines.append(f"t+{entry['at_s']:.1f}s  {entry['command']} {args}  ->  {shown}{simulated}")
        return "\n".join(lines)

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

    def _dispatch_agent(self, command, call_args):
        """Agent-layer commands that run as a Python orchestration on the desktop.

        They still reach the Flipper, but through several device commands orchestrated
        in Python rather than a single CFP frame. This is the non-subagent kind of
        agent command: it is a function here, not a separate model conversation.
        """
        if command["name"] == "agent.ir_control":
            from ir_bruteforce import bruteforce

            args = call_args or {}
            request = args.get("request", "")
            if not request:
                return {"status": "error", "error": "the 'request' argument is required"}
            return bruteforce(
                self._client,
                request,
                brand=args.get("brand"),
                device_type=args.get("device_type"),
                function=args.get("function"),
                on_progress=self._on_progress,
                force_online=bool(args.get("search_online")),
            )

        return {"status": "error", "error": f"agent command not implemented: {command['name']}"}

    def dispatch(self, name, call_args=None, on_subagent_event=None):
        """Returns a dict, the shape Gemini expects as a tool response.

        on_subagent_event(kind, **fields) is forwarded to a summoned subagent, so its
        progress can be shown while it works instead of only once it has finished.
        """
        command = self._by_tool_name.get(name)
        if command is None:
            return self._result({"status": "error", "error": f"unknown command: {name}"})

        if command.get("layer") == "agent":
            # Two kinds of agent command: those backed by a subagent (a separate model
            # conversation) carry a 'subagent' field; the rest are Python orchestrations.
            if command.get("subagent"):
                return self._dispatch_subagent(command, call_args or {}, on_subagent_event)
            return self._result(self._dispatch_agent(command, call_args or {}))
        return self._device_request(command, call_args or {})

    def dispatch_device(self, name, call_args=None):
        """The device-only path, used by subagents.

        A subagent reaches the device exclusively through here, which is also what stops
        it from summoning further subagents: an agent-layer name is refused rather than
        routed onward.
        """
        command = self._by_tool_name.get(name)
        if command is None or command.get("layer") != "device":
            return self._result({"status": "error", "error": f"unknown device command: {name}"})
        return self._device_request(command, call_args or {})

    def _device_request(self, command, call_args):
        args = self._positional_args(command, call_args)
        try:
            # The lock is held only for the exchange itself, not for the interpretation
            # that follows: a subagent's thinking should not block another's measurement.
            with self._device_lock:
                data = self._client.request(command["name"], *args)
        except Exception as exc:  # protocol error or serial port error
            outcome = self._result({"status": "error", "error": str(exc)})
        else:
            outcome = self._result({"status": "ok", "data": data})
        self._record(command["name"], call_args, outcome)
        return outcome

    def _dispatch_subagent(self, command, call_args, on_event):
        if self.subagents is None:
            return self._result(
                {"status": "error", "error": "subagents are not available in this session"}
            )
        key = command.get("subagent")
        task = self._subagent_task(command, call_args)
        return self._result(self.subagents.run(key, task, emit=on_event))

    def _subagent_task(self, command, call_args):
        """The task handed to the subagent, in words rather than as a command line.

        A subagent is a conversation partner, not a device: it receives the catalog's
        description of the job together with the arguments the model chose, and works out
        for itself which commands that requires.
        """
        given = ", ".join(f"{key}: {value}" for key, value in call_args.items())
        task = command.get("task") or command["description"]
        return f"{task}\n\nParameters given by the main agent: {given or '(none)'}"

    def _record(self, name, args, outcome):
        self._log.append(
            {
                "at_s": time.monotonic() - self._log_start,
                "command": name,
                "args": dict(args),
                "result": outcome,
            }
        )

    def _result(self, outcome):
        # When the data comes from the simulator, the marking accompanies every result:
        # the model has to be able to tell a real measurement from a simulated one.
        if self.simulated:
            outcome["simulated"] = True
        return outcome
