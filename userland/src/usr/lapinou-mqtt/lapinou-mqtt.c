/* SPDX-License-Identifier: MIT */
/*
 * lapinou-mqtt: the rabbit in Home Assistant, a bridge between its daemons on D-Bus and an MQTT
 * broker, with Home Assistant's MQTT discovery. docs/lapinou/mqtt.md describes the topics and
 * payloads; discovery.c makes the discovery messages and the state's JSON.
 *
 * Settings ([mqtt] of lapinou.conf, lapinou_conf.h): host, port (1883), user, prefix (lapinou),
 * discovery_prefix (homeassistant), enabled (1 to connect); the password is the secret
 * mqtt_password, given to the broker as it is and never logged. Without enabled=1 and a host the
 * daemon only applies [audio] volume and waits (get_state "disabled"). Topics are
 * <prefix>/<rabbit_id>/..., rabbit_id being /usr/karotz/uuid.conf.
 *
 * The broker: Eclipse Paho's asynchronous client (libpaho-mqtt3a, no TLS), MQTT 3.1.1, a clean
 * session, keep-alive 20 s, the will <base>/availability "offline" (retained). Paho does its
 * network work (name resolution included) in threads of its own; its callbacks only hand an
 * event to the main loop (g_idle_add), and the main loop calls Paho only to queue a connection,
 * a subscription or a message, so the rabbit's D-Bus traffic never waits on the network. A
 * connection that fails or ends is tried again after 2 s, 5 times, then every 10 s; the count
 * starts again once a connection has held for 10 s. Each connection subscribes to <base>/cmd/#,
 * <base>/assistant (the LapiNou server's assistant state, Task 21) and <discovery_prefix>/status,
 * then publishes (retained) every discovery message, availability "online", the state and the
 * update entity's state; again whenever Home Assistant says "online" on its status topic.
 *
 * Commands that arrive retained (a retained cmd/... replayed at each new subscription) are
 * ignored: a command is done once, when it is sent.
 *
 * The daemons: every call is asynchronous (dbus_g_proxy_begin_call), ids "lapinou-mqtt:<n>".
 * cmd/led, cmd/ears and cmd/say call led-daemon, ears-daemon and voice-daemon (or, in the mode
 * without a server, [cloud] enabled=1 and [server] enabled not 1, LapinouCloud.say when
 * com.mindscape.karotz.LapinouCloud is on the bus, NameOwnerChanged); they are refused, as the
 * web API's web_hardware refuses them, while an application owns the rabbit (the controller's
 * states 21 and 22) or while it sleeps (the web API's sleep flag, or the controller's state 24).
 * cmd/sound, cmd/snapshot and cmd/sleep run the web API's own programs (sound, snapshot, sleep and
 * wakeup), as tag_listener's cgi_run runs them (the user web, from 127.0.0.1): their rules (the
 * URL check, the lock, the playback state, the refusals while asleep or while an application
 * runs) are the web API's, not repeated here; their reply decides the outcome, and the snapshot
 * they store is what is published. A program run that ends with a status other than 0, or is
 * killed, is logged.
 *
 * cmd/volume: the codec's mixer (OSS /dev/mixer, SOUND_MIXER_WRITE_VOLUME) when it has a volume
 * control (SOUND_MIXER_READ_DEVMASK), which sets everything the rabbit plays; otherwise MPD's own
 * volume (its software mixer, mpd.conf), which sets MPD's music only, set in a thread through
 * MPD's protocol on 127.0.0.1:6600 with multimedia-daemon's password. The value is published once
 * applied, kept in [audio] volume (conf.sh's lapinou_set) and applied when the daemon starts; a
 * failure is logged and the state published again with the volume as it was.
 *
 * D-Bus: com.mindscape.karotz.LapinouMqtt (/com/mindscape/karotz/LapinouMqtt, interface
 * com.mindscape.karotz.LapinouMqtt): get_state() -> "disabled", "connecting", "connected" or
 * "disconnected"; set_volume(i) -> b, the web page's volume, as cmd/volume; signal mqtt_state(s)
 * when it changes.
 *
 * The update entity (informational: Home Assistant can't install): installed_version is
 * sys_version (the build's stamp when there is none), latest_version the last update check's
 * (/usr/ota/available, written by the web page's webroot.sh, once a day by cron), read again
 * every tick and published when either changes.
 *
 * For the tests: LAPINOU_UUID_FILE, LAPINOU_WEB_RUN (/var/run/lapinlibre-web), LAPINOU_WEB_DATA
 * (/usr/etc/conf/web), LAPINOU_WEB_CGI (the web API's programs, run directly instead of as web),
 * LAPINOU_WIRELESS (/proc/net/wireless), LAPINOU_SYS_VERSION, LAPINOU_OTA_AVAILABLE and
 * LAPINOU_CONF_SH move what they name
 * (LAPINOU_CONF_DIR moves lapinou.conf, lapinou_conf.h).
 */
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/soundcard.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>

#include <dbus/dbus-glib-bindings.h>
#include <dbus/dbus-glib.h>
#include <glib-object.h>
#include <glib-unix.h>

#include <MQTTAsync.h>

#include "discovery.h"
#include "karotz-common.h"
#include "karotz-messages.h"
#include "karotz-marshal.h"
#include "lapinou_conf.h"
#include "mqtt-json.h"

#define SERVICE        "com.mindscape.karotz.LapinouMqtt"
#define OBJECT_PATH    "/com/mindscape/karotz/LapinouMqtt"
#define KAROTZ_IFACE   "com.mindscape.karotz.KarotzInterface"
#define CLOUD_SERVICE  "com.mindscape.karotz.LapinouCloud"
#define UUID_FILE      "/usr/karotz/uuid.conf"
#define BUILD_FILE     "/karotz/etc/lapinlibre-build"
#define SYS_VERSION    "/usr/etc/conf/sys_version"  /* the release tag installed (R44) */
#define OTA_AVAILABLE  "/usr/ota/available"   /* the last update check's version (webroot.sh ota check) */
#define WEB_RUN        "/var/run/lapinlibre-web"
#define WEB_DATA       "/usr/etc/conf/web"
#define WEB_CGI        "/usr/www/cgi-bin"
#define WEB_USER       "admin"  /* the web API's user, as httpd names it (web.sh WEB_USER) */
#define WIRELESS_FILE  "/proc/net/wireless"
#define CONF_SH        "/usr/share/lapinou/conf.sh"
#define MIXER_DEVICE   "/dev/mixer"
#define KEEPALIVE_S    20
#define RETRY_FAST_S   2
#define RETRY_FAST_N   5
#define RETRY_SLOW_S   10
#define HOLD_S         10   /* a connection that held this long resets the retry count */
#define TICK_S         30   /* Wi-Fi signal and sleep state read again */
#define CALL_MS        10000
#define JOB_KILL_S     60   /* a program run for a command that takes longer is killed */
#define JOB_OUT_MAX    4096
#define MPD_PORT       6600
#define MPD_PASSWORD   "karotz_admin"  /* multimedia-daemon's (mpd.conf) */
#define PICTURE_MAX    (1024 * 1024)
#define PAYLOAD_MAX    4096
#define EAR_SPEED      100  /* the web API's speed */
#define CONTROLLER_ASLEEP 24

typedef enum { MQTT_DISABLED, MQTT_CONNECTING, MQTT_CONNECTED, MQTT_DISCONNECTED } MqttState;
static const char *const STATE_NAMES[] = { "disabled", "connecting", "connected", "disconnected" };

/* --- the D-Bus object ---------------------------------------------------------------------- */

typedef struct {
    GObject parent;
} LapinouMqttObject;

typedef struct {
    GObjectClass parent;
    guint state_signal;
} LapinouMqttObjectClass;

#define LAPINOU_MQTT_TYPE_OBJECT (lapinou_mqtt_object_get_type())
#define LAPINOU_MQTT_OBJECT_GET_CLASS(o) \
    (G_TYPE_INSTANCE_GET_CLASS((o), LAPINOU_MQTT_TYPE_OBJECT, LapinouMqttObjectClass))
GType lapinou_mqtt_object_get_type(void);
G_DEFINE_TYPE(LapinouMqttObject, lapinou_mqtt_object, G_TYPE_OBJECT)

static gboolean lapinou_mqtt_object_get_state(LapinouMqttObject *self, char **state, GError **error);
static gboolean lapinou_mqtt_object_set_volume(LapinouMqttObject *self, gint volume, gboolean *ok, GError **error);
#include "lapinou-mqtt-glue.h"

/* --- the daemon's state -------------------------------------------------------------------- */

static struct {
    LapinouMqttObject *object;
    DBusGConnection *bus;
    DBusGProxy *bus_proxy, *led, *ears, *button, *rfid, *voice, *controller, *cloud;
    GMainLoop *loop;

    /* settings */
    char *host, *user, *password, *prefix, *discovery, *uri, *language;
    int port;
    gboolean serverless;
    const char *uuid_file, *web_run, *web_data, *web_cgi, *wireless_file, *conf_sh, *sys_version_file, *available_file;

    /* the broker */
    MQTTAsync client;
    MqttState state;
    int attempt;    /* the current connection attempt; stale callbacks carry an older one */
    int failures;
    guint retry_source;
    char *rabbit_id, *base, *client_id, *will_topic;
    char *last_state;
    gboolean stopping;

    /* the rabbit */
    RabbitState rabbit;
    char last_on[7];      /* the LED colour {"on":true} turns on again */
    char *version, *assistant, *last_tag;
    char *installed, *latest;  /* the update entity: sys_version, and the last check's version */
    int controller_state; /* -1 until known */
    gboolean web_asleep;
    gboolean cloud_present;
    gboolean snapshot_running;
    gboolean mpd_busy;
    int mpd_pending;      /* a volume waiting for MPD (-1: none) */
    guint call_count;
} app;

/* --- small helpers ------------------------------------------------------------------------- */

static char *first_line(const char *path)
{
    char *text = NULL, *nl;

    if (!g_file_get_contents(path, &text, NULL, NULL))
        return NULL;
    nl = strchr(text, '\n');
    if (nl)
        *nl = '\0';
    g_strstrip(text);
    if (*text == '\0') {
        g_free(text);
        return NULL;
    }
    return text;
}

static const char *env_or(const char *name, const char *def)
{
    const char *v = getenv(name);

    return v && *v ? v : def;
}

static gboolean is_color(const char *s)
{
    int i;

    for (i = 0; i < 6; i++)
        if (!g_ascii_isxdigit(s[i]))
            return FALSE;
    return s[6] == '\0';
}

static void set_color(char *dest, const char *src)
{
    int i;

    for (i = 0; i < 6; i++)
        dest[i] = g_ascii_toupper(src[i]);
    dest[6] = '\0';
}

/* A topic part from the settings: not empty, no wildcards, no leading or trailing '/'. */
static gboolean valid_topic_part(const char *s)
{
    size_t n = strlen(s);

    return n > 0 && n <= 128 && !strpbrk(s, "+#") && s[0] != '/' && s[n - 1] != '/' &&
           g_utf8_validate(s, -1, NULL);
}

static gboolean valid_host(const char *s)
{
    const char *p;

    if (*s == '\0' || strlen(s) > 253)
        return FALSE;
    for (p = s; *p; p++)
        if (!g_ascii_isalnum(*p) && !strchr(".-_:[]", *p))
            return FALSE;
    return TRUE;
}

/* A rabbit id that can go into topics and Home Assistant's node id: [A-Za-z0-9_-], 1 to 64. */
static gboolean valid_rabbit_id(const char *s)
{
    const char *p;

    for (p = s; *p; p++)
        if (!g_ascii_isalnum(*p) && *p != '_' && *p != '-')
            return FALSE;
    return p > s && p - s <= 64;
}

static char *next_id(void)
{
    return g_strdup_printf("lapinou-mqtt:%u", ++app.call_count);
}

static void set_state(MqttState state)
{
    if (app.state == state)
        return;
    app.state = state;
    g_signal_emit(app.object, LAPINOU_MQTT_OBJECT_GET_CLASS(app.object)->state_signal, 0, STATE_NAMES[state]);
}

/* --- publishing ---------------------------------------------------------------------------- */

static void publish(const char *topic, const void *payload, int len, int qos, int retained)
{
    MQTTAsync_message m = MQTTAsync_message_initializer;
    MQTTAsync_responseOptions r = MQTTAsync_responseOptions_initializer;
    int rc;

    if (app.state != MQTT_CONNECTED)
        return;
    m.payload = (void *)payload;
    m.payloadlen = len;
    m.qos = qos;
    m.retained = retained;
    rc = MQTTAsync_sendMessage(app.client, topic, &m, &r); /* Paho copies the topic and payload */
    if (rc != MQTTASYNC_SUCCESS)
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "mqtt: %s not sent: %s", topic, MQTTAsync_strerror(rc));
}

static void publish_text(const char *rest, const char *text, int qos, int retained)
{
    char *topic = g_strdup_printf("%s/%s", app.base, rest);

    publish(topic, text, (int)strlen(text), qos, retained);
    g_free(topic);
}

static void publish_state(gboolean force)
{
    char *json;

    app.rabbit.asleep = app.web_asleep || app.controller_state == CONTROLLER_ASLEEP;
    app.rabbit.assistant = app.assistant;
    app.rabbit.version = app.version;
    app.rabbit.last_tag = app.last_tag;
    json = state_json(&app.rabbit);
    if (app.state != MQTT_CONNECTED || (!force && app.last_state && strcmp(app.last_state, json) == 0)) {
        g_free(json);
        return;
    }
    publish_text("state", json, 1, 1);
    g_free(app.last_state);
    app.last_state = json;
}

static void publish_all(void)
{
    DiscoveryContext c = { app.prefix, app.discovery, app.rabbit_id, app.version };
    GPtrArray *messages = discovery_messages(&c);
    char *update;
    guint i;

    for (i = 0; i < messages->len; i++) {
        DiscoveryMessage *m = g_ptr_array_index(messages, i);
        publish(m->topic, m->payload, (int)strlen(m->payload), 1, 1);
    }
    g_ptr_array_free(messages, TRUE);
    publish_text("availability", "online", 1, 1);
    publish_state(TRUE);
    update = update_json(app.installed, app.latest);
    publish_text("update", update, 1, 1);
    g_free(update);
}

/* The update entity's versions read again: published when they changed. */
static void read_versions(void)
{
    char *installed = first_line(app.sys_version_file), *latest = first_line(app.available_file), *update;
    gboolean changed;

    if (!installed)
        installed = g_strdup(app.version);
    changed = g_strcmp0(installed, app.installed) != 0 || g_strcmp0(latest, app.latest) != 0;
    g_free(app.installed);
    g_free(app.latest);
    app.installed = installed;
    app.latest = latest;
    if (changed && app.state == MQTT_CONNECTED) {
        update = update_json(app.installed, app.latest);
        publish_text("update", update, 1, 1);
        g_free(update);
    }
}

/* --- the rabbit's state from files --------------------------------------------------------- */

static void read_wifi(void)
{
    char *text = NULL, *line;

    app.rabbit.has_rssi = FALSE;
    if (!g_file_get_contents(app.wireless_file, &text, NULL, NULL))
        return;
    line = strstr(text, "wlan0:");
    if (line) {
        double status, link, level;
        if (sscanf(line + 6, "%lf %lf %lf", &status, &link, &level) == 3 && level > -256 && level < 0) {
            app.rabbit.has_rssi = TRUE;
            app.rabbit.wifi_rssi = (int)level;
        }
    }
    g_free(text);
}

static void read_web_sleep(void)
{
    char *path = g_build_filename(app.web_run, "sleep", NULL);
    char *line = first_line(path);
    const char *p;

    app.web_asleep = FALSE;
    if (line) {
        app.web_asleep = TRUE;
        for (p = line; *p; p++)
            if (!g_ascii_isdigit(*p))
                app.web_asleep = FALSE;
    }
    g_free(line);
    g_free(path);
}

static void read_web_led(void)
{
    char *path = g_build_filename(app.web_run, "led", NULL);
    char *line = first_line(path);

    set_color(app.rabbit.led, "00FF00"); /* the web API's own default */
    if (line && strlen(line) >= 6) {
        line[6] = '\0';
        if (is_color(line))
            set_color(app.rabbit.led, line);
    }
    memcpy(app.last_on, strcmp(app.rabbit.led, "000000") ? app.rabbit.led : "00FF00", 7);
    g_free(line);
    g_free(path);
}

static gboolean tick(gpointer data)
{
    (void)data;
    read_wifi();
    read_web_sleep();
    publish_state(FALSE);
    read_versions();
    return TRUE;
}

/* --- programs run for a command ------------------------------------------------------------ */

typedef struct Job Job;
typedef void (*JobDone)(Job *job);

struct Job {
    char *name;      /* for the log */
    GPid pid;
    GString *out;    /* its standard output (at most JOB_OUT_MAX bytes), when captured */
    gboolean eof, exited, ok;
    int status;
    guint kill_source, out_source;
    JobDone done;
    gpointer data;
};

static void job_finish(Job *j)
{
    if (!j->eof || !j->exited)
        return;
    if (j->kill_source)
        g_source_remove(j->kill_source);
    j->ok = WIFEXITED(j->status) && WEXITSTATUS(j->status) == 0;
    if (WIFEXITED(j->status) && WEXITSTATUS(j->status) != 0)
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "%s ended with status %d", j->name, WEXITSTATUS(j->status));
    else if (WIFSIGNALED(j->status))
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "%s was killed by signal %d", j->name, WTERMSIG(j->status));
    if (j->done)
        j->done(j);
    g_string_free(j->out, TRUE);
    g_free(j->name);
    g_free(j);
}

static void job_exited(GPid pid, gint status, gpointer data)
{
    Job *j = data;

    g_spawn_close_pid(pid);
    j->status = status;
    j->exited = TRUE;
    job_finish(j);
}

static gboolean job_output(GIOChannel *channel, GIOCondition condition, gpointer data)
{
    Job *j = data;
    char buf[1024];
    gsize n = 0;
    GIOStatus s = G_IO_STATUS_EOF;

    if (condition & G_IO_IN) {
        s = g_io_channel_read_chars(channel, buf, sizeof buf, &n, NULL);
        if (n && j->out->len < JOB_OUT_MAX)
            g_string_append_len(j->out, buf, (gssize)MIN(n, JOB_OUT_MAX - j->out->len));
        if (s == G_IO_STATUS_NORMAL || s == G_IO_STATUS_AGAIN)
            return TRUE;
    }
    j->eof = TRUE;
    j->out_source = 0;
    job_finish(j);
    return FALSE;
}

static gboolean job_kill(gpointer data)
{
    Job *j = data;

    j->kill_source = 0;
    if (j->exited) {
        /* Only its output is still open (a child of its own holds it): what came is enough. */
        g_source_remove(j->out_source);
        j->eof = TRUE;
        job_finish(j);
        return FALSE;
    }
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "%s still runs after %d s: killed", j->name, JOB_KILL_S);
    kill(j->pid, SIGKILL);
    return FALSE;
}

/* Run argv (with envp, or the daemon's environment for NULL); its standard output is kept when
 * `capture`; `done` (may be NULL) once it has ended and its output is read. */
static void job_start(const char *name, char **argv, char **envp, gboolean capture, JobDone done, gpointer data)
{
    GError *error = NULL;
    Job *j = g_new0(Job, 1);
    int out_fd = -1;

    j->name = g_strdup(name);
    j->out = g_string_new(NULL);
    j->done = done;
    j->data = data;
    if (!g_spawn_async_with_pipes(NULL, argv, envp,
                                  G_SPAWN_DO_NOT_REAP_CHILD | G_SPAWN_STDERR_TO_DEV_NULL |
                                      (capture ? 0 : G_SPAWN_STDOUT_TO_DEV_NULL),
                                  NULL, NULL, &j->pid, NULL, capture ? &out_fd : NULL, NULL, &error)) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "%s can't be run: %s", name, error->message);
        g_error_free(error);
        j->eof = j->exited = TRUE;
        j->status = 127 << 8;
        job_finish(j);
        return;
    }
    if (capture) {
        GIOChannel *channel = g_io_channel_unix_new(out_fd);
        g_io_channel_set_encoding(channel, NULL, NULL);
        g_io_channel_set_buffered(channel, FALSE);
        g_io_channel_set_flags(channel, G_IO_FLAG_NONBLOCK, NULL);
        g_io_channel_set_close_on_unref(channel, TRUE);
        j->out_source = g_io_add_watch(channel, G_IO_IN | G_IO_HUP | G_IO_ERR, job_output, j);
        g_io_channel_unref(channel);
    } else {
        j->eof = TRUE;
    }
    j->kill_source = g_timeout_add_seconds(JOB_KILL_S, job_kill, j);
    g_child_watch_add(j->pid, job_exited, j);
}

/* Run the web API's program `endpoint` with `query` (URL-encoded) as tag_listener's cgi_run does:
 * as the user web, from 127.0.0.1, as the web API's user, with no other environment. */
static void cgi_run(const char *endpoint, const char *query, JobDone done, gpointer data)
{
    char *cgi = g_build_filename(app.web_cgi, endpoint, NULL);
    char *name = g_strdup_printf("the web API's %s", endpoint);
    char *qs = g_strdup_printf("QUERY_STRING=%s", query);
    char *script = g_strdup_printf("SCRIPT_NAME=/cgi-bin/%s", endpoint);
    char *uri = g_strdup_printf("REQUEST_URI=/cgi-bin/%s", endpoint);
    char *envp[] = { "PATH=/usr/bin:/usr/sbin:/bin:/sbin", "REQUEST_METHOD=GET", qs, script, uri,
                     "REMOTE_USER=" WEB_USER, "REMOTE_ADDR=127.0.0.1", "GATEWAY_INTERFACE=CGI/1.1", NULL };
    char *as_web[] = { "/bin/busybox", "setuidgid", "web", cgi, NULL };
    char *direct[] = { cgi, NULL };

    /* The tests' programs (LAPINOU_WEB_CGI) are run as they are. */
    job_start(name, getenv("LAPINOU_WEB_CGI") ? direct : as_web, envp, TRUE, done, data);
    g_free(cgi);
    g_free(name);
    g_free(qs);
    g_free(script);
    g_free(uri);
}

/* A web API program's reply (its body, after the CGI headers) as a flat JSON object, or NULL. On
 * a reply that isn't {"return":"0",...}, its message is logged under `what`. */
static GHashTable *cgi_reply(Job *j, const char *what)
{
    const char *body = strstr(j->out->str, "\r\n\r\n");
    GHashTable *o;
    const JsonValue *r, *msg;

    body = body ? body + 4 : (body = strstr(j->out->str, "\n\n")) ? body + 2 : NULL;
    o = body && g_utf8_validate(body, -1, NULL) ? json_parse_flat_object(body, strlen(body)) : NULL;
    r = o ? json_get(o, "return", JSON_STRING) : NULL;
    if (r && !strcmp(r->string, "0"))
        return o;
    msg = o ? json_get(o, "msg", JSON_STRING) : NULL;
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "%s refused by %s: %s", what, j->name, msg ? msg->string : "no answer");
    if (o)
        g_hash_table_destroy(o);
    return NULL;
}

/* `s` with every byte but A-Z a-z 0-9 - . _ ~ as %XX, for a query string. */
static char *query_escape(const char *s)
{
    GString *out = g_string_new(NULL);
    const unsigned char *p;

    for (p = (const unsigned char *)s; *p; p++) {
        if (g_ascii_isalnum(*p) || strchr("-._~", *p))
            g_string_append_c(out, (char)*p);
        else
            g_string_append_printf(out, "%%%02X", *p);
    }
    return g_string_free(out, FALSE);
}

/* --- the volume ---------------------------------------------------------------------------- */

static void volume_set(int volume, gboolean keep);

/* The codec's mixer, when it has a volume control: its descriptor, else -1. */
static int mixer_open(void)
{
    int fd = open(MIXER_DEVICE, O_RDWR | O_NONBLOCK), mask = 0;

    if (fd < 0)
        return -1;
    if (ioctl(fd, SOUND_MIXER_READ_DEVMASK, &mask) != 0 || !(mask & SOUND_MASK_VOLUME)) {
        close(fd);
        return -1;
    }
    return fd;
}

static void volume_keep(int volume)
{
    char value[8];
    char *argv[] = { "/bin/sh", "-c", ". \"$1\" && lapinou_set audio volume \"$2\"", "sh", (char *)app.conf_sh,
                     value, NULL };
    /* The daemon's environment (LAPINOU_CONF_DIR in the tests) with the system's commands. */
    char **envp = g_environ_setenv(g_get_environ(), "PATH", "/usr/bin:/usr/sbin:/bin:/sbin", TRUE);

    g_snprintf(value, sizeof value, "%d", volume);
    job_start("lapinou_set audio volume", argv, envp, FALSE, NULL, NULL);
    g_strfreev(envp);
}

/* A volume applied (or not): the state published either way, so Home Assistant shows the real
 * value; kept in [audio] volume when `keep`. */
static void volume_applied(int volume, gboolean ok, gboolean keep)
{
    if (ok) {
        app.rabbit.volume = volume;
        if (keep)
            volume_keep(volume);
        publish_state(FALSE);
    } else {
        publish_state(TRUE);
    }
}

/* MPD's volume, through its protocol, in a thread: set (volume >= 0) or read (volume -1). */
typedef struct {
    int volume;
    gboolean keep, ok;
    char why[128];
} MpdJob;

static gboolean mpd_line(int fd, char *line, size_t size)
{
    size_t n = 0;
    char c;

    while (n + 1 < size) {
        if (read(fd, &c, 1) != 1)
            return FALSE;
        if (c == '\n')
            break;
        line[n++] = c;
    }
    line[n] = '\0';
    return TRUE;
}

/* Send `command`, then read lines until OK or ACK; a "volume: N" line read on the way into *volume. */
static gboolean mpd_command(int fd, const char *command, int *volume, char *why, size_t why_size)
{
    char line[256];
    size_t n = strlen(command);

    if (write(fd, command, n) != (ssize_t)n) {
        g_snprintf(why, why_size, "MPD can't be written to: %s", strerror(errno));
        return FALSE;
    }
    for (;;) {
        if (!mpd_line(fd, line, sizeof line)) {
            g_snprintf(why, why_size, "MPD didn't answer");
            return FALSE;
        }
        if (!strcmp(line, "OK"))
            return TRUE;
        if (g_str_has_prefix(line, "ACK")) {
            g_snprintf(why, why_size, "MPD refused: %s", line);
            return FALSE;
        }
        if (volume && g_str_has_prefix(line, "volume: "))
            *volume = atoi(line + 8);
    }
}

static gboolean mpd_done(gpointer data)
{
    MpdJob *j = data;

    app.mpd_busy = FALSE;
    if (!j->ok)
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "the volume (MPD): %s", j->why);
    if (j->ok && j->volume >= 0 && j->volume <= 100)
        volume_applied(j->volume, TRUE, j->keep);
    else
        volume_applied(-1, FALSE, FALSE);
    g_free(j);
    if (app.mpd_pending >= 0) {
        int v = app.mpd_pending;
        app.mpd_pending = -1;
        volume_set(v, TRUE);
    }
    return FALSE;
}

static gpointer mpd_thread(gpointer data)
{
    MpdJob *j = data;
    struct sockaddr_in a;
    struct timeval timeout = { 3, 0 };
    char line[256], command[32];
    int fd = socket(AF_INET, SOCK_STREAM, 0), got = -1;

    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_port = htons(MPD_PORT);
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (fd < 0 || setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout) ||
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof timeout) || connect(fd, (struct sockaddr *)&a, sizeof a)) {
        g_snprintf(j->why, sizeof j->why, "no MPD on 127.0.0.1:%d: %s", MPD_PORT, strerror(errno));
    } else if (!mpd_line(fd, line, sizeof line) || !g_str_has_prefix(line, "OK MPD")) {
        g_snprintf(j->why, sizeof j->why, "MPD didn't greet");
    } else if (mpd_command(fd, "password " MPD_PASSWORD "\n", NULL, j->why, sizeof j->why)) {
        if (j->volume >= 0) {
            g_snprintf(command, sizeof command, "setvol %d\n", j->volume);
            j->ok = mpd_command(fd, command, NULL, j->why, sizeof j->why);
        } else if (mpd_command(fd, "status\n", &got, j->why, sizeof j->why)) {
            j->volume = got;
            j->ok = got >= 0;
            if (!j->ok)
                g_snprintf(j->why, sizeof j->why, "MPD's status has no volume");
        }
        if (write(fd, "close\n", 6) < 0) {
        }
    }
    if (fd >= 0)
        close(fd);
    g_idle_add(mpd_done, j);
    return NULL;
}

static void mpd_start(int volume, gboolean keep)
{
    MpdJob *j = g_new0(MpdJob, 1);
    GThread *t;

    j->volume = volume;
    j->keep = keep;
    app.mpd_busy = TRUE;
    t = g_thread_try_new("mpd-volume", mpd_thread, j, NULL);
    if (t) {
        g_thread_unref(t);
    } else {
        g_snprintf(j->why, sizeof j->why, "no thread");
        mpd_done(j);
    }
}

/* Apply a volume: the codec's mixer when it has a volume control, else MPD's. */
static void volume_set(int volume, gboolean keep)
{
    int fd = mixer_open(), value = volume | (volume << 8);
    gboolean ok;

    if (fd >= 0) {
        ok = ioctl(fd, SOUND_MIXER_WRITE_VOLUME, &value) == 0;
        if (!ok)
            g_log(NULL, G_LOG_LEVEL_MESSAGE, "the volume (%s): %s", MIXER_DEVICE, strerror(errno));
        close(fd);
        volume_applied(volume, ok, keep);
        return;
    }
    if (app.mpd_busy) {
        app.mpd_pending = volume;
        return;
    }
    mpd_start(volume, keep);
}

static void volume_start(void)
{
    char *text = lapinou_conf_get("audio", "volume", NULL), *end = NULL;
    long v = -1;
    int fd;

    app.rabbit.volume = -1;
    app.mpd_pending = -1;
    if (text) {
        v = strtol(text, &end, 10);
        if (*end != '\0' || v < 0 || v > 100) {
            g_log(NULL, G_LOG_LEVEL_MESSAGE, "[audio] volume '%s' is not a number from 0 to 100", text);
            v = -1;
        }
    }
    g_free(text);
    fd = mixer_open();
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "the volume: %s", fd >= 0 ? "the codec's mixer" : "MPD's (no volume control in the mixer)");
    if (v >= 0) {
        if (fd >= 0)
            close(fd);
        volume_set((int)v, FALSE);
    } else if (fd >= 0) {
        int value = 0;
        if (ioctl(fd, SOUND_MIXER_READ_VOLUME, &value) == 0)
            app.rabbit.volume = CLAMP(value & 0xFF, 0, 100);
        close(fd);
    } else {
        mpd_start(-1, FALSE);
    }
}

/* --- D-Bus calls --------------------------------------------------------------------------- */

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
#define CALL(proxy, what, method, ...)                                                                        \
    dbus_g_proxy_begin_call_with_timeout((proxy), (method), call_done, (gpointer)(what), NULL, CALL_MS,     \
                                         __VA_ARGS__, G_TYPE_INVALID)

static void cloud_said(DBusGProxy *proxy, DBusGProxyCall *call, gpointer data)
{
    GError *error = NULL;

    (void)data;
    if (!dbus_g_proxy_end_call(proxy, call, &error, G_TYPE_INVALID)) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "LapinouCloud.say failed: %s", error->message);
        g_error_free(error);
    }
}

/* Why the rabbit can't be driven now (NULL when it can), as the web API's web_hardware decides. */
static const char *busy(void)
{
    if (app.controller_state == 21 || app.controller_state == 22)
        return "an application is running on the rabbit";
    if (app.web_asleep || app.controller_state == CONTROLLER_ASLEEP)
        return "the rabbit is asleep";
    return NULL;
}

/* --- the commands -------------------------------------------------------------------------- */

static void cmd_led(GHashTable *o, const char *text)
{
    const JsonValue *color = o ? json_get(o, "color", JSON_STRING) : NULL;
    const JsonValue *pulse = o ? json_get(o, "pulse", JSON_STRING) : NULL;
    const JsonValue *period = o ? json_get(o, "period_ms", JSON_NUMBER) : NULL;
    const JsonValue *on = o ? json_get(o, "on", JSON_BOOL) : NULL;
    char c[7], c2[7];
    char *id;
    int ms = 700;

    if (o == NULL && is_color(text))
        set_color(c, text);
    else if (color && is_color(color->string))
        set_color(c, color->string);
    else if (on && on->truth)
        memcpy(c, app.last_on, 7);
    else {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "cmd/led: no colour (RRGGBB) in the command");
        return;
    }
    if (pulse && !is_color(pulse->string)) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "cmd/led: pulse is not a colour (RRGGBB)");
        return;
    }
    if (period) {
        if (!(period->number >= 25 && period->number <= 10000)) {
            g_log(NULL, G_LOG_LEVEL_MESSAGE, "cmd/led: period_ms must be from 25 to 10000");
            return;
        }
        ms = (int)period->number;
    }
    id = next_id();
    if (pulse) {
        set_color(c2, pulse->string);
        CALL(app.led, "Led.pulse", "pulse", G_TYPE_STRING, id, G_TYPE_STRING, c, G_TYPE_STRING, c2, G_TYPE_INT, ms,
             G_TYPE_INT, LED_FOREVER);
    } else {
        CALL(app.led, "Led.light", "light", G_TYPE_STRING, id, G_TYPE_STRING, c);
    }
    g_free(id);
    memcpy(app.rabbit.led, c, 7);
    if (strcmp(c, "000000") != 0)
        memcpy(app.last_on, c, 7);
}

static gboolean ear_value(const JsonValue *v, int *out)
{
    if (!v || v->number < 0 || v->number > 16 || v->number != floor(v->number))
        return FALSE;
    *out = (int)v->number;
    return TRUE;
}

static void cmd_ears(GHashTable *o)
{
    const JsonValue *reset = o ? json_get(o, "reset", JSON_BOOL) : NULL;
    const JsonValue *preset = o ? json_get(o, "preset", JSON_STRING) : NULL;
    int left, right;
    char *id;

    if (reset && reset->truth) {
        id = next_id();
        CALL(app.ears, "Ears.reset", "reset", G_TYPE_STRING, id, G_TYPE_STRING, "B");
        g_free(id);
        app.rabbit.ear_left = app.rabbit.ear_right = 0;
        return;
    }
    if (preset) {
        const EarPreset *p = ear_preset_find(preset->string);
        if (!p) {
            g_log(NULL, G_LOG_LEVEL_MESSAGE, "cmd/ears: no preset is called '%s'", preset->string);
            return;
        }
        left = p->left;
        right = p->right;
    } else if (o && ear_value(json_get(o, "left", JSON_NUMBER), &left) &&
               ear_value(json_get(o, "right", JSON_NUMBER), &right)) {
    } else {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "cmd/ears: give left and right (0 to 16), a preset or reset");
        return;
    }
    id = next_id();
    CALL(app.ears, "Ears.move", "move", G_TYPE_STRING, id, G_TYPE_INT, left, G_TYPE_INT, EAR_SPEED, G_TYPE_INT, right,
         G_TYPE_INT, EAR_SPEED);
    g_free(id);
    app.rabbit.ear_left = left;
    app.rabbit.ear_right = right;
}

static void cmd_say(const char *text)
{
    char *id;

    if (*text == '\0' || strlen(text) > 1024) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "cmd/say: the text must have 1 to 1024 bytes");
        return;
    }
    if (app.serverless && app.cloud_present) {
        dbus_g_proxy_begin_call_with_timeout(app.cloud, "say", cloud_said, NULL, NULL, CALL_MS, G_TYPE_STRING, text,
                                             G_TYPE_INVALID);
        return;
    }
    id = next_id();
    CALL(app.voice, "Voice.tts_speak", "tts_speak", G_TYPE_STRING, id, G_TYPE_STRING, text, G_TYPE_STRING,
         app.language, G_TYPE_INT, 1);
    g_free(id);
}

static void sound_done(Job *j)
{
    GHashTable *o = cgi_reply(j, "cmd/sound");

    if (o)
        g_hash_table_destroy(o);
}

/* An http(s) URL or a sound's name: the web API's sound program checks and plays it (it stops
 * what plays, takes the lock and keeps the playback state). */
static void cmd_sound(const char *text)
{
    gboolean url = g_str_has_prefix(text, "http://") || g_str_has_prefix(text, "https://");
    char *value = query_escape(text);
    char *query = g_strdup_printf("%s=%s", url ? "url" : "id", value);

    cgi_run("sound", query, sound_done, NULL);
    g_free(value);
    g_free(query);
}

static void cmd_volume(const char *text)
{
    char *end = NULL;
    double v = g_ascii_strtod(text, &end);

    if (end == text || *end != '\0' || !(v >= 0 && v <= 100)) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "cmd/volume: a number from 0 to 100");
        publish_state(TRUE);
        return;
    }
    volume_set((int)floor(v + 0.5), TRUE);
}

static gboolean ears_disabled(void)
{
    char *path = g_build_filename(app.web_data, "ears_disabled", NULL);
    gboolean disabled = g_file_test(path, G_FILE_TEST_EXISTS);

    g_free(path);
    return disabled;
}

/* The web API's sleep or wakeup has answered: the LED and the ears as it left them (asleep: the
 * LED off and the ears at 4; awake: the web API's LED state and the ears recalibrated, unless its
 * ear moves are off). */
static void slept(Job *j)
{
    gboolean on = GPOINTER_TO_INT(j->data);
    GHashTable *o = cgi_reply(j, on ? "cmd/sleep on" : "cmd/sleep off");

    if (o) {
        if (on) {
            memcpy(app.rabbit.led, "000000", 7);
            if (!ears_disabled())
                app.rabbit.ear_left = app.rabbit.ear_right = 4;
        } else {
            read_web_led();
            if (!ears_disabled())
                app.rabbit.ear_left = app.rabbit.ear_right = 0;
        }
        g_hash_table_destroy(o);
    }
    read_web_sleep();
    publish_state(o == NULL); /* refused: published again, so Home Assistant's switch comes back */
}

static void cmd_sleep(const char *text)
{
    gboolean on;

    if (!g_ascii_strcasecmp(text, "on") || !strcmp(text, "1") || !g_ascii_strcasecmp(text, "true"))
        on = TRUE;
    else if (!g_ascii_strcasecmp(text, "off") || !strcmp(text, "0") || !g_ascii_strcasecmp(text, "false"))
        on = FALSE;
    else {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "cmd/sleep: on or off");
        publish_state(TRUE);
        return;
    }
    cgi_run(on ? "sleep" : "wakeup", "", slept, GINT_TO_POINTER(on));
}

/* The web API's snapshot has answered: the picture it stored ($WEB_RUN/snapshots/<filename>). */
static void snapshot_done(Job *j)
{
    GHashTable *o = cgi_reply(j, "cmd/snapshot");
    const JsonValue *name = o ? json_get(o, "filename", JSON_STRING) : NULL;
    char *path, *picture = NULL, *topic;
    gsize len = 0;
    const char *p;

    app.snapshot_running = FALSE;
    if (!o)
        return;
    for (p = name ? name->string : ""; *p && (g_ascii_isalnum(*p) || *p == '_' || *p == '.'); p++)
        ;
    if (!name || *p || !*name->string || strstr(name->string, "..")) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "cmd/snapshot: the web API named no picture");
        g_hash_table_destroy(o);
        return;
    }
    path = g_build_filename(app.web_run, "snapshots", name->string, NULL);
    if (!g_file_get_contents(path, &picture, &len, NULL) || len == 0 || len > PICTURE_MAX) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "cmd/snapshot: the picture %s can't be read", path);
    } else {
        topic = g_strdup_printf("%s/snapshot", app.base);
        publish(topic, picture, (int)len, 0, 0);
        g_free(topic);
    }
    g_free(picture);
    g_free(path);
    g_hash_table_destroy(o);
}

static void cmd_snapshot(void)
{
    if (app.snapshot_running) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "cmd/snapshot: a picture is being taken already");
        return;
    }
    app.snapshot_running = TRUE;
    cgi_run("snapshot", "", snapshot_done, NULL);
}

/* A command's payload: `text` (NUL-terminated, valid UTF-8, trimmed), `o` its JSON object or NULL. */
static void command(const char *name, const char *text, GHashTable *o)
{
    const char *why;

    if (!strcmp(name, "volume"))
        cmd_volume(text);
    else if (!strcmp(name, "sleep"))
        cmd_sleep(text);
    else if (!strcmp(name, "snapshot"))
        cmd_snapshot();
    else if (!strcmp(name, "sound"))
        cmd_sound(text);
    else if (strcmp(name, "led") && strcmp(name, "ears") && strcmp(name, "say")) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "cmd/%s: no such command", name);
        return;
    } else if ((why = busy()) != NULL) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "cmd/%s refused: %s", name, why);
        publish_state(TRUE);
        return;
    } else if (!strcmp(name, "led"))
        cmd_led(o, text);
    else if (!strcmp(name, "ears"))
        cmd_ears(o);
    else
        cmd_say(text);
    publish_state(FALSE);
}

static void message(const char *topic, const char *payload, int len, gboolean retained)
{
    char *cmd_prefix = g_strdup_printf("%s/cmd/", app.base);
    char *assistant = g_strdup_printf("%s/assistant", app.base);
    char *status = g_strdup_printf("%s/status", app.discovery);
    char *text;

    if (len > PAYLOAD_MAX || memchr(payload, '\0', (size_t)len) || !g_utf8_validate(payload, len, NULL)) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "%s: payload ignored (not text, or over %d bytes)", topic, PAYLOAD_MAX);
        goto out;
    }
    text = g_strstrip(g_strndup(payload, (gsize)len));
    if (!strcmp(topic, status)) {
        if (!strcmp(text, "online"))
            publish_all();
    } else if (!strcmp(topic, assistant)) {
        if (strlen(text) <= 64 && !strpbrk(text, "\n\r\t")) {
            g_free(app.assistant);
            app.assistant = g_strdup(*text ? text : "hors ligne");
            publish_state(FALSE);
        }
    } else if (g_str_has_prefix(topic, cmd_prefix) && retained) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "%s: a retained command, ignored (commands are done when sent)", topic);
    } else if (g_str_has_prefix(topic, cmd_prefix) && !strchr(topic + strlen(cmd_prefix), '/')) {
        GHashTable *o = text[0] == '{' ? json_parse_flat_object(text, strlen(text)) : NULL;
        if (text[0] == '{' && !o)
            g_log(NULL, G_LOG_LEVEL_MESSAGE, "%s: not a JSON object of strings, numbers and booleans", topic);
        else
            command(topic + strlen(cmd_prefix), text, o);
        if (o)
            g_hash_table_destroy(o);
    }
    g_free(text);
out:
    g_free(cmd_prefix);
    g_free(assistant);
    g_free(status);
}

/* --- the daemons' signals ------------------------------------------------------------------ */

static void button_event(const char *type)
{
    char *json = g_strdup_printf("{\"type\":\"%s\"}", type);

    publish_text("event/button", json, 0, 0);
    g_free(json);
}

static void on_click(DBusGProxy *p, gpointer d) { (void)p; (void)d; button_event("click"); }
static void on_dclick(DBusGProxy *p, gpointer d) { (void)p; (void)d; button_event("dclick"); }
static void on_tclick(DBusGProxy *p, gpointer d) { (void)p; (void)d; button_event("tclick"); }
static void on_lclick_start(DBusGProxy *p, gpointer d) { (void)p; (void)d; button_event("lclick_start"); }
static void on_lclick_stop(DBusGProxy *p, gpointer d) { (void)p; (void)d; button_event("lclick_stop"); }

static void tag_event(const char *uid, const char *action)
{
    GString *json = g_string_new("{\"tag\":");

    json_append_string(json, g_utf8_validate(uid, -1, NULL) ? uid : "?");
    g_string_append_printf(json, ",\"action\":\"%s\"}", action);
    publish_text("event/rfid", json->str, 0, 0);
    g_string_free(json, TRUE);
}

static void on_tag_in(DBusGProxy *p, const char *uid, const char *blocks, int app_code, int type, int pict, int color,
                      gpointer d)
{
    (void)p; (void)blocks; (void)app_code; (void)type; (void)pict; (void)color; (void)d;
    tag_event(uid, "in");
    if (g_utf8_validate(uid, -1, NULL)) {
        g_free(app.last_tag);
        app.last_tag = g_strdup(uid);
        publish_state(FALSE);
    }
}

static void on_tag_out(DBusGProxy *p, const char *uid, gpointer d)
{
    (void)p; (void)d;
    tag_event(uid, "out");
}

static void on_controller_state(DBusGProxy *p, int state, gpointer d)
{
    (void)p; (void)d;
    app.controller_state = state;
    publish_state(FALSE);
}

static void controller_status(DBusGProxy *proxy, DBusGProxyCall *call, gpointer data)
{
    int karotz = -1, network = -1, platform = -1;

    (void)data;
    if (dbus_g_proxy_end_call(proxy, call, NULL, G_TYPE_INT, &karotz, G_TYPE_INT, &network, G_TYPE_INT, &platform,
                              G_TYPE_INVALID)) {
        app.controller_state = karotz;
        publish_state(FALSE);
    }
}

static void on_name_owner(DBusGProxy *p, const char *name, const char *old_owner, const char *new_owner, gpointer d)
{
    (void)p; (void)old_owner; (void)d;
    if (!strcmp(name, CLOUD_SERVICE))
        app.cloud_present = new_owner && *new_owner;
}

static void cloud_has_owner(DBusGProxy *proxy, DBusGProxyCall *call, gpointer data)
{
    gboolean has = FALSE;

    (void)data;
    if (dbus_g_proxy_end_call(proxy, call, NULL, G_TYPE_BOOLEAN, &has, G_TYPE_INVALID))
        app.cloud_present = has;
}

static DBusGProxy *karotz_proxy(const char *name)
{
    char *service = g_strdup_printf("com.mindscape.karotz.%s", name);
    char *path = g_strdup_printf("/com/mindscape/karotz/%s", name);
    DBusGProxy *p = dbus_g_proxy_new_for_name(app.bus, service, path, KAROTZ_IFACE);

    g_free(service);
    g_free(path);
    return p;
}

static void daemons_connect(void)
{
    dbus_g_object_register_marshaller(karotz_smarshal_VOID__STRING_STRING_INT_INT_INT_INT, G_TYPE_NONE, G_TYPE_STRING,
                                      G_TYPE_STRING, G_TYPE_INT, G_TYPE_INT, G_TYPE_INT, G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_object_register_marshaller(karotz_smarshal_VOID__STRING_STRING_STRING, G_TYPE_NONE, G_TYPE_STRING,
                                      G_TYPE_STRING, G_TYPE_STRING, G_TYPE_INVALID);
    app.led = karotz_proxy("Led");
    app.ears = karotz_proxy("Ears");
    app.button = karotz_proxy("Button");
    app.rfid = karotz_proxy("Rfid");
    app.voice = karotz_proxy("Voice");
    app.controller = karotz_proxy("Controller");
    app.cloud = dbus_g_proxy_new_for_name(app.bus, CLOUD_SERVICE, "/com/mindscape/karotz/LapinouCloud", CLOUD_SERVICE);

    dbus_g_proxy_add_signal(app.button, "click", G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(app.button, "click", G_CALLBACK(on_click), NULL, NULL);
    dbus_g_proxy_add_signal(app.button, "dclick", G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(app.button, "dclick", G_CALLBACK(on_dclick), NULL, NULL);
    dbus_g_proxy_add_signal(app.button, "tclick", G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(app.button, "tclick", G_CALLBACK(on_tclick), NULL, NULL);
    dbus_g_proxy_add_signal(app.button, "lclick_start", G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(app.button, "lclick_start", G_CALLBACK(on_lclick_start), NULL, NULL);
    dbus_g_proxy_add_signal(app.button, "lclick_stop", G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(app.button, "lclick_stop", G_CALLBACK(on_lclick_stop), NULL, NULL);
    dbus_g_proxy_add_signal(app.rfid, "tag_in", G_TYPE_STRING, G_TYPE_STRING, G_TYPE_INT, G_TYPE_INT, G_TYPE_INT,
                            G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(app.rfid, "tag_in", G_CALLBACK(on_tag_in), NULL, NULL);
    dbus_g_proxy_add_signal(app.rfid, "tag_out", G_TYPE_STRING, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(app.rfid, "tag_out", G_CALLBACK(on_tag_out), NULL, NULL);
    dbus_g_proxy_add_signal(app.controller, "karotz_status_changed", G_TYPE_INT, G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(app.controller, "karotz_status_changed", G_CALLBACK(on_controller_state), NULL, NULL);
    dbus_g_proxy_add_signal(app.bus_proxy, "NameOwnerChanged", G_TYPE_STRING, G_TYPE_STRING, G_TYPE_STRING,
                            G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(app.bus_proxy, "NameOwnerChanged", G_CALLBACK(on_name_owner), NULL, NULL);

    dbus_g_proxy_begin_call_with_timeout(app.controller, "get_status", controller_status, NULL, NULL, CALL_MS,
                                         G_TYPE_INVALID);
    dbus_g_proxy_begin_call(app.bus_proxy, "NameHasOwner", cloud_has_owner, NULL, NULL, G_TYPE_STRING, CLOUD_SERVICE,
                            G_TYPE_INVALID);
}

/* --- the broker: Paho's callbacks, in its threads, hand events to the main loop ------------- */

typedef enum { EV_CONNECTED, EV_CONNECT_FAILED, EV_LOST, EV_MESSAGE, EV_DISCONNECTED } EventKind;

typedef struct {
    EventKind kind;
    int attempt;
    int code;
    char *topic;
    char *payload;
    int len;
    gboolean retained;
} Event;

static gboolean handle_event(gpointer data);

static void post(EventKind kind, int attempt, int code)
{
    Event *e = g_new0(Event, 1);

    e->kind = kind;
    e->attempt = attempt;
    e->code = code;
    g_idle_add(handle_event, e);
}

static void paho_connected(void *context, MQTTAsync_successData *response)
{
    (void)response;
    post(EV_CONNECTED, GPOINTER_TO_INT(context), 0);
}

static void paho_connect_failed(void *context, MQTTAsync_failureData *response)
{
    post(EV_CONNECT_FAILED, GPOINTER_TO_INT(context), response ? response->code : 0);
}

static void paho_lost(void *context, char *cause)
{
    (void)context;
    (void)cause;
    post(EV_LOST, 0, 0);
}

static int paho_message(void *context, char *topic, int topic_len, MQTTAsync_message *m)
{
    Event *e = g_new0(Event, 1);

    (void)context;
    e->kind = EV_MESSAGE;
    e->topic = topic_len > 0 ? g_strndup(topic, (gsize)topic_len) : g_strdup(topic);
    e->retained = m->retained != 0;
    e->len = m->payloadlen > 0 ? m->payloadlen : 0;
    e->payload = g_malloc((gsize)e->len + 1);
    if (e->len)
        memcpy(e->payload, m->payload, (size_t)e->len);
    e->payload[e->len] = 0;
    MQTTAsync_freeMessage(&m);
    MQTTAsync_free(topic);
    g_idle_add(handle_event, e);
    return 1;
}

static void paho_disconnected(void *context, MQTTAsync_successData *response)
{
    (void)context;
    (void)response;
    post(EV_DISCONNECTED, 0, 0);
}

static void paho_disconnect_failed(void *context, MQTTAsync_failureData *response)
{
    (void)context;
    (void)response;
    post(EV_DISCONNECTED, 0, 0);
}

static void connect_now(void);

static gboolean retry_now(gpointer data)
{
    (void)data;
    app.retry_source = 0;
    connect_now();
    return FALSE;
}

static void retry_later(const char *why, int code)
{
    int delay = app.failures < RETRY_FAST_N ? RETRY_FAST_S : RETRY_SLOW_S;

    app.failures++;
    if (code)
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "mqtt %s: %s (%d); tried again in %d s", app.uri, why, code, delay);
    else
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "mqtt %s: %s; tried again in %d s", app.uri, why, delay);
    set_state(MQTT_DISCONNECTED);
    if (!app.retry_source && !app.stopping)
        app.retry_source = g_timeout_add_seconds(delay, retry_now, NULL);
}

static gboolean held(gpointer data)
{
    if (app.state == MQTT_CONNECTED && GPOINTER_TO_INT(data) == app.attempt)
        app.failures = 0;
    return FALSE;
}

static void connected(void)
{
    char *topics[3];
    int qos[3] = { 1, 1, 1 };
    MQTTAsync_responseOptions r = MQTTAsync_responseOptions_initializer;
    int rc, i;

    g_log(NULL, G_LOG_LEVEL_MESSAGE, "mqtt %s: connected", app.uri);
    set_state(MQTT_CONNECTED);
    g_timeout_add_seconds(HOLD_S, held, GINT_TO_POINTER(app.attempt));
    topics[0] = g_strdup_printf("%s/cmd/#", app.base);
    topics[1] = g_strdup_printf("%s/assistant", app.base);
    topics[2] = g_strdup_printf("%s/status", app.discovery);
    rc = MQTTAsync_subscribeMany(app.client, 3, topics, qos, &r);
    if (rc != MQTTASYNC_SUCCESS)
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "mqtt: the subscriptions failed: %s", MQTTAsync_strerror(rc));
    for (i = 0; i < 3; i++)
        g_free(topics[i]);
    publish_all();
}

static gboolean handle_event(gpointer data)
{
    Event *e = data;

    switch (e->kind) {
    case EV_CONNECTED:
        if (app.state == MQTT_CONNECTING && e->attempt == app.attempt)
            connected();
        break;
    case EV_CONNECT_FAILED:
        if (app.state == MQTT_CONNECTING && e->attempt == app.attempt)
            retry_later(e->code == 4 || e->code == 5 ? "refused: wrong user or password"
                                                    : "the connection failed", e->code);
        break;
    case EV_LOST:
        if (app.state == MQTT_CONNECTED)
            retry_later("connection lost", 0);
        break;
    case EV_MESSAGE:
        if (app.state == MQTT_CONNECTED)
            message(e->topic, e->payload, e->len, e->retained);
        break;
    case EV_DISCONNECTED:
        g_main_loop_quit(app.loop);
        break;
    }
    g_free(e->topic);
    g_free(e->payload);
    g_free(e);
    return FALSE;
}

static void connect_now(void)
{
    MQTTAsync_connectOptions o = MQTTAsync_connectOptions_initializer;
    MQTTAsync_willOptions w = MQTTAsync_willOptions_initializer;
    MQTTAsync_createOptions co = MQTTAsync_createOptions_initializer;
    int rc;

    if (app.client == NULL) {
        char *id = first_line(app.uuid_file);
        if (id == NULL) {
            retry_later("no rabbit id yet", 0);
            return;
        }
        if (!valid_rabbit_id(id)) {
            g_free(id);
            retry_later("the rabbit id (uuid.conf) has characters other than A-Z a-z 0-9 _ -", 0);
            return;
        }
        app.rabbit_id = id;
        app.base = g_strdup_printf("%s/%s", app.prefix, id);
        app.client_id = g_strdup_printf("lapinou-%s", id);
        app.will_topic = g_strdup_printf("%s/availability", app.base);
        co.MQTTVersion = MQTTVERSION_3_1_1;
        rc = MQTTAsync_createWithOptions(&app.client, app.uri, app.client_id, MQTTCLIENT_PERSISTENCE_NONE, NULL, &co);
        if (rc != MQTTASYNC_SUCCESS) {
            g_log(NULL, G_LOG_LEVEL_WARNING, "mqtt %s: no client: %s", app.uri, MQTTAsync_strerror(rc));
            app.client = NULL;
            set_state(MQTT_DISABLED);
            return;
        }
        MQTTAsync_setCallbacks(app.client, NULL, paho_lost, paho_message, NULL);
    }
    w.topicName = app.will_topic;
    w.message = "offline";
    w.retained = 1;
    w.qos = 1;
    o.will = &w;
    o.keepAliveInterval = KEEPALIVE_S;
    o.cleansession = 1;
    o.connectTimeout = 10;
    o.MQTTVersion = MQTTVERSION_3_1_1;
    /* MQTT 3.1.1 has no password without a user name. */
    o.username = app.user;
    o.password = app.user ? app.password : NULL;
    o.onSuccess = paho_connected;
    o.onFailure = paho_connect_failed;
    o.context = GINT_TO_POINTER(++app.attempt);
    set_state(MQTT_CONNECTING);
    rc = MQTTAsync_connect(app.client, &o);
    if (rc != MQTTASYNC_SUCCESS)
        retry_later(MQTTAsync_strerror(rc), 0);
}

/* --- start and stop ------------------------------------------------------------------------ */

static gboolean stop_anyway(gpointer data)
{
    (void)data;
    g_main_loop_quit(app.loop);
    return FALSE;
}

static gboolean on_term(gpointer data)
{
    MQTTAsync_disconnectOptions d = MQTTAsync_disconnectOptions_initializer;

    (void)data;
    if (app.stopping)
        return TRUE;
    app.stopping = TRUE;
    if (app.state != MQTT_CONNECTED) {
        g_main_loop_quit(app.loop);
        return TRUE;
    }
    publish_text("availability", "offline", 1, 1);
    d.timeout = 2000;
    d.onSuccess = paho_disconnected;
    d.onFailure = paho_disconnect_failed;
    if (MQTTAsync_disconnect(app.client, &d) != MQTTASYNC_SUCCESS)
        g_main_loop_quit(app.loop);
    else
        g_timeout_add_seconds(3, stop_anyway, NULL);
    return TRUE;
}

static void settings_read(void)
{
    char *enabled = lapinou_conf_get("mqtt", "enabled", NULL);
    char *port = lapinou_conf_get("mqtt", "port", "1883");
    char *server = lapinou_conf_get("server", "enabled", NULL);
    char *cloud = lapinou_conf_get("cloud", "enabled", NULL);
    char *end = NULL;
    long p;

    app.host = lapinou_conf_get("mqtt", "host", NULL);
    app.user = lapinou_conf_get("mqtt", "user", NULL);
    app.password = lapinou_secret_get("mqtt_password");
    app.prefix = lapinou_conf_get("mqtt", "prefix", "lapinou");
    app.discovery = lapinou_conf_get("mqtt", "discovery_prefix", "homeassistant");
    app.language = lapinou_conf_get("assistant", "language", "fr");
    app.serverless = cloud && !strcmp(cloud, "1") && !(server && !strcmp(server, "1"));
    p = strtol(port, &end, 10);
    app.port = *end == '\0' && p >= 1 && p <= 65535 ? (int)p : 1883;
    if (app.port != p)
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "[mqtt] port '%s' is not a port: 1883", port);
    if (!valid_topic_part(app.prefix)) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "[mqtt] prefix '%s' can't be a topic's start: lapinou", app.prefix);
        g_free(app.prefix);
        app.prefix = g_strdup("lapinou");
    }
    if (!valid_topic_part(app.discovery)) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "[mqtt] discovery_prefix '%s' can't be a topic's start: homeassistant",
              app.discovery);
        g_free(app.discovery);
        app.discovery = g_strdup("homeassistant");
    }
    app.state = MQTT_DISABLED;
    if (!enabled || strcmp(enabled, "1"))
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "MQTT is off ([mqtt] enabled is not 1)");
    else if (!app.host)
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "MQTT is off: no [mqtt] host");
    else if (!valid_host(app.host))
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "MQTT is off: [mqtt] host '%s' is not a host name or address", app.host);
    else if (strchr(app.host, ':') && app.host[0] != '[')
        app.uri = g_strdup_printf("tcp://[%s]:%d", app.host, app.port);
    else
        app.uri = g_strdup_printf("tcp://%s:%d", app.host, app.port);
    g_free(enabled);
    g_free(port);
    g_free(server);
    g_free(cloud);
}

static gboolean lapinou_mqtt_object_get_state(LapinouMqttObject *self, char **state, GError **error)
{
    (void)self;
    (void)error;
    *state = g_strdup(STATE_NAMES[app.state]);
    return TRUE;
}

/* set_volume(i) -> b: the web page's volume, as cmd/volume sets it (applied, then kept and
 * published); FALSE for a number outside 0 to 100. */
static gboolean lapinou_mqtt_object_set_volume(LapinouMqttObject *self, gint volume, gboolean *ok, GError **error)
{
    (void)self;
    (void)error;
    *ok = volume >= 0 && volume <= 100;
    if (*ok)
        volume_set(volume, TRUE);
    return TRUE;
}

static void lapinou_mqtt_object_class_init(LapinouMqttObjectClass *klass)
{
    klass->state_signal = g_signal_new("mqtt_state", G_TYPE_FROM_CLASS(klass), G_SIGNAL_RUN_FIRST, 0, NULL, NULL,
                                       g_cclosure_marshal_VOID__STRING, G_TYPE_NONE, 1, G_TYPE_STRING);
    dbus_g_object_type_install_info(LAPINOU_MQTT_TYPE_OBJECT, &dbus_glib_lapinou_mqtt_object_object_info);
}

static void lapinou_mqtt_object_init(LapinouMqttObject *self)
{
    (void)self;
}

int main(void)
{
    int log_priority = LOG_NOTICE;
    GError *error = NULL;
    guint request_result;

    init_karotz_log("LapinouMqtt", &log_priority);
    signal(SIGPIPE, SIG_IGN);
    g_type_init();
    app.loop = g_main_loop_new(NULL, FALSE);
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "Starting\n");
    app.uuid_file = env_or("LAPINOU_UUID_FILE", UUID_FILE);
    app.web_data = env_or("LAPINOU_WEB_DATA", WEB_DATA);
    app.web_run = env_or("LAPINOU_WEB_RUN", WEB_RUN);
    app.web_cgi = env_or("LAPINOU_WEB_CGI", WEB_CGI);
    app.wireless_file = env_or("LAPINOU_WIRELESS", WIRELESS_FILE);
    app.conf_sh = env_or("LAPINOU_CONF_SH", CONF_SH);
    app.sys_version_file = env_or("LAPINOU_SYS_VERSION", SYS_VERSION);
    app.available_file = env_or("LAPINOU_OTA_AVAILABLE", OTA_AVAILABLE);
    app.controller_state = -1;
    app.version = first_line(BUILD_FILE);
    read_versions();
    app.assistant = g_strdup("hors ligne");
    app.rabbit.ear_left = app.rabbit.ear_right = 0;
    settings_read();
    volume_start();
    read_web_led();
    read_web_sleep();
    read_wifi();

    app.object = g_object_new(LAPINOU_MQTT_TYPE_OBJECT, NULL);
    app.bus = dbus_g_bus_get(DBUS_BUS_SYSTEM, &error);
    if (app.bus == NULL)
        die("Failed to open connection to bus", error);
    dbus_g_connection_register_g_object(app.bus, OBJECT_PATH, G_OBJECT(app.object));
    app.bus_proxy = dbus_g_proxy_new_for_name(app.bus, "org.freedesktop.DBus", "/org/freedesktop/DBus",
                                              "org.freedesktop.DBus");
    if (!org_freedesktop_DBus_request_name(app.bus_proxy, SERVICE, 0, &request_result, &error))
        die("Failed to get name", error);
    if (request_result != DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER) {
        g_log(NULL, G_LOG_LEVEL_ERROR, "Got result code %u from requesting name", request_result);
        for (;;)
            ;
    }
    daemons_connect();
    g_unix_signal_add(SIGTERM, on_term, NULL);
    g_timeout_add_seconds(TICK_S, tick, NULL);
    if (app.uri)
        connect_now();
    g_main_loop_run(app.loop);
    return 0;
}
