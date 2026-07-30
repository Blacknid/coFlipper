# The coFlipper Protocol (CFP)

CFP is the purpose-built text protocol through which the desktop component (the agent) communicates with the Flipper Zero, over the USB serial port — the same channel normally used by the Flipper CLI. It is designed to be easy to read, easy to type by hand directly in the CLI (for debugging), and easy to implement both on a resource-constrained microcontroller and in a Python client.

## Request format (desktop -> Flipper)

    cfp <id> <command> [argument ...]

- `cfp` — the name of the CLI command registered by the coFlipper application on the Flipper.
- `<id>` — a positive integer chosen by the client, used to match a response with its corresponding request.
- `<command>` — an identifier of the form `module.action` (e.g. `ping`, `subghz.info`, `ir.info`, `nfc.info`).
- `[argument ...]` — space-separated parameters specific to the command. Limitation in v1: arguments cannot contain spaces.

## Response format (Flipper -> desktop)

    CFP/1 <id> OK [data ...]
    CFP/1 <id> ERR <message>

`<id>` is the identifier from the corresponding request. `OK` is followed by the requested data (also space-separated); `ERR` is followed by a code describing the error (`unknown_command`, `not_implemented`, `bad_frame`, etc.).

Any other line received on the port (the CLI banner, the `>: ` prompt, log output) does not begin with `CFP/1` and is ignored by the client.

## Example exchange

    > cfp 1 ping
    < CFP/1 1 OK pong

    > cfp 2 subghz.rssi 433920000
    < CFP/1 2 OK 433919830 -75.0

    > cfp 3 subghz.rssi 999999999
    < CFP/1 3 ERR invalid_frequency

## Implemented commands

| Command     | Arguments | Description                                                |
|-------------|-----------|------------------------------------------------------------|
| ping        | —         | Checks whether the CFP server responds                     |
| info        | —         | The device name/model                                      |
| subghz.rssi | frequency | Signal level (dBm) measured on the given frequency         |
| exit        | —         | Closes the CFP application on the device (internal use)    |

The full catalog, including commands still at the design stage, is in commands.json.

A note on the response of the `subghz.rssi` command: the first value returned is not the requested frequency, but the frequency the radio synthesizer actually managed to generate. The difference, on the order of a few hundred hertz, comes from the finite resolution of the CC1101 circuit and is reported explicitly so that the user knows what was actually measured.

## Error codes

| Code               | Meaning                                                           |
|--------------------|-------------------------------------------------------------------|
| bad_frame          | The frame does not contain at least an identifier and a command    |
| unknown_command    | The command is not recognized by the device                        |
| missing_frequency  | The command requires a frequency but received no argument          |
| invalid_frequency  | The frequency is outside the ranges supported by the radio module  |
