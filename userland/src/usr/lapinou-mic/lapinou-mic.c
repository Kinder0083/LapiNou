/* SPDX-License-Identifier: MIT */
/*
 * lapinou-mic: the rabbit's microphone, read all the time, sent to the LapiNou server and shared
 * with the rabbit's other programs, as com.mindscape.karotz.LapinouMic
 * (/com/mindscape/karotz/LapinouMic, interface com.mindscape.karotz.LapinouMic).
 *
 * The only program that opens /dev/audio for reading: OSS, G.711 mu-law, mono, 16 kHz, read in
 * blocks of at most 2048 bytes (one byte per sample). Each block gets the software gain
 * ([audio] mic_gain_db of lapinou.conf, +12 dB by default, between -20 and +30): every mu-law byte
 * is decoded, scaled (a factor in Q12, so no floating point per sample), clipped and encoded
 * again, through a 256-byte table made at start-up. Then the block goes, as it is, to:
 *
 *  - every client of the local socket /var/run/lapinou/mic.sock (AF_UNIX stream): each receives
 *    the same bytes from its connection on (voice-daemon, multimedia-daemon, lapinou-cloud). A
 *    client that doesn't keep up (its socket full) is disconnected;
 *  - the LapiNou server, TCP <[server] host>:9300, in frames of docs/lapinou/protocole-audio.md:
 *    a 4-byte big-endian length, then a LapinouAudio (userland/proto/lapinou-audio.proto),
 *    encoded here by hand (varints and length-delimited fields: protobuf's wire format, which is
 *    all these three messages need). Every frame has the next seq; an audio frame has mulaw and
 *    first_sample, the index of its first sample since the daemon started. The first frame of a
 *    connection also carries the Hello (field 6): rabbit_id (/usr/karotz/uuid.conf),
 *    firmware_version (/karotz/etc/lapinlibre-build's first line) and sample_rate 16000. It
 *    rides on the oldest frame waiting, or is a frame of its own when none is.
 *
 * Frames wait in a queue that holds at most 2 s of audio (32000 samples): when the server can't
 * take them (not connected, or too slow), the oldest audio frames are dropped, which the server
 * sees as a jump in seq. A connection that fails or ends is tried again after 2 s, 5 times, then
 * every 10 s; a connection attempt that takes more than 5 s, or a connection that takes nothing
 * for 10 s while frames wait, is given up (so a Wi-Fi drop is noticed without TCP's own
 * timeouts).
 *
 * D-Bus: mark_playback(s playback_id, b start) queues a PlaybackMark at the number of samples
 * captured so far (the start or the end of a sound the rabbit plays, for the server's echo
 * cancellation); set_streaming(b on) stops or resumes the stream to the server (the local socket
 * goes on either way; nothing is kept for the server while it is off); signal stream_state(s)
 * says "connected" or "disconnected" when the connection to the server comes or goes.
 *
 * With no [server] host, nothing is sent: the microphone is only shared locally. For the tests,
 * LAPINOU_UUID_FILE and LAPINOU_MIC_SOCKET move uuid.conf and the local socket (LAPINOU_CONF_DIR
 * moves lapinou.conf, lapinou_conf.h). A server host that is a name, not an address, is resolved
 * with a blocking getaddrinfo() at each attempt.
 */
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <netdb.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/soundcard.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

#include <dbus/dbus-glib-bindings.h>
#include <dbus/dbus-glib.h>
#include <glib-object.h>

#include "karotz-common.h"
#include "karotz-messages.h"
#include "lapinou_conf.h"

#define AUDIO_DEVICE   "/dev/audio"
#define SOCKET_PATH    "/var/run/lapinou/mic.sock"
#define UUID_FILE      "/usr/karotz/uuid.conf"
#define BUILD_FILE     "/karotz/etc/lapinlibre-build"
#define SERVER_PORT    "9300"
#define SAMPLE_RATE    16000
#define BLOCK          2048              /* bytes (samples) per read */
#define QUEUE_SAMPLES  (2 * SAMPLE_RATE) /* audio kept for the server: 2 s */
#define QUEUE_MARKS    64                /* playback marks kept */
#define MAX_PLAYBACK_ID 256
#define RETRY_FAST_MS  2000
#define RETRY_SLOW_MS  10000
#define FAST_RETRIES   5
#define CONNECT_TIMEOUT_US (5 * G_USEC_PER_SEC)
#define STALL_US       (10 * G_USEC_PER_SEC)
#define AUDIO_RETRY_MS 1000
#define GAIN_DEFAULT   "12"
#define GAIN_MIN_DB    (-20.0)
#define GAIN_MAX_DB    30.0

enum { SIGNAL_STREAM_STATE, N_SIGNALS };
enum { SERVER_DOWN, SERVER_CONNECTING, SERVER_UP };

/* A frame waiting for the server: audio (mulaw set), a playback mark (mark_id set), or neither
 * (a frame for the Hello alone). */
typedef struct {
    guint32 seq;
    guint64 first_sample;
    GByteArray *mulaw;
    char *mark_id;
    guint64 mark_at;
    gboolean mark_start;
} Frame;

typedef struct {
    int fd;
    guint watch;
} Client;

typedef struct {
    GObject parent;
    DBusGConnection *bus;
    DBusGProxy *bus_proxy;

    char *host;              /* [server] host, NULL if none */
    char *rabbit_id;
    char *firmware;
    guint8 gain[256];
    gboolean gain_on;

    int audio_fd;
    guint audio_watch, audio_retry;
    guint64 samples;         /* samples captured so far */

    gboolean streaming;
    guint32 next_seq;
    GQueue *frames;
    gsize queued_samples, queued_marks;
    int server_fd;
    int server_state;
    guint server_watch, retry_timer, tick;
    GIOCondition watch_condition;
    int failures;            /* connection attempts failed since the last connection */
    gboolean hello_sent;
    GByteArray *out;         /* the head frame, encoded, being sent */
    gsize out_pos;
    gint64 deadline;         /* end of the connection attempt */
    gint64 last_progress;    /* last time the server took bytes */

    int listen_fd;
    guint listen_watch;
    GSList *clients;
} LapinouMicObject;

typedef struct {
    GObjectClass parent;
    guint signals[N_SIGNALS];
} LapinouMicObjectClass;

GType lapinou_mic_object_get_type(void);
#define LAPINOU_MIC_TYPE_OBJECT (lapinou_mic_object_get_type())

G_DEFINE_TYPE(LapinouMicObject, lapinou_mic_object, G_TYPE_OBJECT)

gboolean lapinou_mic_object_mark_playback(LapinouMicObject *self, const char *playback_id, gboolean start,
                                          GError **error);
gboolean lapinou_mic_object_set_streaming(LapinouMicObject *self, gboolean on, GError **error);

#include "lapinou-mic-glue.h"

static void server_pump(LapinouMicObject *self);
static void server_try(LapinouMicObject *self);

/* ---- G.711 mu-law and the gain ----------------------------------------- */

static int mulaw_decode(guint8 b)
{
    int u = ~b & 0xFF;
    int t = (((u & 0x0F) << 3) + 0x84) << ((u >> 4) & 7);
    return (u & 0x80) ? 0x84 - t : t - 0x84;
}

static guint8 mulaw_encode(int s)
{
    int sign = 0, exp = 7;

    if (s < 0) {
        sign = 0x80;
        s = -s;
    }
    if (s > 32635)
        s = 32635;
    s += 0x84;
    while (exp > 0 && !(s & (0x80 << exp)))
        exp--;
    return ~(sign | (exp << 4) | ((s >> (exp + 3)) & 0x0F)) & 0xFF;
}

/* The gain table for [audio] mic_gain_db; no table (gain_on FALSE) for 0 dB. */
static void gain_setup(LapinouMicObject *self)
{
    char *text = lapinou_conf_get("audio", "mic_gain_db", GAIN_DEFAULT);
    char *end;
    double db = g_ascii_strtod(text, &end);
    gint64 factor;
    int i;

    if (end == text || *end != '\0' || !isfinite(db)) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "mic_gain_db '%s' is not a number: %s dB", text, GAIN_DEFAULT);
        db = g_ascii_strtod(GAIN_DEFAULT, NULL);
    }
    db = CLAMP(db, GAIN_MIN_DB, GAIN_MAX_DB);
    g_free(text);
    factor = (gint64)(pow(10.0, db / 20.0) * 4096.0 + 0.5);
    self->gain_on = factor != 4096;
    for (i = 0; i < 256; i++) {
        gint64 v = ((gint64)mulaw_decode(i) * factor) >> 12;
        self->gain[i] = mulaw_encode((int)CLAMP(v, -32768, 32767));
    }
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "microphone gain %+.1f dB", db);
}

/* ---- protobuf wire format ------------------------------------------------ */

static void put_varint(GByteArray *b, guint64 v)
{
    guint8 c;

    while (v >= 0x80) {
        c = (v & 0x7F) | 0x80;
        g_byte_array_append(b, &c, 1);
        v >>= 7;
    }
    c = v;
    g_byte_array_append(b, &c, 1);
}

static void put_uint(GByteArray *b, int field, guint64 v)
{
    put_varint(b, (guint64)field << 3);
    put_varint(b, v);
}

static void put_bytes(GByteArray *b, int field, const guint8 *data, gsize len)
{
    put_varint(b, (guint64)field << 3 | 2);
    put_varint(b, len);
    g_byte_array_append(b, data, len);
}

static void put_string(GByteArray *b, int field, const char *s)
{
    put_bytes(b, field, (const guint8 *)s, strlen(s));
}

static void put_message(GByteArray *b, int field, GByteArray *m)
{
    put_bytes(b, field, m->data, m->len);
    g_byte_array_free(m, TRUE);
}

/* `f` as a whole frame (length, then LapinouAudio) in `out`, with the Hello if `hello`. */
static void frame_encode(LapinouMicObject *self, const Frame *f, gboolean hello, GByteArray *out)
{
    guint8 zero[4] = { 0, 0, 0, 0 };
    guint32 size;

    g_byte_array_set_size(out, 0);
    g_byte_array_append(out, zero, 4);
    put_string(out, 1, self->rabbit_id);
    put_uint(out, 2, f->seq);
    if (f->mulaw) {
        put_bytes(out, 3, f->mulaw->data, f->mulaw->len);
        put_uint(out, 4, f->first_sample);
    }
    if (f->mark_id) {
        GByteArray *m = g_byte_array_new();
        put_string(m, 1, f->mark_id);
        put_uint(m, 2, f->mark_at);
        put_uint(m, 3, f->mark_start ? 1 : 0);
        put_message(out, 5, m);
    }
    if (hello) {
        GByteArray *m = g_byte_array_new();
        put_string(m, 1, self->rabbit_id);
        if (self->firmware && *self->firmware)
            put_string(m, 2, self->firmware);
        put_uint(m, 3, SAMPLE_RATE);
        put_message(out, 6, m);
    }
    size = out->len - 4;
    out->data[0] = size >> 24;
    out->data[1] = size >> 16;
    out->data[2] = size >> 8;
    out->data[3] = size;
}

/* ---- the frames waiting for the server ---------------------------------- */

static void frame_free(Frame *f)
{
    if (f->mulaw)
        g_byte_array_free(f->mulaw, TRUE);
    g_free(f->mark_id);
    g_free(f);
}

static void queue_remove(LapinouMicObject *self, GList *link)
{
    Frame *f = link->data;

    if (f->mulaw)
        self->queued_samples -= f->mulaw->len;
    if (f->mark_id)
        self->queued_marks--;
    g_queue_delete_link(self->frames, link);
    frame_free(f);
}

static void queue_clear(LapinouMicObject *self)
{
    while (!g_queue_is_empty(self->frames))
        queue_remove(self, self->frames->head);
    g_byte_array_set_size(self->out, 0);
    self->out_pos = 0;
}

/* Queue a frame (it gets the next seq), dropping the oldest audio beyond 2 s and the oldest marks
 * beyond QUEUE_MARKS (never the frame being sent), then send what can be sent. */
static void queue_push(LapinouMicObject *self, Frame *f)
{
    GList *link;

    f->seq = self->next_seq++;
    g_queue_push_tail(self->frames, f);
    if (f->mulaw)
        self->queued_samples += f->mulaw->len;
    if (f->mark_id)
        self->queued_marks++;
    link = self->frames->head;
    if (self->out->len > 0 && link)  /* the head frame is being sent */
        link = link->next;
    while (link && (self->queued_samples > QUEUE_SAMPLES || self->queued_marks > QUEUE_MARKS)) {
        GList *next = link->next;
        Frame *old = link->data;
        if ((old->mulaw && self->queued_samples > QUEUE_SAMPLES) || (old->mark_id && self->queued_marks > QUEUE_MARKS)
            || (!old->mulaw && !old->mark_id))
            queue_remove(self, link);
        link = next;
    }
    server_pump(self);
}

/* ---- the connection to the server --------------------------------------- */

static void emit_state(LapinouMicObject *self, const char *state)
{
    g_signal_emit(self, ((LapinouMicObjectClass *)G_OBJECT_GET_CLASS(self))->signals[SIGNAL_STREAM_STATE], 0, state);
}

static gboolean server_event(GIOChannel *channel, GIOCondition condition, gpointer data);

/* Watch the server's socket for what the state needs: writable while connecting or while
 * frames wait, readable (the end of the connection) once connected. */
static void server_watch(LapinouMicObject *self)
{
    GIOCondition condition = 0;
    GIOChannel *channel;

    if (self->server_state == SERVER_CONNECTING)
        condition = G_IO_OUT;
    else if (self->server_state == SERVER_UP)
        condition = G_IO_IN | G_IO_HUP | G_IO_ERR
                    | (self->out->len > 0 || !g_queue_is_empty(self->frames) ? G_IO_OUT : 0);
    if (self->server_watch && condition == self->watch_condition)
        return;
    if (self->server_watch)
        g_source_remove(self->server_watch);
    self->server_watch = 0;
    self->watch_condition = condition;
    if (!condition)
        return;
    channel = g_io_channel_unix_new(self->server_fd);
    self->server_watch = g_io_add_watch(channel, condition, server_event, self);
    g_io_channel_unref(channel);
}

static gboolean server_retry(gpointer data)
{
    LapinouMicObject *self = data;

    self->retry_timer = 0;
    server_try(self);
    return FALSE;
}

static void server_schedule(LapinouMicObject *self)
{
    if (self->retry_timer || !self->streaming || !self->host)
        return;
    self->failures++;
    self->retry_timer = g_timeout_add(self->failures <= FAST_RETRIES ? RETRY_FAST_MS : RETRY_SLOW_MS,
                                      server_retry, self);
}

/* Close the connection (or the attempt); `why` is logged, NULL for a deliberate stop. Tried again
 * later if streaming is on. The frame being sent stays at the head of the queue, sent again in
 * full on the next connection. */
static void server_close(LapinouMicObject *self, const char *why)
{
    int was_up = self->server_state == SERVER_UP;

    if (self->server_watch)
        g_source_remove(self->server_watch);
    self->server_watch = 0;
    if (self->server_fd >= 0)
        close(self->server_fd);
    self->server_fd = -1;
    self->server_state = SERVER_DOWN;
    g_byte_array_set_size(self->out, 0);
    self->out_pos = 0;
    if (was_up) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "server %s: disconnected%s%s", self->host, why ? ": " : "", why ? why : "");
        emit_state(self, "disconnected");
    } else if (why && self->failures == 0) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "server %s: %s (tried again every %d s, then every %d s)", self->host, why,
              RETRY_FAST_MS / 1000, RETRY_SLOW_MS / 1000);
    }
    server_schedule(self);
}

static void server_connected(LapinouMicObject *self)
{
    Frame *f;

    self->server_state = SERVER_UP;
    self->failures = 0;
    self->hello_sent = FALSE;
    self->last_progress = g_get_monotonic_time();
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "server %s: connected", self->host);
    emit_state(self, "connected");
    if (g_queue_is_empty(self->frames)) {
        f = g_new0(Frame, 1);  /* the Hello alone */
        queue_push(self, f);
    } else {
        server_pump(self);
    }
}

/* Read uuid.conf again when the id is still unknown (S40lapinlibre makes it before the daemons). */
static gboolean rabbit_id_load(LapinouMicObject *self)
{
    const char *path = g_getenv("LAPINOU_UUID_FILE");
    char *text = NULL;

    if (self->rabbit_id && *self->rabbit_id)
        return TRUE;
    if (g_file_get_contents(path && *path ? path : UUID_FILE, &text, NULL, NULL)) {
        g_free(self->rabbit_id);
        self->rabbit_id = g_strdup(g_strstrip(text));
        g_free(text);
    }
    return self->rabbit_id && *self->rabbit_id;
}

static void server_try(LapinouMicObject *self)
{
    struct addrinfo hints, *res = NULL;
    int fd, err, one = 1;
    char *why;

    if (!self->streaming || !self->host || self->server_state != SERVER_DOWN || self->retry_timer)
        return;
    if (!rabbit_id_load(self)) {
        server_close(self, "no rabbit id (" UUID_FILE ")");
        return;
    }
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    err = getaddrinfo(self->host, SERVER_PORT, &hints, &res);
    if (err != 0 || !res) {
        why = g_strdup_printf("can't resolve: %s", gai_strerror(err));
        server_close(self, why);
        g_free(why);
        return;
    }
    fd = socket(res->ai_family, SOCK_STREAM, 0);
    if (fd < 0) {
        freeaddrinfo(res);
        server_close(self, strerror(errno));
        return;
    }
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
    fcntl(fd, F_SETFD, FD_CLOEXEC);
    setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof one);
    self->server_fd = fd;
    err = connect(fd, res->ai_addr, res->ai_addrlen) == 0 ? 0 : errno;
    freeaddrinfo(res);
    if (err == 0) {
        server_connected(self);
    } else if (err == EINPROGRESS) {
        self->server_state = SERVER_CONNECTING;
        self->deadline = g_get_monotonic_time() + CONNECT_TIMEOUT_US;
        server_watch(self);
    } else {
        server_close(self, strerror(err));
    }
}

/* Send what the socket takes: the head frame, encoded (with the Hello for a connection's first
 * frame), then the next ones. A frame leaves the queue once it is sent in full. */
static void server_pump(LapinouMicObject *self)
{
    while (self->server_state == SERVER_UP) {
        ssize_t n;

        if (self->out->len == 0) {
            if (g_queue_is_empty(self->frames))
                break;
            frame_encode(self, g_queue_peek_head(self->frames), !self->hello_sent, self->out);
            self->hello_sent = TRUE;
            self->out_pos = 0;
            self->last_progress = g_get_monotonic_time();
        }
        n = send(self->server_fd, self->out->data + self->out_pos, self->out->len - self->out_pos,
                 MSG_NOSIGNAL | MSG_DONTWAIT);
        if (n < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)
                break;
            server_close(self, strerror(errno));
            return;
        }
        self->out_pos += n;
        self->last_progress = g_get_monotonic_time();
        if (self->out_pos == self->out->len) {
            g_byte_array_set_size(self->out, 0);
            self->out_pos = 0;
            queue_remove(self, self->frames->head);
        }
    }
    if (self->server_state != SERVER_DOWN)
        server_watch(self);
}

static gboolean server_event(GIOChannel *channel, GIOCondition condition, gpointer data)
{
    LapinouMicObject *self = data;
    (void)channel;

    if (self->server_state == SERVER_CONNECTING) {
        int err = 0;
        socklen_t len = sizeof err;
        if (getsockopt(self->server_fd, SOL_SOCKET, SO_ERROR, &err, &len) < 0)
            err = errno;
        self->server_watch = 0;
        if (err) {
            server_close(self, strerror(err));
            return FALSE;
        }
        server_connected(self);
        return FALSE;  /* server_connected() made the watch it needs */
    }
    if (condition & (G_IO_IN | G_IO_HUP | G_IO_ERR)) {
        guint8 buf[256];
        ssize_t n = recv(self->server_fd, buf, sizeof buf, MSG_DONTWAIT);  /* the server sends nothing */
        if (n == 0 || (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
            self->server_watch = 0;
            server_close(self, n == 0 ? "closed by the server" : strerror(errno));
            return FALSE;
        }
    }
    if (condition & G_IO_OUT) {
        guint watch = self->server_watch;
        server_pump(self);
        /* server_pump() may have replaced or removed this watch. */
        return self->server_watch == watch && watch != 0;
    }
    return TRUE;
}

/* Once a second: a connection attempt that takes too long, a connection that takes nothing. */
static gboolean server_tick(gpointer data)
{
    LapinouMicObject *self = data;
    gint64 now = g_get_monotonic_time();

    if (self->server_state == SERVER_CONNECTING && now > self->deadline)
        server_close(self, "connection timed out");
    else if (self->server_state == SERVER_UP && self->out->len > 0 && now - self->last_progress > STALL_US)
        server_close(self, "the server takes nothing");
    return TRUE;
}

/* ---- the local socket ---------------------------------------------------- */

static void client_drop(LapinouMicObject *self, Client *c)
{
    if (c->watch)
        g_source_remove(c->watch);
    close(c->fd);
    self->clients = g_slist_remove(self->clients, c);
    g_free(c);
}

static gboolean client_event(GIOChannel *channel, GIOCondition condition, gpointer data)
{
    Client *c = data;
    guint8 buf[256];
    ssize_t n;
    (void)channel;
    (void)condition;

    n = recv(c->fd, buf, sizeof buf, MSG_DONTWAIT);  /* clients send nothing; read until the end */
    if (n > 0 || (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)))
        return TRUE;
    c->watch = 0;
    return FALSE;  /* the client is dropped on the next block (client_send) */
}

static gboolean listen_event(GIOChannel *channel, GIOCondition condition, gpointer data)
{
    LapinouMicObject *self = data;
    GIOChannel *cc;
    Client *c;
    int fd;
    (void)channel;
    (void)condition;

    fd = accept(self->listen_fd, NULL, NULL);
    if (fd < 0)
        return TRUE;
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
    fcntl(fd, F_SETFD, FD_CLOEXEC);
    c = g_new0(Client, 1);
    c->fd = fd;
    cc = g_io_channel_unix_new(fd);
    c->watch = g_io_add_watch(cc, G_IO_IN | G_IO_HUP | G_IO_ERR, client_event, c);
    g_io_channel_unref(cc);
    self->clients = g_slist_prepend(self->clients, c);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "local client %d connected (%u)", fd, g_slist_length(self->clients));
    return TRUE;
}

/* The block to every client; one that has hung up, or can't take it all, is dropped. */
static void client_send(LapinouMicObject *self, const guint8 *data, gsize len)
{
    GSList *l = self->clients;

    while (l) {
        Client *c = l->data;
        ssize_t n;
        l = l->next;
        n = c->watch ? send(c->fd, data, len, MSG_NOSIGNAL | MSG_DONTWAIT) : -1;
        if (n != (ssize_t)len) {
            if (c->watch)
                g_log(NULL, G_LOG_LEVEL_MESSAGE, "local client %d dropped: %s", c->fd,
                      n < 0 ? strerror(errno) : "too slow");
            client_drop(self, c);
        }
    }
}

static void local_listen(LapinouMicObject *self)
{
    const char *env = g_getenv("LAPINOU_MIC_SOCKET");
    const char *path = env && *env ? env : SOCKET_PATH;
    struct sockaddr_un addr;
    char *dir = g_path_get_dirname(path);
    GIOChannel *channel;
    int fd;

    g_mkdir_with_parents(dir, 0755);
    g_free(dir);
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    if (strlen(path) >= sizeof addr.sun_path) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "local socket path too long: %s", path);
        return;
    }
    strcpy(addr.sun_path, path);
    unlink(path);
    fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0 || bind(fd, (struct sockaddr *)&addr, sizeof addr) < 0 || listen(fd, 8) < 0) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "no local socket %s: %s", path, strerror(errno));
        if (fd >= 0)
            close(fd);
        return;
    }
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
    fcntl(fd, F_SETFD, FD_CLOEXEC);
    self->listen_fd = fd;
    channel = g_io_channel_unix_new(fd);
    self->listen_watch = g_io_add_watch(channel, G_IO_IN, listen_event, self);
    g_io_channel_unref(channel);
}

/* ---- the microphone ------------------------------------------------------ */

/* Set /dev/audio up for recording: mu-law, mono, 16 kHz (as voice-daemon does). */
static int audio_setup(int fd)
{
    int value = AFMT_MU_LAW;

    if (ioctl(fd, SNDCTL_DSP_RESET, 0) < 0 || ioctl(fd, SNDCTL_DSP_SETFMT, &value) < 0)
        return -1;
    value = 0;
    if (ioctl(fd, SNDCTL_DSP_STEREO, &value) < 0)
        return -1;
    value = SAMPLE_RATE;
    if (ioctl(fd, SNDCTL_DSP_SPEED, &value) < 0)
        return -1;
    return 0;
}

/* A block read: the gain, the local clients, the server. */
static void audio_block(LapinouMicObject *self, guint8 *data, gsize len)
{
    gsize i;

    if (self->gain_on)
        for (i = 0; i < len; i++)
            data[i] = self->gain[data[i]];
    client_send(self, data, len);
    if (self->streaming && self->host) {
        Frame *f = g_new0(Frame, 1);
        f->first_sample = self->samples;
        f->mulaw = g_byte_array_sized_new(len);
        g_byte_array_append(f->mulaw, data, len);
        self->samples += len;
        queue_push(self, f);
    } else {
        self->samples += len;
    }
}

static gboolean audio_open(gpointer data);

static void audio_reopen_later(LapinouMicObject *self, const char *why)
{
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "%s: %s; opened again in %d s", AUDIO_DEVICE, why, AUDIO_RETRY_MS / 1000);
    if (self->audio_watch)
        g_source_remove(self->audio_watch);
    self->audio_watch = 0;
    if (self->audio_fd >= 0)
        close(self->audio_fd);
    self->audio_fd = -1;
    if (!self->audio_retry)
        self->audio_retry = g_timeout_add(AUDIO_RETRY_MS, audio_open, self);
}

/* Read what is there (at most BLOCK bytes per call); the end of the file or an error closes the
 * device, opened again a second later. */
static gboolean audio_read(LapinouMicObject *self)
{
    guint8 buf[BLOCK];
    ssize_t n = read(self->audio_fd, buf, sizeof buf);

    if (n > 0) {
        audio_block(self, buf, n);
        return TRUE;
    }
    if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
        return TRUE;
    audio_reopen_later(self, n == 0 ? "end of file" : strerror(errno));
    return FALSE;
}

static gboolean audio_event(GIOChannel *channel, GIOCondition condition, gpointer data)
{
    (void)channel;
    (void)condition;
    return audio_read(data);
}

static gboolean audio_open(gpointer data)
{
    LapinouMicObject *self = data;
    GIOChannel *channel;
    int fd;

    self->audio_retry = 0;
    fd = open(AUDIO_DEVICE, O_RDONLY | O_NONBLOCK);
    if (fd < 0) {
        audio_reopen_later(self, strerror(errno));
        return FALSE;
    }
    fcntl(fd, F_SETFD, FD_CLOEXEC);
    self->audio_fd = fd;
    if (audio_setup(fd) != 0) {
        audio_reopen_later(self, "can't set mu-law, mono, 16 kHz");
        return FALSE;
    }
    channel = g_io_channel_unix_new(fd);
    self->audio_watch = g_io_add_watch(channel, G_IO_IN | G_IO_HUP | G_IO_ERR, audio_event, self);
    g_io_channel_unref(channel);  /* OSS starts recording on the first poll() */
    return FALSE;
}

/* ---- D-Bus ---------------------------------------------------------------- */

gboolean lapinou_mic_object_mark_playback(LapinouMicObject *self, const char *playback_id, gboolean start,
                                          GError **error)
{
    Frame *f;

    if (strlen(playback_id) > MAX_PLAYBACK_ID) {
        g_set_error(error, DBUS_GERROR, DBUS_GERROR_INVALID_ARGS, "playback_id longer than %d bytes",
                    MAX_PLAYBACK_ID);
        return FALSE;
    }
    if (!self->streaming || !self->host)
        return TRUE;
    f = g_new0(Frame, 1);
    f->mark_id = g_strdup(playback_id);
    f->mark_at = self->samples;
    f->mark_start = start;
    queue_push(self, f);
    return TRUE;
}

gboolean lapinou_mic_object_set_streaming(LapinouMicObject *self, gboolean on, GError **error)
{
    (void)error;
    if (!on == !self->streaming)
        return TRUE;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "streaming to the server %s", on ? "on" : "off");
    self->streaming = on;
    if (!on) {
        if (self->retry_timer)
            g_source_remove(self->retry_timer);
        self->retry_timer = 0;
        server_close(self, NULL);
        queue_clear(self);
    } else {
        self->failures = 0;
        server_try(self);
    }
    return TRUE;
}

static void lapinou_mic_object_class_init(LapinouMicObjectClass *klass)
{
    GType type = G_TYPE_FROM_CLASS(klass);

    klass->signals[SIGNAL_STREAM_STATE] = g_signal_new("stream_state", type, G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                                       g_cclosure_marshal_VOID__STRING, G_TYPE_NONE, 1, G_TYPE_STRING);
    dbus_g_object_type_install_info(LAPINOU_MIC_TYPE_OBJECT, &dbus_glib_lapinou_mic_object_object_info);
}

static void lapinou_mic_object_init(LapinouMicObject *self)
{
    GError *error = NULL;
    guint request_result;
    char *text = NULL;

    self->audio_fd = -1;
    self->server_fd = -1;
    self->listen_fd = -1;
    self->streaming = TRUE;
    self->frames = g_queue_new();
    self->out = g_byte_array_new();
    self->host = lapinou_conf_get("server", "host", NULL);
    if (g_file_get_contents(BUILD_FILE, &text, NULL, NULL)) {
        char *nl = strchr(text, '\n');
        if (nl)
            *nl = '\0';
        self->firmware = g_strdup(g_strstrip(text));
        g_free(text);
    }
    rabbit_id_load(self);
    gain_setup(self);

    self->bus = dbus_g_bus_get(DBUS_BUS_SYSTEM, &error);
    if (self->bus == NULL)
        die("Failed to open connection to bus", error);
    dbus_g_connection_register_g_object(self->bus, "/com/mindscape/karotz/LapinouMic", G_OBJECT(self));
    self->bus_proxy = dbus_g_proxy_new_for_name(self->bus, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                                                "org.freedesktop.DBus");
    if (!org_freedesktop_DBus_request_name(self->bus_proxy, "com.mindscape.karotz.LapinouMic", 0, &request_result,
                                           &error))
        die("Failed to get name", error);
    if (request_result != DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER) {
        g_log(NULL, G_LOG_LEVEL_ERROR, "Got result code %u from requesting name", request_result);
        for (;;)
            ;
    }
}

int main(void)
{
    int log_priority = LOG_NOTICE;
    GMainLoop *loop;
    LapinouMicObject *mic;

    init_karotz_log("LapinouMic", &log_priority);
    signal(SIGPIPE, SIG_IGN);
    g_type_init();
    loop = g_main_loop_new(NULL, FALSE);
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Starting\n");
    mic = g_object_new(LAPINOU_MIC_TYPE_OBJECT, NULL);
    if (!mic->host)
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "no [server] host: the microphone is only shared locally");
    local_listen(mic);
    audio_open(mic);
    mic->tick = g_timeout_add_seconds(1, server_tick, mic);
    server_try(mic);
    g_main_loop_run(loop);
    return 0;
}
