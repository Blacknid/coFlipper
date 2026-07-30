#include <furi.h>
#include <furi_hal_subghz.h>
#include <furi_hal_infrared.h>
#include <furi_hal_version.h>
#include <furi_hal_serial.h>
#include <furi_hal_serial_control.h>
#include <cli/cli.h>
#include <gui/gui.h>
#include <gui/view_port.h>
#include <input/input.h>
#include <infrared.h>
#include <infrared_transmit.h>
#include <subghz/devices/cc1101_configs.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CFP_VERSION "CFP/1"
#define CFP_CLI_COMMAND "cfp"

/* The CFP protocol format is documented in /PROTOCOL.md, at the project root. */

/* Total sampling duration for the signal level, in steps of CFP_RSSI_STEP_MS. */
#define CFP_RSSI_SAMPLES 20
#define CFP_RSSI_STEP_MS 5

/* Upper bound on a single bruteforce run. The desktop sends the codes it selected, one
   frame per code; this only caps how much the device is willing to hold. */
#define CFP_IR_MAX_CODES 32

/* Pause between two transmitted codes. An appliance needs a moment to act on a command
   it accepted, and back-to-back frames are easy for a receiver to miss. */
#define CFP_IR_GAP_MS 250

/* --- Wi-Fi dev board (ESP32 Marauder) bridge ----------------------------------
   The wifi.* / ble.* families are not served by the Flipper itself but by an ESP32
   dev board on the GPIO/UART header. This firmware is a transparent forwarder: it
   sends the frame out the USART and relays back the one line the board answers with,
   so from the desktop's point of view a wifi.* command is an ordinary CFP command.
   The companion board runs a CFP-speaking bridge (documented in /PROTOCOL.md): it
   receives '<id> <cmd> <args>\n' and answers 'CFP/1 <id> OK|ERR ...\n'. */

/* The WiFi dev board / Marauder speaks at 115200 on the USART (GPIO pins 13/14). */
#define CFP_BOARD_BAUD 115200
/* Longest we wait for the board to answer a whole line before deciding it is not there.
   A scan takes far longer, but the board's CFP shim answers the moment the scan STARTS
   (like ir.bruteforce), so a full second is generous for the acknowledgement itself. */
#define CFP_BOARD_TIMEOUT_MS 1000
/* How long each read blocks while waiting for the next byte, inside that deadline. */
#define CFP_BOARD_POLL_MS 20
/* A board response line cannot be longer than this; the list commands answer over
   several frames rather than one long line, so this need not hold a whole scan. */
#define CFP_BOARD_LINE_MAX 192
#define CFP_BOARD_RX_BUFFER 256

/* The link to the Wi-Fi dev board: the acquired USART handle and a stream buffer the
   receive interrupt drains into, so the forwarder can read the reply a byte at a time. */
typedef struct {
    FuriHalSerialHandle* serial;
    FuriStreamBuffer* rx;
} CfpBoardBridge;

/* One IR power code, as queued by the desktop before a bruteforce run. */
typedef struct {
    InfraredProtocol protocol;
    uint32_t address;
    uint32_t command;
} CfpIrCode;

/* State shared between the CLI thread (which queues codes and starts the run) and the
   main thread (which transmits and watches for the OK button). Every access is guarded
   by the mutex: the two threads touch these fields concurrently. */
typedef struct {
    FuriMutex* mutex;
    CfpIrCode codes[CFP_IR_MAX_CODES];
    size_t count; /* how many codes are queued */
    size_t sent; /* how many have been transmitted so far */
    bool running; /* a bruteforce is in progress */
    bool stopped; /* the user pressed OK to stop it */
    char label[24]; /* what is being bruteforced, for the screen */
} CfpIrState;

/* Measures the signal level (RSSI) on a given frequency.
   The radio is configured, listened to briefly, and released on every call: the command
   is stateless, so that it does not block the module between requests. */
static void cfp_cmd_subghz_rssi(uint32_t id, const char* freq_arg) {
    if(!freq_arg) {
        printf(CFP_VERSION " %lu ERR missing_frequency\r\n", id);
        return;
    }

    uint32_t frequency = (uint32_t)strtoul(freq_arg, NULL, 10);

    if(!furi_hal_subghz_is_frequency_valid(frequency)) {
        printf(CFP_VERSION " %lu ERR invalid_frequency\r\n", id);
        return;
    }

    /* The internal radio is already initialized by the firmware at startup; the sequence
       below (reset -> preset -> frequency -> rx) is the same one the stock apps use. */
    furi_hal_subghz_reset();
    furi_hal_subghz_load_registers(subghz_device_cc1101_preset_ook_650khz_async_regs);
    uint32_t actual = furi_hal_subghz_set_frequency_and_path(frequency);
    furi_hal_subghz_rx();

    float peak = -127.0f;
    for(size_t i = 0; i < CFP_RSSI_SAMPLES; i++) {
        furi_delay_ms(CFP_RSSI_STEP_MS);
        float sample = furi_hal_subghz_get_rssi();
        if(sample > peak) peak = sample;
    }

    furi_hal_subghz_idle();
    furi_hal_subghz_sleep();

    /* The firmware's printf does not format floats, so we split the integer part from
       the decimal one manually (RSSI is negative: -92.3 -> "-92" and "3"). */
    int32_t decidbm = (int32_t)(peak * 10.0f);
    int32_t whole = decidbm / 10;
    int32_t fraction = decidbm % 10;
    if(fraction < 0) fraction = -fraction;

    printf(CFP_VERSION " %lu OK %lu %ld.%ld\r\n", id, actual, whole, fraction);
}

/* Defined further down, next to the frame parsing; declared here because the IR queue
   command consumes several tokens of its own. */
static char* cfp_next_token(char** cursor);

/* --- IR power bruteforce -------------------------------------------------------

   Split across the two threads on purpose. The CLI callback may not transmit: it runs
   on the CLI's own thread, and a run lasting several seconds would block the serial
   port and, worse, could not observe the OK button. So the callback only fills the
   queue and raises the 'running' flag, and the main loop does the transmitting. */

/* Maps a protocol name from the desktop onto the firmware's enum.
   Returns InfraredProtocolUnknown when the name is not recognized.

   Deliberately delegates to the SDK's own parser rather than comparing names here: it
   is the same function the Flipper's IR application uses to read .ir files, so the
   names the desktop sends are exactly the ones written in stock remote files, and a
   firmware update that renames or adds a protocol needs no change on this side. */
static InfraredProtocol cfp_ir_protocol_by_name(const char* name) {
    InfraredProtocol protocol = infrared_get_protocol_by_name(name);
    return infrared_is_protocol_valid(protocol) ? protocol : InfraredProtocolUnknown;
}

/* ir.queue <protocol> <address> <command> - adds one code to the pending run.
   Queueing and starting are separate commands because CFP v1 arguments cannot contain
   spaces, so a whole code list cannot travel in a single frame. */
static void cfp_cmd_ir_queue(uint32_t id, const char* proto_arg, char** cursor, CfpIrState* ir) {
    /* The protocol arrives as the frame's first argument, which the caller has already
       split off; only the address and command are still on the cursor. Reading three
       tokens here instead would skip the protocol and take the address for it. */
    char* address_arg = cfp_next_token(cursor);
    char* command_arg = cfp_next_token(cursor);

    if(!proto_arg || !address_arg || !command_arg) {
        printf(CFP_VERSION " %lu ERR missing_code\r\n", id);
        return;
    }

    InfraredProtocol protocol = cfp_ir_protocol_by_name(proto_arg);
    if(protocol == InfraredProtocolUnknown) {
        printf(CFP_VERSION " %lu ERR unknown_protocol\r\n", id);
        return;
    }

    furi_mutex_acquire(ir->mutex, FuriWaitForever);

    if(ir->running) {
        furi_mutex_release(ir->mutex);
        printf(CFP_VERSION " %lu ERR busy\r\n", id);
        return;
    }
    if(ir->count >= CFP_IR_MAX_CODES) {
        furi_mutex_release(ir->mutex);
        printf(CFP_VERSION " %lu ERR queue_full\r\n", id);
        return;
    }

    ir->codes[ir->count].protocol = protocol;
    ir->codes[ir->count].address = (uint32_t)strtoul(address_arg, NULL, 0);
    ir->codes[ir->count].command = (uint32_t)strtoul(command_arg, NULL, 0);
    ir->count++;
    size_t queued = ir->count;

    furi_mutex_release(ir->mutex);
    printf(CFP_VERSION " %lu OK %u\r\n", id, (unsigned)queued);
}

/* ir.bruteforce [label] - starts transmitting the queued codes.
   Returns as soon as the run has started: the desktop must not sit blocked on the
   serial port for the whole sequence, and its own read timeout is only 2 s. */
static void cfp_cmd_ir_bruteforce(uint32_t id, const char* label, CfpIrState* ir) {
    furi_mutex_acquire(ir->mutex, FuriWaitForever);

    if(ir->running) {
        furi_mutex_release(ir->mutex);
        printf(CFP_VERSION " %lu ERR busy\r\n", id);
        return;
    }
    if(ir->count == 0) {
        furi_mutex_release(ir->mutex);
        printf(CFP_VERSION " %lu ERR empty_queue\r\n", id);
        return;
    }

    ir->sent = 0;
    ir->stopped = false;
    ir->running = true;
    strncpy(ir->label, label ? label : "device", sizeof(ir->label) - 1);
    ir->label[sizeof(ir->label) - 1] = '\0';
    size_t total = ir->count;

    furi_mutex_release(ir->mutex);
    printf(CFP_VERSION " %lu OK started %u\r\n", id, (unsigned)total);
}

/* ir.status - how far the run has got, and whether the user stopped it.
   This is how the desktop learns that the appliance turned off: the user pressing OK
   is the signal, and it surfaces here as 'stopped'. */
static void cfp_cmd_ir_status(uint32_t id, CfpIrState* ir) {
    furi_mutex_acquire(ir->mutex, FuriWaitForever);
    bool running = ir->running;
    bool stopped = ir->stopped;
    size_t sent = ir->sent;
    size_t total = ir->count;
    furi_mutex_release(ir->mutex);

    printf(
        CFP_VERSION " %lu OK %s %u %u\r\n",
        id,
        running ? "running" : (stopped ? "stopped" : "idle"),
        (unsigned)sent,
        (unsigned)total);
}

/* ir.reset - clears the queue, so the next run starts from a clean slate. */
static void cfp_cmd_ir_reset(uint32_t id, CfpIrState* ir) {
    furi_mutex_acquire(ir->mutex, FuriWaitForever);
    ir->count = 0;
    ir->sent = 0;
    ir->running = false;
    ir->stopped = false;
    furi_mutex_release(ir->mutex);

    printf(CFP_VERSION " %lu OK cleared\r\n", id);
}

/* Closes the application remotely, by sending it the same event as pressing Back.
   Without this, any reinstall requires physically pressing the button on the device. */
static void cfp_cmd_exit(uint32_t id, FuriMessageQueue* event_queue) {
    printf(CFP_VERSION " %lu OK closing\r\n", id);
    InputEvent event = {.key = InputKeyBack, .type = InputTypeShort};
    furi_message_queue_put(event_queue, &event, 0);
}

/* Called from the UART receive interrupt: drains each byte the board sends into the
   stream buffer, where the forwarder picks it up. Kept to the single byte the event
   reports, so nothing is done in interrupt context beyond the copy. */
static void
    cfp_board_rx_callback(FuriHalSerialHandle* handle, FuriHalSerialRxEvent event, void* context) {
    CfpBoardBridge* bridge = context;
    if(event & FuriHalSerialRxEventData) {
        uint8_t data = furi_hal_serial_async_rx(handle);
        furi_stream_buffer_send(bridge->rx, &data, 1, 0);
    }
}

/* Acquires the USART and starts listening. If the port cannot be acquired the bridge is
   left with a NULL handle, and every board command then answers
   wifi_board_not_connected rather than crashing. */
static void cfp_board_bridge_init(CfpBoardBridge* bridge) {
    bridge->rx = furi_stream_buffer_alloc(CFP_BOARD_RX_BUFFER, 1);
    bridge->serial = furi_hal_serial_control_acquire(FuriHalSerialIdUsart);
    if(bridge->serial) {
        furi_hal_serial_init(bridge->serial, CFP_BOARD_BAUD);
        furi_hal_serial_async_rx_start(bridge->serial, cfp_board_rx_callback, bridge, false);
    }
}

static void cfp_board_bridge_deinit(CfpBoardBridge* bridge) {
    if(bridge->serial) {
        furi_hal_serial_async_rx_stop(bridge->serial);
        furi_hal_serial_deinit(bridge->serial);
        furi_hal_serial_control_release(bridge->serial);
        bridge->serial = NULL;
    }
    if(bridge->rx) {
        furi_stream_buffer_free(bridge->rx);
        bridge->rx = NULL;
    }
}

/* Whether a command is served by the Wi-Fi dev board rather than by the Flipper. These
   are forwarded over the USART; everything else is handled locally by cfp_dispatch. */
static bool cfp_is_board_command(const char* cmd) {
    return strncmp(cmd, "wifi.", 5) == 0 || strncmp(cmd, "ble.", 4) == 0 ||
           strcmp(cmd, "marauder.reboot") == 0;
}

/* Forwards one frame to the board and relays its answer to the desktop unchanged.
   'frame' is the request as the desktop sent it, minus the 'cfp' CLI word: the
   '<id> <cmd> <args>' the board's CFP shim expects. A reply that never arrives means no
   board is attached or powered, reported as wifi_board_not_connected so the desktop and
   the model see a definite cause rather than a silent timeout. */
static void cfp_forward_to_board(uint32_t id, const char* frame, CfpBoardBridge* bridge) {
    if(!bridge || !bridge->serial) {
        printf(CFP_VERSION " %lu ERR wifi_board_not_connected\r\n", id);
        return;
    }

    /* Discard anything left in the buffer from an earlier exchange, so a slow reply to a
       previous command cannot be mistaken for the answer to this one. */
    uint8_t stale;
    while(furi_stream_buffer_receive(bridge->rx, &stale, 1, 0) == 1) {
    }

    furi_hal_serial_tx(bridge->serial, (const uint8_t*)frame, strlen(frame));
    furi_hal_serial_tx(bridge->serial, (const uint8_t*)"\n", 1);

    char line[CFP_BOARD_LINE_MAX];
    size_t len = 0;
    bool complete = false;
    uint32_t deadline = furi_get_tick() + furi_ms_to_ticks(CFP_BOARD_TIMEOUT_MS);

    while(furi_get_tick() < deadline && len < sizeof(line) - 1) {
        uint8_t byte;
        if(furi_stream_buffer_receive(bridge->rx, &byte, 1, furi_ms_to_ticks(CFP_BOARD_POLL_MS)) ==
           1) {
            if(byte == '\r' || byte == '\n') {
                /* End of line - but only once we have something, so a leading newline
                   left on the wire does not end an empty read prematurely. */
                if(len > 0) {
                    complete = true;
                    break;
                }
                continue;
            }
            line[len++] = (char)byte;
        }
    }
    line[len] = '\0';

    if(!complete && len == 0) {
        printf(CFP_VERSION " %lu ERR wifi_board_not_connected\r\n", id);
        return;
    }

    /* The board already answers in CFP, so its line is relayed verbatim; the Flipper does
       not parse or reformat it. */
    printf("%s\r\n", line);
}

static void cfp_dispatch(
    uint32_t id,
    const char* cmd,
    const char* arg,
    char** cursor,
    const char* frame,
    FuriMessageQueue* event_queue,
    CfpIrState* ir,
    CfpBoardBridge* bridge) {
    if(strcmp(cmd, "ping") == 0) {
        printf(CFP_VERSION " %lu OK pong\r\n", id);
    } else if(strcmp(cmd, "info") == 0) {
        printf(CFP_VERSION " %lu OK %s\r\n", id, furi_hal_version_get_model_name());
    } else if(strcmp(cmd, "subghz.rssi") == 0) {
        cfp_cmd_subghz_rssi(id, arg);
    } else if(strcmp(cmd, "ir.queue") == 0) {
        cfp_cmd_ir_queue(id, arg, cursor, ir);
    } else if(strcmp(cmd, "ir.bruteforce") == 0) {
        cfp_cmd_ir_bruteforce(id, arg, ir);
    } else if(strcmp(cmd, "ir.status") == 0) {
        cfp_cmd_ir_status(id, ir);
    } else if(strcmp(cmd, "ir.reset") == 0) {
        cfp_cmd_ir_reset(id, ir);
    } else if(strcmp(cmd, "exit") == 0) {
        cfp_cmd_exit(id, event_queue);
    } else if(cfp_is_board_command(cmd)) {
        cfp_forward_to_board(id, frame, bridge);
    } else {
        printf(CFP_VERSION " %lu ERR unknown_command\r\n", id);
    }
}

/* Splits off the next word from the buffer, terminating it with '\0'.
   Returns the start of the word, or NULL when no word follows. */
static char* cfp_next_token(char** cursor) {
    char* pos = *cursor;
    while(*pos == ' ')
        pos++;
    if(*pos == '\0') {
        *cursor = pos;
        return NULL;
    }

    char* token = pos;
    while(*pos && *pos != ' ')
        pos++;
    if(*pos) {
        *pos = '\0';
        pos++;
    }
    *cursor = pos;
    return token;
}

/* Everything the CLI callback needs; the callback receives a single context pointer,
   and it now has both the event queue and the IR state to reach. */
typedef struct {
    FuriMessageQueue* event_queue;
    CfpIrState* ir;
    CfpBoardBridge* bridge;
} CfpContext;

static void cfp_cli_callback(PipeSide* pipe, FuriString* args, void* context) {
    UNUSED(pipe);
    CfpContext* ctx = context;

    char buffer[160];
    strncpy(buffer, furi_string_get_cstr(args), sizeof(buffer) - 1);
    buffer[sizeof(buffer) - 1] = '\0';

    /* A second, untouched copy: the tokeniser below overwrites spaces with '\0' in
       buffer, but a command bound for the Wi-Fi board has to be forwarded whole. This
       is the frame minus the 'cfp' word, i.e. exactly '<id> <cmd> <args>'. */
    char frame[160];
    strncpy(frame, furi_string_get_cstr(args), sizeof(frame) - 1);
    frame[sizeof(frame) - 1] = '\0';

    /* strtok_r is not available in the firmware API, so we split the tokens
       manually; buffer is a local copy, so we are free to modify it. */
    char* cursor = buffer;
    char* id_token = cfp_next_token(&cursor);
    char* cmd_token = cfp_next_token(&cursor);
    char* arg_token = cfp_next_token(&cursor);

    if(!id_token || !cmd_token) {
        printf(CFP_VERSION " 0 ERR bad_frame\r\n");
        return;
    }

    uint32_t id = (uint32_t)strtoul(id_token, NULL, 10);
    /* arg_token is the first argument; cursor still points at the rest, which the
       multi-argument commands (ir.queue) consume themselves. */
    cfp_dispatch(
        id, cmd_token, arg_token, &cursor, frame, ctx->event_queue, ctx->ir, ctx->bridge);
}

static void cfp_draw_callback(Canvas* canvas, void* context) {
    CfpIrState* ir = context;

    furi_mutex_acquire(ir->mutex, FuriWaitForever);
    bool running = ir->running;
    size_t sent = ir->sent;
    size_t total = ir->count;
    char label[sizeof(ir->label)];
    strncpy(label, ir->label, sizeof(label));
    label[sizeof(label) - 1] = '\0';
    furi_mutex_release(ir->mutex);

    canvas_clear(canvas);

    if(running) {
        /* The bruteforce screen: what is being sent, how far along it is, and the one
           thing the user needs to know - press OK the moment the appliance reacts. */
        canvas_set_font(canvas, FontPrimary);
        canvas_draw_str(canvas, 2, 12, "Bruteforcing IR");

        canvas_set_font(canvas, FontSecondary);
        char line[32];
        snprintf(line, sizeof(line), "%s  %u/%u", label, (unsigned)sent, (unsigned)total);
        canvas_draw_str(canvas, 2, 26, line);

        /* Progress bar. Width 124 leaves a 2 px margin either side on the 128 px screen. */
        const uint8_t bar_x = 2, bar_y = 32, bar_w = 124, bar_h = 10;
        canvas_draw_frame(canvas, bar_x, bar_y, bar_w, bar_h);
        if(total > 0) {
            uint8_t fill = (uint8_t)((bar_w - 2) * sent / total);
            if(fill > 0) canvas_draw_box(canvas, bar_x + 1, bar_y + 1, fill, bar_h - 2);
        }

        canvas_draw_str(canvas, 2, 54, "OK = it worked, stop");
        canvas_draw_str(canvas, 2, 64, "Back = quit");
    } else {
        canvas_set_font(canvas, FontPrimary);
        canvas_draw_str(canvas, 2, 12, "coFlipper - CFP");
        canvas_set_font(canvas, FontSecondary);
        canvas_draw_str(canvas, 2, 28, "Server running on CLI (USB)");
        canvas_draw_str(canvas, 2, 40, "command: cfp <id> <cmd>");
        canvas_draw_str(canvas, 2, 60, "Back or 'cfp N exit' = quit");
    }
}

static void cfp_input_callback(InputEvent* event, void* context) {
    FuriMessageQueue* queue = context;
    furi_message_queue_put(queue, event, FuriWaitForever);
}

/* Sends the next queued code, if a run is in progress.
   Returns true when it transmitted something, so the caller knows to redraw. */
static bool cfp_ir_send_next(CfpIrState* ir) {
    furi_mutex_acquire(ir->mutex, FuriWaitForever);

    if(!ir->running || ir->sent >= ir->count) {
        /* Reaching the end without the user stopping it means none of the codes
           worked; the run ends either way. */
        if(ir->running && ir->sent >= ir->count) ir->running = false;
        furi_mutex_release(ir->mutex);
        return false;
    }

    CfpIrCode code = ir->codes[ir->sent];
    furi_mutex_release(ir->mutex);

    /* Transmitting is done outside the lock: it takes tens of milliseconds, and the
       draw callback must not be blocked waiting on the mutex for that long. */
    InfraredMessage message = {
        .protocol = code.protocol,
        .address = code.address,
        .command = code.command,
        .repeat = false,
    };

    /* Most appliances ignore a single burst: a real remote held for an instant still
       emits several frames, and receivers debounce on that. Each protocol declares how
       many repeats it needs to be accepted, so ask rather than guess - sending one
       frame is the difference between a correct code that works and a correct code
       that appears to do nothing. */
    int repeats = (int)infrared_get_protocol_min_repeat_count(code.protocol);
    if(repeats < 1) repeats = 1;
    infrared_send(&message, repeats);

    furi_mutex_acquire(ir->mutex, FuriWaitForever);
    ir->sent++;
    furi_mutex_release(ir->mutex);

    return true;
}

int32_t cfp_app_main(void* p) {
    UNUSED(p);

    FuriMessageQueue* event_queue = furi_message_queue_alloc(8, sizeof(InputEvent));

    CfpIrState ir = {
        .mutex = furi_mutex_alloc(FuriMutexTypeNormal),
        .count = 0,
        .sent = 0,
        .running = false,
        .stopped = false,
        .label = "device",
    };

    /* The link to the optional Wi-Fi dev board. Acquired here for the whole session:
       if no board is attached the reads simply time out, and every Wi-Fi/BLE command
       answers wifi_board_not_connected. */
    CfpBoardBridge bridge = {.serial = NULL, .rx = NULL};
    cfp_board_bridge_init(&bridge);

    ViewPort* view_port = view_port_alloc();
    view_port_draw_callback_set(view_port, cfp_draw_callback, &ir);
    view_port_input_callback_set(view_port, cfp_input_callback, event_queue);

    Gui* gui = furi_record_open(RECORD_GUI);
    gui_add_view_port(gui, view_port, GuiLayerFullscreen);

    CfpContext ctx = {.event_queue = event_queue, .ir = &ir, .bridge = &bridge};

    CliRegistry* cli = furi_record_open(RECORD_CLI);
    // ParallelSafe: the command must work while our application (the one registering it)
    // is running on screen, not only when no application is open.
    cli_registry_add_command(
        cli, CFP_CLI_COMMAND, CliCommandFlagParallelSafe, cfp_cli_callback, &ctx);

    InputEvent event;
    bool running = true;
    while(running) {
        /* A short wait when idle; while bruteforcing we come back promptly to send the
           next code, so the button stays responsive throughout the run. */
        bool bruteforcing;
        furi_mutex_acquire(ir.mutex, FuriWaitForever);
        bruteforcing = ir.running;
        furi_mutex_release(ir.mutex);

        uint32_t wait = bruteforcing ? CFP_IR_GAP_MS : 100;

        if(furi_message_queue_get(event_queue, &event, wait) == FuriStatusOk) {
            if(event.key == InputKeyBack) {
                running = false;
            } else if(event.key == InputKeyOk && event.type == InputTypeShort) {
                /* The middle button is how the user says "it worked, stop now". */
                furi_mutex_acquire(ir.mutex, FuriWaitForever);
                if(ir.running) {
                    ir.running = false;
                    ir.stopped = true;
                }
                furi_mutex_release(ir.mutex);
                view_port_update(view_port);
            }
        } else if(bruteforcing) {
            /* No input within the gap: time to send the next code. */
            cfp_ir_send_next(&ir);
            view_port_update(view_port);
        }
    }

    cli_registry_delete_command(cli, CFP_CLI_COMMAND);
    furi_record_close(RECORD_CLI);

    cfp_board_bridge_deinit(&bridge);

    gui_remove_view_port(gui, view_port);
    furi_record_close(RECORD_GUI);
    view_port_free(view_port);
    furi_message_queue_free(event_queue);
    furi_mutex_free(ir.mutex);

    return 0;
}
