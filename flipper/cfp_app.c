#include <furi.h>
#include <furi/core/memmgr.h>
#include <furi_hal_subghz.h>
#include <furi_hal_infrared.h>
#include <furi_hal_version.h>
#include <furi_hal_serial.h>
#include <furi_hal_serial_control.h>
#include <cli/cli.h>
#include <toolbox/pipe.h>
#include <gui/gui.h>
#include <gui/view_port.h>
#include <input/input.h>
#include <infrared.h>
#include <infrared_transmit.h>
#include <subghz/devices/cc1101_configs.h>
<<<<<<< HEAD
#include <nfc/nfc.h>
#include <nfc/protocols/iso14443_3a/iso14443_3a.h>
#include <nfc/protocols/iso14443_3a/iso14443_3a_poller_sync.h>
=======
#include <lib/subghz/environment.h>
#include <lib/subghz/receiver.h>
#include <lib/subghz/transmitter.h>
#include <lib/subghz/subghz_protocol_registry.h>
#include <lib/subghz/protocols/base.h>
#include <lib/subghz/protocols/raw.h>
#include <lib/subghz/types.h>
#include <lib/subghz/devices/devices.h>
#include <lib/subghz/devices/cc1101_int/cc1101_int_interconnect.h>
#include <lib/subghz/subghz_worker.h>
#include <lib/flipper_format/flipper_format.h>
#include <storage/storage.h>
>>>>>>> 0235cf490d58a0b56881f3880d8a9b3b216bd724
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>

#define CFP_VERSION "CFP/1"
#define CFP_CLI_COMMAND "cfp"

/* The CFP protocol format is documented in /PROTOCOL.md, at the project root. */

/* All CFP responses go out through the CLI pipe, not printf. Measured on-device, every
   command sent through the firmware's printf/stdio path leaked ~44 bytes of heap that was
   never reclaimed - harmless for a one-shot command, but the desktop's watcher loops
   subghz.read for as long as it is waiting for a signal, so the leak accumulated until an
   allocation failed and the app aborted with "out of memory". pipe_send writes straight to
   the CLI pipe the command callback is handed, bypassing stdio entirely, so it does not
   leak. cfp_set_reply_pipe() stores the current command's pipe for the duration of the
   dispatch; cfp_reply() formats into a stack buffer and sends it. */
static PipeSide* g_cfp_pipe = NULL;

static void cfp_set_reply_pipe(PipeSide* pipe) {
    g_cfp_pipe = pipe;
}

static void cfp_reply(const char* format, ...) {
    char line[192];
    va_list args;
    va_start(args, format);
    int n = vsnprintf(line, sizeof(line), format, args);
    va_end(args);
    if(n < 0) return;
    size_t len = (n < (int)sizeof(line)) ? (size_t)n : sizeof(line) - 1;
    /* A CLI-originated command always has a bound pipe; if somehow it does not, dropping the
       line is safer than reintroducing the leaky stdio path. */
    if(g_cfp_pipe) {
        pipe_send(g_cfp_pipe, line, len);
    }
}

/* Total sampling duration for the signal level, in steps of CFP_RSSI_STEP_MS. */
#define CFP_RSSI_SAMPLES 20
#define CFP_RSSI_STEP_MS 5

/* subghz.read listens for at most this long for one decodable packet, unless the caller
   passes a shorter timeout. A busy band decodes within a few hundred ms; a quiet one is
   better reported as no_signal than waited on forever, so the desktop's watcher/listener
   can loop. Capped so a stray argument cannot pin the radio down for minutes. */
#define CFP_READ_DEFAULT_MS 3000
/* One read holds the decoder stack for the whole window, so a caller waiting for a signal
   about to be played should do ONE long read rather than a tight loop of short ones: each
   read cycle allocates and frees the environment/receiver/worker, and the underlying SDK
   leaks a little heap on every cycle (measured ~90 B), so ten short reads leak ~10x what one
   long read does. The cap is generous enough to cover a realistic "about to be triggered"
   window in a single call. */
#define CFP_READ_MAX_MS 30000
/* How often the read loop pumps the decoder and checks whether a packet arrived. */
#define CFP_READ_POLL_MS 10

/* Raw capture and raw replay both default to a 5 s window (subghz.read_raw / subghz.send_raw):
   long enough for a person to trigger a remote once and for its burst to repeat a few times,
   short enough not to pin the radio. Replay is capped at the same 5 s so a long capture cannot
   key the transmitter indefinitely - the burst that matters is at the start of the file. */
#define CFP_RAW_DEFAULT_MS 5000
#define CFP_RAW_TX_MAX_MS 5000

/* Bringing up the whole Sub-GHz decoder stack (environment + registry + receiver + a worker
   thread with its own stack and stream buffer) costs several KB of heap. When the CFP app,
   its GUI viewport and the CLI are all already resident, free heap can be tighter than that,
   and a failed allocation aborts the firmware ("out of memory" on screen) - which is exactly
   the crash seen when listening for a relay remote. So we refuse up front, cleanly, if the
   largest free block cannot comfortably hold the stack, turning a fatal abort into an ordinary
   CFP error the desktop can report. The margin is empirical: the stock Read stack measured
   ~9-11 KB, so we require a contiguous block above that plus slack for the async-RX path. */
#define CFP_SUBGHZ_HEAP_FLOOR 14000u

/* Upper bound on a single bruteforce run. The desktop sends the codes it selected, one
   frame per code; this only caps how much the device is willing to hold. */
#define CFP_IR_MAX_CODES 32

/* Pause between two transmitted codes. An appliance needs a moment to act on a command
   it accepted, and back-to-back frames are easy for a receiver to miss. */
#define CFP_IR_GAP_MS 250

/* --- NFC (ISO14443-3A) ----------------------------------------------------------
   Reads/watches an NFC tag brought close to the Flipper. Deliberately scoped to
   ISO14443-3A, the base transport layer of Mifare Classic/Mini/Ultralight, NTAG
   stickers and amiibo - the large majority of tags anyone actually taps. Both
   commands run synchronously on the CLI thread, exactly like subghz.rssi, just for
   longer: there is only ever one CFP request in flight at a time, so blocking here
   is no different in kind, and the desktop widens its own serial read timeout to
   match before sending either one (see commands.py). Full protocol identification
   (Mifare Classic vs. DESFire vs. a bank card, ISO15693 tags) and true emulation are
   out of scope for this pass - nfc.emulate and nfc.stop remain firmware stubs. */

/* How often a pending read/watch retries the card. */
#define CFP_NFC_POLL_MS 100
/* How many distinct taps nfc.watch can remember in one session, and the longest one
   'ms;type;uid' token can be. */
#define CFP_NFC_MAX_EVENTS 8
#define CFP_NFC_EVENT_LEN  40

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
        cfp_reply(CFP_VERSION " %lu ERR missing_frequency\r\n", id);
        return;
    }

    uint32_t frequency = (uint32_t)strtoul(freq_arg, NULL, 10);

    if(!furi_hal_subghz_is_frequency_valid(frequency)) {
        cfp_reply(CFP_VERSION " %lu ERR invalid_frequency\r\n", id);
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

    cfp_reply(CFP_VERSION " %lu OK %lu %ld.%ld\r\n", id, actual, whole, fraction);
}

<<<<<<< HEAD
/* Encodes 'len' bytes as uppercase hex into 'dst', which must hold at least 2*len+1
   bytes. Written by hand rather than through snprintf's %X per byte, to keep the loop a
   single, obviously correct pass with no format-string surprises. */
static void cfp_hex_encode(char* dst, const uint8_t* src, size_t len) {
    static const char digits[] = "0123456789ABCDEF";
    for(size_t i = 0; i < len; i++) {
        dst[i * 2] = digits[(src[i] >> 4) & 0x0F];
        dst[i * 2 + 1] = digits[src[i] & 0x0F];
    }
    dst[len * 2] = '\0';
}

/* A tag identifies itself at the transport layer only as UID/ATQA/SAK - what family of
   chip that belongs to is common industry knowledge, not something the card states
   outright. This covers the handful of SAK values behind the large majority of tags
   found in the wild; anything else is reported as an unrecognised card rather than
   guessed at. Working out what the tag is FOR (an access badge, a transit pass, an
   amiibo) is the model's job, reasoning from exactly these fields - this only carries
   them, honestly labelled. CFP tokens cannot contain spaces, so the names are
   underscore-joined. */
static void cfp_nfc_describe_sak(uint8_t sak, const char** type_out, const char** bytes_out) {
    switch(sak) {
    case 0x08:
        *type_out = "Mifare_Classic_1K";
        *bytes_out = "1024";
        break;
    case 0x18:
        *type_out = "Mifare_Classic_4K";
        *bytes_out = "4096";
        break;
    case 0x09:
        *type_out = "Mifare_Mini";
        *bytes_out = "320";
        break;
    case 0x00:
        *type_out = "Mifare_Ultralight_or_NTAG";
        *bytes_out = "unknown";
        break;
    case 0x20:
        *type_out = "ISO14443-4_DESFire_or_JCOP";
        *bytes_out = "unknown";
        break;
    default:
        *type_out = "unrecognised_ISO14443-3A";
        *bytes_out = "unknown";
        break;
    }
}

/* nfc.read [timeout_ms] - waits for an ISO14443-3A tag and reports the raw technical
   facts a reader gets from it (UID, ATQA, SAK), nothing more: what the tag is used for
   is deduced afterwards, by the model, from exactly these fields. Retries every
   CFP_NFC_POLL_MS until one answers or timeout_ms runs out (5000 ms by default). */
static void cfp_cmd_nfc_read(uint32_t id, const char* timeout_arg) {
    uint32_t timeout_ms = timeout_arg ? (uint32_t)strtoul(timeout_arg, NULL, 10) : 5000;
    if(timeout_ms == 0) timeout_ms = 5000;

    Nfc* nfc = nfc_alloc();
    Iso14443_3aData* data = iso14443_3a_alloc();

    bool found = false;
    uint32_t deadline = furi_get_tick() + furi_ms_to_ticks(timeout_ms);
    while(furi_get_tick() < deadline) {
        if(iso14443_3a_poller_sync_read(nfc, data) == Iso14443_3aErrorNone) {
            found = true;
            break;
        }
        furi_delay_ms(CFP_NFC_POLL_MS);
    }
    nfc_free(nfc);

    if(!found) {
        iso14443_3a_free(data);
        printf(CFP_VERSION " %lu ERR no_card_detected\r\n", id);
        return;
    }

    size_t uid_len = 0;
    const uint8_t* uid = iso14443_3a_get_uid(data, &uid_len);
    uint8_t atqa[2];
    iso14443_3a_get_atqa(data, atqa);
    uint8_t sak = iso14443_3a_get_sak(data);

    char uid_hex[ISO14443_3A_MAX_UID_SIZE * 2 + 1];
    cfp_hex_encode(uid_hex, uid, uid_len);
    char atqa_hex[5];
    cfp_hex_encode(atqa_hex, atqa, 2);

    const char* type_name;
    const char* byte_count;
    cfp_nfc_describe_sak(sak, &type_name, &byte_count);

    iso14443_3a_free(data);

    printf(
        CFP_VERSION " %lu OK type=%s uid=%s atqa=%s sak=%02X protocol=ISO14443-3A bytes=%s\r\n",
        id,
        type_name,
        uid_hex,
        atqa_hex,
        sak,
        byte_count);
}

/* nfc.watch <duration_ms> - monitors for the given time and reports, in order, each
   distinct moment a tag came into range: edge-triggered, so a tag held in place the
   whole time is one event, not one every poll. Each event is 'ms;type;uid', ms counted
   from the start of the watch. Passive throughout - it only ever reads. */
static void cfp_cmd_nfc_watch(uint32_t id, const char* duration_arg) {
    uint32_t duration_ms = duration_arg ? (uint32_t)strtoul(duration_arg, NULL, 10) : 0;
    if(duration_ms == 0) {
        printf(CFP_VERSION " %lu ERR missing_duration\r\n", id);
        return;
    }

    Nfc* nfc = nfc_alloc();
    Iso14443_3aData* data = iso14443_3a_alloc();

    char events[CFP_NFC_MAX_EVENTS][CFP_NFC_EVENT_LEN];
    size_t event_count = 0;
    bool was_present = false;

    uint32_t start = furi_get_tick();
    uint32_t deadline = start + furi_ms_to_ticks(duration_ms);
    while(furi_get_tick() < deadline) {
        bool present = iso14443_3a_poller_sync_read(nfc, data) == Iso14443_3aErrorNone;
        if(present && !was_present && event_count < CFP_NFC_MAX_EVENTS) {
            size_t uid_len = 0;
            const uint8_t* uid = iso14443_3a_get_uid(data, &uid_len);
            char uid_hex[ISO14443_3A_MAX_UID_SIZE * 2 + 1];
            cfp_hex_encode(uid_hex, uid, uid_len);
            /* furi_get_tick() already counts milliseconds on this platform - no
               separate ticks-to-ms conversion is needed. */
            uint32_t elapsed_ms = furi_get_tick() - start;
            snprintf(
                events[event_count],
                CFP_NFC_EVENT_LEN,
                "%lu;ISO14443-3A;%s",
                (unsigned long)elapsed_ms,
                uid_hex);
            event_count++;
        }
        was_present = present;
        furi_delay_ms(CFP_NFC_POLL_MS);
    }

    nfc_free(nfc);
    iso14443_3a_free(data);

    printf(CFP_VERSION " %lu OK", id);
    for(size_t i = 0; i < event_count; i++) {
        printf(" %s", events[i]);
    }
    printf("\r\n");
}

/* Defined further down, next to the frame parsing; declared here because the IR queue
   command consumes several tokens of its own. */
=======
/* Splits off the next word from the frame buffer; defined next to the frame parser far
   below, but declared here because the Sub-GHz commands consume their own extra tokens. */
>>>>>>> 0235cf490d58a0b56881f3880d8a9b3b216bd724
static char* cfp_next_token(char** cursor);

/* --- Sub-GHz receive & replay --------------------------------------------------
   subghz.read decodes ONE packet off the air; subghz.send re-encodes and transmits a
   signal the desktop captured earlier. Both use the same protocol stack the stock Sub-GHz
   app uses: an environment holding the protocol registry, and a receiver that runs the
   decoders over the async stream the CC1101 produces. The .sub files live on the desktop,
   so replay does not read a file here - the desktop reads the file and sends us the decoded
   frequency/protocol/bits/key, which we hand to the protocol encoder. */

/* Filled in by the receiver's callback the moment a decoder recognises a packet. One packet
   is enough: subghz.read returns the first decode, and the desktop loops for more. */
typedef struct {
    bool got;
    char protocol[32];
    char key[24]; /* the code as hex, e.g. 0x4E7B90 */
    uint32_t bits;
} CfpReadResult;

/* Pull the code and bit-length out of a decoder using its own human-readable dump.
   subghz_protocol_decoder_base_get_string() writes a FuriString describing the decode, and
   every fixed-code decoder includes a 'Key: 0x...' and a 'Bit: N' line in it. We parse those
   two rather than serialise to a FlipperFormat, because serialisation needs a valid
   SubGhzRadioPreset the receive path does not readily have, whereas get_string needs nothing
   but the decoder. Leaves *out zeroed if a field is absent. */
static void cfp_decoder_extract(SubGhzProtocolDecoderBase* decoder, uint64_t* key_out, uint32_t* bits_out) {
    *key_out = 0;
    *bits_out = 0;
    FuriString* dump = furi_string_alloc();
    if(subghz_protocol_decoder_base_get_string(decoder, dump)) {
        const char* text = furi_string_get_cstr(dump);
        const char* key_at = strstr(text, "Key:");
        if(key_at) {
            *key_out = (uint64_t)strtoull(key_at + 4, NULL, 0);
        }
        const char* bit_at = strstr(text, "Bit:");
        if(bit_at) {
            *bits_out = (uint32_t)strtoul(bit_at + 4, NULL, 10);
        }
    }
    furi_string_free(dump);
}

/* The receiver calls this from the worker's context when a protocol decodes a packet. We
   copy out the fields we report and mark the result taken; the loop then stops reading. */
static void cfp_subghz_rx_callback(SubGhzReceiver* receiver, SubGhzProtocolDecoderBase* decoder, void* context) {
    UNUSED(receiver);
    CfpReadResult* out = context;
    if(out->got) return; /* already have one; ignore the rest of the burst */

    const char* name = decoder->protocol ? decoder->protocol->name : "Unknown";
    strncpy(out->protocol, name ? name : "Unknown", sizeof(out->protocol) - 1);
    out->protocol[sizeof(out->protocol) - 1] = '\0';

    uint64_t code = 0;
    uint32_t count_bit = 0;
    cfp_decoder_extract(decoder, &code, &count_bit);
    snprintf(out->key, sizeof(out->key), "0x%llX", (unsigned long long)code);
    out->bits = count_bit;
    out->got = true;
}

/* subghz.read [frequency] [timeout_ms] - decode one received Sub-GHz packet.
   Answers 'OK signal <protocol> <key> <bits> <freq> <rssi>' on a decode, or 'OK no_signal'
   if the window passed with nothing decodable. Stateless like rssi: the radio is set up,
   listened to, and released on every call. */
static void cfp_cmd_subghz_read(uint32_t id, const char* freq_arg, char** cursor) {
    if(!freq_arg) {
        cfp_reply(CFP_VERSION " %lu ERR missing_frequency\r\n", id);
        return;
    }
    uint32_t frequency = (uint32_t)strtoul(freq_arg, NULL, 10);
    if(!furi_hal_subghz_is_frequency_valid(frequency)) {
        cfp_reply(CFP_VERSION " %lu ERR invalid_frequency\r\n", id);
        return;
    }

    /* Optional second argument: how long to listen, clamped so it cannot pin the radio. */
    char* to_arg = cfp_next_token(cursor);
    uint32_t timeout_ms = to_arg ? (uint32_t)strtoul(to_arg, NULL, 10) : CFP_READ_DEFAULT_MS;
    if(timeout_ms == 0) timeout_ms = CFP_READ_DEFAULT_MS;
    if(timeout_ms > CFP_READ_MAX_MS) timeout_ms = CFP_READ_MAX_MS;

    /* Refuse before allocating if the heap cannot hold the decoder stack, rather than let a
       mid-way malloc abort the whole app. max_free_block, not total free heap, is the right
       gauge: the worker thread's stack must land in one contiguous block. */
    if(memmgr_heap_get_max_free_block() < CFP_SUBGHZ_HEAP_FLOOR) {
        cfp_reply(CFP_VERSION " %lu ERR out_of_memory\r\n", id);
        return;
    }

    /* The protocol stack: an environment holding the registry the stock app registers, and a
       receiver running those decoders. subghz_protocol_registry is the standard list. */
    SubGhzEnvironment* environment = subghz_environment_alloc();
    subghz_environment_set_protocol_registry(environment, &subghz_protocol_registry);

    CfpReadResult result = {.got = false, .bits = 0};
    SubGhzReceiver* receiver = subghz_receiver_alloc_init(environment);
    subghz_receiver_set_filter(receiver, SubGhzProtocolFlag_Decodable);
    subghz_receiver_set_rx_callback(receiver, cfp_subghz_rx_callback, &result);

    /* A worker turns the async capture stream into decoder input; its output feeds the
       receiver. This is the same wiring the Sub-GHz Read screen uses. */
    SubGhzWorker* worker = subghz_worker_alloc();
    subghz_worker_set_overrun_callback(
        worker, (SubGhzWorkerOverrunCallback)subghz_receiver_reset);
    subghz_worker_set_pair_callback(
        worker, (SubGhzWorkerPairCallback)subghz_receiver_decode);
    subghz_worker_set_context(worker, receiver);

    furi_hal_subghz_reset();
    furi_hal_subghz_load_registers(subghz_device_cc1101_preset_ook_650khz_async_regs);
    uint32_t actual = furi_hal_subghz_set_frequency_and_path(frequency);

    furi_hal_subghz_start_async_rx(subghz_worker_rx_callback, worker);
    subghz_worker_start(worker);

    float peak = -127.0f;
    uint32_t waited = 0;
    while(waited < timeout_ms && !result.got) {
        furi_delay_ms(CFP_READ_POLL_MS);
        waited += CFP_READ_POLL_MS;
        float sample = furi_hal_subghz_get_rssi();
        if(sample > peak) peak = sample;
    }

    subghz_worker_stop(worker);
    furi_hal_subghz_stop_async_rx();
    furi_hal_subghz_idle();
    furi_hal_subghz_sleep();

    subghz_worker_free(worker);
    subghz_receiver_free(receiver);
    subghz_environment_free(environment);

    if(!result.got) {
        cfp_reply(CFP_VERSION " %lu OK no_signal\r\n", id);
        return;
    }

    int32_t decidbm = (int32_t)(peak * 10.0f);
    int32_t whole = decidbm / 10;
    int32_t fraction = decidbm % 10;
    if(fraction < 0) fraction = -fraction;

    /* signal <protocol> <key> <bits> <freq> <rssi> - the order the desktop's listener and
       watcher parse (see PROTOCOL.md and the listener subagent). */
    cfp_reply(
        CFP_VERSION " %lu OK signal %s %s %lu %lu %ld.%ld\r\n",
        id,
        result.protocol,
        result.key,
        (unsigned long)result.bits,
        actual,
        whole,
        fraction);
}

/* subghz.send <frequency> <protocol> <bits> <key> - re-transmit a captured signal.
   The desktop reads the .sub it saved and sends us the decoded fields; we build a
   FlipperFormat in memory holding exactly what a .sub carries, hand it to the protocol
   encoder, and key the radio. OFFENSIVE: this transmits, and the desktop only sends it
   after the user has confirmed authorization (see agent.confirm_authorized_action). */
static void cfp_cmd_subghz_send(uint32_t id, const char* freq_arg, char** cursor) {
    char* proto_arg = cfp_next_token(cursor);
    char* bits_arg = cfp_next_token(cursor);
    char* key_arg = cfp_next_token(cursor);
    if(!freq_arg || !proto_arg || !bits_arg || !key_arg) {
        cfp_reply(CFP_VERSION " %lu ERR missing_code\r\n", id);
        return;
    }

    uint32_t frequency = (uint32_t)strtoul(freq_arg, NULL, 10);
    if(!furi_hal_subghz_is_frequency_valid(frequency)) {
        cfp_reply(CFP_VERSION " %lu ERR invalid_frequency\r\n", id);
        return;
    }
    uint32_t bits = (uint32_t)strtoul(bits_arg, NULL, 10);
    uint64_t key = (uint64_t)strtoull(key_arg, NULL, 0);

    /* Same heap guard as subghz.read: refuse cleanly rather than abort if the encoder stack
       will not fit. The TX path holds no worker thread, so it needs less, but the environment
       and FlipperFormat still cost enough to matter under a resident app. */
    if(memmgr_heap_get_max_free_block() < CFP_SUBGHZ_HEAP_FLOOR) {
        cfp_reply(CFP_VERSION " %lu ERR out_of_memory\r\n", id);
        return;
    }

    SubGhzEnvironment* environment = subghz_environment_alloc();
    subghz_environment_set_protocol_registry(environment, &subghz_protocol_registry);

    SubGhzTransmitter* transmitter = subghz_transmitter_alloc_init(environment, proto_arg);
    if(!transmitter) {
        subghz_environment_free(environment);
        cfp_reply(CFP_VERSION " %lu ERR unknown_protocol\r\n", id);
        return;
    }

    /* The encoder expects its parameters the way a .sub presents them: a FlipperFormat with
       the same fields the generic decoder base reads back - Frequency, Preset, Protocol, Bit,
       Key. We assemble that minimal set (it is exactly what our .sub files carry); a protocol
       needing more will make deserialize fail, reported below as bad_code rather than a silent
       no-op. Preset matches the OOK 650 kHz async modulation the radio is set to below. */
    FlipperFormat* format = flipper_format_string_alloc();
    uint32_t freq32 = frequency;
    flipper_format_write_uint32(format, "Frequency", &freq32, 1);
    flipper_format_write_string_cstr(format, "Preset", "FuriHalSubGhzPresetOok650Async");
    flipper_format_write_string_cstr(format, "Protocol", proto_arg);
    uint32_t bit32 = bits;
    flipper_format_write_uint32(format, "Bit", &bit32, 1);
    uint8_t key_bytes[8];
    for(int i = 0; i < 8; i++) {
        key_bytes[7 - i] = (uint8_t)((key >> (i * 8)) & 0xFF);
    }
    flipper_format_write_hex(format, "Key", key_bytes, 8);

    SubGhzProtocolStatus status = subghz_transmitter_deserialize(transmitter, format);
    if(status != SubGhzProtocolStatusOk) {
        flipper_format_free(format);
        subghz_transmitter_free(transmitter);
        subghz_environment_free(environment);
        cfp_reply(CFP_VERSION " %lu ERR bad_code\r\n", id);
        return;
    }

    furi_hal_subghz_reset();
    furi_hal_subghz_load_registers(subghz_device_cc1101_preset_ook_650khz_async_regs);
    furi_hal_subghz_set_frequency_and_path(frequency);

    furi_hal_subghz_start_async_tx(subghz_transmitter_yield, transmitter);
    while(!furi_hal_subghz_is_async_tx_complete()) {
        furi_delay_ms(5);
    }
    furi_hal_subghz_stop_async_tx();
    furi_hal_subghz_idle();
    furi_hal_subghz_sleep();

    flipper_format_free(format);
    subghz_transmitter_free(transmitter);
    subghz_environment_free(environment);

    cfp_reply(CFP_VERSION " %lu OK transmitted\r\n", id);
}

/* --- Raw Sub-GHz capture & replay ----------------------------------------------

   The decoded path above identifies a signal (protocol + key) and can only replay signals a
   registered decoder understands. The raw path makes no attempt to understand the signal: it
   records the bare timing edges the CC1101 produced straight to a .sub file on the Flipper's
   SD card, and replays that file back edge for edge. Two consequences make it the robust
   fallback. It works for ANY signal, decodable or not - a relay remote whose protocol the
   registry does not know still captures and replays. And it is far lighter on RAM: the RAW
   decoder streams samples to the file as they arrive (subghz_protocol_raw_save_to_file_init)
   instead of building the whole decoder registry and a receiver, so it does not run the
   device out of memory the way the decoded read did on a busy signal.

   The file lives at /ext/subghz/<name>.sub, the stock Sub-GHz folder, so a capture is also
   openable from the Flipper's own Sub-GHz app. The desktop owns the NAME (it derives it from
   what the user said - "a relay remote" -> raw_relay_remote), and passes it in; the firmware
   only sanitises it into a filename and never invents one. Replay is offensive and gated on
   the desktop behind the authorization confirmation, exactly like subghz.send. */

#define CFP_SUBGHZ_DIR "/ext/subghz"

/* Sanitise name into out_safe, keeping only filename-safe characters so a crafted name cannot
   escape the folder or inject a path. Returns false if name is empty or nothing survived. */
static bool cfp_raw_sanitise(const char* name, char* out_safe, size_t out_size) {
    if(!name || !*name) return false;
    size_t j = 0;
    for(size_t i = 0; name[i] && j < out_size - 1; i++) {
        char c = name[i];
        bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
                  c == '_' || c == '-';
        if(ok) out_safe[j++] = c;
    }
    out_safe[j] = '\0';
    return j > 0;
}

/* Build /ext/subghz/<name>.sub into out from an already-sanitised stem. */
static bool cfp_raw_path(const char* safe_stem, char* out, size_t out_size) {
    int n = snprintf(out, out_size, CFP_SUBGHZ_DIR "/%s.sub", safe_stem);
    return n > 0 && (size_t)n < out_size;
}

/* subghz.read_raw <frequency> <name> [timeout_ms] - capture raw timings to a .sub file.
   Answers 'OK captured <samples> <name>' once the window closes, or 'OK no_signal' if nothing
   was recorded (an empty capture is not saved). The desktop then records the human name and
   can replay it later with subghz.send_raw <name>. */
static void cfp_cmd_subghz_read_raw(uint32_t id, const char* freq_arg, char** cursor) {
    if(!freq_arg) {
        cfp_reply(CFP_VERSION " %lu ERR missing_frequency\r\n", id);
        return;
    }
    uint32_t frequency = (uint32_t)strtoul(freq_arg, NULL, 10);
    if(!furi_hal_subghz_is_frequency_valid(frequency)) {
        cfp_reply(CFP_VERSION " %lu ERR invalid_frequency\r\n", id);
        return;
    }
    char* name_arg = cfp_next_token(cursor);
    char stem[64];
    if(!cfp_raw_sanitise(name_arg, stem, sizeof(stem))) {
        cfp_reply(CFP_VERSION " %lu ERR missing_name\r\n", id);
        return;
    }
    char* to_arg = cfp_next_token(cursor);
    uint32_t timeout_ms = to_arg ? (uint32_t)strtoul(to_arg, NULL, 10) : CFP_RAW_DEFAULT_MS;
    if(timeout_ms == 0) timeout_ms = CFP_RAW_DEFAULT_MS;
    if(timeout_ms > CFP_READ_MAX_MS) timeout_ms = CFP_READ_MAX_MS;

    SubGhzEnvironment* environment = subghz_environment_alloc();
    subghz_environment_set_protocol_registry(environment, &subghz_protocol_registry);
    SubGhzProtocolDecoderRAW* decoder = subghz_protocol_decoder_raw_alloc(environment);
    if(!decoder) {
        subghz_environment_free(environment);
        cfp_reply(CFP_VERSION " %lu ERR save_failed\r\n", id);
        return;
    }

    /* The preset the file records so replay knows the modulation. It must be written as a
       STANDARD named preset (Preset: FuriHalSubGhzPresetOok650Async) - exactly what a working
       Flipper-made .sub carries - NOT as a Custom_preset_data register blob. save_to_file_init
       only writes the custom block when preset.data is non-NULL; an earlier version passed the
       OOK650 register array there, which made the file record 'Preset: FuriHalSubGhzPresetCustom'
       with a register dump the transmit path could not correctly reconstruct - so the capture
       replayed as nothing, in our app AND in the stock Sub-GHz app. Passing NULL data makes it
       write the plain named preset instead, matching a known-good capture. */
    SubGhzRadioPreset preset = {
        .name = furi_string_alloc_set("FuriHalSubGhzPresetOok650Async"),
        .frequency = frequency,
        .data = NULL,
        .data_size = 0,
    };

    /* save_to_file_init takes the BARE device name, not a path: it builds the full path itself
       as SUBGHZ_RAW_FOLDER/<dev_name>.sub (= /ext/subghz/<stem>.sub, which is what replay
       opens) and creates the folder itself. Passing a path with folders or a .sub extension
       makes it build a doubled, invalid path and return false - the cause of the earlier
       save_failed. So we hand it just the sanitised stem. */
    if(!subghz_protocol_raw_save_to_file_init(decoder, stem, &preset)) {
        furi_string_free(preset.name);
        subghz_protocol_decoder_raw_free(decoder);
        subghz_environment_free(environment);
        cfp_reply(CFP_VERSION " %lu ERR save_failed\r\n", id);
        return;
    }

    /* The captured edges must reach the RAW decoder on a normal thread, not in the async-RX
       capture context: the decoder streams samples to a file, and file I/O from the capture
       callback would fault. So the same worker the decoded read uses sits between them - the
       HAL callback (subghz_worker_rx_callback) only buffers edges, and the worker thread hands
       them to subghz_protocol_decoder_raw_feed, which does the file write safely. This is how
       the stock Sub-GHz "Read RAW" screen is wired. */
    SubGhzWorker* worker = subghz_worker_alloc();
    subghz_worker_set_pair_callback(
        worker, (SubGhzWorkerPairCallback)subghz_protocol_decoder_raw_feed);
    subghz_worker_set_context(worker, decoder);

    furi_hal_subghz_reset();
    furi_hal_subghz_load_registers(subghz_device_cc1101_preset_ook_650khz_async_regs);
    furi_hal_subghz_set_frequency_and_path(frequency);
    furi_hal_subghz_start_async_rx(subghz_worker_rx_callback, worker);
    subghz_worker_start(worker);

    uint32_t waited = 0;
    while(waited < timeout_ms) {
        furi_delay_ms(CFP_READ_POLL_MS);
        waited += CFP_READ_POLL_MS;
    }

    subghz_worker_stop(worker);
    furi_hal_subghz_stop_async_rx();
    furi_hal_subghz_idle();
    furi_hal_subghz_sleep();

    size_t samples = subghz_protocol_raw_get_sample_write(decoder);
    subghz_protocol_raw_save_to_file_stop(decoder);

    subghz_worker_free(worker);
    furi_string_free(preset.name);
    subghz_protocol_decoder_raw_free(decoder);
    subghz_environment_free(environment);

    /* A capture with almost no edges is just noise, not a signal; report no_signal so the
       desktop does not save an empty file under a meaningful name. The threshold is low - a
       real remote burst is hundreds of edges. */
    if(samples < 16) {
        /* The file was created by save_to_file_init; leave it - the desktop treats no_signal
           as "nothing to save" and will not name it, and a stray tiny file is harmless. */
        cfp_reply(CFP_VERSION " %lu OK no_signal\r\n", id);
        return;
    }

    /* Echo back the sanitised stem (no directory, no extension) - exactly what the desktop
       records and later passes to subghz.send_raw. */
    cfp_reply(CFP_VERSION " %lu OK captured %u %s\r\n", id, (unsigned)samples, stem);
}

/* subghz.send_raw <name> - replay a raw capture recorded by subghz.read_raw.
   Loads /ext/subghz/<name>.sub, hands it to the RAW encoder, and keys the radio through the
   same async TX path subghz.send uses. OFFENSIVE. Answers 'OK transmitted', or an error if
   the capture is missing or unreadable. */
static void cfp_cmd_subghz_send_raw(uint32_t id, const char* name_arg, char** cursor) {
    UNUSED(cursor);
    char stem[64];
    if(!cfp_raw_sanitise(name_arg, stem, sizeof(stem))) {
        cfp_reply(CFP_VERSION " %lu ERR missing_name\r\n", id);
        return;
    }
    char path[128];
    cfp_raw_path(stem, path, sizeof(path));

    /* Read the transmit frequency AND the modulation preset out of the capture file itself.
       The RAW encoder does NOT take the .sub's contents for tuning - it takes a File_name
       pointing AT the file for the sample stream (see below) - so the radio's frequency and
       registers have to be set here. Crucially the preset must be the one the capture was
       RECORDED with: subghz.read_raw writes 'Preset: FuriHalSubGhzPresetCustom' with a
       Custom_preset_data register block, and replaying with a different modulation (an earlier
       version hardcoded OOK 650) keys the radio wrong, so the timings go out but the receiver
       does not recognise them - which is exactly why a replay transmitted yet the relay never
       clicked. We load the file's own register block instead. */
    Storage* storage = furi_record_open(RECORD_STORAGE);
    FlipperFormat* cap = flipper_format_file_alloc(storage);
    uint32_t frequency = 0;
    uint8_t custom_preset[128];
    uint32_t custom_preset_size = 0;
    bool have_file = flipper_format_file_open_existing(cap, path);
    if(have_file) {
        flipper_format_read_uint32(cap, "Frequency", &frequency, 1);
        /* Custom_preset_data is a variable-length hex array; ask its length, then read it. If
           the capture used a standard preset instead, this key is absent and size stays 0. */
        if(flipper_format_get_value_count(cap, "Custom_preset_data", &custom_preset_size) &&
           custom_preset_size > 0 && custom_preset_size <= sizeof(custom_preset)) {
            if(!flipper_format_read_hex(
                   cap, "Custom_preset_data", custom_preset, custom_preset_size)) {
                custom_preset_size = 0;
            }
        } else {
            custom_preset_size = 0;
        }
    }
    flipper_format_free(cap);
    if(!have_file) {
        furi_record_close(RECORD_STORAGE);
        cfp_reply(CFP_VERSION " %lu ERR no_capture\r\n", id);
        return;
    }
    if(!furi_hal_subghz_is_frequency_valid(frequency)) {
        furi_record_close(RECORD_STORAGE);
        cfp_reply(CFP_VERSION " %lu ERR bad_capture\r\n", id);
        return;
    }

    /* The RAW encoder replays a .sub by being handed a FlipperFormat that names the file and
       the radio to use - NOT the file's own body. subghz_protocol_raw_gen_fff_data builds
       exactly that (File_name + Radio_device_name). Handing deserialize the .sub's contents
       instead, as an earlier version did, left the encoder's file worker uninitialised, and
       yield then dereferenced a NULL worker mid-transmit - the null pointer dereference. The
       device registry must be initialised so the worker can resolve "cc1101_int" by name. */
    subghz_devices_init();

    SubGhzEnvironment* environment = subghz_environment_alloc();
    subghz_environment_set_protocol_registry(environment, &subghz_protocol_registry);
    SubGhzProtocolEncoderRAW* encoder = subghz_protocol_encoder_raw_alloc(environment);

    FlipperFormat* tx = flipper_format_string_alloc();
    subghz_protocol_raw_gen_fff_data(tx, path, SUBGHZ_DEVICE_CC1101_INT_NAME);

    SubGhzProtocolStatus status = subghz_protocol_encoder_raw_deserialize(encoder, tx);
    if(status != SubGhzProtocolStatusOk) {
        flipper_format_free(tx);
        subghz_protocol_encoder_raw_free(encoder);
        subghz_environment_free(environment);
        subghz_devices_deinit();
        furi_record_close(RECORD_STORAGE);
        cfp_reply(CFP_VERSION " %lu ERR bad_capture\r\n", id);
        return;
    }

    /* deserialize started the file-encoder worker; it streams samples off the file as TX
       drains them, so tx and the storage record must stay open until the radio has stopped. */
    furi_hal_subghz_reset();
    /* Load the modulation the capture recorded: its own custom register block if it carried one
       (the usual case for our raw captures), else fall back to the standard OOK 650. */
    if(custom_preset_size > 0) {
        furi_hal_subghz_load_custom_preset(custom_preset);
    } else {
        furi_hal_subghz_load_registers(subghz_device_cc1101_preset_ook_650khz_async_regs);
    }
    furi_hal_subghz_set_frequency_and_path(frequency);
    furi_hal_subghz_start_async_tx(subghz_protocol_encoder_raw_yield, encoder);
    /* Stop when the file has played out, or at the 5 s cap - whichever comes first. A capture
       longer than the cap is truncated on replay; the useful burst is at the start. */
    uint32_t tx_waited = 0;
    while(!furi_hal_subghz_is_async_tx_complete() && tx_waited < CFP_RAW_TX_MAX_MS) {
        furi_delay_ms(5);
        tx_waited += 5;
    }
    furi_hal_subghz_stop_async_tx();
    furi_hal_subghz_idle();
    furi_hal_subghz_sleep();

    flipper_format_free(tx);
    subghz_protocol_encoder_raw_free(encoder);
    subghz_environment_free(environment);
    subghz_devices_deinit();
    furi_record_close(RECORD_STORAGE);

    cfp_reply(CFP_VERSION " %lu OK transmitted\r\n", id);
}

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
        cfp_reply(CFP_VERSION " %lu ERR missing_code\r\n", id);
        return;
    }

    InfraredProtocol protocol = cfp_ir_protocol_by_name(proto_arg);
    if(protocol == InfraredProtocolUnknown) {
        cfp_reply(CFP_VERSION " %lu ERR unknown_protocol\r\n", id);
        return;
    }

    furi_mutex_acquire(ir->mutex, FuriWaitForever);

    if(ir->running) {
        furi_mutex_release(ir->mutex);
        cfp_reply(CFP_VERSION " %lu ERR busy\r\n", id);
        return;
    }
    if(ir->count >= CFP_IR_MAX_CODES) {
        furi_mutex_release(ir->mutex);
        cfp_reply(CFP_VERSION " %lu ERR queue_full\r\n", id);
        return;
    }

    ir->codes[ir->count].protocol = protocol;
    ir->codes[ir->count].address = (uint32_t)strtoul(address_arg, NULL, 0);
    ir->codes[ir->count].command = (uint32_t)strtoul(command_arg, NULL, 0);
    ir->count++;
    size_t queued = ir->count;

    furi_mutex_release(ir->mutex);
    cfp_reply(CFP_VERSION " %lu OK %u\r\n", id, (unsigned)queued);
}

/* ir.bruteforce [label] - starts transmitting the queued codes.
   Returns as soon as the run has started: the desktop must not sit blocked on the
   serial port for the whole sequence, and its own read timeout is only 2 s. */
static void cfp_cmd_ir_bruteforce(uint32_t id, const char* label, CfpIrState* ir) {
    furi_mutex_acquire(ir->mutex, FuriWaitForever);

    if(ir->running) {
        furi_mutex_release(ir->mutex);
        cfp_reply(CFP_VERSION " %lu ERR busy\r\n", id);
        return;
    }
    if(ir->count == 0) {
        furi_mutex_release(ir->mutex);
        cfp_reply(CFP_VERSION " %lu ERR empty_queue\r\n", id);
        return;
    }

    ir->sent = 0;
    ir->stopped = false;
    ir->running = true;
    strncpy(ir->label, label ? label : "device", sizeof(ir->label) - 1);
    ir->label[sizeof(ir->label) - 1] = '\0';
    size_t total = ir->count;

    furi_mutex_release(ir->mutex);
    cfp_reply(CFP_VERSION " %lu OK started %u\r\n", id, (unsigned)total);
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

    cfp_reply(
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

    cfp_reply(CFP_VERSION " %lu OK cleared\r\n", id);
}

/* Closes the application remotely, by sending it the same event as pressing Back.
   Without this, any reinstall requires physically pressing the button on the device. */
static void cfp_cmd_exit(uint32_t id, FuriMessageQueue* event_queue) {
    cfp_reply(CFP_VERSION " %lu OK closing\r\n", id);
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
        cfp_reply(CFP_VERSION " %lu ERR wifi_board_not_connected\r\n", id);
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
        cfp_reply(CFP_VERSION " %lu ERR wifi_board_not_connected\r\n", id);
        return;
    }

    /* The board already answers in CFP, so its line is relayed verbatim; the Flipper does
       not parse or reformat it. Through the pipe, not printf, for the same no-leak reason. */
    cfp_reply("%s\r\n", line);
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
        cfp_reply(CFP_VERSION " %lu OK pong\r\n", id);
    } else if(strcmp(cmd, "info") == 0) {
        /* model, then free heap and largest contiguous free block (bytes). The last two are
           what the Sub-GHz decoder stack has to fit into; reporting them lets the desktop see
           on real hardware whether a subghz.read will fit before it is attempted. */
        cfp_reply(
            CFP_VERSION " %lu OK %s heap=%u maxblock=%u\r\n",
            id,
            furi_hal_version_get_model_name(),
            (unsigned)memmgr_get_free_heap(),
            (unsigned)memmgr_heap_get_max_free_block());
    } else if(strcmp(cmd, "subghz.rssi") == 0) {
        cfp_cmd_subghz_rssi(id, arg);
    } else if(strcmp(cmd, "subghz.read") == 0) {
        cfp_cmd_subghz_read(id, arg, cursor);
    } else if(strcmp(cmd, "subghz.send") == 0) {
        cfp_cmd_subghz_send(id, arg, cursor);
    } else if(strcmp(cmd, "subghz.read_raw") == 0) {
        cfp_cmd_subghz_read_raw(id, arg, cursor);
    } else if(strcmp(cmd, "subghz.send_raw") == 0) {
        cfp_cmd_subghz_send_raw(id, arg, cursor);
    } else if(strcmp(cmd, "ir.queue") == 0) {
        cfp_cmd_ir_queue(id, arg, cursor, ir);
    } else if(strcmp(cmd, "ir.bruteforce") == 0) {
        cfp_cmd_ir_bruteforce(id, arg, ir);
    } else if(strcmp(cmd, "ir.status") == 0) {
        cfp_cmd_ir_status(id, ir);
    } else if(strcmp(cmd, "ir.reset") == 0) {
        cfp_cmd_ir_reset(id, ir);
    } else if(strcmp(cmd, "nfc.read") == 0) {
        cfp_cmd_nfc_read(id, arg);
    } else if(strcmp(cmd, "nfc.watch") == 0) {
        cfp_cmd_nfc_watch(id, arg);
    } else if(strcmp(cmd, "exit") == 0) {
        cfp_cmd_exit(id, event_queue);
    } else if(cfp_is_board_command(cmd)) {
        cfp_forward_to_board(id, frame, bridge);
    } else {
        cfp_reply(CFP_VERSION " %lu ERR unknown_command\r\n", id);
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
    CfpContext* ctx = context;

    /* Every response for this command goes out through this pipe (see cfp_reply). */
    cfp_set_reply_pipe(pipe);

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
        cfp_reply(CFP_VERSION " 0 ERR bad_frame\r\n");
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
