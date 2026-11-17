/* SPDX-License-Identifier: MIT */
/*
 * lapinou-webroot: the LapiNou web page's one way to root. The page's programs run as the user web,
 * but LapiNou's settings and secrets are root's (lapinou.secrets is 0600), and so are the daemons
 * and the updates. This program is installed setuid root, mode 4750, group web: only root and the
 * group web may run it. It checks its caller and its arguments, then runs one fixed script,
 * /usr/share/lapinou/webroot.sh, as root with a clean environment, the same standard input
 * (where every value goes: never in an argument) and no other open file.
 *
 *   lapinou-webroot COMMAND [WORD [WORD]]
 *
 * The words are 1 to 32 characters among a-z, 0-9, _ and -. The script checks the command and the
 * words again (docs/lapinou/page-web.md). Status 126 when refused here.
 *
 * Built with -DLAPINOU_WEBROOT_TEST (tests only), it neither checks the group nor becomes root,
 * and runs the script named by LAPINOU_WEBROOT_SCRIPT, so a test can check what it passes on.
 */
#include <errno.h>
#include <grp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define SCRIPT    "/usr/share/lapinou/webroot.sh"
#define SHELL     "/bin/sh"
#define WEB_GROUP "web"
#define WORD_MAX  32
#define ARGS_MAX  3

static int refuse(const char *why)
{
    fprintf(stderr, "lapinou-webroot: %s\n", why);
    return 126;
}

static int word_ok(const char *w)
{
    size_t n = strlen(w), i;

    if (n == 0 || n > WORD_MAX)
        return 0;
    for (i = 0; i < n; i++) {
        char c = w[i];
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '-'))
            return 0;
    }
    return 1;
}

int main(int argc, char **argv)
{
    char *args[ARGS_MAX + 3];
    char *env[] = { "PATH=/bin:/sbin:/usr/bin:/usr/sbin", "LC_ALL=C", "HOME=/", NULL };
    const char *script = SCRIPT;
    long fd, max;
    int i;

    if (argc < 2 || argc > ARGS_MAX + 1)
        return refuse("usage: lapinou-webroot COMMAND [WORD [WORD]]");
    for (i = 1; i < argc; i++)
        if (!word_ok(argv[i]))
            return refuse("a word isn't 1 to 32 of a-z, 0-9, _ and -");

#ifndef LAPINOU_WEBROOT_TEST
    {
        struct group *g = getgrnam(WEB_GROUP);

        if (getuid() != 0 && (g == NULL || getgid() != g->gr_gid))
            return refuse("only root and the group web may run this");
    }
    if (setgroups(0, NULL) != 0 || setgid(0) != 0 || setuid(0) != 0)
        return refuse("can't become root");
#else
    if (getenv("LAPINOU_WEBROOT_SCRIPT"))
        script = getenv("LAPINOU_WEBROOT_SCRIPT");
#endif

    /* Nothing but the standard input, output and error goes on to the script. */
    max = sysconf(_SC_OPEN_MAX);
    if (max < 0 || max > 4096)
        max = 4096;
    for (fd = 3; fd < max; fd++)
        close((int)fd);

    args[0] = "sh";
    args[1] = (char *)script;
    for (i = 1; i < argc; i++)
        args[i + 1] = argv[i];
    args[argc + 1] = NULL;
    execve(SHELL, args, env);
    fprintf(stderr, "lapinou-webroot: can't run %s: %s\n", SHELL, strerror(errno));
    return 126;
}
