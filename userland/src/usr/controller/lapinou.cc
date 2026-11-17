// SPDX-License-Identifier: MIT
/*
 * LapiNou's settings for the controller, from /usr/etc/conf/lapinou.conf (lapinou_conf_get(), read
 * once at start-up):
 *
 * - The standalone mode ([server] enabled not 1 and [cloud] enabled=1): there is no platform, so the
 *   controller doesn't look for one. Once the network is up it becomes available (green pulse, ears
 *   up: not the disconnected look), and writes [time] tz to /etc/TZ: a zone of /etc/timezone.list
 *   ("Europe/Paris") or a POSIX TZ string ("CET-1CEST,M3.5.0,M10.5.0/3").
 * - The long press while no platform has logged the rabbit in: with [cloud] enabled=1 and
 *   com.mindscape.karotz.LapinouCloud on the bus (followed by NameHasOwner and NameOwnerChanged),
 *   LapinouCloud.start_listening(), then stop_listening() at the release. Otherwise, when LapiNou
 *   is set up ([server] or [cloud] enabled=1), the no-server sound ("Je n'arrive pas à me
 *   connecter.") and the orange LED for 3 s, then the status LED again. A press that went to
 *   LapinouCloud is released there, even once a platform has logged the rabbit in.
 *
 * Without either setting (LapinLibre's settings alone, no lapinou.conf) nothing here acts: the
 * controller does what LapinLibre's does. Once a platform has logged the rabbit in, the long press is
 * LapinLibre's too (the platform gets the ButtonCallback).
 */
#include <stdio.h>
#include <string.h>

#include "controller.h"

extern "C" {
#include "lapinou_conf.h"
}

#define CLOUD_SERVICE "com.mindscape.karotz.LapinouCloud"
#define CLOUD_PATH "/com/mindscape/karotz/LapinouCloud"
#define NO_SERVER_SOUND "/usr/karotz/sounds/lapinou-no-server.mp3"
#define NO_SERVER_LED_S 3
#define TZ_MAX 80 /* as find_timezone() and image/tools/tzlist.py */

static struct {
    gboolean server;       /* [server] enabled=1 */
    gboolean cloud;        /* [cloud] enabled=1 */
    gboolean cloud_present; /* LapinouCloud is on the bus */
    gboolean listening;    /* start_listening() was sent, stop_listening() not yet */
    guint led_timer;       /* the orange LED's end, 0 if none */
    DBusGProxy *bus_proxy;
    DBusGProxy *cloud_proxy;
} lapinou;

static gboolean setting_on(const char *section)
{
    char *value = lapinou_conf_get(section, "enabled", NULL);
    gboolean on = value != NULL && strcmp(value, "1") == 0;

    g_free(value);
    return on;
}

void lapinou_load_settings(void)
{
    lapinou.server = setting_on("server");
    lapinou.cloud = setting_on("cloud");
    if (lapinou.server || lapinou.cloud)
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "LapiNou - server %s, cloud %s%s", lapinou.server ? "on" : "off",
              lapinou.cloud ? "on" : "off", lapinou_standalone() ? ": standalone, no platform" : "");
}

gboolean lapinou_standalone(void)
{
    return !lapinou.server && lapinou.cloud;
}

/* ---- the time zone ------------------------------------------------------------------- */

/* The TZ of `zone`'s own line in /etc/timezone.list ("<zone>;<TZ>"), NULL if it has none. */
static char *listed_timezone(const char *zone)
{
    char line[256];
    char *tz = NULL;
    size_t length = strlen(zone);
    FILE *list = fopen("/etc/timezone.list", "r");

    if (list == NULL)
        return NULL;
    while (tz == NULL && fgets(line, sizeof line, list) != NULL) {
        if (strncmp(line, zone, length) == 0 && line[length] == ';') {
            size_t n = strcspn(line + length + 1, "\r\n");
            if (n > 0 && n <= TZ_MAX)
                tz = g_strndup(line + length + 1, n);
        }
    }
    fclose(list);
    return tz;
}

/* A POSIX TZ string as uClibc reads it: a name ("CET" or "<+03>"), an offset, maybe more, at most
 * TZ_MAX printable characters, no '/' (a zone name, then, that the list doesn't have). */
static gboolean posix_timezone(const char *tz)
{
    gboolean digit = FALSE;
    size_t n = strlen(tz);

    if (n == 0 || n > TZ_MAX || !(g_ascii_isalpha(tz[0]) || tz[0] == '<'))
        return FALSE;
    for (size_t i = 0; i < n; i++) {
        if (!g_ascii_isgraph(tz[i]) || tz[i] == '/')
            return FALSE;
        digit = digit || g_ascii_isdigit(tz[i]);
    }
    return digit;
}

/* [time] tz to /etc/TZ, with its newline (uClibc ignores a TZ file without one). */
static void apply_timezone(void)
{
    char *zone = lapinou_conf_get("time", "tz", NULL);
    char *tz;

    if (zone == NULL)
        return;
    tz = listed_timezone(zone);
    if (tz == NULL && posix_timezone(zone))
        tz = g_strdup(zone);
    if (tz == NULL) {
        g_log(NULL, G_LOG_LEVEL_WARNING, "LapiNou - unknown time zone in lapinou.conf: /etc/TZ unchanged");
    } else {
        FILE *file = fopen("/etc/TZ", "w");
        if (file == NULL || fprintf(file, "%s\n", tz) < 0 || fclose(file) != 0)
            g_log(NULL, G_LOG_LEVEL_WARNING, "LapiNou - cannot write /etc/TZ");
        else
            g_log(NULL, G_LOG_LEVEL_MESSAGE, "Setting timezone to %s (%s)", tz, zone);
    }
    g_free(tz);
    g_free(zone);
}

/* The network is up in the standalone mode: no platform to wait for. */
void lapinou_standalone_online(ControllerObject *controller)
{
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "LapiNou - standalone: no platform");
    apply_timezone();
    controller_set_status(controller, E_STATUS_KAROTZ_AVAILABLE);
}

/* ---- LapinouCloud on the bus ------------------------------------------------------------ */

static void cloud_owner_changed(DBusGProxy *proxy, const char *name, const char *old_owner, const char *new_owner,
                                gpointer data)
{
    (void)proxy;
    (void)old_owner;
    (void)data;
    if (strcmp(name, CLOUD_SERVICE) == 0) {
        lapinou.cloud_present = new_owner != NULL && *new_owner != '\0';
        g_log(NULL, G_LOG_LEVEL_DEBUG, "LapiNou - LapinouCloud %s", lapinou.cloud_present ? "up" : "gone");
    }
}

static void cloud_has_owner(DBusGProxy *proxy, DBusGProxyCall *call, gpointer data)
{
    gboolean has = FALSE;

    (void)data;
    if (dbus_g_proxy_end_call(proxy, call, NULL, G_TYPE_BOOLEAN, &has, G_TYPE_INVALID))
        lapinou.cloud_present = has;
}

/* Only with [cloud] enabled=1: otherwise the controller asks the bus nothing more than LapinLibre's. */
void lapinou_watch_cloud(DBusGConnection *bus)
{
    if (!lapinou.cloud)
        return;
    dbus_g_object_register_marshaller(karotz_smarshal_VOID__STRING_STRING_STRING, G_TYPE_NONE, G_TYPE_STRING,
                                      G_TYPE_STRING, G_TYPE_STRING, G_TYPE_INVALID);
    lapinou.cloud_proxy = dbus_g_proxy_new_for_name(bus, CLOUD_SERVICE, CLOUD_PATH, CLOUD_SERVICE);
    lapinou.bus_proxy = dbus_g_proxy_new_for_name(bus, DBUS_SERVICE_DBUS, DBUS_PATH_DBUS, DBUS_INTERFACE_DBUS);
    dbus_g_proxy_add_signal(lapinou.bus_proxy, "NameOwnerChanged", G_TYPE_STRING, G_TYPE_STRING, G_TYPE_STRING,
                            G_TYPE_INVALID);
    dbus_g_proxy_connect_signal(lapinou.bus_proxy, "NameOwnerChanged", G_CALLBACK(cloud_owner_changed), NULL, NULL);
    dbus_g_proxy_begin_call(lapinou.bus_proxy, "NameHasOwner", cloud_has_owner, NULL, NULL, G_TYPE_STRING,
                            CLOUD_SERVICE, G_TYPE_INVALID);
}

/* ---- the long press ------------------------------------------------------------------- */

/* The LED of the current status, as the status listeners set it. */
static gboolean restore_led(gpointer data)
{
    ControllerObject *controller = (ControllerObject *)data;

    lapinou.led_timer = 0;
    if (controller->sleeping)
        ledLight(controller->led, NULL, LED_OFF);
    else if (controller->status.karotz == E_STATUS_KAROTZ_AVAILABLE)
        ledPulse(controller->led, NULL, LED_GREEN, controller->pending_message ? LED_BLUE : LED_OFF, PULSE_PERIOD,
                 LED_FOREVER);
    else if (controller->status.karotz == E_STATUS_KAROTZ_INTERACTIVE)
        ledPulse(controller->led, NULL, LED_LIGHT_GREEN, LED_OFF, PULSE_PERIOD, LED_FOREVER);
    else if (controller->status.karotz == E_STATUS_KAROTZ_DISCONNECTED)
        ledPulse(controller->led, NULL, LED_RED, LED_OFF, PULSE_PERIOD, LED_FOREVER);
    else
        ledLight(controller->led, NULL, LED_OFF);
    return G_SOURCE_REMOVE;
}

/* A long press that no platform takes. Asleep or held by an application, it does nothing, as
 * LapinLibre's long press. */
void lapinou_long_press_start(ControllerObject *controller)
{
    if (!lapinou.server && !lapinou.cloud)
        return;
    if (controller->sleeping || controller->status.karotz == E_STATUS_KAROTZ_INTERACTIVE)
        return;
    if (lapinou.cloud && lapinou.cloud_present) {
        g_log(NULL, G_LOG_LEVEL_MESSAGE, "LapiNou - long press: LapinouCloud listens");
        dbus_g_proxy_call_no_reply(lapinou.cloud_proxy, "start_listening", G_TYPE_INVALID);
        lapinou.listening = TRUE;
        return;
    }
    g_log(NULL, G_LOG_LEVEL_MESSAGE, "LapiNou - long press: no server to listen");
    multimediaPlay(controller->multimedia, NULL, NO_SERVER_SOUND, 1);
    ledLight(controller->led, NULL, LED_ORANGE);
    if (lapinou.led_timer != 0)
        g_source_remove(lapinou.led_timer);
    lapinou.led_timer = g_timeout_add_seconds(NO_SERVER_LED_S, restore_led, controller);
}

/* The release: TRUE when the press went to LapinouCloud, which then gets stop_listening(), even if a
 * platform logged the rabbit in meanwhile (the platform never saw that press). */
gboolean lapinou_long_press_stop(ControllerObject *controller)
{
    (void)controller;
    if (!lapinou.listening)
        return FALSE;
    dbus_g_proxy_call_no_reply(lapinou.cloud_proxy, "stop_listening", G_TYPE_INVALID);
    lapinou.listening = FALSE;
    return TRUE;
}
