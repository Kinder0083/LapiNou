/* SPDX-License-Identifier: MIT */
/*
 * LapiNou's settings on the rabbit, for C programs: /usr/etc/conf/lapinou.conf (INI, not secret)
 * and /usr/etc/conf/lapinou.secrets (mode 0600, [secrets] section), the files lapinou_get and
 * lapinou_secret_get read in image/core/usr/share/lapinou/conf.sh. A value is one line, with \ as
 * \\, a line feed as \n, a carriage return as \r and = as \=. The environment variable
 * LAPINOU_CONF_DIR moves both files (tests). Nothing here writes: the shell library does.
 */
#ifndef LAPINOU_CONF_H
#define LAPINOU_CONF_H

#include <glib.h>

/* The value of `key` in [`section`], or a copy of `def` (NULL if `def` is NULL) when the key is
 * absent or empty or the file can't be read. Free with g_free(). */
char *lapinou_conf_get(const char *section, const char *key, const char *def);

/* The secret `key` (mqtt_password, cloud_stt_key, cloud_llm_key, cloud_tts_key, ota_token), or
 * NULL when it isn't set. Free with g_free(). */
char *lapinou_secret_get(const char *key);

#endif
