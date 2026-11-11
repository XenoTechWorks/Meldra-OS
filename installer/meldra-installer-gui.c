// SPDX-License-Identifier: GPL-2.0-only
//
// Install Meldra: the graphical front end of meldra-install. It collects a disk, an account and a computer name,
// then runs `pkexec /usr/sbin/meldra-install --yes ...` and follows its "meldra-install: step N/6" lines. The
// password is handed over in a private file (mode 0600 in $XDG_RUNTIME_DIR), never on a command line.

#include <fcntl.h>
#include <gio/gio.h>
#include <glib/gstdio.h>
#include <gtk/gtk.h>
#include <json-glib/json-glib.h>
#include <string.h>
#include <unistd.h>

#define INSTALLER "/usr/sbin/meldra-install"
#define MIN_BYTES (8ULL * 1024 * 1024 * 1024)

typedef struct {
    GtkWidget *window, *stack;
    GtkWidget *disks, *disk_note;
    GtkWidget *user, *password, *again, *hostname, *account_note;
    GtkWidget *summary, *erase_check, *install_button;
    GtkWidget *progress, *progress_label;
    GtkWidget *done_title, *done_text, *restart, *try_again;
    char *password_file;
    GString *errors;
} Installer;

static void free_disk(gpointer data) { g_free(data); }

static GtkWidget *page(const char *title, const char *subtitle) {
    GtkWidget *box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 14);
    gtk_widget_set_margin_top(box, 36);
    gtk_widget_set_margin_bottom(box, 36);
    gtk_widget_set_margin_start(box, 48);
    gtk_widget_set_margin_end(box, 48);
    GtkWidget *heading = gtk_label_new(NULL);
    char *markup = g_markup_printf_escaped("<span size='x-large' weight='bold'>%s</span>", title);
    gtk_label_set_markup(GTK_LABEL(heading), markup);
    g_free(markup);
    gtk_label_set_xalign(GTK_LABEL(heading), 0);
    gtk_box_append(GTK_BOX(box), heading);
    if (subtitle) {
        GtkWidget *text = gtk_label_new(subtitle);
        gtk_label_set_wrap(GTK_LABEL(text), TRUE);
        gtk_label_set_xalign(GTK_LABEL(text), 0);
        gtk_widget_add_css_class(text, "dim-label");
        gtk_box_append(GTK_BOX(box), text);
    }
    return box;
}

static GtkWidget *buttons(GtkWidget *back, GtkWidget *next) {
    GtkWidget *row = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 8);
    gtk_widget_set_valign(row, GTK_ALIGN_END);
    gtk_widget_set_vexpand(row, TRUE);
    GtkWidget *spacer = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 0);
    gtk_widget_set_hexpand(spacer, TRUE);
    if (back) gtk_box_append(GTK_BOX(row), back);
    gtk_box_append(GTK_BOX(row), spacer);
    if (next) gtk_box_append(GTK_BOX(row), next);
    return row;
}

static char *human_size(guint64 bytes) { return g_format_size_full(bytes, G_FORMAT_SIZE_DEFAULT); }

// Fill the disk list from `lsblk -J -b -d`: whole, writable disks of at least 8 GiB.
static void load_disks(Installer *app) {
    GtkWidget *child;
    while ((child = gtk_widget_get_first_child(app->disks))) gtk_list_box_remove(GTK_LIST_BOX(app->disks), child);
    char *out = NULL;
    const char *argv[] = {"lsblk", "-J", "-b", "-d", "-o", "NAME,SIZE,MODEL,TYPE,RO,TRAN", NULL};
    if (!g_spawn_sync(NULL, (char **)argv, NULL, G_SPAWN_SEARCH_PATH | G_SPAWN_STDERR_TO_DEV_NULL, NULL, NULL,
                      &out, NULL, NULL, NULL)) {
        gtk_label_set_text(GTK_LABEL(app->disk_note), "Could not list the disks (lsblk failed).");
        return;
    }
    JsonParser *parser = json_parser_new();
    int found = 0;
    if (json_parser_load_from_data(parser, out, -1, NULL)) {
        JsonObject *root = json_node_get_object(json_parser_get_root(parser));
        JsonArray *devices = json_object_get_array_member(root, "blockdevices");
        for (guint i = 0; devices && i < json_array_get_length(devices); i++) {
            JsonObject *device = json_array_get_object_element(devices, i);
            const char *type = json_object_get_string_member_with_default(device, "type", "");
            gboolean read_only = json_object_get_boolean_member_with_default(device, "ro", FALSE);
            guint64 size = (guint64)json_object_get_int_member_with_default(device, "size", 0);
            if (!g_str_equal(type, "disk") || read_only || size < MIN_BYTES) continue;
            const char *name = json_object_get_string_member_with_default(device, "name", "");
            if (g_str_has_prefix(name, "zram") || g_str_has_prefix(name, "loop")) continue;
            const char *model = json_object_get_null_member(device, "model") ? NULL
                                : json_object_get_string_member_with_default(device, "model", NULL);
            const char *transport = json_object_get_null_member(device, "tran") ? NULL
                                    : json_object_get_string_member_with_default(device, "tran", NULL);
            char *pretty = human_size(size);
            char *text = g_strdup_printf("%s — %s%s%s%s", model && *model ? g_strstrip(g_strdup(model)) : "Disk",
                                         pretty, transport ? " · " : "", transport ? transport : "", "");
            char *detail = g_strdup_printf("/dev/%s", name);
            GtkWidget *row = gtk_box_new(GTK_ORIENTATION_VERTICAL, 2);
            gtk_widget_set_margin_top(row, 8);
            gtk_widget_set_margin_bottom(row, 8);
            gtk_widget_set_margin_start(row, 12);
            GtkWidget *title = gtk_label_new(text);
            gtk_label_set_xalign(GTK_LABEL(title), 0);
            GtkWidget *path = gtk_label_new(detail);
            gtk_label_set_xalign(GTK_LABEL(path), 0);
            gtk_widget_add_css_class(path, "dim-label");
            gtk_box_append(GTK_BOX(row), title);
            gtk_box_append(GTK_BOX(row), path);
            gtk_list_box_append(GTK_LIST_BOX(app->disks), row);
            g_object_set_data_full(G_OBJECT(gtk_widget_get_parent(row)), "device", detail, free_disk);
            g_object_set_data_full(G_OBJECT(gtk_widget_get_parent(row)), "label", text, free_disk);
            g_free(pretty);
            found++;
        }
    }
    g_object_unref(parser);
    g_free(out);
    gtk_label_set_text(GTK_LABEL(app->disk_note),
                       found ? "Everything on the chosen disk will be erased."
                             : "No suitable disk found. Meldra needs a disk of at least 8 GB.");
}

static const char *chosen_device(Installer *app, const char *key) {
    GtkListBoxRow *row = gtk_list_box_get_selected_row(GTK_LIST_BOX(app->disks));
    return row ? g_object_get_data(G_OBJECT(row), key) : NULL;
}

static void go(Installer *app, const char *name) { gtk_stack_set_visible_child_name(GTK_STACK(app->stack), name); }

static void to_disks(GtkButton *b, gpointer data) { (void)b; load_disks(data); go(data, "disks"); }
static void to_welcome(GtkButton *b, gpointer data) { (void)b; go(data, "welcome"); }

static void to_account(GtkButton *b, gpointer data) {
    (void)b;
    Installer *app = data;
    if (!chosen_device(app, "device")) {
        gtk_label_set_text(GTK_LABEL(app->disk_note), "Choose a disk first.");
        return;
    }
    go(app, "account");
}

static gboolean valid_name(const char *text, gboolean user) {
    if (!*text || strlen(text) > 31) return FALSE;
    if (user && !g_ascii_islower(text[0]) && text[0] != '_') return FALSE;
    for (const char *c = text; *c; c++) {
        gboolean ok = user ? (g_ascii_islower(*c) || g_ascii_isdigit(*c) || *c == '-' || *c == '_')
                           : (g_ascii_isalnum(*c) || *c == '-');
        if (!ok) return FALSE;
    }
    return !user || !g_str_equal(text, "root");
}

static void to_confirm(GtkButton *b, gpointer data) {
    (void)b;
    Installer *app = data;
    const char *user = gtk_editable_get_text(GTK_EDITABLE(app->user));
    const char *host = gtk_editable_get_text(GTK_EDITABLE(app->hostname));
    const char *password = gtk_editable_get_text(GTK_EDITABLE(app->password));
    const char *again = gtk_editable_get_text(GTK_EDITABLE(app->again));
    const char *problem = NULL;
    if (!valid_name(user, TRUE)) problem = "User names use lower-case letters, digits, - and _, and start with a letter.";
    else if (!valid_name(host, FALSE)) problem = "Computer names use letters, digits and -.";
    else if (!*password) problem = "Choose a password.";
    else if (!g_str_equal(password, again)) problem = "The passwords do not match.";
    if (problem) {
        gtk_label_set_text(GTK_LABEL(app->account_note), problem);
        return;
    }
    gtk_label_set_text(GTK_LABEL(app->account_note), "");
    char *text = g_strdup_printf("Meldra will be installed on:\n\n    %s (%s)\n\nAccount: %s    Computer name: %s\n\n"
                                 "ALL DATA ON THIS DISK WILL BE ERASED.",
                                 chosen_device(app, "label"), chosen_device(app, "device"), user, host);
    gtk_label_set_text(GTK_LABEL(app->summary), text);
    g_free(text);
    gtk_check_button_set_active(GTK_CHECK_BUTTON(app->erase_check), FALSE);
    go(app, "confirm");
}

static void to_account_back(GtkButton *b, gpointer data) { (void)b; go(data, "account"); }
static void to_disks_back(GtkButton *b, gpointer data) { (void)b; go(data, "disks"); }

static void erase_toggled(GtkCheckButton *check, gpointer data) {
    gtk_widget_set_sensitive(((Installer *)data)->install_button, gtk_check_button_get_active(check));
}

static void forget_password(Installer *app) {
    if (app->password_file) {
        g_unlink(app->password_file);
        g_clear_pointer(&app->password_file, g_free);
    }
}

static void set_heading(GtkWidget *label, const char *text) {
    char *markup = g_markup_printf_escaped("<span size='x-large' weight='bold'>%s</span>", text);
    gtk_label_set_markup(GTK_LABEL(label), markup);
    g_free(markup);
}

static void finished(Installer *app, gboolean ok, const char *why) {
    forget_password(app);
    gtk_widget_set_visible(app->try_again, !ok);
    if (ok) {
        set_heading(app->done_title, "Meldra is installed");
        gtk_label_set_text(GTK_LABEL(app->done_text),
                           "Restart the computer and remove the installation media. Secure Boot must be turned off "
                           "in the firmware settings for Meldra to start.");
        gtk_widget_set_visible(app->restart, TRUE);
    } else {
        set_heading(app->done_title, "Installation failed");
        gtk_label_set_text(GTK_LABEL(app->done_text), why && *why ? why : "The installer stopped with an error.");
        gtk_widget_set_visible(app->restart, FALSE);
    }
    go(app, "done");
}

static void read_line(GObject *source, GAsyncResult *result, gpointer data);

static void wait_done(GObject *source, GAsyncResult *result, gpointer data) {
    Installer *app = data;
    GError *error = NULL;
    gboolean ok = g_subprocess_wait_check_finish(G_SUBPROCESS(source), result, &error);
    if (!ok && error && g_error_matches(error, G_SPAWN_EXIT_ERROR, 126))
        finished(app, FALSE, "Permission to install was not granted.");
    else
        finished(app, ok, app->errors->str);
    g_clear_error(&error);
}

static void read_line(GObject *source, GAsyncResult *result, gpointer data) {
    Installer *app = data;
    char *line = g_data_input_stream_read_line_finish(G_DATA_INPUT_STREAM(source), result, NULL, NULL);
    if (!line) return;
    int step = 0, total = 0;
    const char *at = strstr(line, "step ");
    if (at && sscanf(at, "step %d/%d", &step, &total) == 2 && total > 0) {
        gtk_progress_bar_set_fraction(GTK_PROGRESS_BAR(app->progress), (double)(step - 1) / total);
        const char *colon = strchr(at, ':');
        gtk_label_set_text(GTK_LABEL(app->progress_label), colon ? colon + 2 : line);
    } else if (strstr(line, "meldra-install: done")) {
        gtk_progress_bar_set_fraction(GTK_PROGRESS_BAR(app->progress), 1.0);
    }
    g_free(line);
    g_data_input_stream_read_line_async(G_DATA_INPUT_STREAM(source), G_PRIORITY_DEFAULT, NULL, read_line, app);
}

static void read_errors(GObject *source, GAsyncResult *result, gpointer data) {
    Installer *app = data;
    char *line = g_data_input_stream_read_line_finish(G_DATA_INPUT_STREAM(source), result, NULL, NULL);
    if (!line) return;
    const char *message = g_str_has_prefix(line, "meldra-install: error: ") ? line + 23 : line;
    if (app->errors->len) g_string_append_c(app->errors, '\n');
    g_string_append(app->errors, message);
    g_free(line);
    g_data_input_stream_read_line_async(G_DATA_INPUT_STREAM(source), G_PRIORITY_DEFAULT, NULL, read_errors, app);
}

static void install(GtkButton *b, gpointer data) {
    (void)b;
    Installer *app = data;
    GError *error = NULL;
    const char *runtime = g_get_user_runtime_dir();
    char *path = g_build_filename(runtime, "meldra-install-XXXXXX", NULL);
    int fd = g_mkstemp_full(path, O_WRONLY, 0600);
    if (fd < 0) {
        g_free(path);
        finished(app, FALSE, "Could not prepare the installation.");
        return;
    }
    const char *password = gtk_editable_get_text(GTK_EDITABLE(app->password));
    gboolean written = write(fd, password, strlen(password)) == (ssize_t)strlen(password) && write(fd, "\n", 1) == 1;
    close(fd);
    app->password_file = path;
    gtk_editable_set_text(GTK_EDITABLE(app->password), "");
    gtk_editable_set_text(GTK_EDITABLE(app->again), "");
    if (!written) {
        finished(app, FALSE, "Could not prepare the installation.");
        return;
    }
    g_string_truncate(app->errors, 0);
    gtk_progress_bar_set_fraction(GTK_PROGRESS_BAR(app->progress), 0);
    gtk_label_set_text(GTK_LABEL(app->progress_label), "Waiting for permission");
    go(app, "progress");
    GSubprocess *process = g_subprocess_new(
        G_SUBPROCESS_FLAGS_STDOUT_PIPE | G_SUBPROCESS_FLAGS_STDERR_PIPE, &error, "pkexec", INSTALLER, "--yes",
        "--user", gtk_editable_get_text(GTK_EDITABLE(app->user)),
        "--hostname", gtk_editable_get_text(GTK_EDITABLE(app->hostname)),
        "--password-file", app->password_file, chosen_device(app, "device"), NULL);
    if (!process) {
        finished(app, FALSE, error->message);
        g_clear_error(&error);
        return;
    }
    GDataInputStream *out = g_data_input_stream_new(g_subprocess_get_stdout_pipe(process));
    GDataInputStream *err = g_data_input_stream_new(g_subprocess_get_stderr_pipe(process));
    g_data_input_stream_read_line_async(out, G_PRIORITY_DEFAULT, NULL, read_line, app);
    g_data_input_stream_read_line_async(err, G_PRIORITY_DEFAULT, NULL, read_errors, app);
    g_subprocess_wait_check_async(process, NULL, wait_done, app);
}

static void try_again(GtkButton *b, gpointer data) {
    (void)b;
    // The passwords were cleared when the install started; ask for them again.
    go(data, "account");
}

static void restart(GtkButton *b, gpointer data) {
    (void)b;
    (void)data;
    const char *argv[] = {"/usr/bin/loginctl", "reboot", NULL};
    g_spawn_async(NULL, (char **)argv, NULL, G_SPAWN_DEFAULT, NULL, NULL, NULL, NULL);
}

static GtkWidget *entry(const char *placeholder, gboolean secret) {
    GtkWidget *widget = secret ? gtk_password_entry_new() : gtk_entry_new();
    if (secret) {
        g_object_set(widget, "placeholder-text", placeholder, NULL);
        gtk_password_entry_set_show_peek_icon(GTK_PASSWORD_ENTRY(widget), TRUE);
    } else {
        gtk_entry_set_placeholder_text(GTK_ENTRY(widget), placeholder);
    }
    return widget;
}

static GtkWidget *field_label(const char *text) {
    GtkWidget *label = gtk_label_new(text);
    gtk_label_set_xalign(GTK_LABEL(label), 0);
    gtk_widget_add_css_class(label, "heading");
    return label;
}

static GtkWidget *button(const char *label, gboolean primary, GCallback callback, Installer *app) {
    GtkWidget *widget = gtk_button_new_with_label(label);
    if (primary) gtk_widget_add_css_class(widget, "suggested-action");
    g_signal_connect(widget, "clicked", callback, app);
    return widget;
}

static void activate(GtkApplication *gtk_app, gpointer data) {
    Installer *app = data;
    app->window = gtk_application_window_new(gtk_app);
    gtk_window_set_title(GTK_WINDOW(app->window), "Install Meldra");
    gtk_window_set_default_size(GTK_WINDOW(app->window), 720, 520);
    app->stack = gtk_stack_new();
    gtk_stack_set_transition_type(GTK_STACK(app->stack), GTK_STACK_TRANSITION_TYPE_SLIDE_LEFT_RIGHT);

    GtkWidget *welcome = page("Install Meldra", "This puts Meldra on a disk of this computer, so it starts without the "
                              "installation media and keeps your files. It takes a few minutes.");
    gtk_box_append(GTK_BOX(welcome), buttons(NULL, button("Continue", TRUE, G_CALLBACK(to_disks), app)));
    gtk_stack_add_named(GTK_STACK(app->stack), welcome, "welcome");

    GtkWidget *disks = page("Choose a disk", NULL);
    app->disks = gtk_list_box_new();
    gtk_widget_add_css_class(app->disks, "boxed-list");
    GtkWidget *scroll = gtk_scrolled_window_new();
    gtk_scrolled_window_set_child(GTK_SCROLLED_WINDOW(scroll), app->disks);
    gtk_widget_set_vexpand(scroll, TRUE);
    app->disk_note = gtk_label_new("");
    gtk_label_set_xalign(GTK_LABEL(app->disk_note), 0);
    gtk_box_append(GTK_BOX(disks), scroll);
    gtk_box_append(GTK_BOX(disks), app->disk_note);
    gtk_box_append(GTK_BOX(disks), buttons(button("Back", FALSE, G_CALLBACK(to_welcome), app),
                                           button("Continue", TRUE, G_CALLBACK(to_account), app)));
    gtk_stack_add_named(GTK_STACK(app->stack), disks, "disks");

    GtkWidget *account = page("Your account", "This account can administer the computer.");
    app->user = entry("for example: alex", FALSE);
    app->password = entry("Password", TRUE);
    app->again = entry("Password again", TRUE);
    app->hostname = entry("Computer name", FALSE);
    gtk_editable_set_text(GTK_EDITABLE(app->hostname), "meldra");
    app->account_note = gtk_label_new("");
    gtk_label_set_xalign(GTK_LABEL(app->account_note), 0);
    gtk_box_append(GTK_BOX(account), field_label("User name"));
    gtk_box_append(GTK_BOX(account), app->user);
    gtk_box_append(GTK_BOX(account), field_label("Password"));
    gtk_box_append(GTK_BOX(account), app->password);
    gtk_box_append(GTK_BOX(account), app->again);
    gtk_box_append(GTK_BOX(account), field_label("Computer name (how this computer appears on the network)"));
    gtk_box_append(GTK_BOX(account), app->hostname);
    gtk_box_append(GTK_BOX(account), app->account_note);
    gtk_box_append(GTK_BOX(account), buttons(button("Back", FALSE, G_CALLBACK(to_disks_back), app),
                                             button("Continue", TRUE, G_CALLBACK(to_confirm), app)));
    gtk_stack_add_named(GTK_STACK(app->stack), account, "account");

    GtkWidget *confirm = page("Ready to install", NULL);
    app->summary = gtk_label_new("");
    gtk_label_set_xalign(GTK_LABEL(app->summary), 0);
    gtk_label_set_wrap(GTK_LABEL(app->summary), TRUE);
    app->erase_check = gtk_check_button_new_with_label("I understand that everything on this disk will be erased");
    app->install_button = button("Erase and install", TRUE, G_CALLBACK(install), app);
    gtk_widget_remove_css_class(app->install_button, "suggested-action");
    gtk_widget_add_css_class(app->install_button, "destructive-action");
    gtk_widget_set_sensitive(app->install_button, FALSE);
    g_signal_connect(app->erase_check, "toggled", G_CALLBACK(erase_toggled), app);
    gtk_box_append(GTK_BOX(confirm), app->summary);
    gtk_box_append(GTK_BOX(confirm), app->erase_check);
    gtk_box_append(GTK_BOX(confirm), buttons(button("Back", FALSE, G_CALLBACK(to_account_back), app),
                                             app->install_button));
    gtk_stack_add_named(GTK_STACK(app->stack), confirm, "confirm");

    GtkWidget *progress = page("Installing Meldra", "Do not turn the computer off.");
    app->progress = gtk_progress_bar_new();
    app->progress_label = gtk_label_new("");
    gtk_label_set_xalign(GTK_LABEL(app->progress_label), 0);
    gtk_box_append(GTK_BOX(progress), app->progress);
    gtk_box_append(GTK_BOX(progress), app->progress_label);
    gtk_stack_add_named(GTK_STACK(app->stack), progress, "progress");

    GtkWidget *done = page("", NULL);
    app->done_title = gtk_widget_get_first_child(done);
    app->done_text = gtk_label_new("");
    gtk_label_set_wrap(GTK_LABEL(app->done_text), TRUE);
    gtk_label_set_xalign(GTK_LABEL(app->done_text), 0);
    app->restart = button("Restart now", TRUE, G_CALLBACK(restart), app);
    app->try_again = button("Back", FALSE, G_CALLBACK(try_again), app);
    gtk_box_append(GTK_BOX(done), app->done_text);
    GtkWidget *done_buttons = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 8);
    gtk_box_append(GTK_BOX(done_buttons), app->try_again);
    gtk_box_append(GTK_BOX(done_buttons), app->restart);
    gtk_box_append(GTK_BOX(done), buttons(NULL, done_buttons));
    gtk_stack_add_named(GTK_STACK(app->stack), done, "done");

    gtk_window_set_child(GTK_WINDOW(app->window), app->stack);
    gtk_window_present(GTK_WINDOW(app->window));
}

static void shutdown_app(GApplication *gtk_app, gpointer data) {
    (void)gtk_app;
    forget_password(data);
}

int main(int argc, char **argv) {
    Installer app = {.errors = g_string_new(NULL)};
    GtkApplication *gtk_app = gtk_application_new("org.meldra.Installer", G_APPLICATION_DEFAULT_FLAGS);
    g_signal_connect(gtk_app, "activate", G_CALLBACK(activate), &app);
    g_signal_connect(gtk_app, "shutdown", G_CALLBACK(shutdown_app), &app);
    int status = g_application_run(G_APPLICATION(gtk_app), argc, argv);
    g_object_unref(gtk_app);
    g_string_free(app.errors, TRUE);
    return status;
}
