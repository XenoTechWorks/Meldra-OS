// SPDX-License-Identifier: GPL-2.0-only
#include <gtk/gtk.h>

static void save_document(GtkButton *button, gpointer data) {
    (void)button;
    GtkTextBuffer *buffer = gtk_text_view_get_buffer(GTK_TEXT_VIEW(data));
    GtkTextIter start, end;
    gtk_text_buffer_get_bounds(buffer, &start, &end);
    char *text = gtk_text_buffer_get_text(buffer, &start, &end, false);
    char *path = g_build_filename(g_get_home_dir(), "Document.txt", NULL);
    GError *error = NULL;
    if (!g_file_set_contents(path, text, -1, &error)) {
        g_warning("could not save %s: %s", path, error->message);
        g_clear_error(&error);
    }
    g_free(path);
    g_free(text);
}

static GtkWidget *editor(void) {
    GtkWidget *box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 8);
    GtkWidget *label = gtk_label_new("Document.txt — saved in your home folder");
    GtkWidget *view = gtk_text_view_new();
    GtkWidget *scroll = gtk_scrolled_window_new();
    GtkWidget *save = gtk_button_new_with_label("Save");
    gtk_scrolled_window_set_child(GTK_SCROLLED_WINDOW(scroll), view);
    gtk_widget_set_vexpand(scroll, true);
    gtk_box_append(GTK_BOX(box), label);
    gtk_box_append(GTK_BOX(box), scroll);
    gtk_box_append(GTK_BOX(box), save);
    g_signal_connect(save, "clicked", G_CALLBACK(save_document), view);
    char *path = g_build_filename(g_get_home_dir(), "Document.txt", NULL);
    char *contents = NULL;
    if (g_file_get_contents(path, &contents, NULL, NULL))
        gtk_text_buffer_set_text(gtk_text_view_get_buffer(GTK_TEXT_VIEW(view)), contents, -1);
    g_free(contents);
    g_free(path);
    return box;
}

static void dark_changed(GtkSwitch *toggle, GParamSpec *spec, gpointer data) {
    (void)spec;
    (void)data;
    g_object_set(gtk_settings_get_default(), "gtk-application-prefer-dark-theme",
                 gtk_switch_get_active(toggle), NULL);
}

static GtkWidget *settings(void) {
    GtkWidget *box = gtk_box_new(GTK_ORIENTATION_VERTICAL, 18);
    GtkWidget *title = gtk_label_new(NULL);
    gtk_label_set_markup(GTK_LABEL(title), "<span size='xx-large' weight='bold'>Meldra Settings</span>");
    GtkWidget *version = gtk_label_new("Meldra OS developer preview\nGNOME Shell 50 · Linux 6.18");
    GtkWidget *row = gtk_box_new(GTK_ORIENTATION_HORIZONTAL, 12);
    GtkWidget *dark = gtk_switch_new();
    gtk_box_append(GTK_BOX(row), gtk_label_new("Prefer dark appearance"));
    gtk_box_append(GTK_BOX(row), dark);
    gtk_box_append(GTK_BOX(box), title);
    gtk_box_append(GTK_BOX(box), version);
    gtk_box_append(GTK_BOX(box), row);
    g_signal_connect(dark, "notify::active", G_CALLBACK(dark_changed), NULL);
    return box;
}

static void activate(GtkApplication *app, gpointer data) {
    const char *mode = data;
    GtkWidget *window = gtk_application_window_new(app);
    gtk_window_set_default_size(GTK_WINDOW(window), 760, 520);
    if (g_str_equal(mode, "files")) {
        gtk_window_set_title(GTK_WINDOW(window), "Files");
        GtkWidget *chooser = gtk_file_chooser_widget_new(GTK_FILE_CHOOSER_ACTION_OPEN);
        gtk_file_chooser_set_select_multiple(GTK_FILE_CHOOSER(chooser), true);
        gtk_window_set_child(GTK_WINDOW(window), chooser);
    } else if (g_str_equal(mode, "settings")) {
        gtk_window_set_title(GTK_WINDOW(window), "Settings");
        GtkWidget *content = settings();
        gtk_widget_set_margin_top(content, 32);
        gtk_widget_set_margin_bottom(content, 32);
        gtk_widget_set_margin_start(content, 32);
        gtk_widget_set_margin_end(content, 32);
        gtk_window_set_child(GTK_WINDOW(window), content);
    } else {
        gtk_window_set_title(GTK_WINDOW(window), "Text Editor");
        gtk_window_set_child(GTK_WINDOW(window), editor());
    }
    gtk_window_present(GTK_WINDOW(window));
}

int main(int argc, char **argv) {
    const char *name = g_path_get_basename(argv[0]);
    const char *mode = g_str_has_suffix(name, "files") ? "files" :
                       g_str_has_suffix(name, "settings") ? "settings" : "editor";
    const char *id = g_str_equal(mode, "files") ? "org.meldra.Files" :
                     g_str_equal(mode, "settings") ? "org.meldra.Settings" : "org.meldra.TextEditor";
    GtkApplication *app = gtk_application_new(id, G_APPLICATION_DEFAULT_FLAGS);
    g_signal_connect(app, "activate", G_CALLBACK(activate), (gpointer)mode);
    int status = g_application_run(G_APPLICATION(app), argc, argv);
    g_object_unref(app);
    return status;
}
