// SPDX-License-Identifier: GPL-2.0-only
//
// meldra-session: open a logind session for one user through PAM, then run a command as that user.
//
//     meldra-session USER TTY COMMAND [ARGUMENT...]
//
// Mutter's native backend needs a logind session on a seat. elogind creates one from its PAM module,
// so this program is the smallest thing that can ask PAM for a session: it authenticates through the
// "meldra-session" PAM service (which permits the login, since the caller is already root), opens the
// session, drops privileges, runs COMMAND, and closes the session when COMMAND exits.
//
// It must be started as root. COMMAND runs with the user's uid, gid and supplementary groups and a
// small, fixed environment plus whatever PAM added (XDG_RUNTIME_DIR, XDG_SESSION_ID, ...). Anything
// else the command needs can be passed with env(1): `meldra-session meldra tty1 env A=b gnome-shell`.

#define _GNU_SOURCE
#include <errno.h>
#include <grp.h>
#include <pwd.h>
#include <security/pam_appl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define SESSION_PAM_SERVICE "meldra-session"

static volatile sig_atomic_t child_pid = 0;

static void forward_signal(int number) {
    if (child_pid > 0) kill((pid_t)child_pid, number);
}

// The PAM stack asks for nothing, so answer any prompt with an empty response rather than hanging.
static int conversation(int count, const struct pam_message **messages, struct pam_response **responses,
                        void *data) {
    (void)messages;
    (void)data;
    if (count <= 0) return PAM_CONV_ERR;
    *responses = calloc((size_t)count, sizeof(**responses));
    return *responses ? PAM_SUCCESS : PAM_BUF_ERR;
}

static int fail_pam(pam_handle_t *handle, const char *step, int result) {
    fprintf(stderr, "meldra-session: %s: %s\n", step, pam_strerror(handle, result));
    return 1;
}

static int put_environment(pam_handle_t *handle, const char *assignment) {
    int result = pam_putenv(handle, assignment);
    if (result != PAM_SUCCESS) return fail_pam(handle, assignment, result);
    return 0;
}

// "tty3" -> 3. Returns 0 when TTY has no trailing number (no VT).
static unsigned vt_number(const char *tty) {
    size_t length = strlen(tty);
    size_t start = length;
    while (start > 0 && tty[start - 1] >= '0' && tty[start - 1] <= '9') start--;
    return start < length ? (unsigned)strtoul(tty + start, NULL, 10) : 0;
}

static void run_command(const struct passwd *user, char **pam_environment, char **command) {
    setsid();
    // Drop supplementary groups, then gid, then uid, and verify each: a failure here must never fall
    // through to running the command as root.
    if (initgroups(user->pw_name, user->pw_gid) < 0) {
        perror("meldra-session: initgroups");
        _exit(126);
    }
    if (setgid(user->pw_gid) < 0 || setuid(user->pw_uid) < 0) {
        perror("meldra-session: dropping privileges");
        _exit(126);
    }
    if (setuid(0) == 0) {
        fprintf(stderr, "meldra-session: privileges could be regained; refusing to continue\n");
        _exit(126);
    }
    if (chdir(user->pw_dir) < 0 && chdir("/") < 0) _exit(126);

    clearenv();
    setenv("HOME", user->pw_dir, 1);
    setenv("USER", user->pw_name, 1);
    setenv("LOGNAME", user->pw_name, 1);
    setenv("SHELL", user->pw_shell[0] ? user->pw_shell : "/bin/sh", 1);
    setenv("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", 1);
    setenv("XDG_DATA_DIRS", "/usr/local/share:/usr/share", 1);
    setenv("XDG_CONFIG_DIRS", "/etc/xdg", 1);
    setenv("LANG", "C", 1);
    for (char **entry = pam_environment; entry && *entry; entry++) putenv(*entry);

    execvp(command[0], command);
    fprintf(stderr, "meldra-session: cannot run %s: %s\n", command[0], strerror(errno));
    _exit(127);
}

int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: meldra-session USER TTY COMMAND [ARGUMENT...]\n");
        return 2;
    }
    if (geteuid() != 0) {
        fprintf(stderr, "meldra-session: must be run as root\n");
        return 2;
    }
    const char *name = argv[1];
    const char *tty = argv[2];
    struct passwd *user = getpwnam(name);
    if (!user) {
        fprintf(stderr, "meldra-session: no such user: %s\n", name);
        return 2;
    }
    if (user->pw_uid == 0) {
        fprintf(stderr, "meldra-session: refusing to open a graphical session for root\n");
        return 2;
    }

    struct pam_conv conv = {conversation, NULL};
    pam_handle_t *handle = NULL;
    int result = pam_start(SESSION_PAM_SERVICE, name, &conv, &handle);
    if (result != PAM_SUCCESS) return fail_pam(handle, "pam_start", result);

    char assignment[64];
    unsigned vt = vt_number(tty);
    int status = 1;
    if ((result = pam_set_item(handle, PAM_TTY, tty)) != PAM_SUCCESS) {
        status = fail_pam(handle, "pam_set_item(PAM_TTY)", result);
        goto finish;
    }
    if (put_environment(handle, "XDG_SESSION_TYPE=wayland") || put_environment(handle, "XDG_SESSION_CLASS=user") ||
        put_environment(handle, "XDG_SESSION_DESKTOP=gnome") || put_environment(handle, "XDG_SEAT=seat0")) {
        goto finish;
    }
    if (vt) {
        snprintf(assignment, sizeof(assignment), "XDG_VTNR=%u", vt);
        if (put_environment(handle, assignment)) goto finish;
    }
    if ((result = pam_authenticate(handle, 0)) != PAM_SUCCESS) {
        status = fail_pam(handle, "pam_authenticate", result);
        goto finish;
    }
    if ((result = pam_acct_mgmt(handle, 0)) != PAM_SUCCESS) {
        status = fail_pam(handle, "pam_acct_mgmt", result);
        goto finish;
    }
    if ((result = pam_setcred(handle, PAM_ESTABLISH_CRED)) != PAM_SUCCESS) {
        status = fail_pam(handle, "pam_setcred", result);
        goto finish;
    }
    if ((result = pam_open_session(handle, 0)) != PAM_SUCCESS) {
        status = fail_pam(handle, "pam_open_session", result);
        pam_setcred(handle, PAM_DELETE_CRED);
        goto finish;
    }

    char **pam_environment = pam_getenvlist(handle);
    struct sigaction action = {0};
    action.sa_handler = forward_signal;
    sigemptyset(&action.sa_mask);
    sigaction(SIGTERM, &action, NULL);
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGHUP, &action, NULL);

    pid_t pid = fork();
    if (pid < 0) {
        perror("meldra-session: fork");
    } else if (pid == 0) {
        run_command(user, pam_environment, &argv[3]);
    } else {
        child_pid = pid;
        int wait_status = 0;
        while (waitpid(pid, &wait_status, 0) < 0 && errno == EINTR) {
        }
        status = WIFEXITED(wait_status) ? WEXITSTATUS(wait_status) : 128 + WTERMSIG(wait_status);
    }

    pam_close_session(handle, 0);
    pam_setcred(handle, PAM_DELETE_CRED);
    if (pam_environment) {
        for (char **entry = pam_environment; *entry; entry++) free(*entry);
        free(pam_environment);
    }

finish:
    pam_end(handle, result);
    return status;
}
