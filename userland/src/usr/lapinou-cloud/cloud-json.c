/* SPDX-License-Identifier: MIT */
/* The JSON lapinou-cloud needs (cloud-json.h). */
#include <stdlib.h>
#include <string.h>

#include "cloud-json.h"

#define MAX_DEPTH 32

typedef struct {
    const char *p, *end;
} Reader;

static CjNode *parse_value(Reader *r, int depth);

void cj_free(CjNode *node)
{
    if (node == NULL)
        return;
    if (node->items != NULL)
        g_ptr_array_free(node->items, TRUE);
    g_free(node->key);
    g_free(node->string);
    g_free(node);
}

static CjNode *new_node(CjType type)
{
    CjNode *n = g_new0(CjNode, 1);

    n->type = type;
    if (type == CJ_ARRAY || type == CJ_OBJECT)
        n->items = g_ptr_array_new_with_free_func((GDestroyNotify)cj_free);
    return n;
}

static void skip_space(Reader *r)
{
    while (r->p < r->end && (*r->p == ' ' || *r->p == '\t' || *r->p == '\n' || *r->p == '\r'))
        r->p++;
}

static gboolean literal(Reader *r, const char *word)
{
    size_t n = strlen(word);

    if ((size_t)(r->end - r->p) < n || memcmp(r->p, word, n) != 0)
        return FALSE;
    r->p += n;
    return TRUE;
}

static gboolean hex4(Reader *r, gunichar *out)
{
    int i;

    *out = 0;
    if (r->end - r->p < 4)
        return FALSE;
    for (i = 0; i < 4; i++) {
        int d = g_ascii_xdigit_value(r->p[i]);
        if (d < 0)
            return FALSE;
        *out = (*out << 4) | (gunichar)d;
    }
    r->p += 4;
    return TRUE;
}

/* A string, the opening quote already read. A lone surrogate becomes U+FFFD. */
static char *parse_string(Reader *r)
{
    GString *s = g_string_new(NULL);

    while (r->p < r->end) {
        unsigned char c = (unsigned char)*r->p++;
        gunichar u;

        if (c == '"') {
            if (!g_utf8_validate(s->str, (gssize)s->len, NULL))
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
        switch (*r->p++) {
        case '"': g_string_append_c(s, '"'); continue;
        case '\\': g_string_append_c(s, '\\'); continue;
        case '/': g_string_append_c(s, '/'); continue;
        case 'b': g_string_append_c(s, '\b'); continue;
        case 'f': g_string_append_c(s, '\f'); continue;
        case 'n': g_string_append_c(s, '\n'); continue;
        case 'r': g_string_append_c(s, '\r'); continue;
        case 't': g_string_append_c(s, '\t'); continue;
        case 'u': break;
        default: goto bad;
        }
        if (!hex4(r, &u))
            break;
        if (u >= 0xD800 && u <= 0xDBFF) {
            gunichar low;
            Reader save = *r;
            if (r->end - r->p >= 6 && r->p[0] == '\\' && r->p[1] == 'u' && (r->p += 2, hex4(r, &low)) &&
                low >= 0xDC00 && low <= 0xDFFF)
                u = 0x10000 + ((u - 0xD800) << 10) + (low - 0xDC00);
            else {
                *r = save;
                u = 0xFFFD;
            }
        } else if (u >= 0xDC00 && u <= 0xDFFF) {
            u = 0xFFFD;
        }
        if (u == 0)
            break; /* a NUL can't go into a C string */
        g_string_append_unichar(s, u);
    }
bad:
    g_string_free(s, TRUE);
    return NULL;
}

static CjNode *parse_number(Reader *r)
{
    const char *start = r->p;
    char buf[64];
    char *end;
    CjNode *n;
    size_t len;

    while (r->p < r->end && strchr("+-0123456789.eE", *r->p) != NULL)
        r->p++;
    len = (size_t)(r->p - start);
    if (len == 0 || len >= sizeof buf)
        return NULL;
    memcpy(buf, start, len);
    buf[len] = '\0';
    n = new_node(CJ_NUMBER);
    n->number = g_ascii_strtod(buf, &end);
    if (*end != '\0') {
        cj_free(n);
        return NULL;
    }
    return n;
}

static CjNode *parse_container(Reader *r, int depth, gboolean object)
{
    CjNode *n = new_node(object ? CJ_OBJECT : CJ_ARRAY);
    char close = object ? '}' : ']';

    skip_space(r);
    if (r->p < r->end && *r->p == close) {
        r->p++;
        return n;
    }
    for (;;) {
        char *key = NULL;
        CjNode *item;

        skip_space(r);
        if (object) {
            if (r->p >= r->end || *r->p != '"')
                break;
            r->p++;
            key = parse_string(r);
            skip_space(r);
            if (key == NULL || r->p >= r->end || *r->p != ':') {
                g_free(key);
                break;
            }
            r->p++;
        }
        item = parse_value(r, depth + 1);
        if (item == NULL) {
            g_free(key);
            break;
        }
        item->key = key;
        g_ptr_array_add(n->items, item);
        skip_space(r);
        if (r->p < r->end && *r->p == ',') {
            r->p++;
            continue;
        }
        if (r->p < r->end && *r->p == close) {
            r->p++;
            return n;
        }
        break;
    }
    cj_free(n);
    return NULL;
}

static CjNode *parse_value(Reader *r, int depth)
{
    CjNode *n;

    if (depth > MAX_DEPTH)
        return NULL;
    skip_space(r);
    if (r->p >= r->end)
        return NULL;
    switch (*r->p) {
    case '{':
        r->p++;
        return parse_container(r, depth, TRUE);
    case '[':
        r->p++;
        return parse_container(r, depth, FALSE);
    case '"': {
        char *s;
        r->p++;
        s = parse_string(r);
        if (s == NULL)
            return NULL;
        n = new_node(CJ_STRING);
        n->string = s;
        return n;
    }
    case 't':
    case 'f':
        n = new_node(CJ_BOOL);
        n->truth = *r->p == 't';
        if (literal(r, n->truth ? "true" : "false"))
            return n;
        cj_free(n);
        return NULL;
    case 'n':
        return literal(r, "null") ? new_node(CJ_NULL) : NULL;
    default:
        return parse_number(r);
    }
}

CjNode *cj_parse(const char *text, gsize len)
{
    Reader r = { text, text + len };
    CjNode *n;

    if (text == NULL)
        return NULL;
    n = parse_value(&r, 0);
    skip_space(&r);
    if (n != NULL && r.p != r.end) {
        cj_free(n);
        return NULL;
    }
    return n;
}

const CjNode *cj_member(const CjNode *node, const char *key)
{
    const CjNode *found = NULL;
    guint i;

    if (node == NULL || node->type != CJ_OBJECT)
        return NULL;
    for (i = 0; i < node->items->len; i++) {
        const CjNode *m = g_ptr_array_index(node->items, i);
        if (strcmp(m->key, key) == 0)
            found = m;
    }
    return found;
}

const CjNode *cj_index(const CjNode *node, guint i)
{
    if (node == NULL || node->type != CJ_ARRAY || i >= node->items->len)
        return NULL;
    return g_ptr_array_index(node->items, i);
}

const char *cj_string(const CjNode *node)
{
    return node != NULL && node->type == CJ_STRING ? node->string : NULL;
}

void cj_append_string(GString *out, const char *s)
{
    const unsigned char *p;

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
