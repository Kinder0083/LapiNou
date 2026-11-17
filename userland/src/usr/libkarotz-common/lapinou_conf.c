/* SPDX-License-Identifier: MIT */
/* LapiNou's settings on the rabbit, read in C; see lapinou_conf.h for the format. */
#include "lapinou_conf.h"

#include <string.h>

static gboolean name_ok(const char *s)
{
    if (!s || !*s)
        return FALSE;
    for (; *s; s++)
        if (!g_ascii_isalnum(*s) && *s != '_' && *s != '-' && *s != '.')
            return FALSE;
    return TRUE;
}

static char *decode(const char *v)
{
    GString *out = g_string_new(NULL);
    for (; *v; v++) {
        if (*v == '\\' && v[1]) {
            v++;
            if (*v == 'n')
                g_string_append_c(out, '\n');
            else if (*v == 'r')
                g_string_append_c(out, '\r');
            else if (*v == '\\' || *v == '=')
                g_string_append_c(out, *v);
            else {
                g_string_append_c(out, '\\');
                g_string_append_c(out, *v);
            }
        } else {
            g_string_append_c(out, *v);
        }
    }
    return g_string_free(out, FALSE);
}

/* The decoded value of the last `key=` line in [section] of `file`, or NULL. */
static char *read_value(const char *file, const char *section, const char *key)
{
    char *text = NULL, *found = NULL;
    if (!g_file_get_contents(file, &text, NULL, NULL))
        return NULL;
    char *header = g_strdup_printf("[%s]", section);
    char *prefix = g_strdup_printf("%s=", key);
    gsize plen = strlen(prefix);
    gboolean in_section = FALSE;
    char **lines = g_strsplit(text, "\n", -1);
    for (char **l = lines; *l; l++) {
        if ((*l)[0] == '[')
            in_section = strcmp(*l, header) == 0;
        else if (in_section && strncmp(*l, prefix, plen) == 0) {
            g_free(found);
            found = decode(*l + plen);
        }
    }
    g_strfreev(lines);
    g_free(header);
    g_free(prefix);
    g_free(text);
    return found;
}

static char *path_in_conf_dir(const char *name)
{
    const char *dir = g_getenv("LAPINOU_CONF_DIR");
    return g_build_filename(dir && *dir ? dir : "/usr/etc/conf", name, NULL);
}

char *lapinou_conf_get(const char *section, const char *key, const char *def)
{
    char *value = NULL;
    if (name_ok(section) && name_ok(key)) {
        char *file = path_in_conf_dir("lapinou.conf");
        value = read_value(file, section, key);
        g_free(file);
    }
    if (value && *value)
        return value;
    g_free(value);
    return g_strdup(def);
}

char *lapinou_secret_get(const char *key)
{
    char *value = NULL;
    if (name_ok(key)) {
        char *file = path_in_conf_dir("lapinou.secrets");
        value = read_value(file, "secrets", key);
        g_free(file);
    }
    if (value && !*value) {
        g_free(value);
        value = NULL;
    }
    return value;
}
