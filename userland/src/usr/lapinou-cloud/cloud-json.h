/* SPDX-License-Identifier: MIT */
/*
 * The JSON lapinou-cloud needs, without a JSON library (the rabbit has no json-glib): reading a
 * service's answer into a tree, and writing strings into a request.
 */
#ifndef CLOUD_JSON_H
#define CLOUD_JSON_H

#include <glib.h>

typedef enum { CJ_NULL, CJ_BOOL, CJ_NUMBER, CJ_STRING, CJ_ARRAY, CJ_OBJECT } CjType;

typedef struct CjNode {
    CjType type;
    char *key;       /* a member of an object: its name */
    char *string;    /* CJ_STRING: the decoded text (UTF-8) */
    double number;   /* CJ_NUMBER */
    gboolean truth;  /* CJ_BOOL */
    GPtrArray *items; /* CJ_ARRAY: the elements; CJ_OBJECT: the members (CjNode with key) */
} CjNode;

/* The tree of `text` (`len` bytes), or NULL if it isn't one JSON value (invalid UTF-8, nested
 * deeper than 32, more than 20000 values, trailing text). Free with cj_free(). */
CjNode *cj_parse(const char *text, gsize len);
void cj_free(CjNode *node);

/* The member `key` of an object (the last one, if given twice), the element `i` of an array, the
 * text of a string: NULL when `node` isn't of that type (or is NULL) or has none. */
const CjNode *cj_member(const CjNode *node, const char *key);
const CjNode *cj_index(const CjNode *node, guint i);
const char *cj_string(const CjNode *node);

/* Append `s` (UTF-8) as a JSON string, quotes included. */
void cj_append_string(GString *out, const char *s);

#endif
