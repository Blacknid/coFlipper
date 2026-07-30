#include <furi.h>
#include <furi_hal_subghz.h>
#include <furi_hal_version.h>
#include <cli/cli.h>
#include <gui/gui.h>
#include <gui/view_port.h>
#include <input/input.h>
#include <subghz/devices/cc1101_configs.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CFP_VERSION "CFP/1"
#define CFP_CLI_COMMAND "cfp"

/* Formatul protocolului CFP este documentat in /PROTOCOL.md, la radacina proiectului. */

/* Durata totala de esantionare a nivelului de semnal, in pasi de CFP_RSSI_STEP_MS. */
#define CFP_RSSI_SAMPLES 20
#define CFP_RSSI_STEP_MS 5

/* Masoara nivelul de semnal (RSSI) pe o frecventa data.
   Radioul e configurat, ascultat scurt si eliberat la fiecare apel: comanda e
   fara stare, ca sa nu blocheze modulul intre cereri. */
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

    /* Radioul intern e deja initializat de firmware la pornire; secventa de mai jos
       (reset -> preset -> frecventa -> rx) e cea folosita si de aplicatiile stock. */
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

    /* printf-ul din firmware nu formateaza float, deci separam manual
       partea intreaga de zecimala (RSSI e negativ: -92.3 -> "-92" si "3"). */
    int32_t decidbm = (int32_t)(peak * 10.0f);
    int32_t whole = decidbm / 10;
    int32_t fraction = decidbm % 10;
    if(fraction < 0) fraction = -fraction;

    printf(CFP_VERSION " %lu OK %lu %ld.%ld\r\n", id, actual, whole, fraction);
}

/* Inchide aplicatia de la distanta, trimitandu-i acelasi eveniment ca apasarea Back.
   Fara asta, orice reinstalare cere apasarea fizica a butonului pe dispozitiv. */
static void cfp_cmd_exit(uint32_t id, FuriMessageQueue* event_queue) {
    printf(CFP_VERSION " %lu OK closing\r\n", id);
    InputEvent event = {.key = InputKeyBack, .type = InputTypeShort};
    furi_message_queue_put(event_queue, &event, 0);
}

static void cfp_dispatch(
    uint32_t id,
    const char* cmd,
    const char* arg,
    FuriMessageQueue* event_queue) {
    if(strcmp(cmd, "ping") == 0) {
        printf(CFP_VERSION " %lu OK pong\r\n", id);
    } else if(strcmp(cmd, "info") == 0) {
        printf(CFP_VERSION " %lu OK %s\r\n", id, furi_hal_version_get_model_name());
    } else if(strcmp(cmd, "subghz.rssi") == 0) {
        cfp_cmd_subghz_rssi(id, arg);
    } else if(strcmp(cmd, "exit") == 0) {
        cfp_cmd_exit(id, event_queue);
    } else {
        printf(CFP_VERSION " %lu ERR unknown_command\r\n", id);
    }
}

/* Separa cuvintele urmatoare din buffer, terminandu-le cu '\0'.
   Returneaza inceputul cuvantului sau NULL cand nu mai urmeaza niciunul. */
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

static void cfp_cli_callback(PipeSide* pipe, FuriString* args, void* context) {
    UNUSED(pipe);
    FuriMessageQueue* event_queue = context;

    char buffer[160];
    strncpy(buffer, furi_string_get_cstr(args), sizeof(buffer) - 1);
    buffer[sizeof(buffer) - 1] = '\0';

    /* strtok_r nu e disponibil in API-ul firmware-ului, deci separam manual
       token-urile; buffer e o copie locala, deci o putem modifica. */
    char* cursor = buffer;
    char* id_token = cfp_next_token(&cursor);
    char* cmd_token = cfp_next_token(&cursor);
    char* arg_token = cfp_next_token(&cursor);

    if(!id_token || !cmd_token) {
        printf(CFP_VERSION " 0 ERR bad_frame\r\n");
        return;
    }

    uint32_t id = (uint32_t)strtoul(id_token, NULL, 10);
    cfp_dispatch(id, cmd_token, arg_token, event_queue);
}

static void cfp_draw_callback(Canvas* canvas, void* context) {
    UNUSED(context);
    canvas_clear(canvas);
    canvas_set_font(canvas, FontPrimary);
    canvas_draw_str(canvas, 2, 12, "coFlipper - CFP");
    canvas_set_font(canvas, FontSecondary);
    canvas_draw_str(canvas, 2, 28, "Server activ pe CLI (USB)");
    canvas_draw_str(canvas, 2, 40, "comanda: cfp <id> <cmd>");
    canvas_draw_str(canvas, 2, 60, "Back sau 'cfp N exit' = iesire");
}

static void cfp_input_callback(InputEvent* event, void* context) {
    FuriMessageQueue* queue = context;
    furi_message_queue_put(queue, event, FuriWaitForever);
}

int32_t cfp_app_main(void* p) {
    UNUSED(p);

    FuriMessageQueue* event_queue = furi_message_queue_alloc(8, sizeof(InputEvent));

    ViewPort* view_port = view_port_alloc();
    view_port_draw_callback_set(view_port, cfp_draw_callback, NULL);
    view_port_input_callback_set(view_port, cfp_input_callback, event_queue);

    Gui* gui = furi_record_open(RECORD_GUI);
    gui_add_view_port(gui, view_port, GuiLayerFullscreen);

    CliRegistry* cli = furi_record_open(RECORD_CLI);
    // ParallelSafe: comanda trebuie sa functioneze cat timp aplicatia noastra
    // (care o inregistreaza) ruleaza pe ecran, nu doar cand nu-i nicio aplicatie deschisa.
    cli_registry_add_command(
        cli, CFP_CLI_COMMAND, CliCommandFlagParallelSafe, cfp_cli_callback, event_queue);

    InputEvent event;
    bool running = true;
    while(running) {
        if(furi_message_queue_get(event_queue, &event, 100) == FuriStatusOk) {
            if(event.key == InputKeyBack) {
                running = false;
            }
        }
    }

    cli_registry_delete_command(cli, CFP_CLI_COMMAND);
    furi_record_close(RECORD_CLI);

    gui_remove_view_port(gui, view_port);
    furi_record_close(RECORD_GUI);
    view_port_free(view_port);
    furi_message_queue_free(event_queue);

    return 0;
}
