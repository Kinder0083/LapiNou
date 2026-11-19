/* SPDX-License-Identifier: MIT */
/*
 * lapinou-mqtt's Home Assistant side: the MQTT discovery messages of the rabbit's entities, and
 * the JSON of its state topic. The topics and payloads are described in docs/lapinou/mqtt.md.
 */
#ifndef LAPINOU_DISCOVERY_H
#define LAPINOU_DISCOVERY_H

#include <glib.h>

/* The ears' positions by name (the select entity's options), 0 to 16 for each ear. */
typedef struct {
    const char *name;
    int left, right;
} EarPreset;

extern const EarPreset EAR_PRESETS[];
extern const int EAR_PRESET_COUNT;
/* The option shown when the ears are at no preset's positions. */
#define EAR_PRESET_OTHER "personnalisée"

/* The preset at these positions, or EAR_PRESET_OTHER. */
const char *ear_preset_name(int left, int right);
/* The preset called `name`, or NULL. */
const EarPreset *ear_preset_find(const char *name);

typedef struct {
    const char *prefix;    /* [mqtt] prefix: topics are <prefix>/<rabbit_id>/... */
    const char *discovery; /* [mqtt] discovery_prefix */
    const char *rabbit_id;
    const char *version;   /* the firmware's version (the build stamp) */
} DiscoveryContext;

typedef struct {
    char *topic;
    char *payload;
} DiscoveryMessage;

/* Every entity's discovery message (retained by the caller), in a fixed order; the array frees
 * its messages. */
GPtrArray *discovery_messages(const DiscoveryContext *c);

/* A list's select (cmd/<object>, its options `labels`), or, for an empty list, the empty message
 * that removes it from Home Assistant. Free with discovery_message_free(). */
DiscoveryMessage *discovery_list(const DiscoveryContext *c, const char *object, const char *name,
                                 const char *icon, const GPtrArray *labels);
void discovery_message_free(DiscoveryMessage *m);

/* What the state topic carries. */
typedef struct {
    char led[7];           /* RRGGBB */
    int ear_left, ear_right;
    int volume;            /* 0-100, or -1 when unknown */
    gboolean asleep;
    const char *assistant; /* as the server publishes it */
    gboolean has_rssi;
    int wifi_rssi;         /* dBm */
    const char *version;
    const char *last_tag;  /* NULL before the first tag */
} RabbitState;

/* The state topic's JSON. Free with g_free(). */
char *state_json(const RabbitState *s);

/* The update entity's state: {"installed_version": ..., "latest_version": ...}, latest_version only
 * when known. Free with g_free(). */
char *update_json(const char *installed, const char *latest);

#endif
