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
    GtkWidget *avatar, *initials, *name, *user_row, *not_listed, *clock;
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
        gtk_widget_grab_focus(greeter->user);
        return;
    }
    show(greeter, "");
    gtk_widget_set_sensitive(greeter->button, FALSE);
    gtk_widget_set_sensitive(greeter->password, FALSE);
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
    gtk_widget_set_sensitive(greeter->password, TRUE);
    gtk_widget_add_css_class(greeter->password, "error");
    gtk_widget_grab_focus(greeter->password);
}

static void power(GtkButton *button, gpointer data) {
    (void)button;
    const char *argv[] = {"/usr/bin/loginctl", data, NULL};
    g_spawn_async(NULL, (char **)argv, NULL, G_SPAWN_DEFAULT, NULL, NULL, NULL, NULL);
}

// The first ordinary account (uid 1000-59999) is offered by default.
static struct passwd *default_account(void) {
    struct passwd *entry;
    setpwent();
    while ((entry = getpwent()))
        if (entry->pw_uid >= 1000 && entry->pw_uid < 60000) break;
    endpwent();
    return entry;
}

// "Ada Lovelace" -> "AL", "meldra" -> "M".
static char *initials_of(const char *name) {
    GString *out = g_string_new(NULL);
    gboolean word_start = TRUE;
    for (const char *p = name; *p && out->len < 8; p = g_utf8_next_char(p)) {
        gunichar c = g_utf8_get_char(p);
        if (g_unichar_isspace(c)) {
            word_start = TRUE;
        } else if (word_start) {
            g_string_append_unichar(out, g_unichar_toupper(c));
            word_start = FALSE;
        }
    }
    char *result = g_utf8_substring(out->str, 0, MIN(2, g_utf8_strlen(out->str, -1)));
    g_string_free(out, TRUE);
    return result;
}

// Shows the account picture (AccountsService icon) or coloured initials, and the full name, for `user`.
static void show_account(Greeter *greeter, const char *user) {
    struct passwd *entry = user && *user ? getpwnam(user) : NULL;
    char *real = NULL;
    if (entry && entry->pw_gecos && *entry->pw_gecos) real = g_strndup(entry->pw_gecos, strcspn(entry->pw_gecos, ","));
    const char *shown = real && *real ? real : (user && *user ? user : "");
    gtk_label_set_text(GTK_LABEL(greeter->name), shown);
    char *icon = user && *user ? g_build_filename("/var/lib/AccountsService/icons", user, NULL) : NULL;
    if (icon && g_file_test(icon, G_FILE_TEST_IS_REGULAR)) {
        gtk_picture_set_filename(GTK_PICTURE(greeter->avatar), icon);
        gtk_widget_set_visible(greeter->avatar, TRUE);
        gtk_widget_set_visible(greeter->initials, FALSE);
    } else {
        char *letters = initials_of(*shown ? shown : "?");
        gtk_label_set_text(GTK_LABEL(greeter->initials), letters);
        g_free(letters);
        // A stable colour per user, from the Adwaita palette.
        static const char *const colours[] = {"avatar-blue", "avatar-teal", "avatar-green", "avatar-yellow",
                                              "avatar-orange", "avatar-red", "avatar-purple"};
        for (size_t i = 0; i < G_N_ELEMENTS(colours); i++) gtk_widget_remove_css_class(greeter->initials, colours[i]);
        gtk_widget_add_css_class(greeter->initials, colours[g_str_hash(*shown ? shown : "?") % G_N_ELEMENTS(colours)]);
        gtk_widget_set_visible(greeter->avatar, FALSE);
        gtk_widget_set_visible(greeter->initials, TRUE);
    }
    g_free(icon);
    g_free(real);
}

static void user_changed(GtkEditable *editable, gpointer data) {
    show_account(data, gtk_editable_get_text(editable));
}

static void password_changed(GtkEditable *editable, gpointer data) {
    (void)editable;
    gtk_widget_remove_css_class(((Greeter *)data)->password, "error");
}

// "Not listed?": type another user name.
static void choose_user(GtkButton *button, gpointer data) {
    (void)button;
    Greeter *greeter = data;
    gtk_widget_set_visible(greeter->user_row, TRUE);
    gtk_widget_set_visible(greeter->not_listed, FALSE);
    gtk_editable_set_text(GTK_EDITABLE(greeter->user), "");
    show(greeter, "");
    gtk_widget_grab_focus(greeter->user);
}

static gboolean tick(gpointer data) {
    Greeter *greeter = data;
    GDateTime *now = g_date_time_new_now_local();
    char *text = g_date_time_format(now, "%a %b %-d  %H:%M");
    gtk_label_set_text(GTK_LABEL(greeter->clock), text);
    g_free(text);
    g_date_time_unref(now);
    return G_SOURCE_CONTINUE;
}

// GNOME's default wallpaper (dark variant), the same picture the desktop starts with.
#define BACKGROUND "/usr/share/backgrounds/gnome/adwaita-d.jxl"

static const char *STYLE =
    "window { background-color: #1d1d20; }"
    // Top bar, as in GNOME Shell.
    ".topbar { background-color: alpha(black, 0.55); min-height: 30px; padding: 0 8px; }"
    ".topbar label { color: white; font-weight: bold; font-size: 11pt; }"
    ".topbar .brand { font-weight: 800; letter-spacing: 1px; }"
    ".topbar menubutton > button { color: white; min-height: 26px; padding: 0 8px; border-radius: 99px; }"
    ".topbar menubutton > button:hover { background-color: alpha(white, 0.15); }"
    ".dim { background-color: alpha(black, 0.35); }"
    // The account and password column.
    ".avatar { border-radius: 9999px; }"
    ".initials { border-radius: 9999px; min-width: 128px; min-height: 128px; font-size: 44pt; font-weight: 800;"
    "  color: white; }"
    ".avatar-blue { background-color: #3584e4; } .avatar-teal { background-color: #2190a4; }"
    ".avatar-green { background-color: #3a944a; } .avatar-yellow { background-color: #c88800; }"
    ".avatar-orange { background-color: #ed5b00; } .avatar-red { background-color: #e62d42; }"
    ".avatar-purple { background-color: #9141ac; }"
    ".account-name { color: white; font-size: 20pt; font-weight: 800; }"
    "entry.login-entry { min-width: 280px; min-height: 40px; border-radius: 99px; padding: 0 16px;"
    "  background-color: alpha(white, 0.12); color: white; }"
    "entry.login-entry.error { outline: 2px solid #ff7b63; }"
    "button.go { min-width: 40px; min-height: 40px; padding: 0; border-radius: 99px; }"
    ".message { color: #ffb4a9; font-weight: bold; }"
    "button.link-button { color: alpha(white, 0.75); }"
    "button.link-button:hover { color: white; }";

static GtkWidget *power_menu(void) {
    GtkWidget *box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 2);
    const struct { const char *label, *icon, *action; } items[] = {
        {"Restart…", "system-reboot-symbolic", "reboot"},
        {"Power Off…", "system-shutdown-symbolic", "poweroff"},
    };
    for (size_t i = 0; i < G_N_ELEMENTS(items); i++) {
        GtkWidget *row = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 10);
        gtk_box_append(GTK_BOX(row), gtk_image_new_from_icon_name(items[i].icon));
        gtk_box_append(GTK_BOX(row), gtk_label_new(items[i].label));
        GtkWidget *button = gtk_button_new();
        gtk_button_set_child(GTK_BUTTON(button), row);
        gtk_widget_add_css_class(button, "flat");
        g_signal_connect(button, "clicked", G_CALLBACK(power), (gpointer)items[i].action);
        gtk_box_append(GTK_BOX(box), button);
    }
    GtkWidget *popover = gtk_popover_new();
    gtk_popover_set_child(GTK_POPOVER(popover), box);
    GtkWidget *button = gtk_menu_button_new();
    gtk_menu_button_set_icon_name(GTK_MENU_BUTTON(button), "system-shutdown-symbolic");
    gtk_menu_button_set_popover(GTK_MENU_BUTTON(button), popover);
    gtk_widget_set_tooltip_text(button, "Power");
    gtk_accessible_update_property(GTK_ACCESSIBLE(button), GTK_ACCESSIBLE_PROPERTY_LABEL, "Power", -1);
    return button;
}

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

    // Background: the wallpaper, dimmed so the white text stays readable (a plain dark fill if it is missing).
    GtkWidget *overlay = gtk_overlay_new();
    if (g_file_test(BACKGROUND, G_FILE_TEST_IS_REGULAR)) {
        GtkWidget *wallpaper = gtk_picture_new_for_filename(BACKGROUND);
        gtk_picture_set_content_fit(GTK_PICTURE(wallpaper), GTK_CONTENT_FIT_COVER);
        gtk_overlay_set_child(GTK_OVERLAY(overlay), wallpaper);
    } else {
        gtk_overlay_set_child(GTK_OVERLAY(overlay), gtk_box_new(GTK_ORIENTATION_VERTICAL, 0));
    }
    GtkWidget *layout = gtk_box_new(GTK_ORIENTATION_VERTICAL, 0);
    gtk_widget_add_css_class(layout, "dim");
    gtk_overlay_add_overlay(GTK_OVERLAY(overlay), layout);

    // Top bar: name, clock, power menu.
    GtkWidget *bar = gtk_center_box_new();
    gtk_widget_add_css_class(bar, "topbar");
    GtkWidget *brand = gtk_label_new("Meldra");
    gtk_widget_add_css_class(brand, "brand");
    greeter->clock = gtk_label_new("");
    gtk_center_box_set_start_widget(GTK_CENTER_BOX(bar), brand);
    gtk_center_box_set_center_widget(GTK_CENTER_BOX(bar), greeter->clock);
    gtk_center_box_set_end_widget(GTK_CENTER_BOX(bar), power_menu());
    gtk_box_append(GTK_BOX(layout), bar);
    tick(greeter);
    g_timeout_add_seconds(1, tick, greeter);

    // Centre column: picture, name, (user name), password, message, "Not listed?".
    GtkWidget *column = gtk_box_new(GTK_ORIENTATION_VERTICAL, 14);
    gtk_widget_set_halign(column, GTK_ALIGN_CENTER);
    gtk_widget_set_valign(column, GTK_ALIGN_CENTER);
    gtk_widget_set_vexpand(column, TRUE);

    greeter->avatar = gtk_picture_new();
    gtk_picture_set_content_fit(GTK_PICTURE(greeter->avatar), GTK_CONTENT_FIT_COVER);
    gtk_widget_set_size_request(greeter->avatar, 128, 128);
    gtk_widget_set_halign(greeter->avatar, GTK_ALIGN_CENTER);
    gtk_widget_set_overflow(greeter->avatar, GTK_OVERFLOW_HIDDEN);
    gtk_widget_add_css_class(greeter->avatar, "avatar");
    greeter->initials = gtk_label_new("");
    gtk_widget_set_halign(greeter->initials, GTK_ALIGN_CENTER);
    gtk_widget_add_css_class(greeter->initials, "initials");
    greeter->name = gtk_label_new("");
    gtk_widget_add_css_class(greeter->name, "account-name");

    greeter->user = gtk_entry_new();
    gtk_entry_set_placeholder_text(GTK_ENTRY(greeter->user), "User name");
    gtk_widget_add_css_class(greeter->user, "login-entry");
    greeter->user_row = greeter->user;

    greeter->password = gtk_password_entry_new();
    gtk_password_entry_set_show_peek_icon(GTK_PASSWORD_ENTRY(greeter->password), TRUE);
    g_object_set(greeter->password, "placeholder-text", "Password", NULL);
    gtk_widget_add_css_class(greeter->password, "login-entry");
    gtk_widget_set_hexpand(greeter->password, TRUE);
    greeter->button = gtk_button_new_from_icon_name("go-next-symbolic");
    gtk_widget_add_css_class(greeter->button, "suggested-action");
    gtk_widget_add_css_class(greeter->button, "go");
    gtk_widget_set_tooltip_text(greeter->button, "Log In");
    gtk_accessible_update_property(GTK_ACCESSIBLE(greeter->button), GTK_ACCESSIBLE_PROPERTY_LABEL, "Log In", -1);
    GtkWidget *password_row = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 8);
    gtk_box_append(GTK_BOX(password_row), greeter->password);
    gtk_box_append(GTK_BOX(password_row), greeter->button);

    greeter->message = gtk_label_new("");
    gtk_widget_add_css_class(greeter->message, "message");
    gtk_label_set_wrap(GTK_LABEL(greeter->message), TRUE);
    gtk_label_set_max_width_chars(GTK_LABEL(greeter->message), 40);
    gtk_label_set_justify(GTK_LABEL(greeter->message), GTK_JUSTIFY_CENTER);
    gtk_accessible_update_property(GTK_ACCESSIBLE(greeter->message), GTK_ACCESSIBLE_PROPERTY_LABEL,
                                   "Login message", -1);

    greeter->not_listed = gtk_button_new_with_label("Not listed?");
    gtk_widget_add_css_class(greeter->not_listed, "link-button");
    gtk_widget_add_css_class(greeter->not_listed, "flat");
    gtk_widget_set_halign(greeter->not_listed, GTK_ALIGN_CENTER);

    gtk_box_append(GTK_BOX(column), greeter->avatar);
    gtk_box_append(GTK_BOX(column), greeter->initials);
    gtk_box_append(GTK_BOX(column), greeter->name);
    gtk_box_append(GTK_BOX(column), greeter->user);
    gtk_box_append(GTK_BOX(column), password_row);
    gtk_box_append(GTK_BOX(column), greeter->message);
    gtk_box_append(GTK_BOX(column), greeter->not_listed);
    gtk_box_append(GTK_BOX(layout), column);
    gtk_window_set_child(GTK_WINDOW(greeter->window), overlay);

    struct passwd *account = default_account();
    if (account) {
        gtk_editable_set_text(GTK_EDITABLE(greeter->user), account->pw_name);
        gtk_widget_set_visible(greeter->user, FALSE);   // shown by "Not listed?"
    } else {
        gtk_widget_set_visible(greeter->not_listed, FALSE);
    }
    show_account(greeter, gtk_editable_get_text(GTK_EDITABLE(greeter->user)));

    g_signal_connect(greeter->user, "changed", G_CALLBACK(user_changed), greeter);
    g_signal_connect(greeter->password, "changed", G_CALLBACK(password_changed), greeter);
    g_signal_connect(greeter->user, "activate", G_CALLBACK(submit), greeter);
    g_signal_connect(greeter->password, "activate", G_CALLBACK(submit), greeter);
    g_signal_connect(greeter->button, "clicked", G_CALLBACK(submit), greeter);
    g_signal_connect(greeter->not_listed, "clicked", G_CALLBACK(choose_user), greeter);

    // No gtk_window_fullscreen(): cage already shows every window fullscreen, and asking before the first frame
    // trips an assertion in cage 0.2.1 (wlr_xdg_surface_schedule_configure on an uninitialised surface).
    gtk_window_present(GTK_WINDOW(greeter->window));
    gtk_widget_grab_focus(account ? greeter->password : greeter->user);
}

int main(int argc, char **argv) {
    Greeter greeter = {0};
    GtkApplication *app = gtk_application_new("org.meldra.Greeter", G_APPLICATION_NON_UNIQUE);
    g_signal_connect(app, "activate", G_CALLBACK(activate), &greeter);
    int status = g_application_run(G_APPLICATION(app), argc, argv);
    g_object_unref(app);
    return status;
}
