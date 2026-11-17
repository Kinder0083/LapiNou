/* SPDX-License-Identifier: MIT */
/*
 * The little JSON lapinou-mqtt needs, without a JSON library (the rabbit has no json-glib):
 * writing strings into a GString, and reading a command's payload, a flat object whose values
 * are strings, numbers, true, false or null (anything nested is refused).
 */
#ifndef MQTT_JSON_H
#define MQTT_JSON_H

#include <glib.h>

/* Append `s` (UTF-8) as a JSON string, quotes included; NULL is appended as null. */
void json_append_string(GString *out, const char *s);

typedef enum { JSON_STRING, JSON_NUMBER, JSON_BOOL, JSON_NULL } JsonKind;

typedef struct {
    JsonKind kind;
    char *string;   /* JSON_STRING: the decoded text (UTF-8) */
    double number;  /* JSON_NUMBER */
    gboolean truth; /* JSON_BOOL */
} JsonValue;

/* A flat object read from `text` (`len` bytes): key -> JsonValue, or NULL if it isn't one (not
 * JSON, not an object, nested values, invalid UTF-8, trailing text). Free with
 * g_hash_table_destroy(). A key given twice keeps its last value. */
GHashTable *json_parse_flat_object(const char *text, gsize len);

/* The value of `key` if it has that kind, else NULL. */
const JsonValue *json_get(GHashTable *object, const char *key, JsonKind kind);

#endif
