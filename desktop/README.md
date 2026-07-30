# desktop/ — the coFlipper agent

The component that runs on the computer: it interprets user requests with the help of the Gemini model and translates them into CFP commands sent to the Flipper Zero. The protocol is documented in [/PROTOCOL.md](../PROTOCOL.md), the command catalog in [/commands.json](../commands.json).

## Installation

    pip install -r requirements.txt

Then copy `.env.example` to `.env` and fill in the key obtained from [Google AI Studio](https://aistudio.google.com/apikey):

    GEMINI_API_KEY=your_key

The `.env` file is excluded from git and must not be published.

### Choosing the model

The model is set explicitly in `agent.py` and can be changed, without modifying the code, through the `COFLIPPER_MODEL` environment variable. We deliberately avoided aliases of the `gemini-flash-latest` kind: these always track the most recent generation, and usage limits differ substantially from one generation to the next.

Practical findings from during development, on the free plan:

- the limit is approximately **20 requests per day for each model** among the recent generations (verified on `gemini-3.6-flash` and `gemini-3.5-flash`). A single exchange of messages can consume two or three requests, since every round of tool calls needs an additional request, so the limit is reached quickly;
- the quota is counted separately for each model, so switching to another model through `COFLIPPER_MODEL` provides a fresh quota;
- the `gemini-2.0-flash` and `gemini-2.0-flash-lite` models are not available on the free plan (they respond with `limit: 0`), and `gemini-2.5-flash` and `gemini-2.5-flash-lite` are no longer accessible to new keys (404 error). `list_models.py` shows the models accessible to the configured key.

Consequence for development: work on the interface is done in `--mock` mode wherever possible, and requests to the model are reserved for the checks that actually need them. For a public demonstration it is worth checking the remaining quota ahead of time.

Changing the model for the current session, when one model's quota has been exhausted:

    set COFLIPPER_MODEL=gemini-3.5-flash-lite
    python gui.py

## Running

The graphical application, the usual way to use the project:

    python gui.py             # with a Flipper Zero connected over USB
    python gui.py --mock      # without a physical device, for development

The same functionality is available in the console as well, useful when debugging:

    python agent.py
    python agent.py --mock

In `--mock` mode, commands do not reach a real device: they are served by a simulated Flipper that responds exactly like the firmware (`ping` and `info` succeed, the rest return `not_implemented`). This mode is useful for working on the agent side when the device is not at hand.

### Signalling simulated mode

Simulated mode raises an honesty problem that we only discovered while using the application: the simulator returns plausible responses, and the model, having no way to know they come from a simulator, would inform the user that the device is connected and working normally. The statement was false, even though no element of the code was, formally, wrong.

The solution has three components that complement one another:

- every tool result produced in simulated mode contains the `simulated` field, which the model receives directly;
- the system instruction receives an additional section that explicitly forbids stating that a device is connected, and requires signalling the provenance of the data in every response;
- the interface uses yellow instead of green for the connection status, shows a warning at startup, and marks every result with "(simulated result)".

Without the first measure, the restriction in the instruction would have remained a mere recommendation, one the model had no way to apply: nothing in the data it received indicated that it was inside a simulation.

## The graphical interface

The window is split into two panels. On the left is the conversation proper, on the right the list of commands actually sent to the Flipper Zero, with their arguments and responses.

This second area is not a simple debugging log, but a design decision: an agent that phrases answers in natural language risks appearing to know things it has not measured. By permanently displaying the commands executed on the device, the user can check whether the agent's statements are backed by real data — and, in the case of an error returned by the hardware, sees exactly what failed.

The top bar shows the connection status (green for connected, red for error), the serial port in use and the active language model.

## Bringing up the connection

Both the agent and the manual console go through `connect()` in `cfp_client.py`, which handles the two steps a CFP session needs:

1. Finding the serial port, by USB VID/PID rather than by description (on Windows the Flipper shows up as a nondescript "USB Serial Device").
2. Launching the coFlipper application on the device, if it is not already open.

The second step matters more than it looks: the `cfp` command exists only while our application is running, because it is that application which registers the command into the Flipper's CLI. With it closed, every request fails with `could not find command cfp`. To launch it, the client briefly speaks the Flipper's *native* CLI (`loader info` to see what is open, `loader open` to start our application), then hands the port over to the CFP session proper.

If the application is already open, it is detected and left alone. Use `--no-launch` to skip this step entirely and assume the application is running.

Two device-side details worth knowing: `loader open` needs the full `.fap` path, since it resolves plain names only for built-in applications; and `loader close` does not work on our application, which exits only on a Back event — the `cfp <id> exit` command is what closes it remotely.

### Manual console

    python cfp_client.py                      # interactive, auto-detected port
    python cfp_client.py --port COM12         # explicit port
    python cfp_client.py -c "ping" -c "info"  # run commands and exit
    python cfp_client.py --list-ports         # list serial ports and exit
    python cfp_client.py --no-launch          # assume the application is already open

It can also be used as a module, which is how `agent.py` obtains its client:

    from cfp_client import connect

    with connect() as flipper:
        print(flipper.request("ping"))

## Files

| File | Role |
|---|---|
| gui.py | the graphical application (Tkinter) |
| agent.py | the agent proper: the conversation loop and orchestration of tool calls |
| commands.py | conversion of the commands.json catalog into Gemini tools, and dispatching of calls |
| device.py | the device connection used by the interface, on a background thread |
| protocol.py | implementation of the CFP client over the serial port |
| cfp_client.py | connection setup (port detection + launching the application) and a console for sending CFP commands manually, without a language model |
| mock_flipper.py | a simulated Flipper, with the same interface as the real client |
| test_gemini.py | a minimal check of the connection to the Gemini API |
| list_models.py | lists the models available to the configured key |

## Verification status

The full loop model → tool → CFP command → response → final phrasing has been tested against the real Gemini API, both in simulated mode and on a physical Flipper Zero (Momentum firmware `mntm-012`, USB serial port).

Scenarios verified on the physical device:

- querying device state (`ping`, `info`), with two tools called within a single conversation turn;
- measuring the signal level on a frequency indicated by the user, with the value interpreted in natural language;
- comparing two frequencies, with the agent deciding on its own to perform two successive measurements and formulate a conclusion;
- requesting a physically impossible frequency (2.4 GHz), in which case the agent reported the error returned by the device and correctly explained the hardware limitation, without inventing a measurement.

The graphical interface was verified separately, with the simulated device: connecting, correct enabling of the controls, sending a request, displaying the executed commands and the final response, as well as handling errors without freezing the window.
