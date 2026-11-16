/* SPDX-License-Identifier: MIT */
/*
 * The shared microphone, for the programs that used to open /dev/audio themselves (voice-daemon,
 * multimedia-daemon). lapinou-mic is the only opener of /dev/audio when it runs; it serves the same
 * bytes (G.711 mu-law, 16 kHz, mono, its gain applied) on a local socket. A program asks
 * lapinou_mic_connect() first and opens /dev/audio itself only when that returns -1 (no lapinou-mic).
 *
 * A client that reads too slowly is dropped by lapinou-mic: a short read from
 * lapinou_mic_read_full() means the connection is gone and the client must connect again.
 *
 * Header only (static inline), so C and C++ programs can both include it.
 */
#ifndef LAPINOU_MIC_CLIENT_H
#define LAPINOU_MIC_CLIENT_H

#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

#define LAPINOU_MIC_SOCKET_PATH "/var/run/lapinou/mic.sock"
/* mu-law silence (a zero sample). */
#define LAPINOU_MIC_SILENCE 0xFF

/* A connected socket to lapinou-mic, or -1 if it isn't there (no socket, nobody listening).
 * $LAPINOU_MIC_SOCKET moves the socket, as for lapinou-mic itself (tests). */
static inline int lapinou_mic_connect(void)
{
    const char *env = getenv("LAPINOU_MIC_SOCKET");
    const char *path = env != NULL && *env != '\0' ? env : LAPINOU_MIC_SOCKET_PATH;
    struct sockaddr_un addr;
    int fd;

    /* No socket file: no system call beyond this one, so a rabbit without lapinou-mic behaves as it did. */
    if (access(path, F_OK) != 0 || strlen(path) >= sizeof addr.sun_path)
        return -1;
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    strcpy(addr.sun_path, path);
    fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0)
        return -1;
    if (connect(fd, (struct sockaddr *)&addr, sizeof addr) != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

/* Read len bytes from the socket, waiting for them. Fewer than len (possibly 0) only when the
 * connection has ended or failed. */
static inline size_t lapinou_mic_read_full(int fd, void *buf, size_t len)
{
    size_t got = 0;

    while (got < len) {
        ssize_t n = read(fd, (char *)buf + got, len - got);

        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            break;
        got += (size_t)n;
    }
    return got;
}

#endif
