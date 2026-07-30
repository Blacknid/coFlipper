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

Anything not handled at all — the `commands.json` entries still at the design stage — falls through to the single `else` branch and produces `ERR unknown_command`; the firmware has no `not_implemented` code and never emits one. Malformed requests are reported separately: a frame without both an identifier and a command gives `ERR bad_frame`, and `subghz.rssi` gives `ERR missing_frequency` or `ERR invalid_frequency`.

## Two observations useful for further development

Both were discovered while testing on the physical device and are worth keeping in mind, since they are not obvious from the documentation:

1. The Flipper Zero CLI interprets the `\r` character as command confirmation. A line terminated only with `\n` is received but never executed — behavior that manifests as a wait timeout, with no error message.

2. A command registered with `CliCommandFlagDefault` is refused as long as an application is open on the device, with the message `this command cannot be run while an application is open`. Since in our architecture it is precisely the application registering the command that stays open on screen, the command must be declared with `CliCommandFlagParallelSafe`, otherwise it becomes unusable.
