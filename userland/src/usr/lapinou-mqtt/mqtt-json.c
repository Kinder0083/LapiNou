/* SPDX-License-Identifier: MIT */
/* The little JSON lapinou-mqtt needs (mqtt-json.h). */
#include <stdlib.h>
#include <string.h>

#include "mqtt-json.h"

void json_append_string(GString *out, const char *s)
{
    const unsigned char *p;

    if (s == NULL) {
        g_string_append(out, "null");
        return;
    }
    g_string_append_c(out, '"');
    for (p = (const unsigned char *)s; *p; p++) {
        switch (*p) {
        case '"': g_string_append(out, "\\\""); break;
        case '\\': g_string_append(out, "\\\\"); break;
        case '\n': g_string_append(out, "\\n"); break;
        case '\r': g_string_append(out, "\\r"); break;
        case '\t': g_string_append(out, "\\t"); break;
        default:
            if (*p < 0x20)
                g_string_append_printf(out, "\\u%04x", *p);
            else
                g_string_append_c(out, (char)*p);
        }
    }
    g_string_append_c(out, '"');
}

typedef struct {
    const char *p, *end;
} Reader;

static void skip_space(Reader *r)
{
    while (r->p < r->end && (*r->p == ' ' || *r->p == '\t' || *r->p == '\n' || *r->p == '\r'))
        r->p++;
}

static int hex4(Reader *r, gunichar *out)
{
    int i;

    *out = 0;
    if (r->end - r->p < 4)
        return 0;
    for (i = 0; i < 4; i++) {
        int d = g_ascii_xdigit_value(r->p[i]);
        if (d < 0)
            return 0;
        *out = (*out << 4) | (gunichar)d;
    }
    r->p += 4;
    return 1;
}

/* A string at r->p (on its opening quote), decoded, or NULL. */
static char *read_string(Reader *r)
{
    GString *s;

    if (r->p >= r->end || *r->p != '"')
        return NULL;
    r->p++;
    s = g_string_new(NULL);
    while (r->p < r->end) {
        unsigned char c = (unsigned char)*r->p++;
        gunichar u, low;

        if (c == '"') {
            if (!g_utf8_validate(s->str, s->len, NULL))
                break;
            return g_string_free(s, FALSE);
        }
        if (c < 0x20)
            break;
        if (c != '\\') {
            g_string_append_c(s, (char)c);
            continue;
        }
        if (r->p >= r->end)
            break;
        c = (unsigned char)*r->p++;
        switch (c) {
        case '"': case '\\': case '/': g_string_append_c(s, (char)c); continue;
        case 'b': g_string_append_c(s, '\b'); continue;
        case 'f': g_string_append_c(s, '\f'); continue;
        case 'n': g_string_append_c(s, '\n'); continue;
        case 'r': g_string_append_c(s, '\r'); continue;
        case 't': g_string_append_c(s, '\t'); continue;
        case 'u':
            if (!hex4(r, &u))
                goto bad;
            if (u >= 0xD800 && u <= 0xDBFF) {
                if (r->end - r->p < 6 || r->p[0] != '\\' || r->p[1] != 'u')
                    goto bad;
                r->p += 2;
                if (!hex4(r, &low) || low < 0xDC00 || low > 0xDFFF)
                    goto bad;
                u = 0x10000 + ((u - 0xD800) << 10) + (low - 0xDC00);
            } else if (u >= 0xDC00 && u <= 0xDFFF) {
                goto bad;
            }
            if (u == 0)
                goto bad; /* no NUL inside a C string */
            g_string_append_unichar(s, u);
            continue;
        default:
            goto bad;
        }
    }
bad:
    g_string_free(s, TRUE);
    return NULL;
}

static void value_free(gpointer v)
{
    JsonValue *value = v;

    g_free(value->string);
    g_free(value);
}

static int read_number(Reader *r, double *out)
{
    const char *start = r->p;
    char *text, *end;

    if (r->p < r->end && *r->p == '-')
        r->p++;
    if (r->p >= r->end || !g_ascii_isdigit(*r->p))
        return 0;
    while (r->p < r->end && (g_ascii_isdigit(*r->p) || *r->p == '.' || *r->p == 'e' || *r->p == 'E' ||
                             *r->p == '+' || *r->p == '-'))
        r->p++;
    text = g_strndup(start, (gsize)(r->p - start));
    *out = g_ascii_strtod(text, &end);
    if (*end != '\0') {
        g_free(text);
        return 0;
    }
    g_free(text);
    return 1;
}

static int read_word(Reader *r, const char *word)
{
    size_t n = strlen(word);

    if ((size_t)(r->end - r->p) < n || strncmp(r->p, word, n) != 0)
        return 0;
    r->p += n;
    return 1;
}

/* A scalar value at r->p (string, number, true, false or null), or NULL. */
static JsonValue *read_scalar(Reader *r)
{
    JsonValue *value = g_new0(JsonValue, 1);

    if (r->p < r->end && *r->p == '"') {
        value->kind = JSON_STRING;
        value->string = read_string(r);
        if (value->string != NULL)
            return value;
    } else if (read_word(r, "true")) {
        value->kind = JSON_BOOL;
        value->truth = TRUE;
        return value;
    } else if (read_word(r, "false")) {
        value->kind = JSON_BOOL;
        return value;
    } else if (read_word(r, "null")) {
        value->kind = JSON_NULL;
        return value;
    } else if (read_number(r, &value->number)) {
        value->kind = JSON_NUMBER;
        return value;
    }
    value_free(value);
    return NULL;
}

/* A flat object at r->p (on its opening brace), or NULL. When `list_key` is given, that member
 * may be an array of flat objects instead: it goes to *list (a new array of GHashTable). */
static GHashTable *read_object(Reader *r, const char *list_key, GPtrArray **list)
{
    GHashTable *object = g_hash_table_new_full(g_str_hash, g_str_equal, g_free, value_free);

    skip_space(r);
    if (r->p >= r->end || *r->p++ != '{')
        goto bad;
    skip_space(r);
    if (r->p < r->end && *r->p == '}') {
        r->p++;
        return object;
    }
    for (;;) {
        char *key;
        JsonValue *value;

        skip_space(r);
        key = read_string(r);
        if (key == NULL)
            goto bad;
        skip_space(r);
        if (r->p >= r->end || *r->p++ != ':') {
            g_free(key);
            goto bad;
        }
        skip_space(r);
        if (list_key && list && *list == NULL && strcmp(key, list_key) == 0 && r->p < r->end && *r->p == '[') {
            g_free(key);
            r->p++;
            *list = g_ptr_array_new_with_free_func((GDestroyNotify)g_hash_table_destroy);
            skip_space(r);
            if (r->p < r->end && *r->p == ']') {
                r->p++;
            } else {
                for (;;) {
                    GHashTable *item = read_object(r, NULL, NULL);

                    if (item == NULL)
                        goto bad;
                    g_ptr_array_add(*list, item);
                    skip_space(r);
                    if (r->p < r->end && *r->p == ',') {
                        r->p++;
                        skip_space(r);
                        continue;
                    }
                    if (r->p < r->end && *r->p == ']') {
                        r->p++;
                        break;
                    }
                    goto bad;
                }
            }
        } else {
            value = read_scalar(r);
            if (value == NULL) {
                g_free(key);
                goto bad;
            }
            g_hash_table_replace(object, key, value);
        }
        skip_space(r);
        if (r->p < r->end && *r->p == ',') {
            r->p++;
            continue;
        }
        if (r->p < r->end && *r->p == '}') {
            r->p++;
            return object;
        }
        goto bad;
    }
bad:
    g_hash_table_destroy(object);
    return NULL;
}

GHashTable *json_parse_flat_object(const char *text, gsize len)
{
    Reader r = { text, text + len };
    GHashTable *object = read_object(&r, NULL, NULL);

    skip_space(&r);
    if (object && r.p == r.end)
        return object;
    if (object)
        g_hash_table_destroy(object);
    return NULL;
}

GPtrArray *json_parse_object_list(const char *text, gsize len, const char *key, GHashTable **rest)
{
    Reader r = { text, text + len };
    GPtrArray *list = NULL;
    GHashTable *object = read_object(&r, key, &list);

    skip_space(&r);
    if (object && r.p == r.end && list) {
        if (rest)
            *rest = object;
        else
            g_hash_table_destroy(object);
        return list;
    }
    if (object)
        g_hash_table_destroy(object);
    if (list)
        g_ptr_array_free(list, TRUE);
    return NULL;
}

const JsonValue *json_get(GHashTable *object, const char *key, JsonKind kind)
{
    const JsonValue *v = g_hash_table_lookup(object, key);

    return v && v->kind == kind ? v : NULL;
}
