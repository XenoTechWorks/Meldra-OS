// SPDX-License-Identifier: GPL-2.0-only
#define _DEFAULT_SOURCE
#define _POSIX_C_SOURCE 200809L

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define MAX_SERVICES 64
#define COMMAND_SIZE 512
#define MAX_CONSOLES 4
// A respawning service that stays up this long counts as started.
#define SETTLE_SECONDS 2
// Rapid failures in a row before Hearth stops restarting a service.
#define MAX_RAPID_FAILURES 5

struct service {
    char name[128];
    char description[128];
    char command[COMMAND_SIZE];
    bool respawn;
    pid_t pid;
    double started_at;
    bool reported;  // the current run has printed its start status
    bool gave_up;
    int rapid_failures;
};

enum status { STATUS_OK, STATUS_WARN, STATUS_FAILED, STATUS_INFO };

static int console_fds[MAX_CONSOLES];
static size_t console_count;
static bool use_color;

static double now_seconds(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

// Status lines go to every active console (e.g. both tty0 and ttyS0), because
// /dev/console is only the last console= on the kernel command line.
static void open_consoles(bool dry_run) {
    if (!dry_run) {
        FILE *active = fopen("/sys/class/tty/console/active", "r");
        char names[256] = "";
        if (active) {
            if (!fgets(names, sizeof(names), active)) names[0] = '\0';
            fclose(active);
        }
        for (char *name = strtok(names, " \n"); name && console_count < MAX_CONSOLES;
             name = strtok(NULL, " \n")) {
            char path[64];
            snprintf(path, sizeof(path), "/dev/%s", name);
            int fd = open(path, O_WRONLY | O_NOCTTY | O_CLOEXEC);
            if (fd >= 0) console_fds[console_count++] = fd;
        }
    }
    if (console_count == 0) console_fds[console_count++] = STDERR_FILENO;
    const char *term = getenv("TERM");
    use_color = !getenv("NO_COLOR") && !(term && strcmp(term, "dumb") == 0) &&
                (console_fds[0] != STDERR_FILENO || isatty(STDERR_FILENO));
}

static void status_line(enum status status, const char *format, ...) {
    static const char *const plain[] = {"[  OK  ]", "[ WARN ]", "[FAILED]", "[ INFO ]"};
    static const char *const colored[] = {
        "[\033[0;32m  OK  \033[0m]", "[\033[0;33m WARN \033[0m]",
        "[\033[0;1;31mFAILED\033[0m]", "[\033[0;36m INFO \033[0m]"};
    char message[384];
    va_list args;
    va_start(args, format);
    vsnprintf(message, sizeof(message), format, args);
    va_end(args);
    char line[512];
    int length = snprintf(line, sizeof(line), "%s %s\r\n",
                          use_color ? colored[status] : plain[status], message);
    if (length < 0) return;
    if ((size_t)length >= sizeof(line)) length = sizeof(line) - 1;
    for (size_t i = 0; i < console_count; ++i) {
        ssize_t ignored = write(console_fds[i], line, (size_t)length);
        (void)ignored;
    }
}

static int compare_services(const void *left, const void *right) {
    const struct service *a = left;
    const struct service *b = right;
    return strcmp(a->name, b->name);
}

static volatile sig_atomic_t reap_requested;
static volatile sig_atomic_t tick_requested;
static volatile sig_atomic_t shutdown_requested;

static void handle_signal(int signal_number) {
    if (signal_number == SIGCHLD) reap_requested = 1;
    else if (signal_number == SIGALRM) tick_requested = 1;
    else shutdown_requested = signal_number;
}

static void install_signals(void) {
    struct sigaction action = {0};
    sigemptyset(&action.sa_mask);
    action.sa_handler = handle_signal;
    action.sa_flags = SA_RESTART;
    sigaction(SIGCHLD, &action, NULL);
    sigaction(SIGALRM, &action, NULL);
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGUSR1, &action, NULL);
    sigaction(SIGUSR2, &action, NULL);
}

// Consoles are not open yet while these run, so failures are reported afterwards.
static char mount_failures[256];

static void mount_one(const char *source, const char *target, const char *type,
                      unsigned long flags, const char *data) {
    mkdir(target, 0755);
    if (mount(source, target, type, flags, data) < 0 && errno != EBUSY) {
        fprintf(stderr, "hearth: mount %s: %s\n", target, strerror(errno));
        size_t used = strlen(mount_failures);
        snprintf(mount_failures + used, sizeof(mount_failures) - used, "%s%s", used ? ", " : "", target);
    }
}

static void mount_early_filesystems(void) {
    mount_one("proc", "/proc", "proc", 0, NULL);
    mount_one("sysfs", "/sys", "sysfs", 0, NULL);
    mount_one("devtmpfs", "/dev", "devtmpfs", 0, "mode=0755");
    mount_one("tmpfs", "/dev/shm", "tmpfs", MS_NOSUID | MS_NODEV, "mode=1777");
    mount_one("tmpfs", "/run", "tmpfs", MS_NOSUID | MS_NODEV, "mode=0755");
}

// "20-dbus" -> "dbus": the numeric prefix only orders services.
static void default_description(struct service *service) {
    const char *name = service->name;
    while (isdigit((unsigned char)*name)) ++name;
    if (*name == '-' && name != service->name) ++name;
    snprintf(service->description, sizeof(service->description), "%s", *name ? name : service->name);
}

static size_t load_services(const char *directory, struct service *services) {
    DIR *dir = opendir(directory);
    if (!dir) {
        fprintf(stderr, "hearth: %s: %s\n", directory, strerror(errno));
        status_line(STATUS_FAILED, "No services in %s: %s", directory, strerror(errno));
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
            else if (strncmp(line, "description=", 12) == 0)
                snprintf(current.description, sizeof(current.description), "%.*s",
                         (int)sizeof(current.description) - 1, line + 12);
            else if (strcmp(line, "respawn=yes") == 0) current.respawn = true;
        }
        fclose(file);
        if (!current.description[0]) default_description(&current);
        if (current.command[0]) services[count++] = current;
    }
    closedir(dir);
    qsort(services, count, sizeof(*services), compare_services);
    return count;
}

static void start_service(struct service *service, bool dry_run) {
    fprintf(stderr, "hearth: start %s: %s\n", service->name, service->command);
    service->reported = false;
    service->started_at = now_seconds();
    if (dry_run) {
        status_line(STATUS_OK, "%s %s", service->respawn ? "Started" : "Finished", service->description);
        return;
    }
    pid_t child = fork();
    if (child == 0) {
        execl("/bin/sh", "sh", "-c", service->command, (char *)NULL);
        fprintf(stderr, "hearth: exec %s: %s\n", service->name, strerror(errno));
        _exit(127);
    }
    if (child < 0) {
        fprintf(stderr, "hearth: fork: %s\n", strerror(errno));
        status_line(STATUS_FAILED, "Failed to start %s: %s", service->description, strerror(errno));
        service->reported = true;
    } else {
        service->pid = child;
    }
}

static void describe_exit(int status, char *buffer, size_t size) {
    if (WIFEXITED(status)) snprintf(buffer, size, "exit code %d", WEXITSTATUS(status));
    else if (WIFSIGNALED(status)) snprintf(buffer, size, "signal %s", strsignal(WTERMSIG(status)));
    else snprintf(buffer, size, "status %d", status);
}

// Respawning services that have stayed up long enough are reported as started.
static void report_settled(struct service *services, size_t count) {
    double now = now_seconds();
    bool pending = false;
    for (size_t i = 0; i < count; ++i) {
        struct service *service = &services[i];
        if (!service->respawn || service->reported || service->pid <= 0) continue;
        if (now - service->started_at >= SETTLE_SECONDS) {
            service->reported = true;
            service->rapid_failures = 0;
            status_line(STATUS_OK, "Started %s", service->description);
        } else {
            pending = true;
        }
    }
    tick_requested = 0;
    if (pending) alarm(1);
}

static void service_exited(struct service *service, int status) {
    char reason[64];
    describe_exit(status, reason, sizeof(reason));
    bool success = WIFEXITED(status) && WEXITSTATUS(status) == 0;
    fprintf(stderr, "hearth: %s exited (%d)\n", service->name, status);
    if (shutdown_requested) {
        status_line(STATUS_OK, "Stopped %s", service->description);
        return;
    }
    if (!service->respawn) {
        if (success) status_line(STATUS_OK, "Finished %s", service->description);
        else status_line(STATUS_FAILED, "Failed %s (%s)", service->description, reason);
        return;
    }
    bool rapid = now_seconds() - service->started_at < SETTLE_SECONDS;
    service->rapid_failures = rapid ? service->rapid_failures + 1 : 0;
    if (service->rapid_failures >= MAX_RAPID_FAILURES) {
        service->gave_up = true;
        status_line(STATUS_FAILED, "Failed to start %s (%s); gave up after %d tries",
                    service->description, reason, service->rapid_failures);
        return;
    }
    if (success)
        status_line(STATUS_WARN, "%s exited; restarting", service->description);
    else if (!service->reported)
        status_line(STATUS_FAILED, "Failed to start %s (%s); retrying", service->description, reason);
    else
        status_line(STATUS_WARN, "%s stopped (%s); restarting", service->description, reason);
    if (rapid) sleep(1);  // keep a crash loop from flooding the console
    start_service(service, false);
    alarm(1);
}

static void reap_children(struct service *services, size_t count) {
    int status;
    pid_t child;
    reap_requested = 0;
    while ((child = waitpid(-1, &status, WNOHANG)) > 0) {
        for (size_t i = 0; i < count; ++i) {
            if (services[i].pid != child) continue;
            services[i].pid = 0;
            service_exited(&services[i], status);
            break;
        }
    }
}

static void stop_services(struct service *services, size_t count) {
    status_line(STATUS_INFO, "Stopping services");
    for (size_t i = 0; i < count; ++i)
        if (services[i].pid > 0) kill(services[i].pid, SIGTERM);
    for (int tries = 0; tries < 30; ++tries) {
        reap_children(services, count);
        bool running = false;
        for (size_t i = 0; i < count; ++i) running |= services[i].pid > 0;
        if (!running) return;
        usleep(100000);
    }
    for (size_t i = 0; i < count; ++i) {
        if (services[i].pid <= 0) continue;
        kill(services[i].pid, SIGKILL);
        status_line(STATUS_WARN, "Killed %s (did not stop in time)", services[i].description);
    }
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
    open_consoles(dry_run);
    status_line(STATUS_INFO, "Hearth is starting Meldra OS");
    if (!dry_run) {
        if (mount_failures[0]) status_line(STATUS_FAILED, "Failed to mount %s", mount_failures);
        else status_line(STATUS_OK, "Mounted early filesystems");
    }
    setenv("PATH", "/usr/bin:/usr/sbin:/bin:/sbin", 0);
    setenv("LD_LIBRARY_PATH", "/usr/lib:/usr/lib64:/lib:/lib64", 0);
    install_signals();
    size_t count = load_services(directory, services);
    for (size_t i = 0; i < count; ++i) start_service(&services[i], dry_run);
    if (dry_run) return count ? 0 : 1;
    alarm(1);
    while (!shutdown_requested) {
        pause();
        if (reap_requested) reap_children(services, count);
        if (tick_requested) report_settled(services, count);
    }
    alarm(0);
    stop_services(services, count);
    sync();
    status_line(STATUS_OK, "Synced filesystems");
    // busybox's halt sends SIGUSR1 and poweroff SIGUSR2 (both power the machine off); reboot sends SIGTERM.
    int command = (shutdown_requested == SIGUSR1 || shutdown_requested == SIGUSR2) ? RB_POWER_OFF : RB_AUTOBOOT;
    status_line(STATUS_INFO, command == RB_POWER_OFF ? "Powering off" : "Rebooting");
    reboot(command);
    return 0;
}
