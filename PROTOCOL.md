# The coFlipper Protocol (CFP)

CFP is the purpose-built text protocol through which the desktop component (the agent) communicates with the Flipper Zero, over the USB serial port — the same channel normally used by the Flipper CLI. It is designed to be easy to read, easy to type by hand directly in the CLI (for debugging), and easy to implement both on a resource-constrained microcontroller and in a Python client.

## Request format (desktop -> Flipper)

    cfp <id> <command> [argument ...]

- `cfp` — the name of the CLI command registered by the coFlipper application on the Flipper.
- `<id>` — a positive integer chosen by the client, used to match a response with its corresponding request.
- `<command>` — an identifier of the form `module.action` (e.g. `ping`, `subghz.rssi`, `ir.read`, `nfc.read`).
- `[argument ...]` — space-separated parameters specific to the command. Limitation in v1: arguments cannot contain spaces.

## Response format (Flipper -> desktop)

    CFP/1 <id> OK [data ...]
    CFP/1 <id> ERR <message>

`<id>` is the identifier from the corresponding request. `OK` is followed by the requested data (also space-separated); `ERR` is followed by a code describing the error (`bad_frame`, `unknown_command`, `missing_frequency`, `invalid_frequency` — these four are the whole set, listed with their meanings in the table at the end of this document).

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

## The Wi-Fi module (Marauder board)

Commands in the `wifi.*` and `ble.*` families are not served by the Flipper itself but by an ESP32 Wi-Fi dev board flashed with [Marauder](https://github.com/justcallmekoko/ESP32Marauder) firmware, attached to the Flipper's GPIO/UART header. The coFlipper CFP application on the Flipper forwards these frames to the board over its second UART and relays the reply, so from the desktop client's point of view they are ordinary CFP commands.

These commands cover the whole Marauder feature set: tuning the channel; scanning and listing access points and their client stations; selecting a target; passive sniffing (beacons, probes, deauth-detection, WPA handshakes/PMKID, raw capture); active attacks (deauthentication, beacon spam, evil-portal, karma, probe flood); wardriving with GPS; and the Bluetooth side (BLE scanning, AirTag/tracker detection, and per-vendor pairing spam). Each carries an `impact` field in commands.json — `passive` (only listens) or `offensive` (transmits and disrupts other devices) — and the agent is instructed to obtain the user's authorization before any offensive one. Marauder scans and attacks run continuously until stopped with `wifi.stop` / `ble.stop`.

A note on the response of the `subghz.rssi` command: the first value returned is not the requested frequency, but the frequency the radio synthesizer actually managed to generate. The difference, on the order of a few hundred hertz, comes from the finite resolution of the CC1101 circuit and is reported explicitly so that the user knows what was actually measured.

## Error codes

| Code               | Meaning                                                           |
|--------------------|-------------------------------------------------------------------|
| bad_frame          | The frame does not contain at least an identifier and a command    |
| unknown_command    | The command is not recognized by the device                        |
| missing_frequency  | The command requires a frequency but received no argument          |
| invalid_frequency  | The frequency is outside the ranges supported by the radio module  |
| wifi_board_not_connected | A `wifi.*`/`ble.*` command was sent but no Marauder board is attached/powered |
| no_target_selected | An attack was requested before a target was chosen with `wifi.select_ap`/`wifi.select_station` |
| invalid_channel    | A Wi-Fi channel outside the 1–14 range was requested                |
| invalid_selection  | A target selector referenced an index that is not in the captured list |
