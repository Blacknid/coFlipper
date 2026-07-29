#include <furi.h>
#include <furi_hal_version.h>
#include <cli/cli.h>
#include <gui/gui.h>
#include <gui/view_port.h>
#include <input/input.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CFP_VERSION "CFP/1"
#define CFP_CLI_COMMAND "cfp"

/* Formatul protocolului CFP este documentat in /PROTOCOL.md, la radacina proiectului. */

static void cfp_dispatch(uint32_t id, const char* cmd) {
    if(strcmp(cmd, "ping") == 0) {
        printf(CFP_VERSION " %lu OK pong\r\n", id);
    } else if(strcmp(cmd, "info") == 0) {
        printf(CFP_VERSION " %lu OK %s\r\n", id, furi_hal_version_get_model_name());
    } else if(
        strcmp(cmd, "subghz.info") == 0 || strcmp(cmd, "ir.info") == 0 ||
        strcmp(cmd, "nfc.info") == 0) {
        /* TODO: legate de modulele hardware reale (radio, IR, NFC). */
        printf(CFP_VERSION " %lu ERR not_implemented\r\n", id);
    } else {
        printf(CFP_VERSION " %lu ERR unknown_command\r\n", id);
    }
}

/* Pipe-ul e instalat ca stdio al thread-ului comenzii, deci printf() scrie direct pe portul serial. */
static void cfp_cli_callback(PipeSide* pipe, FuriString* args, void* context) {
    UNUSED(pipe);
    UNUSED(context);

    char buffer[160];
    strncpy(buffer, furi_string_get_cstr(args), sizeof(buffer) - 1);
    buffer[sizeof(buffer) - 1] = '\0';

    char* saveptr = NULL;
    char* id_token = strtok_r(buffer, " ", &saveptr);
    char* cmd_token = strtok_r(NULL, " ", &saveptr);

    if(!id_token || !cmd_token) {
        printf(CFP_VERSION " 0 ERR bad_frame\r\n");
        return;
    }

    uint32_t id = (uint32_t)strtoul(id_token, NULL, 10);
    cfp_dispatch(id, cmd_token);
}

static void cfp_draw_callback(Canvas* canvas, void* context) {
    UNUSED(context);
    canvas_clear(canvas);
    canvas_set_font(canvas, FontPrimary);
    canvas_draw_str(canvas, 2, 12, "coFlipper - CFP");
    canvas_set_font(canvas, FontSecondary);
    canvas_draw_str(canvas, 2, 28, "Server activ pe CLI (USB)");
    canvas_draw_str(canvas, 2, 40, "comanda: cfp <id> <cmd>");
    canvas_draw_str(canvas, 2, 60, "Back = iesire");
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
        cli, CFP_CLI_COMMAND, CliCommandFlagParallelSafe, cfp_cli_callback, NULL);

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
