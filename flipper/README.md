# flipper/ — CFP server for the Flipper Zero

A FAP application that runs on the Flipper Zero and registers the `cfp` CLI command, used by the desktop component to communicate over the coFlipper Protocol (CFP). The protocol is documented in [/PROTOCOL.md](../PROTOCOL.md).

## Build and run

Requires [ufbt](https://github.com/flipperdevices/flipperzero-ufbt) (Micro Flipper Build Tool):

    pip install ufbt
    cd flipper
    ufbt launch

`ufbt launch` compiles the application, sends it to the Flipper connected over USB, and starts it. To only compile, without flashing: `ufbt build`.

The device used in development runs Momentum firmware, not the official firmware. Since an external application must be compiled with the SDK matching the firmware version it will run on, `ufbt` was configured to use the Momentum index:

    ufbt update -c release --index-url=https://up.momentum-fw.dev/firmware/directory.json

Checking that they match is done by comparing the `Target` and `API` values reported at the end of the build with those reported by the device for the `device_info` command (in our case, target 7 and API 87.1).

## Testing

With the application running on the Flipper (the screen shows "coFlipper - CFP"), the device responds to commands sent over the CLI serial port — the same port used by qFlipper. From the desktop:

    python ../desktop/cfp_client.py

The application does not have to be started by hand: `cfp_client.py` checks whether it is open and launches it over the native CLI (`loader open`) if it is not. Details in [/desktop/README.md](../desktop/README.md).

Note that `loader close` has no effect on this application — the loader reports that it "has to be closed manually", because the event loop in `cfp_app.c` exits only on a Back event. Closing it remotely is what the `cfp <id> exit` command is for, which injects exactly that event.

## Verification status

Not every command in `cfp_dispatch` (`cfp_app.c`) has reached the same level of verification, so they are listed here by what has actually been done with them, rather than as one undifferentiated set.

Compiled, installed and tested on a physical Flipper Zero:

- `ping` and `info` return real device data;
- `subghz.rssi` returns a real measurement taken with the CC1101 (the frequency the synthesizer produced, plus the peak signal level);
- the IR bruteforce (`ir.queue`, `ir.bruteforce`, `ir.status`, `ir.reset`) transmits real codes through the IR LED and reacts to the OK button, as the on-screen progress bar shows;
- `exit` closes the application remotely.

Written against the documented `furi_hal_serial` API but NOT yet built or flashed, and not yet exercised against a real board:

- the Wi-Fi dev board bridge (`cfp_is_board_command` / `cfp_forward_to_board` and the `CfpBoardBridge` setup). It forwards any `wifi.*` / `ble.*` frame out the USART and relays the board's reply. This path needs the companion CFP bridge on the ESP32 (see [/PROTOCOL.md](../PROTOCOL.md)) to answer; until it is built and tested on hardware, treat it as designed rather than verified. Without a board attached it answers `ERR wifi_board_not_connected`, which is the one branch of it that can be reasoned about without the hardware. The desktop side, the protocol and the Marauder simulator for this feature are complete and are exercised end to end by the desktop test suite.

Compiled against the real Momentum SDK (target 7, API 87.1) but the on-radio decode/transmit is still unverified on hardware:

- `subghz.read` (`cfp_cmd_subghz_read`) decodes one received signal by running the stock decoder registry over the CC1101's async RX stream, then reports `signal <protocol> <key> <bits> <freq> <rssi>` or `no_signal`. The decoded key and bit-length are read back through `subghz_protocol_decoder_base_get_string` — which every fixed-code decoder fills with a `Key: 0x…`/`Bit: N` dump — and parsed out of that text, rather than serialising to a `FlipperFormat` (serialisation needs a valid `SubGhzRadioPreset` the receive path does not readily have, whereas `get_string` needs nothing but the decoder). Extraction happens inside the RX callback; the field copy is small and guarded so only the first packet of a burst is taken. **Memory:** bringing up the environment + receiver + worker (the worker runs its own thread with a stack and stream buffer) costs several KB, and on an early build this aborted the app with "out of memory" when listening for a relay remote while the GUI and CLI were resident. It now checks `memmgr_heap_get_max_free_block()` against `CFP_SUBGHZ_HEAP_FLOOR` (14 KB) up front and answers `ERR out_of_memory` instead of allocating into a fatal failure — so a tight heap degrades to a clean, reported error rather than a crash. Still to verify on-device: that `get_string`'s dump carries parseable `Key:`/`Bit:` lines for the target protocols, and that the 14 KB floor is neither so low it still crashes nor so high it needlessly refuses.
- `subghz.send` (`cfp_cmd_subghz_send`) re-encodes a decoded signal and transmits it: it assembles a `FlipperFormat` with `Protocol`/`Bit`/`Key`, hands it to `subghz_transmitter_deserialize`, and keys the radio through `furi_hal_subghz_start_async_tx`. Because the saved `.sub` files live on the desktop, the desktop reads the capture and sends the decoded fields as `subghz.send <freq> <protocol> <bits> <key>` — the firmware never reads a file path. The whole `subghz.read` → save → `subghz.send` replay round-trip is exercised end to end by the desktop suite against the mock (`test_listen.py`, `test_watch.py`); only the on-radio decode/transmit is unverified until it is flashed.

Anything not handled at all — the `commands.json` entries still at the design stage — falls through to the single `else` branch and produces `ERR unknown_command`; the firmware has no `not_implemented` code and never emits one. Malformed requests are reported separately: a frame without both an identifier and a command gives `ERR bad_frame`, and `subghz.rssi` gives `ERR missing_frequency` or `ERR invalid_frequency`.

## Two observations useful for further development

Both were discovered while testing on the physical device and are worth keeping in mind, since they are not obvious from the documentation:

1. The Flipper Zero CLI interprets the `\r` character as command confirmation. A line terminated only with `\n` is received but never executed — behavior that manifests as a wait timeout, with no error message.

2. A command registered with `CliCommandFlagDefault` is refused as long as an application is open on the device, with the message `this command cannot be run while an application is open`. Since in our architecture it is precisely the application registering the command that stays open on screen, the command must be declared with `CliCommandFlagParallelSafe`, otherwise it becomes unusable.
