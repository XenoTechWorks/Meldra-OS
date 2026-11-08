// SPDX-License-Identifier: GPL-2.0-only
#define _DEFAULT_SOURCE
#define _POSIX_C_SOURCE 200809L

#include <dirent.h>
#include <errno.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#define MAX_SERVICES 64
#define COMMAND_SIZE 512

struct service {
    char name[128];
    char command[COMMAND_SIZE];
    bool respawn;
    pid_t pid;
};

static int compare_services(const void *left, const void *right) {
    const struct service *a = left;
    const struct service *b = right;
    return strcmp(a->name, b->name);
}

static volatile sig_atomic_t reap_requested;
static volatile sig_atomic_t shutdown_requested;

static void handle_signal(int signal_number) {
    if (signal_number == SIGCHLD) reap_requested = 1;
    else shutdown_requested = signal_number;
}

static void install_signals(void) {
    struct sigaction action = {0};
    sigemptyset(&action.sa_mask);
    action.sa_handler = handle_signal;
    action.sa_flags = SA_RESTART;
    sigaction(SIGCHLD, &action, NULL);
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGUSR1, &action, NULL);
    sigaction(SIGUSR2, &action, NULL);
}

static void mount_one(const char *source, const char *target, const char *type,
                      unsigned long flags, const char *data) {
    mkdir(target, 0755);
    if (mount(source, target, type, flags, data) < 0 && errno != EBUSY)
        fprintf(stderr, "hearth: mount %s: %s\n", target, strerror(errno));
}

static void mount_early_filesystems(void) {
    mount_one("proc", "/proc", "proc", 0, NULL);
    mount_one("sysfs", "/sys", "sysfs", 0, NULL);
    mount_one("devtmpfs", "/dev", "devtmpfs", 0, "mode=0755");
    mount_one("tmpfs", "/dev/shm", "tmpfs", MS_NOSUID | MS_NODEV, "mode=1777");
    mount_one("tmpfs", "/run", "tmpfs", MS_NOSUID | MS_NODEV, "mode=0755");
}

static size_t load_services(const char *directory, struct service *services) {
    DIR *dir = opendir(directory);
    if (!dir) {
        fprintf(stderr, "hearth: %s: %s\n", directory, strerror(errno));
        return 0;
    }
    size_t count = 0;
    struct dirent *entry;
    while ((entry = readdir(dir)) && count < MAX_SERVICES) {
        size_t length = strlen(entry->d_name);
        if (length < 9 || strcmp(entry->d_name + length - 8, ".service") != 0) continue;
        char path[512];
        snprintf(path, sizeof(path), "%s/%s", directory, entry->d_name);
        FILE *file = fopen(path, "r");
        if (!file) continue;
        struct service current = {0};
        snprintf(current.name, sizeof(current.name), "%.*s", (int)(length - 8), entry->d_name);
        char line[COMMAND_SIZE + 32];
        while (fgets(line, sizeof(line), file)) {
            line[strcspn(line, "\r\n")] = '\0';
            if (strncmp(line, "command=", 8) == 0)
                snprintf(current.command, sizeof(current.command), "%.*s",
                         (int)sizeof(current.command) - 1, line + 8);
            else if (strcmp(line, "respawn=yes") == 0) current.respawn = true;
        }
        fclose(file);
        if (current.command[0]) services[count++] = current;
    }
    closedir(dir);
    qsort(services, count, sizeof(*services), compare_services);
    return count;
}

static void start_service(struct service *service, bool dry_run) {
    fprintf(stderr, "hearth: start %s: %s\n", service->name, service->command);
    if (dry_run) return;
    pid_t child = fork();
    if (child == 0) {
        execl("/bin/sh", "sh", "-c", service->command, (char *)NULL);
        fprintf(stderr, "hearth: exec %s: %s\n", service->name, strerror(errno));
        _exit(127);
    }
    if (child < 0) fprintf(stderr, "hearth: fork: %s\n", strerror(errno));
    else service->pid = child;
}

static void reap_children(struct service *services, size_t count) {
    int status;
    pid_t child;
    while ((child = waitpid(-1, &status, WNOHANG)) > 0) {
        for (size_t i = 0; i < count; ++i) {
            if (services[i].pid != child) continue;
            services[i].pid = 0;
            fprintf(stderr, "hearth: %s exited (%d)\n", services[i].name, status);
            if (services[i].respawn && !shutdown_requested) start_service(&services[i], false);
            break;
        }
    }
    reap_requested = 0;
}

static void stop_services(struct service *services, size_t count) {
    for (size_t i = 0; i < count; ++i)
        if (services[i].pid > 0) kill(services[i].pid, SIGTERM);
    for (int tries = 0; tries < 30; ++tries) {
        reap_children(services, count);
        bool running = false;
        for (size_t i = 0; i < count; ++i) running |= services[i].pid > 0;
        if (!running) return;
        usleep(100000);
    }
    for (size_t i = 0; i < count; ++i)
        if (services[i].pid > 0) kill(services[i].pid, SIGKILL);
}

int main(int argc, char **argv) {
    bool dry_run = argc == 3 && strcmp(argv[1], "--dry-run") == 0;
    const char *directory = dry_run ? argv[2] : "/etc/meldra/services";
    if (!dry_run && getpid() != 1) {
        fprintf(stderr, "hearth: must run as PID 1 (or use --dry-run DIR)\n");
        return 1;
    }
    struct service services[MAX_SERVICES] = {0};
    if (!dry_run) mount_early_filesystems();
    setenv("PATH", "/usr/bin:/usr/sbin:/bin:/sbin", 0);
    setenv("LD_LIBRARY_PATH", "/usr/lib:/usr/lib64:/lib:/lib64", 0);
    install_signals();
    size_t count = load_services(directory, services);
    for (size_t i = 0; i < count; ++i) start_service(&services[i], dry_run);
    if (dry_run) return count ? 0 : 1;
    while (!shutdown_requested) {
        pause();
        if (reap_requested) reap_children(services, count);
    }
    stop_services(services, count);
    sync();
    int command = shutdown_requested == SIGUSR1 ? RB_POWER_OFF : RB_AUTOBOOT;
    reboot(command);
    return 0;
}
