// SPDX-License-Identifier: GPL-2.0-only
//
// meldra-greeter: Meldra's login screen. A GTK window that runs inside the cage kiosk compositor as the
// unprivileged "greeter" user and asks greetd (over the socket named by $GREETD_SOCK) to authenticate a user and
// start their GNOME session. greetd does all PAM work as root; this program only relays prompts and answers.
//
// greetd's IPC: each message is a native-endian 32-bit length followed by that many bytes of JSON.
//   requests:  create_session {username}, post_auth_message_response {response}, start_session {cmd, env},
//              cancel_session
//   responses: success, error {error_type, description}, auth_message {auth_message_type, auth_message}

#include <errno.h>
#include <gtk/gtk.h>
#include <json-glib/json-glib.h>
#include <pwd.h>
#include <stdint.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#define SESSION_COMMAND "/usr/libexec/meldra/start-gnome"

typedef struct {
    GtkWidget *window, *user, *password, *message, *button;
} Greeter;

static int greetd_connect(void) {
    const char *path = g_getenv("GREETD_SOCK");
    if (!path || strlen(path) >= sizeof(((struct sockaddr_un *)0)->sun_path)) return -1;
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return -1;
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    strcpy(address.sun_path, path);
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) < 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static gboolean write_all(int fd, const void *data, size_t size) {
    const char *bytes = data;
    while (size > 0) {
        ssize_t done = write(fd, bytes, size);
        if (done < 0 && errno == EINTR) continue;
        if (done <= 0) return FALSE;
        bytes += done;
        size -= (size_t)done;
    }
    return TRUE;
}

static gboolean read_all(int fd, void *data, size_t size) {
    char *bytes = data;
    while (size > 0) {
        ssize_t done = read(fd, bytes, size);
        if (done < 0 && errno == EINTR) continue;
        if (done <= 0) return FALSE;
        bytes += done;
        size -= (size_t)done;
    }
    return TRUE;
}

// Send one request and return the parsed response object (caller unrefs the parser), or NULL.
static JsonParser *roundtrip(int fd, JsonBuilder *request, JsonObject **response) {
    JsonGenerator *generator = json_generator_new();
    JsonNode *root = json_builder_get_root(request);
    json_generator_set_root(generator, root);
    gsize length = 0;
    char *text = json_generator_to_data(generator, &length);
    uint32_t header = (uint32_t)length;
    gboolean sent = write_all(fd, &header, sizeof(header)) && write_all(fd, text, length);
    // The request may carry a password: clear it before freeing.
    explicit_bzero(text, length);
    g_free(text);
    json_node_unref(root);
    g_object_unref(generator);
    if (!sent || !read_all(fd, &header, sizeof(header)) || header == 0 || header > 1 << 20) return NULL;
    char *reply = g_malloc(header + 1);
    if (!read_all(fd, reply, header)) {
        g_free(reply);
        return NULL;
    }
    reply[header] = '\0';
    JsonParser *parser = json_parser_new();
    gboolean parsed = json_parser_load_from_data(parser, reply, header, NULL);
    g_free(reply);
    if (!parsed || !JSON_NODE_HOLDS_OBJECT(json_parser_get_root(parser))) {
        g_object_unref(parser);
        return NULL;
    }
    *response = json_node_get_object(json_parser_get_root(parser));
    return parser;
}

static JsonBuilder *request(const char *type) {
    JsonBuilder *builder = json_builder_new();
    json_builder_begin_object(builder);
    json_builder_set_member_name(builder, "type");
    json_builder_add_string_value(builder, type);
    return builder;
}

static void show(Greeter *greeter, const char *text) {
    gtk_label_set_text(GTK_LABEL(greeter->message), text ? text : "");
}

static void cancel(int fd) {
    JsonBuilder *builder = request("cancel_session");
    json_builder_end_object(builder);
    JsonObject *response = NULL;
    JsonParser *parser = roundtrip(fd, builder, &response);
    if (parser) g_object_unref(parser);
    g_object_unref(builder);
}

// Runs the whole login conversation. Returns TRUE when greetd accepted the session (the greeter then exits and
// greetd starts it).
static gboolean log_in(Greeter *greeter, const char *user, const char *password) {
    int fd = greetd_connect();
    if (fd < 0) {
        show(greeter, "Cannot reach the login service (greetd).");
        return FALSE;
    }
    JsonBuilder *builder = request("create_session");
    json_builder_set_member_name(builder, "username");
    json_builder_add_string_value(builder, user);
    json_builder_end_object(builder);
    gboolean password_sent = FALSE, started = FALSE;
    for (int round = 0; round < 8 && builder; round++) {
        JsonObject *response = NULL;
        JsonParser *parser = roundtrip(fd, builder, &response);
        g_object_unref(builder);
        builder = NULL;
        if (!parser) {
            show(greeter, "The login service stopped responding.");
            break;
        }
        const char *type = json_object_get_string_member_with_default(response, "type", "");
        if (g_str_equal(type, "auth_message")) {
            const char *kind = json_object_get_string_member_with_default(response, "auth_message_type", "");
            const char *text = json_object_get_string_member_with_default(response, "auth_message", "");
            builder = request("post_auth_message_response");
            json_builder_set_member_name(builder, "response");
            if (g_str_equal(kind, "secret") && !password_sent) {
                json_builder_add_string_value(builder, password);
                password_sent = TRUE;
            } else if (g_str_equal(kind, "secret") || g_str_equal(kind, "visible")) {
                // A second prompt (for example a changed-password request) is not supported here.
                show(greeter, text);
                json_builder_add_null_value(builder);
            } else {
                show(greeter, text);
                json_builder_add_null_value(builder);
            }
            json_builder_end_object(builder);
        } else if (g_str_equal(type, "success") && !started) {
            // Authenticated: ask greetd to start GNOME once the greeter has exited.
            builder = request("start_session");
            json_builder_set_member_name(builder, "cmd");
            json_builder_begin_array(builder);
            json_builder_add_string_value(builder, SESSION_COMMAND);
            json_builder_end_array(builder);
            json_builder_set_member_name(builder, "env");
            json_builder_begin_array(builder);
            json_builder_add_string_value(builder, "XDG_SESSION_TYPE=wayland");
            json_builder_add_string_value(builder, "XDG_SESSION_DESKTOP=gnome");
            json_builder_add_string_value(builder, "XDG_CURRENT_DESKTOP=GNOME");
            json_builder_end_array(builder);
            json_builder_end_object(builder);
            started = TRUE;
        } else if (g_str_equal(type, "success")) {
            g_object_unref(parser);
            close(fd);
            return TRUE;
        } else {
            const char *kind = json_object_get_string_member_with_default(response, "error_type", "");
            show(greeter, g_str_equal(kind, "auth_error") ? "Wrong user name or password."
                          : json_object_get_string_member_with_default(response, "description", "Login failed."));
            cancel(fd);
        }
        g_object_unref(parser);
    }
    if (builder) g_object_unref(builder);
    close(fd);
    return FALSE;
}

static void submit(GtkWidget *widget, gpointer data) {
    (void)widget;
    Greeter *greeter = data;
    const char *user = gtk_editable_get_text(GTK_EDITABLE(greeter->user));
    if (!*user) {
        show(greeter, "Enter your user name.");
        return;
    }
    show(greeter, "");
    gtk_widget_set_sensitive(greeter->button, FALSE);
    char *password = g_strdup(gtk_editable_get_text(GTK_EDITABLE(greeter->password)));
    gboolean ok = log_in(greeter, user, password);
    explicit_bzero(password, strlen(password));
    g_free(password);
    gtk_editable_set_text(GTK_EDITABLE(greeter->password), "");
    if (ok) {
        gtk_window_destroy(GTK_WINDOW(greeter->window));
        return;
    }
    gtk_widget_set_sensitive(greeter->button, TRUE);
    gtk_widget_grab_focus(greeter->password);
}

static void power(GtkButton *button, gpointer data) {
    (void)button;
    const char *argv[] = {"/usr/bin/loginctl", data, NULL};
    g_spawn_async(NULL, (char **)argv, NULL, G_SPAWN_DEFAULT, NULL, NULL, NULL, NULL);
}

// The first ordinary account (uid 1000-59999) is offered by default.
static char *default_user(void) {
    struct passwd *entry;
    char *found = NULL;
    setpwent();
    while ((entry = getpwent())) {
        if (entry->pw_uid >= 1000 && entry->pw_uid < 60000) {
            found = g_strdup(entry->pw_name);
            break;
        }
    }
    endpwent();
    return found;
}

static const char *STYLE =
    "window { background: linear-gradient(160deg, #1d2333, #303a5a); }"
    ".card { background: alpha(#ffffff, 0.08); border-radius: 18px; padding: 36px; }"
    ".brand { color: white; font-size: 30px; font-weight: 800; }"
    ".hint { color: alpha(white, 0.7); }"
    ".message { color: #ffb4a9; }"
    "entry { min-width: 280px; min-height: 40px; }"
    "button.login { min-height: 40px; }";

static void activate(GtkApplication *app, gpointer data) {
    Greeter *greeter = data;
    GtkCssProvider *css = gtk_css_provider_new();
    gtk_css_provider_load_from_string(css, STYLE);
    gtk_style_context_add_provider_for_display(gdk_display_get_default(), GTK_STYLE_PROVIDER(css),
                                               GTK_STYLE_PROVIDER_PRIORITY_APPLICATION);
    g_object_set(gtk_settings_get_default(), "gtk-application-prefer-dark-theme", TRUE, NULL);

    greeter->window = gtk_application_window_new(app);
    gtk_window_set_title(GTK_WINDOW(greeter->window), "Meldra login");
    gtk_window_set_decorated(GTK_WINDOW(greeter->window), FALSE);

    GtkWidget *card = gtk_box_new(GTK_ORIENTATION_VERTICAL, 12);
    gtk_widget_add_css_class(card, "card");
    gtk_widget_set_halign(card, GTK_ALIGN_CENTER);
    gtk_widget_set_valign(card, GTK_ALIGN_CENTER);

    GtkWidget *brand = gtk_label_new("Meldra");
    gtk_widget_add_css_class(brand, "brand");
    GtkWidget *hint = gtk_label_new("Sign in to continue");
    gtk_widget_add_css_class(hint, "hint");

    greeter->user = gtk_entry_new();
    gtk_entry_set_placeholder_text(GTK_ENTRY(greeter->user), "User name");
    char *user = default_user();
    if (user) gtk_editable_set_text(GTK_EDITABLE(greeter->user), user);
    g_free(user);
    greeter->password = gtk_password_entry_new();
    gtk_password_entry_set_show_peek_icon(GTK_PASSWORD_ENTRY(greeter->password), TRUE);
    g_object_set(greeter->password, "placeholder-text", "Password", NULL);
    greeter->button = gtk_button_new_with_label("Log in");
    gtk_widget_add_css_class(greeter->button, "suggested-action");
    gtk_widget_add_css_class(greeter->button, "login");
    greeter->message = gtk_label_new("");
    gtk_widget_add_css_class(greeter->message, "message");
    gtk_label_set_wrap(GTK_LABEL(greeter->message), TRUE);

    GtkWidget *powers = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 8);
    gtk_widget_set_halign(powers, GTK_ALIGN_CENTER);
    GtkWidget *restart = gtk_button_new_with_label("Restart");
    GtkWidget *off = gtk_button_new_with_label("Power off");
    gtk_widget_add_css_class(restart, "flat");
    gtk_widget_add_css_class(off, "flat");
    gtk_box_append(GTK_BOX(powers), restart);
    gtk_box_append(GTK_BOX(powers), off);

    gtk_box_append(GTK_BOX(card), brand);
    gtk_box_append(GTK_BOX(card), hint);
    gtk_box_append(GTK_BOX(card), greeter->user);
    gtk_box_append(GTK_BOX(card), greeter->password);
    gtk_box_append(GTK_BOX(card), greeter->button);
    gtk_box_append(GTK_BOX(card), greeter->message);
    gtk_box_append(GTK_BOX(card), powers);
    gtk_window_set_child(GTK_WINDOW(greeter->window), card);

    g_signal_connect(greeter->user, "activate", G_CALLBACK(submit), greeter);
    g_signal_connect(greeter->password, "activate", G_CALLBACK(submit), greeter);
    g_signal_connect(greeter->button, "clicked", G_CALLBACK(submit), greeter);
    g_signal_connect(restart, "clicked", G_CALLBACK(power), "reboot");
    g_signal_connect(off, "clicked", G_CALLBACK(power), "poweroff");

    // No gtk_window_fullscreen(): cage already shows every window fullscreen, and asking before the first frame
    // trips an assertion in cage 0.2.1 (wlr_xdg_surface_schedule_configure on an uninitialised surface).
    gtk_window_present(GTK_WINDOW(greeter->window));
    gtk_widget_grab_focus(gtk_editable_get_text(GTK_EDITABLE(greeter->user))[0] ? greeter->password : greeter->user);
}

int main(int argc, char **argv) {
    Greeter greeter = {0};
    GtkApplication *app = gtk_application_new("org.meldra.Greeter", G_APPLICATION_NON_UNIQUE);
    g_signal_connect(app, "activate", G_CALLBACK(activate), &greeter);
    int status = g_application_run(G_APPLICATION(app), argc, argv);
    g_object_unref(app);
    return status;
}
