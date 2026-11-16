/* SPDX-License-Identifier: MIT */
/* lapinou-mqtt's Home Assistant discovery messages and state JSON (discovery.h). */
#include <string.h>

#include "discovery.h"
#include "mqtt-json.h"

/* 17 steps make a turn; after a reset both ears are up (0). 4 is where the controller puts them
 * while the rabbit sleeps (the web API's WEB_EARS_ASLEEP). */
const EarPreset EAR_PRESETS[] = {
    { "debout", 0, 0 },
    { "endormies", 4, 4 },
    { "tombantes", 8, 8 },
    { "perplexe", 0, 8 },
    { "curieuse", 2, 15 },
};
const int EAR_PRESET_COUNT = G_N_ELEMENTS(EAR_PRESETS);

const char *ear_preset_name(int left, int right)
{
    int i;

    for (i = 0; i < EAR_PRESET_COUNT; i++)
        if (EAR_PRESETS[i].left == left && EAR_PRESETS[i].right == right)
            return EAR_PRESETS[i].name;
    return EAR_PRESET_OTHER;
}

const EarPreset *ear_preset_find(const char *name)
{
    int i;

    for (i = 0; i < EAR_PRESET_COUNT; i++)
        if (strcmp(EAR_PRESETS[i].name, name) == 0)
            return &EAR_PRESETS[i];
    return NULL;
}

/* --- a JSON object written member by member --------------------------------------------- */

typedef struct {
    GString *s;
    int members;
} Obj;

static void key(Obj *o, const char *k)
{
    g_string_append(o->s, o->members++ ? "," : "{");
    json_append_string(o->s, k);
    g_string_append_c(o->s, ':');
}

static void str(Obj *o, const char *k, const char *v)
{
    key(o, k);
    json_append_string(o->s, v);
}

static void raw(Obj *o, const char *k, const char *v)
{
    key(o, k);
    g_string_append(o->s, v);
}

static void num(Obj *o, const char *k, int v)
{
    key(o, k);
    g_string_append_printf(o->s, "%d", v);
}

static char *end(Obj *o)
{
    g_string_append(o->s, o->members ? "}" : "{}");
    return g_string_free(o->s, FALSE);
}

/* --- the entities ------------------------------------------------------------------------ */

typedef struct {
    const DiscoveryContext *c;
    GPtrArray *out;
    char *base;   /* <prefix>/<rabbit_id> */
    char *device; /* the device block, the same in every message */
    char *origin;
} Builder;

static void message_free(gpointer p)
{
    DiscoveryMessage *m = p;

    g_free(m->topic);
    g_free(m->payload);
    g_free(m);
}

static char *topic(Builder *b, const char *rest)
{
    return g_strdup_printf("%s/%s", b->base, rest);
}

/* An entity's message: its common members first (name, unique_id, availability, device,
 * origin); the caller adds the component's own, then done() files it. */
static Obj entity(Builder *b, const char *name, const char *object)
{
    Obj o = { g_string_new(NULL), 0 };
    char *t, *uid;

    str(&o, "name", name);
    uid = g_strdup_printf("lapinou_%s_%s", b->c->rabbit_id, object);
    str(&o, "unique_id", uid);
    g_free(uid);
    t = topic(b, "availability");
    str(&o, "availability_topic", t);
    g_free(t);
    raw(&o, "device", b->device);
    raw(&o, "origin", b->origin);
    return o;
}

static void str_topic(Builder *b, Obj *o, const char *k, const char *rest)
{
    char *t = topic(b, rest);

    str(o, k, t);
    g_free(t);
}

static void done(Builder *b, Obj *o, const char *component, const char *object)
{
    DiscoveryMessage *m = g_new0(DiscoveryMessage, 1);

    m->topic = g_strdup_printf("%s/%s/%s/%s/config", b->c->discovery, component, b->c->rabbit_id, object);
    m->payload = end(o);
    g_ptr_array_add(b->out, m);
}

GPtrArray *discovery_messages(const DiscoveryContext *c)
{
    Builder b = { c, g_ptr_array_new_with_free_func(message_free), NULL, NULL, NULL };
    Obj o, d;
    GString *options;
    int i;

    b.base = g_strdup_printf("%s/%s", c->prefix, c->rabbit_id);
    d = (Obj){ g_string_new(NULL), 0 };
    {
        char *id = g_strdup_printf("lapinou_%s", c->rabbit_id);
        GString *ids = g_string_new("[");

        json_append_string(ids, id);
        g_string_append_c(ids, ']');
        raw(&d, "identifiers", ids->str);
        g_string_free(ids, TRUE);
        g_free(id);
    }
    str(&d, "name", "Lapinou");
    str(&d, "manufacturer", "LapiNou");
    str(&d, "model", "Karotz");
    str(&d, "sw_version", c->version);
    b.device = end(&d);
    d = (Obj){ g_string_new(NULL), 0 };
    str(&d, "name", "lapinou-mqtt");
    str(&d, "sw_version", c->version);
    b.origin = end(&d);

    /* The LED: a light of the template schema, on the state topic's "led" (RRGGBB). */
    o = entity(&b, "LED", "led");
    str(&o, "schema", "template");
    str_topic(&b, &o, "command_topic", "cmd/led");
    str(&o, "command_on_template",
        "{%- if red is defined and green is defined and blue is defined -%}"
        "{\"color\":\"{{ '%02X%02X%02X' | format(red, green, blue) }}\"}"
        "{%- else -%}{\"on\":true}{%- endif -%}");
    str(&o, "command_off_template", "{\"color\":\"000000\"}");
    str_topic(&b, &o, "state_topic", "state");
    str(&o, "state_template", "{{ 'off' if value_json.led == '000000' else 'on' }}");
    str(&o, "red_template", "{{ value_json.led[0:2] | int(base=16) }}");
    str(&o, "green_template", "{{ value_json.led[2:4] | int(base=16) }}");
    str(&o, "blue_template", "{{ value_json.led[4:6] | int(base=16) }}");
    done(&b, &o, "light", "led");

    /* The ears: a select of the presets. */
    o = entity(&b, "Oreilles", "ears");
    str(&o, "icon", "mdi:rabbit");
    str_topic(&b, &o, "command_topic", "cmd/ears");
    str(&o, "command_template", "{\"preset\":{{ value | to_json }}}");
    options = g_string_new("[");
    for (i = 0; i < EAR_PRESET_COUNT; i++) {
        json_append_string(options, EAR_PRESETS[i].name);
        g_string_append_c(options, ',');
    }
    json_append_string(options, EAR_PRESET_OTHER);
    g_string_append_c(options, ']');
    raw(&o, "options", options->str);
    g_string_free(options, TRUE);
    str_topic(&b, &o, "state_topic", "state");
    str(&o, "value_template", "{{ value_json.ears.preset }}");
    done(&b, &o, "select", "ears");

    o = entity(&b, "Réinitialiser les oreilles", "ears_reset");
    str(&o, "icon", "mdi:restore");
    str_topic(&b, &o, "command_topic", "cmd/ears");
    str(&o, "payload_press", "{\"reset\":true}");
    done(&b, &o, "button", "ears_reset");

    /* The head's button and the RFID tags: events. */
    o = entity(&b, "Bouton", "button");
    str(&o, "icon", "mdi:gesture-tap-button");
    str_topic(&b, &o, "state_topic", "event/button");
    raw(&o, "event_types", "[\"click\",\"dclick\",\"tclick\",\"lclick_start\",\"lclick_stop\"]");
    str(&o, "value_template", "{{ {'event_type': value_json.type} | to_json }}");
    done(&b, &o, "event", "button");

    o = entity(&b, "Badge", "rfid");
    str(&o, "icon", "mdi:nfc-variant");
    str_topic(&b, &o, "state_topic", "event/rfid");
    raw(&o, "event_types", "[\"in\",\"out\"]");
    str(&o, "value_template", "{{ {'event_type': value_json.action, 'tag': value_json.tag} | to_json }}");
    done(&b, &o, "event", "rfid");

    o = entity(&b, "Dernier badge", "last_tag");
    str(&o, "icon", "mdi:nfc-variant");
    str_topic(&b, &o, "state_topic", "state");
    str(&o, "value_template", "{{ value_json.last_tag }}");
    done(&b, &o, "sensor", "last_tag");

    o = entity(&b, "Volume", "volume");
    str(&o, "icon", "mdi:volume-high");
    str_topic(&b, &o, "command_topic", "cmd/volume");
    str_topic(&b, &o, "state_topic", "state");
    str(&o, "value_template", "{{ value_json.volume }}");
    num(&o, "min", 0);
    num(&o, "max", 100);
    num(&o, "step", 1);
    str(&o, "mode", "slider");
    str(&o, "unit_of_measurement", "%");
    done(&b, &o, "number", "volume");

    o = entity(&b, "Parler", "say");
    str(&o, "icon", "mdi:account-voice");
    str_topic(&b, &o, "command_topic", "cmd/say");
    num(&o, "max", 255);
    str(&o, "mode", "text");
    done(&b, &o, "text", "say");

    o = entity(&b, "Jouer un son", "sound");
    str(&o, "icon", "mdi:music-note");
    str_topic(&b, &o, "command_topic", "cmd/sound");
    num(&o, "max", 255);
    str(&o, "mode", "text");
    done(&b, &o, "text", "sound");

    /* The camera: the picture comes on <base>/snapshot (cmd/snapshot takes one). */
    o = entity(&b, "Photo", "snapshot");
    str_topic(&b, &o, "image_topic", "snapshot");
    str(&o, "content_type", "image/jpeg");
    done(&b, &o, "image", "snapshot");

    o = entity(&b, "Prendre une photo", "take_snapshot");
    str(&o, "icon", "mdi:camera");
    str_topic(&b, &o, "command_topic", "cmd/snapshot");
    done(&b, &o, "button", "take_snapshot");

    o = entity(&b, "Veille", "sleep");
    str(&o, "icon", "mdi:sleep");
    str_topic(&b, &o, "command_topic", "cmd/sleep");
    str(&o, "payload_on", "on");
    str(&o, "payload_off", "off");
    str_topic(&b, &o, "state_topic", "state");
    str(&o, "value_template", "{{ value_json.sleep }}");
    str(&o, "state_on", "on");
    str(&o, "state_off", "off");
    done(&b, &o, "switch", "sleep");

    o = entity(&b, "Assistant", "assistant");
    str(&o, "icon", "mdi:robot");
    str_topic(&b, &o, "state_topic", "state");
    str(&o, "value_template", "{{ value_json.assistant }}");
    done(&b, &o, "sensor", "assistant");

    o = entity(&b, "Signal Wi-Fi", "wifi");
    str(&o, "entity_category", "diagnostic");
    str(&o, "device_class", "signal_strength");
    str(&o, "state_class", "measurement");
    str(&o, "unit_of_measurement", "dBm");
    str_topic(&b, &o, "state_topic", "state");
    str(&o, "value_template", "{{ value_json.wifi_rssi }}");
    done(&b, &o, "sensor", "wifi");

    /* The firmware: installed_version only for now; the OTA (Task 11) adds latest_version and
     * the install command on the same topic (docs/lapinou/mqtt.md). */
    o = entity(&b, "Micrologiciel", "firmware");
    str(&o, "device_class", "firmware");
    str(&o, "title", "LapiNou");
    str_topic(&b, &o, "state_topic", "update");
    done(&b, &o, "update", "firmware");

    g_free(b.base);
    g_free(b.device);
    g_free(b.origin);
    return b.out;
}

char *state_json(const RabbitState *s)
{
    Obj o = { g_string_new(NULL), 0 }, ears = { g_string_new(NULL), 0 };
    char *e;

    str(&o, "led", s->led);
    num(&ears, "left", s->ear_left);
    num(&ears, "right", s->ear_right);
    str(&ears, "preset", ear_preset_name(s->ear_left, s->ear_right));
    e = end(&ears);
    raw(&o, "ears", e);
    g_free(e);
    if (s->volume >= 0)
        num(&o, "volume", s->volume);
    else
        raw(&o, "volume", "null");
    str(&o, "sleep", s->asleep ? "on" : "off");
    str(&o, "assistant", s->assistant);
    if (s->has_rssi)
        num(&o, "wifi_rssi", s->wifi_rssi);
    else
        raw(&o, "wifi_rssi", "null");
    str(&o, "version", s->version);
    str(&o, "last_tag", s->last_tag);
    return end(&o);
}

char *update_json(const char *version)
{
    Obj o = { g_string_new(NULL), 0 };

    str(&o, "installed_version", version);
    return end(&o);
}
