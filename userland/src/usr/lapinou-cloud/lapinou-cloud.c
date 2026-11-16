/* SPDX-License-Identifier: MIT */
/*
 * lapinou-cloud: LapiNou's mode without a server. A long press on the head (the controller,
 * Task 9) calls start_listening(), its release stop_listening(): the recording (the microphone of
 * lapinou-mic's socket, lapinou-mic-client.h: G.711 mu-law, 16 kHz, mono, at most 15 s) goes to a
 * speech to text service as a WAV file (PCM16, made in memory), the transcript to a language model
 * with the persona (/usr/share/lapinou/persona_fr_court.txt) and the conversation so far, the
 * answer to a text to speech service, whose MP3 (/tmp/lapinou-tts.mp3) multimedia-daemon plays.
 * say(text) speaks a text the same way (lapinou-mqtt's cmd/say in this mode). providers.c makes
 * the requests and reads the answers; docs/lapinou/cloud.md describes the settings.
 *
 * Settings (lapinou_conf.h), read again at each start_listening() and say(): [cloud] enabled (1,
 * else nothing is sent), stt_url, stt_model, llm_kind (anthropic or openai), llm_url, llm_model,
 * tts_url, tts_model, tts_voice; the keys are the secrets cloud_stt_key, cloud_llm_key and
 * cloud_tts_key. Keys, transcripts and answers are never logged.
 *
 * The requests are libcurl's multi interface in the GLib main loop (its sockets and timer are GLib
 * sources), one at a time, so D-Bus is never kept waiting. HTTPS certificates are checked against
 * the CA certificates in /etc/ssl/certs (one file each, by subject hash; $SSL_CERT_DIR replaces
 * it), as karotzVM checks them; while the clock isn't set (before 2026) an HTTPS request fails. A
 * failure (the network, an HTTP status other than 200, an answer that isn't one, a time-out, the
 * model refusing) never ends the daemon: the error sound, the orange LED for 3 s (then the status
 * LED of an available rabbit, a slow green pulse) and the state "error"; a refusal says a gentle
 * sentence instead of the error sound.
 *
 * The conversation (questions and answers) is kept in RAM and forgotten 5 minutes after the last
 * answer (providers.h).
 *
 * D-Bus: com.mindscape.karotz.LapinouCloud (/com/mindscape/karotz/LapinouCloud, interface
 * com.mindscape.karotz.LapinouCloud): start_listening(), stop_listening(), say(s text); signal
 * state(s): "listening", "thinking", "speaking", "idle" or "error".
 *
 * For the tests: LAPINOU_MIC_SOCKET moves the microphone's socket (lapinou-mic-client.h),
 * LAPINOU_CONF_DIR the settings (lapinou_conf.h).
 */
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <time.h>
#include <unistd.h>

#include <curl/curl.h>
#include <dbus/dbus-glib-bindings.h>
#include <dbus/dbus-glib.h>
#include <glib-object.h>
#include <glib-unix.h>

#include "karotz-common.h"
#include "karotz-marshal.h"
#include "karotz-messages.h"
#include "lapinou-mic-client.h"
#include "lapinou_conf.h"
#include "providers.h"

#define SERVICE        "com.mindscape.karotz.LapinouCloud"
#define OBJECT_PATH    "/com/mindscape/karotz/LapinouCloud"
#define KAROTZ_IFACE   "com.mindscape.karotz.KarotzInterface"
#define PERSONA_FILE   "/usr/share/lapinou/persona_fr_court.txt"
#define PERSONA_MAX    8192
#define TTS_FILE       "/tmp/lapinou-tts.mp3"
#define TTS_PART       "/tmp/lapinou-tts.mp3.part"
#define ERROR_SOUND    "/usr/karotz/sounds/error.mp3"
#define NOT_HEARD      "/usr/karotz/sounds/fr_speech_not_understood.mp3"
#define PLAY_ID        "lapinou-cloud"
#define CA_DIR         "/etc/ssl/certs"
#define USER_AGENT     "lapinou-cloud"
#define RATE           16000
#define LISTEN_MAX     (15 * RATE)        /* bytes of mu-law: 15 s */
#define LISTEN_MIN     (RATE * 3 / 10)    /* under 0.3 s, nothing is sent */
#define LISTEN_GUARD_MS 16000             /* a microphone that stalls: the recording ends anyway */
#define CONNECT_S      10
#define STT_S          30
#define LLM_S          60
#define TTS_S          60
#define STT_MAX        (64 * 1024)
#define LLM_MAX        (256 * 1024)
#define TTS_MAX        (4 * 1024 * 1024)
#define ERROR_LED_S    3
#define SPEAK_GUARD_S  300                /* no multi_playfinished: idle again after this */
#define CALL_MS        10000
#define PULSE_PERIOD   1600               /* the controller's status pulse */
/* The rabbit's clock reads 1970 until ntpd sets it: before 2026-01-01 it counts as not set. */
#define CLOCK_SET_AFTER 1767225600L
#define GENTLE_REFUSAL "Je ne peux pas répondre à ça. Demande-moi autre chose !"
#define DEFAULT_PERSONA "Tu es Lapinou, un lapin gentil qui parle avec des enfants. Réponds en français, " \
                        "en deux ou trois phrases courtes, sans listes ni liens."

typedef enum { ST_IDLE, ST_LISTENING, ST_THINKING, ST_SPEAKING, ST_ERROR } State;
static const char *const STATE_NAMES[] = { "idle", "listening", "thinking", "speaking", "error" };

/* --- the D-Bus object -------------------------------------------------------------------- */

typedef struct {
    GObject parent;
} LapinouCloudObject;

typedef struct {
    GObjectClass parent;
    guint state_signal;
} LapinouCloudObjectClass;

#define LAPINOU_CLOUD_TYPE_OBJECT (lapinou_cloud_object_get_type())
#define LAPINOU_CLOUD_OBJECT_GET_CLASS(o) \
    (G_TYPE_INSTANCE_GET_CLASS((o), LAPINOU_CLOUD_TYPE_OBJECT, LapinouCloudObjectClass))
GType lapinou_cloud_object_get_type(void);
G_DEFINE_TYPE(LapinouCloudObject, lapinou_cloud_object, G_TYPE_OBJECT)

static gboolean lapinou_cloud_object_start_listening(LapinouCloudObject *self, GError **error);
static gboolean lapinou_cloud_object_stop_listening(LapinouCloudObject *self, GError **error);
static gboolean lapinou_cloud_object_say(LapinouCloudObject *self, const char *text, GError **error);
#include "lapinou-cloud-glue.h"

/* --- the daemon's state ---------------------------------------------------------------- */

typedef struct {
    char *stt_url, *stt_model, *stt_key;
    LlmKind llm_kind;
    char *llm_url, *llm_model, *llm_key;
    char *tts_url, *tts_model, *tts_voice, *tts_key;
} Settings;

typedef struct Request Request;
typedef void (*RequestDone)(Request *r, const char *failure);

struct Request {
    CURL *easy;
    struct curl_slist *headers;
    curl_mime *mime;
    GString *upload;     /* the body sent (kept until the end of the transfer) */
    gsize upload_pos;    /* the WAV's read position (multipart) */
    GString *response;   /* the answer, at most `cap` bytes */
    FILE *file;          /* or the answer to this file (text to speech) */
    gsize received, cap;
    gboolean too_big;
    const char *what;    /* "speech to text", "the language model", "text to speech" */
    RequestDone done;
    char error[CURL_ERROR_SIZE];
};

typedef struct {
    guint source;
} SocketWatch;

static struct {
    GMainLoop *loop;
    DBusGConnection *bus;
    LapinouCloudObject *object;
    DBusGProxy *led, *multimedia;
    State state;
    Settings *settings;
    /* the recording */
    int mic_fd;
    guint mic_source, listen_timer;
    GByteArray *audio;
    /* the requests */
    CURLM *multi;
    guint curl_timer;
    Request *req;
    /* the conversation */
    History history;
    char *question;      /* the transcript waiting for its answer */
    guint led_timer, speak_timer;
} app;

static void set_state(State state)
{
    if (app.state == state)
        return;
    app.state = state;
    g_signal_emit(app.object, LAPINOU_CLOUD_OBJECT_GET_CLASS(app.object)->state_signal, 0, STATE_NAMES[state]);
}

/* --- the other daemons ------------------------------------------------------------------- */

static void call_done(DBusGProxy *proxy, DBusGProxyCall *call, gpointer what)
{
    GError *error = NULL;
    gboolean result = FALSE;

    if (!dbus_g_proxy_end_call(proxy, call, &error, G_TYPE_BOOLEAN, &result, G_TYPE_INVALID)) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "%s failed: %s", (const char *)what, error->message);
        g_error_free(error);
    } else if (!result) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "%s: refused", (const char *)what);
    }
}

/* An asynchronous call answered by one boolean; `what` is a static string for the log. */
#define CALL(proxy, what, method, ...)                                                                    \
    dbus_g_proxy_begin_call_with_timeout((proxy), (method), call_done, (gpointer)(what), NULL, CALL_MS, \
                                         __VA_ARGS__, G_TYPE_INVALID)

static void play(const char *path)
{
    CALL(app.multimedia, "Multimedia.play", "play", G_TYPE_STRING, PLAY_ID, G_TYPE_STRING, path, G_TYPE_INT, 1);
}

static void stop_playing(void)
{
    CALL(app.multimedia, "Multimedia.stop", "stop", G_TYPE_STRING, PLAY_ID, G_TYPE_INT, 0);
}

static void on_play_finished(DBusGProxy *proxy, const char *id, int reason, gpointer data)
{
    (void)proxy;
    (void)reason;
    (void)data;
    if (app.state == ST_SPEAKING && id != NULL && strcmp(id, PLAY_ID) == 0) {
        if (app.speak_timer != 0)
            g_source_remove(app.speak_timer);
        app.speak_timer = 0;
        set_state(ST_IDLE);
    }
}

static gboolean speak_guard(gpointer data)
{
    (void)data;
    app.speak_timer = 0;
    if (app.state == ST_SPEAKING)
        set_state(ST_IDLE);
    return G_SOURCE_REMOVE;
}

/* 3 s after an error: the LED of an available rabbit, as the controller lights it; idle again. */
static gboolean error_over(gpointer data)
{
    (void)data;
    app.led_timer = 0;
    CALL(app.led, "Led.pulse", "pulse", G_TYPE_STRING, "", G_TYPE_STRING, LED_GREEN, G_TYPE_STRING, LED_OFF,
         G_TYPE_INT, PULSE_PERIOD, G_TYPE_INT, LED_FOREVER);
    if (app.state == ST_ERROR)
        set_state(ST_IDLE);
    return G_SOURCE_REMOVE;
}

static void error_led(void)
{
    CALL(app.led, "Led.light", "light", G_TYPE_STRING, "", G_TYPE_STRING, LED_ORANGE);
    if (app.led_timer != 0)
        g_source_remove(app.led_timer);
    app.led_timer = g_timeout_add_seconds(ERROR_LED_S, error_over, NULL);
}

/* --- the settings ---------------------------------------------------------------------------- */

static void settings_free(Settings *s)
{
    char **keys[] = { &s->stt_key, &s->llm_key, &s->tts_key };
    size_t i;

    if (s == NULL)
        return;
    for (i = 0; i < G_N_ELEMENTS(keys); i++)
        if (*keys[i] != NULL)
            memset(*keys[i], 0, strlen(*keys[i]));
    g_free(s->stt_url);
    g_free(s->stt_model);
    g_free(s->stt_key);
    g_free(s->llm_url);
    g_free(s->llm_model);
    g_free(s->llm_key);
    g_free(s->tts_url);
    g_free(s->tts_model);
    g_free(s->tts_voice);
    g_free(s->tts_key);
    g_free(s);
}

/* A key goes into an HTTP header: printable ASCII only. */
static gboolean valid_key(const char *key)
{
    const char *p;

    for (p = key; *p; p++)
        if (*p <= ' ' || *p > '~')
            return FALSE;
    return TRUE;
}

/* The settings, or NULL with the reason logged (`listening`: all three services, else text to
 * speech only). */
static Settings *settings_load(gboolean listening)
{
    Settings *s = g_new0(Settings, 1);
    char *enabled = lapinou_conf_get("cloud", "enabled", "0");
    char *kind = lapinou_conf_get("cloud", "llm_kind", "anthropic");
    char *stt = lapinou_conf_get("cloud", "stt_url", "https://api.openai.com/v1/audio/transcriptions");
    char *tts = lapinou_conf_get("cloud", "tts_url", "https://api.openai.com/v1/audio/speech");
    char *llm;
    const char *missing = NULL;

    if (strcmp(enabled, "1") != 0)
        missing = "[cloud] enabled is not 1";
    s->llm_kind = strcmp(kind, "openai") == 0 ? LLM_OPENAI : LLM_ANTHROPIC;
    if (strcmp(kind, "openai") != 0 && strcmp(kind, "anthropic") != 0 && missing == NULL)
        missing = "[cloud] llm_kind is neither anthropic nor openai";
    llm = lapinou_conf_get("cloud", "llm_url", s->llm_kind == LLM_OPENAI ? "https://api.openai.com" : "https://api.anthropic.com");
    s->stt_url = endpoint(stt, "/v1/audio/transcriptions");
    s->tts_url = endpoint(tts, "/v1/audio/speech");
    s->llm_url = endpoint(llm, s->llm_kind == LLM_OPENAI ? "/v1/chat/completions" : "/v1/messages");
    s->stt_model = lapinou_conf_get("cloud", "stt_model", "whisper-1");
    s->llm_model = lapinou_conf_get("cloud", "llm_model", s->llm_kind == LLM_OPENAI ? "gpt-4o-mini" : "claude-opus-5-5");
    s->tts_model = lapinou_conf_get("cloud", "tts_model", "tts-1");
    s->tts_voice = lapinou_conf_get("cloud", "tts_voice", "alloy");
    s->stt_key = lapinou_secret_get("cloud_stt_key");
    s->llm_key = lapinou_secret_get("cloud_llm_key");
    s->tts_key = lapinou_secret_get("cloud_tts_key");
    if (missing == NULL && s->tts_url == NULL)
        missing = "[cloud] tts_url is not an http:// or https:// URL";
    if (missing == NULL && listening && s->stt_url == NULL)
        missing = "[cloud] stt_url is not an http:// or https:// URL";
    if (missing == NULL && listening && s->llm_url == NULL)
        missing = "[cloud] llm_url is not an http:// or https:// URL";
    if (missing == NULL && ((s->stt_key && !valid_key(s->stt_key)) || (s->llm_key && !valid_key(s->llm_key)) ||
                            (s->tts_key && !valid_key(s->tts_key))))
        missing = "a cloud key has characters a key can't have";
    if (missing != NULL) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "the cloud is not configured: %s", missing);
        settings_free(s);
        s = NULL;
    }
    g_free(enabled);
    g_free(kind);
    g_free(stt);
    g_free(tts);
    g_free(llm);
    return s;
}

/* The persona file without its comment lines at the start (# ..., its licence). */
static char *persona(void)
{
    char *text = NULL, *body, *out;
    gsize len = 0;

    if (!g_file_get_contents(PERSONA_FILE, &text, &len, NULL) || len > PERSONA_MAX ||
        !g_utf8_validate(text, (gssize)len, NULL) || strlen(text) != len) {
        g_free(text);
        text = NULL;
    }
    body = text;
    while (body != NULL && *body == '#') {
        char *end = strchr(body, '\n');
        body = end != NULL ? end + 1 : body + strlen(body);
    }
    out = body != NULL ? g_strstrip(g_strdup(body)) : NULL;
    g_free(text);
    if (out == NULL || *out == '\0') {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "no persona in %s: the default one", PERSONA_FILE);
        g_free(out);
        return g_strdup(DEFAULT_PERSONA);
    }
    return out;
}

/* --- the requests: libcurl's multi interface in the main loop ---------------------------- */

static void request_free(Request *r)
{
    if (r == NULL)
        return;
    curl_easy_cleanup(r->easy);
    curl_slist_free_all(r->headers);
    curl_mime_free(r->mime);
    if (r->upload != NULL)
        g_string_free(r->upload, TRUE);
    if (r->response != NULL)
        g_string_free(r->response, TRUE);
    if (r->file != NULL)
        fclose(r->file);
    g_free(r);
}

static void request_cancel(void)
{
    if (app.req == NULL)
        return;
    curl_multi_remove_handle(app.multi, app.req->easy);
    request_free(app.req);
    app.req = NULL;
}

static size_t on_body(char *data, size_t size, size_t n, void *userp)
{
    Request *r = userp;
    size_t len = size * n;

    if (r->received + len > r->cap) {
        r->too_big = TRUE;
        return 0;
    }
    r->received += len;
    if (r->file != NULL)
        return fwrite(data, 1, len, r->file);
    g_string_append_len(r->response, data, (gssize)len);
    return len;
}

static size_t on_wav_read(char *buffer, size_t size, size_t n, void *arg)
{
    Request *r = arg;
    size_t len = MIN(size * n, r->upload->len - r->upload_pos);

    memcpy(buffer, r->upload->str + r->upload_pos, len);
    r->upload_pos += len;
    return len;
}

static int on_wav_seek(void *arg, curl_off_t offset, int origin)
{
    Request *r = arg;

    if (origin != SEEK_SET || offset < 0 || (gsize)offset > r->upload->len)
        return CURL_SEEKFUNC_CANTSEEK;
    r->upload_pos = (gsize)offset;
    return CURL_SEEKFUNC_OK;
}

static void add_header(Request *r, const char *name, const char *value)
{
    char *line = g_strdup_printf("%s: %s", name, value);

    r->headers = curl_slist_append(r->headers, line);
    memset(line, 0, strlen(line)); /* it may hold a key */
    g_free(line);
}

static Request *request_new(const char *what, const char *url, long timeout_s, gsize cap, RequestDone done)
{
    Request *r = g_new0(Request, 1);
    const char *ca = getenv("SSL_CERT_DIR");

    r->what = what;
    r->done = done;
    r->cap = cap;
    r->response = g_string_new(NULL);
    r->easy = curl_easy_init();
    curl_easy_setopt(r->easy, CURLOPT_URL, url);
    curl_easy_setopt(r->easy, CURLOPT_PRIVATE, r);
    curl_easy_setopt(r->easy, CURLOPT_WRITEFUNCTION, on_body);
    curl_easy_setopt(r->easy, CURLOPT_WRITEDATA, r);
    curl_easy_setopt(r->easy, CURLOPT_ERRORBUFFER, r->error);
    curl_easy_setopt(r->easy, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(r->easy, CURLOPT_CONNECTTIMEOUT, (long)CONNECT_S);
    curl_easy_setopt(r->easy, CURLOPT_TIMEOUT, timeout_s);
    curl_easy_setopt(r->easy, CURLOPT_USERAGENT, USER_AGENT);
    curl_easy_setopt(r->easy, CURLOPT_PROTOCOLS_STR, "http,https");
    curl_easy_setopt(r->easy, CURLOPT_FOLLOWLOCATION, 0L);
    /* The CA directory, not curl's single bundle file (about 1 s of CPU more on the rabbit). */
    curl_easy_setopt(r->easy, CURLOPT_CAINFO, (char *)NULL);
    curl_easy_setopt(r->easy, CURLOPT_CAPATH, ca != NULL && *ca != '\0' ? ca : CA_DIR);
    curl_easy_setopt(r->easy, CURLOPT_SSL_VERIFYPEER, 1L);
    curl_easy_setopt(r->easy, CURLOPT_SSL_VERIFYHOST, 2L);
    r->headers = curl_slist_append(r->headers, "Expect:");
    return r;
}

/* Send r (the only request at a time). FALSE, with r freed and the reason in *why, when it can't. */
static gboolean request_start(Request *r, const char *url, const char **why)
{
    if (g_ascii_strncasecmp(url, "https:", 6) == 0 && time(NULL) < CLOCK_SET_AFTER) {
        *why = "the clock isn't set: certificates can't be checked";
        request_free(r);
        return FALSE;
    }
    curl_easy_setopt(r->easy, CURLOPT_HTTPHEADER, r->headers);
    if (curl_multi_add_handle(app.multi, r->easy) != CURLM_OK) {
        *why = "curl can't start the request";
        request_free(r);
        return FALSE;
    }
    app.req = r;
    return TRUE;
}

static void check_done(void)
{
    CURLMsg *msg;
    int left;

    while ((msg = curl_multi_info_read(app.multi, &left)) != NULL) {
        Request *r = NULL;
        CURLcode code = msg->data.result;
        CURL *easy = msg->easy_handle;
        long status = 0;
        char *failure = NULL;

        if (msg->msg != CURLMSG_DONE)
            continue;
        curl_easy_getinfo(easy, CURLINFO_PRIVATE, (char **)&r);
        curl_easy_getinfo(easy, CURLINFO_RESPONSE_CODE, &status);
        curl_multi_remove_handle(app.multi, easy);
        if (app.req == r)
            app.req = NULL;
        if (r->file != NULL) {
            if (fclose(r->file) != 0 && code == CURLE_OK)
                failure = g_strdup("the answer can't be written");
            r->file = NULL;
        }
        if (failure == NULL && r->too_big)
            failure = g_strdup("the answer is too long");
        else if (failure == NULL && code != CURLE_OK)
            failure = g_strdup(r->error[0] != '\0' ? r->error : curl_easy_strerror(code));
        else if (failure == NULL && status != 200)
            failure = g_strdup_printf("HTTP %ld", status);
        r->done(r, failure);
        g_free(failure);
        request_free(r);
    }
}

static gboolean on_curl_socket(gint fd, GIOCondition condition, gpointer data)
{
    int action = 0;
    int running;

    (void)data;
    if (condition & (G_IO_IN | G_IO_HUP))
        action |= CURL_CSELECT_IN;
    if (condition & G_IO_OUT)
        action |= CURL_CSELECT_OUT;
    if (condition & G_IO_ERR)
        action |= CURL_CSELECT_ERR;
    curl_multi_socket_action(app.multi, fd, action, &running);
    check_done();
    return G_SOURCE_CONTINUE;
}

static int curl_socket(CURL *easy, curl_socket_t s, int what, void *userp, void *socketp)
{
    SocketWatch *w = socketp;
    GIOCondition condition = G_IO_ERR | G_IO_HUP;

    (void)easy;
    (void)userp;
    if (what == CURL_POLL_REMOVE) {
        if (w != NULL) {
            g_source_remove(w->source);
            g_free(w);
        }
        curl_multi_assign(app.multi, s, NULL);
        return 0;
    }
    if (what & CURL_POLL_IN)
        condition |= G_IO_IN;
    if (what & CURL_POLL_OUT)
        condition |= G_IO_OUT;
    if (w == NULL) {
        w = g_new0(SocketWatch, 1);
        curl_multi_assign(app.multi, s, w);
    } else {
        g_source_remove(w->source);
    }
    w->source = g_unix_fd_add(s, condition, on_curl_socket, NULL);
    return 0;
}

static gboolean on_curl_timeout(gpointer data)
{
    int running;

    (void)data;
    app.curl_timer = 0;
    curl_multi_socket_action(app.multi, CURL_SOCKET_TIMEOUT, 0, &running);
    check_done();
    return G_SOURCE_REMOVE;
}

static int curl_timer(CURLM *multi, long timeout_ms, void *userp)
{
    (void)multi;
    (void)userp;
    if (app.curl_timer != 0)
        g_source_remove(app.curl_timer);
    app.curl_timer = 0;
    if (timeout_ms >= 0)
        app.curl_timer = g_timeout_add((guint)timeout_ms, on_curl_timeout, NULL);
    return 0;
}

/* --- what goes wrong ------------------------------------------------------------------------- */

static void stop_recording(void);
static void drop_audio(void);

/* Everything in progress stopped: the recording, the request, the question. */
static void cancel_all(void)
{
    stop_recording();
    request_cancel();
    g_free(app.question);
    app.question = NULL;
    if (app.speak_timer != 0)
        g_source_remove(app.speak_timer);
    app.speak_timer = 0;
}

static void fail(const char *format, ...) G_GNUC_PRINTF(1, 2);
static void fail(const char *format, ...)
{
    va_list args;
    char *why;

    va_start(args, format);
    why = g_strdup_vprintf(format, args);
    va_end(args);
    g_log(NULL, G_LOG_LEVEL_WARNING, "%s", why);
    g_free(why);
    cancel_all();
    set_state(ST_ERROR);
    play(ERROR_SOUND);
    error_led();
}

/* --- speaking -------------------------------------------------------------------------------- */

static void tts_done(Request *r, const char *failure)
{
    if (failure == NULL && r->received == 0)
        failure = "an empty answer";
    if (failure == NULL && rename(TTS_PART, TTS_FILE) != 0)
        failure = "the answer can't be written";
    if (failure != NULL) {
        unlink(TTS_PART);
        fail("%s failed: %s", r->what, failure);
        return;
    }
    play(TTS_FILE);
    set_state(ST_SPEAKING);
    if (app.speak_timer != 0)
        g_source_remove(app.speak_timer);
    app.speak_timer = g_timeout_add_seconds(SPEAK_GUARD_S, speak_guard, NULL);
}

/* `text` to speech, then played. */
static void speak(const char *text)
{
    Settings *s = app.settings;
    Request *r = request_new("text to speech", s->tts_url, TTS_S, TTS_MAX, tts_done);
    const char *why = NULL;

    request_cancel();
    r->file = fopen(TTS_PART, "wb");
    if (r->file == NULL) {
        request_free(r);
        fail("text to speech failed: %s can't be written", TTS_PART);
        return;
    }
    r->upload = tts_request(s->tts_model, s->tts_voice, text);
    curl_easy_setopt(r->easy, CURLOPT_POSTFIELDS, r->upload->str);
    curl_easy_setopt(r->easy, CURLOPT_POSTFIELDSIZE, (long)r->upload->len);
    r->headers = curl_slist_append(r->headers, "Content-Type: application/json");
    if (s->tts_key != NULL) {
        char *bearer = g_strconcat("Bearer ", s->tts_key, NULL);
        add_header(r, "Authorization", bearer);
        memset(bearer, 0, strlen(bearer));
        g_free(bearer);
    }
    if (!request_start(r, s->tts_url, &why)) {
        unlink(TTS_PART);
        fail("text to speech failed: %s", why);
    }
}

/* --- the language model ------------------------------------------------------------------ */

static void llm_done(Request *r, const char *failure)
{
    char *answer = NULL;
    char *spoken;
    AnswerKind kind;

    if (failure != NULL) {
        fail("%s failed: %s", r->what, failure);
        return;
    }
    kind = llm_answer(app.settings->llm_kind, r->response->str, r->response->len, &answer);
    if (kind == ANSWER_REFUSED) {
        /* No part of an answer: a gentle sentence instead, with the error's LED. */
        g_log(NULL, G_LOG_LEVEL_WARNING, "the language model refused to answer");
        g_free(app.question);
        app.question = NULL;
        set_state(ST_ERROR);
        error_led();
        speak(GENTLE_REFUSAL);
        return;
    }
    if (kind != ANSWER_OK) {
        fail("%s failed: not an answer", r->what);
        return;
    }
    history_add(&app.history, app.question, answer, time(NULL));
    g_free(app.question);
    app.question = NULL;
    spoken = speech_text(answer);
    g_log(NULL, G_LOG_LEVEL_DEBUG, "answer: %u bytes", (unsigned)strlen(spoken));
    speak(spoken);
    g_free(spoken);
    g_free(answer);
}

static void ask(void)
{
    Settings *s = app.settings;
    Request *r = request_new("the language model", s->llm_url, LLM_S, LLM_MAX, llm_done);
    char *system = persona();
    const char *why = NULL;

    history_expire(&app.history, time(NULL));
    r->upload = llm_request(s->llm_kind, s->llm_model, system, &app.history, app.question);
    g_free(system);
    curl_easy_setopt(r->easy, CURLOPT_POSTFIELDS, r->upload->str);
    curl_easy_setopt(r->easy, CURLOPT_POSTFIELDSIZE, (long)r->upload->len);
    r->headers = curl_slist_append(r->headers, "Content-Type: application/json");
    if (s->llm_kind == LLM_ANTHROPIC) {
        if (s->llm_key != NULL)
            add_header(r, "x-api-key", s->llm_key);
        add_header(r, "anthropic-version", ANTHROPIC_VERSION);
        if (anthropic_has_fallback(s->llm_model))
            add_header(r, "anthropic-beta", ANTHROPIC_FALLBACK_BETA);
    } else if (s->llm_key != NULL) {
        char *bearer = g_strconcat("Bearer ", s->llm_key, NULL);
        add_header(r, "Authorization", bearer);
        memset(bearer, 0, strlen(bearer));
        g_free(bearer);
    }
    if (!request_start(r, s->llm_url, &why))
        fail("the language model failed: %s", why);
}

/* --- speech to text ---------------------------------------------------------------------------- */

static void stt_done(Request *r, const char *failure)
{
    char *text;

    if (failure != NULL) {
        fail("%s failed: %s", r->what, failure);
        return;
    }
    text = stt_answer(r->response->str, r->response->len);
    if (text == NULL) {
        fail("%s failed: not an answer", r->what);
        return;
    }
    if (*text == '\0') {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "nothing heard");
        g_free(text);
        play(NOT_HEARD);
        set_state(ST_IDLE);
        return;
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "transcript: %u bytes", (unsigned)strlen(text));
    g_free(app.question);
    app.question = text;
    ask();
}

static void transcribe(void)
{
    Settings *s = app.settings;
    Request *r = request_new("speech to text", s->stt_url, STT_S, STT_MAX, stt_done);
    curl_mimepart *part;
    const char *why = NULL;

    r->upload = wav_from_mulaw(app.audio->data, app.audio->len);
    r->mime = curl_mime_init(r->easy);
    part = curl_mime_addpart(r->mime);
    curl_mime_name(part, "model");
    curl_mime_data(part, s->stt_model, CURL_ZERO_TERMINATED);
    part = curl_mime_addpart(r->mime);
    curl_mime_name(part, "language");
    curl_mime_data(part, "fr", CURL_ZERO_TERMINATED);
    part = curl_mime_addpart(r->mime);
    curl_mime_name(part, "response_format");
    curl_mime_data(part, "json", CURL_ZERO_TERMINATED);
    part = curl_mime_addpart(r->mime);
    curl_mime_name(part, "file");
    curl_mime_filename(part, "audio.wav");
    curl_mime_type(part, "audio/wav");
    curl_mime_data_cb(part, (curl_off_t)r->upload->len, on_wav_read, on_wav_seek, NULL, r);
    curl_easy_setopt(r->easy, CURLOPT_MIMEPOST, r->mime);
    if (s->stt_key != NULL) {
        char *bearer = g_strconcat("Bearer ", s->stt_key, NULL);
        add_header(r, "Authorization", bearer);
        memset(bearer, 0, strlen(bearer));
        g_free(bearer);
    }
    drop_audio();
    if (!request_start(r, s->stt_url, &why))
        fail("speech to text failed: %s", why);
}

/* --- the recording ------------------------------------------------------------------------- */

/* The recording's memory given back (up to 240 kB): the rabbit has 64 MB. */
static void drop_audio(void)
{
    g_byte_array_unref(app.audio);
    app.audio = g_byte_array_new();
}

static void stop_recording(void)
{
    if (app.mic_source != 0)
        g_source_remove(app.mic_source);
    app.mic_source = 0;
    if (app.listen_timer != 0)
        g_source_remove(app.listen_timer);
    app.listen_timer = 0;
    if (app.mic_fd >= 0)
        close(app.mic_fd);
    app.mic_fd = -1;
}

/* The recording ends: sent when it is long enough. */
static void end_listening(void)
{
    guint ms = app.audio->len * 1000 / RATE;

    stop_recording();
    if (app.state != ST_LISTENING)
        return;
    if (app.audio->len < LISTEN_MIN) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "a recording of %u ms: too short, nothing sent", ms);
        drop_audio();
        set_state(ST_IDLE);
        return;
    }
    g_log(NULL, G_LOG_LEVEL_DEBUG, "a recording of %u ms", ms);
    set_state(ST_THINKING);
    transcribe();
}

static gboolean on_mic(gint fd, GIOCondition condition, gpointer data)
{
    guint8 buf[4096];
    ssize_t n;

    (void)condition;
    (void)data;
    for (;;) {
        size_t room = LISTEN_MAX - app.audio->len;
        n = read(fd, buf, MIN(sizeof buf, room));
        if (n > 0) {
            g_byte_array_append(app.audio, buf, (guint)n);
            if (app.audio->len >= LISTEN_MAX) {
                g_log(NULL, G_LOG_LEVEL_MESSAGE, "the recording is cut at 15 s");
                app.mic_source = 0;
                end_listening();
                return G_SOURCE_REMOVE;
            }
            continue;
        }
        if (n < 0 && errno == EINTR)
            continue;
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
            return G_SOURCE_CONTINUE;
        break;
    }
    /* The connection has ended: lapinou-mic stopped. What was recorded is sent. */
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "the microphone's connection has ended");
    app.mic_source = 0;
    end_listening();
    return G_SOURCE_REMOVE;
}

static gboolean listen_guard(gpointer data)
{
    (void)data;
    app.listen_timer = 0;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "the recording is cut: the microphone is slow");
    end_listening();
    return G_SOURCE_REMOVE;
}

/* --- the D-Bus methods ------------------------------------------------------------------------ */

/* Ready for something new: anything in progress stopped (the playback too), the settings read. */
static gboolean begin(gboolean listening)
{
    if (app.state == ST_SPEAKING)
        stop_playing();
    cancel_all();
    settings_free(app.settings);
    app.settings = settings_load(listening);
    return app.settings != NULL;
}

static gboolean lapinou_cloud_object_start_listening(LapinouCloudObject *self, GError **error)
{
    int fd;

    (void)self;
    (void)error;
    if (!begin(TRUE)) {
        fail("start_listening refused: the cloud is not configured");
        return TRUE;
    }
    fd = lapinou_mic_connect();
    if (fd < 0) {
        fail("the microphone is not available (%s)", fd == LAPINOU_MIC_ABSENT ? "no lapinou-mic" : "lapinou-mic doesn't answer");
        return TRUE;
    }
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
    app.mic_fd = fd;
    g_byte_array_set_size(app.audio, 0);
    app.mic_source = g_unix_fd_add(fd, G_IO_IN | G_IO_HUP | G_IO_ERR, on_mic, NULL);
    app.listen_timer = g_timeout_add(LISTEN_GUARD_MS, listen_guard, NULL);
    set_state(ST_LISTENING);
    return TRUE;
}

static gboolean lapinou_cloud_object_stop_listening(LapinouCloudObject *self, GError **error)
{
    (void)self;
    (void)error;
    if (app.state == ST_LISTENING)
        end_listening();
    return TRUE;
}

static gboolean lapinou_cloud_object_say(LapinouCloudObject *self, const char *text, GError **error)
{
    char *spoken;

    (void)self;
    (void)error;
    if (app.state == ST_LISTENING || app.state == ST_THINKING) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "say refused: a question is in progress");
        return TRUE;
    }
    spoken = speech_text(text != NULL ? text : "");
    if (*spoken == '\0') {
        g_free(spoken);
        return TRUE;
    }
    if (!begin(FALSE))
        fail("say refused: the cloud is not configured");
    else
        speak(spoken);
    g_free(spoken);
    return TRUE;
}

static void lapinou_cloud_object_class_init(LapinouCloudObjectClass *klass)
{
    klass->state_signal = g_signal_new("state", G_TYPE_FROM_CLASS(klass), G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                       g_cclosure_marshal_VOID__STRING, G_TYPE_NONE, 1, G_TYPE_STRING);
    dbus_g_object_type_install_info(LAPINOU_CLOUD_TYPE_OBJECT, &dbus_glib_lapinou_cloud_object_object_info);
}

static void lapinou_cloud_object_init(LapinouCloudObject *self)
{
    (void)self;
}

static gboolean on_term(gpointer data)
{
    (void)data;
    g_main_loop_quit(app.loop);
    return G_SOURCE_REMOVE;
}

int main(void)
{
    int log_priority = LOG_NOTICE;
    GError *error = NULL;
    DBusGProxy *bus_proxy;
    guint request_result;

    init_karotz_log("LapinouCloud", &log_priority);
    signal(SIGPIPE, SIG_IGN);
    g_type_init();
    app.loop = g_main_loop_new(NULL, FALSE);
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Starting\n");
    app.mic_fd = -1;
    app.audio = g_byte_array_new();
    history_init(&app.history);
    if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) {
        g_log(NULL, G_LOG_LEVEL_ERROR, "curl_global_init failed");
        return 1;
    }
    app.multi = curl_multi_init();
    curl_multi_setopt(app.multi, CURLMOPT_SOCKETFUNCTION, curl_socket);
    curl_multi_setopt(app.multi, CURLMOPT_TIMERFUNCTION, curl_timer);

    app.object = g_object_new(LAPINOU_CLOUD_TYPE_OBJECT, NULL);
    app.bus = dbus_g_bus_get(DBUS_BUS_SYSTEM, &error);
    if (app.bus == NULL)
        die("Failed to open connection to bus", error);
    dbus_g_connection_register_g_object(app.bus, OBJECT_PATH, G_OBJECT(app.object));
    bus_proxy = dbus_g_proxy_new_for_name(app.bus, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus");
    if (!org_freedesktop_DBus_request_name(bus_proxy, SERVICE, 0, &request_result, &error))
        die("Failed to get name", error);
    if (request_result != DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER) {
        g_log(NULL, G_LOG_LEVEL_ERROR, "Got result code %u from requesting name", request_result);
        for (;;)
            ;
    }
    app.led = dbus_g_proxy_new_for_name(app.bus, "com.mindscape.karotz.Led", "/com/mindscape/karotz/Led", KAROTZ_IFACE);
    app.multimedia = dbus_g_proxy_new_for_name(app.bus, "com.mindscape.karotz.Multimedia",
                                               "/com/mindscape/karotz/Multimedia", KAROTZ_IFACE);
    dbus_g_object_register_marshaller(karotz_smarshal_VOID__STRING_INT, G_TYPE_NONE, G_TYPE_STRING, G_TYPE_INT,
                                      G_TYPE_INVALID);
    dbus_g_proxy_add_signal(app.multimedia, "multi_playfinished", G_TYPE_STRING, G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(app.multimedia, "multi_playfinished", G_CALLBACK(on_play_finished), NULL, NULL);
    g_unix_signal_add(SIGTERM, on_term, NULL);
    g_main_loop_run(app.loop);
    cancel_all();
    curl_multi_cleanup(app.multi);
    curl_global_cleanup();
    return 0;
}
