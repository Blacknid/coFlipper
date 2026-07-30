# desktop/ — the coFlipper agent

The component that runs on the computer: it interprets user requests with the help of the Gemini model and translates them into CFP commands sent to the Flipper Zero. The protocol is documented in [/PROTOCOL.md](../PROTOCOL.md), the command catalog in [/commands.json](../commands.json).

## Installation

    pip install -r requirements.txt

Then copy `.env.example` to `.env` and fill in the key obtained from [Google AI Studio](https://aistudio.google.com/apikey):

    GEMINI_API_KEY=your_key

The `.env` file is excluded from git and must not be published.

### Choosing the model

The model is set explicitly in `agent.py` and can be changed, without modifying the code, through the `COFLIPPER_MODEL` environment variable. We deliberately avoided aliases of the `gemini-flash-latest` kind: these always track the most recent generation, and usage limits differ substantially from one generation to the next.

A practical finding from during development, on the free plan: `gemini-flash-latest` pointed to `gemini-3.6-flash`, limited to 20 requests per day, which was insufficient for development. The `gemini-2.0-flash` and `gemini-2.5-flash` models are not available at all for new API keys — the former respond with `limit: 0`, the latter with a 404 error. `list_models.py` shows the models accessible to the configured key.

## Running

    python agent.py           # with a Flipper Zero connected over USB
    python agent.py --mock    # without a physical device, for development

In `--mock` mode, commands do not reach a real device: they are served by a simulated Flipper that responds exactly like the firmware (`ping` and `info` succeed, the rest return `not_implemented`). This mode is useful for working on the agent side when the device is not at hand.

### Bringing up the connection

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
| agent.py | the agent proper: the conversation loop and orchestration of tool calls |
| commands.py | conversion of the commands.json catalog into Gemini tools, and dispatching of calls |
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
