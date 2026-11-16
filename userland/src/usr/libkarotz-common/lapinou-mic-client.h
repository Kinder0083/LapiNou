/* SPDX-License-Identifier: MIT */
/*
 * The shared microphone, for the programs that used to open /dev/audio themselves (voice-daemon,
 * multimedia-daemon). lapinou-mic is the only opener of /dev/audio when it runs; it serves the same
 * bytes (G.711 mu-law, 16 kHz, mono, its gain applied) on a local socket.
 *
 * A program calls lapinou_mic_connect():
 *  - LAPINOU_MIC_ABSENT (-1): there is no socket file, no lapinou-mic on this rabbit: open /dev/audio
 *    as before;
 *  - LAPINOU_MIC_REFUSED (-2): the socket file is there but nobody answers (lapinou-mic is down or
 *    restarting): do NOT take /dev/audio (lapinou-mic will want it back); try again later, or give up;
 *  - else a connected descriptor, with a 1 s receive timeout (a stuck lapinou-mic can't freeze the
 *    client's main loop) and close-on-exec.
 *
 * A client that reads too slowly is dropped by lapinou-mic: lapinou_mic_read_full() then reports the
 * connection lost, and the client connects again.
 *
 * Header only (static inline), so C and C++ programs can both include it.
 */
#ifndef LAPINOU_MIC_CLIENT_H
#define LAPINOU_MIC_CLIENT_H

#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

#define LAPINOU_MIC_SOCKET_PATH "/var/run/lapinou/mic.sock"
#define LAPINOU_MIC_ABSENT (-1)
#define LAPINOU_MIC_REFUSED (-2)
/* mu-law silence (a zero sample). */
#define LAPINOU_MIC_SILENCE 0xFF
/* Reconnecting: how often and how many times before giving up. */
#define LAPINOU_MIC_RETRY_MS 200
#define LAPINOU_MIC_RETRIES 100

/* $LAPINOU_MIC_SOCKET moves the socket, as for lapinou-mic itself (tests). */
static inline int lapinou_mic_connect(void)
{
    const char *env = getenv("LAPINOU_MIC_SOCKET");
    const char *path = env != NULL && *env != '\0' ? env : LAPINOU_MIC_SOCKET_PATH;
    struct sockaddr_un addr;
    struct timeval timeout;
    int fd;

    /* No socket file: no system call beyond this one, so a rabbit without lapinou-mic behaves as it did. */
    if (access(path, F_OK) != 0)
        return LAPINOU_MIC_ABSENT;
    if (strlen(path) >= sizeof addr.sun_path)
        return LAPINOU_MIC_REFUSED;
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    strcpy(addr.sun_path, path);
    fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0)
        return LAPINOU_MIC_REFUSED;
    fcntl(fd, F_SETFD, FD_CLOEXEC);
    timeout.tv_sec = 1;
    timeout.tv_usec = 0;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout);
    if (connect(fd, (struct sockaddr *)&addr, sizeof addr) != 0) {
        close(fd);
        return LAPINOU_MIC_REFUSED;
    }
    return fd;
}

/* Read len bytes from the socket, waiting for them. Returns how many came. Fewer than len means
 * either 1 s without a byte (*lost is 0: lapinou-mic is slow, the connection is kept) or the
 * connection has ended or failed (*lost is 1: connect again). */
static inline size_t lapinou_mic_read_full(int fd, void *buf, size_t len, int *lost)
{
    size_t got = 0;

    *lost = 0;
    while (got < len) {
        ssize_t n = read(fd, (char *)buf + got, len - got);

        if (n < 0 && errno == EINTR)
            continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
            break;
        if (n <= 0) {
            *lost = 1;
            break;
        }
        got += (size_t)n;
    }
    return got;
}

#endif
