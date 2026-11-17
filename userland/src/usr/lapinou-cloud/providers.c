/* SPDX-License-Identifier: MIT */
/* The cloud services' requests and answers (providers.h). */
#include <string.h>

#include "cloud-json.h"
#include "providers.h"

#define RATE 16000

/* --- the recording ----------------------------------------------------------------------- */

/* G.711 mu-law to linear PCM, as G.711 defines it (-32124 to 32124). */
gint16 mulaw_to_pcm(guint8 byte)
{
    int u = ~byte & 0xFF;
    int sample = (((u & 0x0F) << 3) + 0x84) << ((u >> 4) & 7);

    sample -= 0x84;
    return (gint16)(u & 0x80 ? -sample : sample);
}

static void put_le32(GString *s, guint32 v)
{
    char b[4] = { (char)(v & 0xFF), (char)((v >> 8) & 0xFF), (char)((v >> 16) & 0xFF), (char)(v >> 24) };
    g_string_append_len(s, b, 4);
}

static void put_le16(GString *s, guint16 v)
{
    char b[2] = { (char)(v & 0xFF), (char)(v >> 8) };
    g_string_append_len(s, b, 2);
}

GString *wav_from_mulaw(const guint8 *mulaw, gsize n)
{
    GString *s = g_string_sized_new(44 + 2 * n);
    gsize i;

    g_string_append_len(s, "RIFF", 4);
    put_le32(s, (guint32)(36 + 2 * n));
    g_string_append_len(s, "WAVEfmt ", 8);
    put_le32(s, 16);
    put_le16(s, 1);            /* PCM */
    put_le16(s, 1);            /* mono */
    put_le32(s, RATE);
    put_le32(s, RATE * 2);     /* bytes a second */
    put_le16(s, 2);            /* bytes a frame */
    put_le16(s, 16);           /* bits a sample */
    g_string_append_len(s, "data", 4);
    put_le32(s, (guint32)(2 * n));
    for (i = 0; i < n; i++)
        put_le16(s, (guint16)mulaw_to_pcm(mulaw[i]));
    return s;
}

char *endpoint(const char *url, const char *path)
{
    char *base;
    size_t n;

    if (url == NULL || (g_ascii_strncasecmp(url, "http://", 7) != 0 && g_ascii_strncasecmp(url, "https://", 8) != 0))
        return NULL;
    base = g_strdup(url);
    n = strlen(base);
    while (n > 0 && base[n - 1] == '/')
        base[--n] = '\0';
    if (g_str_has_suffix(base, path)) {
        /* already the whole endpoint */
    } else if (g_str_has_suffix(base, "/v1") && g_str_has_prefix(path, "/v1/")) {
        char *full = g_strconcat(base, path + 3, NULL);
        g_free(base);
        base = full;
    } else {
        char *full = g_strconcat(base, path, NULL);
        g_free(base);
        base = full;
    }
    return base;
}

/* --- the conversation ------------------------------------------------------------------- */

static void turn_free(gpointer p)
{
    Turn *t = p;

    g_free(t->text);
    g_free(t);
}

void history_init(History *h)
{
    h->turns = g_ptr_array_new_with_free_func(turn_free);
    h->last = 0;
}

void history_clear(History *h)
{
    g_ptr_array_set_size(h->turns, 0);
}

void history_expire(History *h, time_t now)
{
    if (h->turns->len > 0 && (now < h->last || now - h->last > HISTORY_S))
        history_clear(h);
}

static gsize history_bytes(const History *h)
{
    gsize n = 0;
    guint i;

    for (i = 0; i < h->turns->len; i++)
        n += strlen(((Turn *)g_ptr_array_index(h->turns, i))->text);
    return n;
}

void history_add(History *h, const char *question, const char *answer, time_t now)
{
    Turn *q = g_new0(Turn, 1), *a = g_new0(Turn, 1);

    q->user = TRUE;
    q->text = g_strdup(question);
    a->text = g_strdup(answer);
    g_ptr_array_add(h->turns, q);
    g_ptr_array_add(h->turns, a);
    h->last = now;
    /* The oldest question and answer go first; the newest pair always stays. */
    while (h->turns->len > 2 && (h->turns->len > HISTORY_TURNS || history_bytes(h) > HISTORY_BYTES))
        g_ptr_array_remove_range(h->turns, 0, 2);
}

/* --- the requests ------------------------------------------------------------------------- */

gboolean anthropic_has_fallback(const char *model)
{
    static const char *const models[] = { "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5",
                                          "claude-fable-5-2", "claude-fable-5-1", NULL };
    int i;

    for (i = 0; models[i] != NULL; i++) {
        size_t n = strlen(models[i]);
        /* the model, or a dated name of it (e.g. claude-opus-5-5-20260101), not another one */
        if (strncmp(model, models[i], n) == 0 &&
            (model[n] == '\0' || (model[n] == '-' && g_ascii_isdigit(model[n + 1]) && strlen(model + n + 1) == 8)))
            return TRUE;
    }
    return FALSE;
}

static void append_message(GString *out, const char *role, const char *text)
{
    g_string_append(out, "{\"role\":");
    cj_append_string(out, role);
    g_string_append(out, ",\"content\":");
    cj_append_string(out, text);
    g_string_append_c(out, '}');
}

GString *llm_request(LlmKind kind, const char *model, const char *effort, const char *system, const History *h,
                     const char *question)
{
    GString *out = g_string_new("{\"model\":");
    guint i;

    cj_append_string(out, model);
    g_string_append_printf(out, ",\"max_tokens\":%d", LLM_MAX_TOKENS);
    if (kind == LLM_ANTHROPIC) {
        /* No temperature, top_p or thinking: current models refuse them. */
        if (anthropic_has_fallback(model))
            g_string_append(out, ",\"fallbacks\":\"default\"");
        if (effort != NULL && *effort != '\0') {
            g_string_append(out, ",\"output_config\":{\"effort\":");
            cj_append_string(out, effort);
            g_string_append_c(out, '}');
        }
        g_string_append(out, ",\"system\":");
        cj_append_string(out, system);
        g_string_append(out, ",\"messages\":[");
    } else {
        g_string_append(out, ",\"messages\":[");
        append_message(out, "system", system);
        g_string_append_c(out, ',');
    }
    for (i = 0; i < h->turns->len; i++) {
        const Turn *t = g_ptr_array_index(h->turns, i);
        append_message(out, t->user ? "user" : "assistant", t->text);
        g_string_append_c(out, ',');
    }
    append_message(out, "user", question);
    g_string_append(out, "]}");
    return out;
}

GString *tts_request(const char *model, const char *voice, const char *input)
{
    GString *out = g_string_new("{\"model\":");

    cj_append_string(out, model);
    g_string_append(out, ",\"voice\":");
    cj_append_string(out, voice);
    g_string_append(out, ",\"input\":");
    cj_append_string(out, input);
    g_string_append(out, ",\"response_format\":\"mp3\"}");
    return out;
}

/* --- the answers -------------------------------------------------------------------------- */

char *stt_answer(const char *body, gsize len)
{
    CjNode *root = cj_parse(body, len);
    const char *text = cj_string(cj_member(root, "text"));
    char *out = text != NULL ? g_strstrip(g_strdup(text)) : NULL;

    cj_free(root);
    return out;
}

AnswerKind llm_answer(LlmKind kind, const char *body, gsize len, char **text)
{
    CjNode *root = cj_parse(body, len);
    AnswerKind result = ANSWER_BAD;
    GString *s = g_string_new(NULL);

    *text = NULL;
    if (root == NULL) {
        g_string_free(s, TRUE);
        return ANSWER_BAD;
    }
    if (kind == LLM_ANTHROPIC) {
        const CjNode *content = cj_member(root, "content");
        const char *stop = cj_string(cj_member(root, "stop_reason"));
        guint i;

        if (stop != NULL && strcmp(stop, "refusal") == 0) {
            result = ANSWER_REFUSED; /* and nothing of a partial answer */
        } else if (content != NULL && content->type == CJ_ARRAY) {
            /* Only the text blocks: thinking blocks, a fallback's marker and the like aren't said. */
            for (i = 0; i < content->items->len; i++) {
                const CjNode *block = g_ptr_array_index(content->items, i);
                const char *type = cj_string(cj_member(block, "type"));
                const char *t = cj_string(cj_member(block, "text"));
                if (type != NULL && strcmp(type, "text") == 0 && t != NULL) {
                    if (s->len > 0)
                        g_string_append_c(s, ' ');
                    g_string_append(s, t);
                }
            }
            result = ANSWER_OK;
        }
    } else {
        const CjNode *choice = cj_index(cj_member(root, "choices"), 0);
        const CjNode *message = cj_member(choice, "message");
        const char *finish = cj_string(cj_member(choice, "finish_reason"));
        const char *refusal = cj_string(cj_member(message, "refusal"));
        const char *t = cj_string(cj_member(message, "content"));

        if ((finish != NULL && strcmp(finish, "content_filter") == 0) || (refusal != NULL && *refusal != '\0')) {
            result = ANSWER_REFUSED;
        } else if (t != NULL) {
            g_string_append(s, t);
            result = ANSWER_OK;
        }
    }
    g_strstrip(s->str);
    if (result == ANSWER_OK && s->str[0] == '\0') {
        /* No text: cut by the token limit, or not an answer. */
        const char *stop = kind == LLM_ANTHROPIC
                               ? cj_string(cj_member(root, "stop_reason"))
                               : cj_string(cj_member(cj_index(cj_member(root, "choices"), 0), "finish_reason"));
        const char *cut = kind == LLM_ANTHROPIC ? "max_tokens" : "length";
        result = stop != NULL && strcmp(stop, cut) == 0 ? ANSWER_CUT : ANSWER_BAD;
    }
    cj_free(root);
    if (result == ANSWER_OK)
        *text = g_strdup(s->str);
    g_string_free(s, TRUE);
    return result;
}

char *speech_text(const char *answer)
{
    GString *s = g_string_new(NULL);
    const char *p;
    gboolean space = FALSE;
    gsize cut = 0;
    gsize i;

    for (p = answer; *p; p++) {
        if (strchr("*#`_", *p) != NULL)
            continue;
        if (g_ascii_isspace(*p)) {
            space = s->len > 0;
            continue;
        }
        if (space)
            g_string_append_c(s, ' ');
        space = FALSE;
        g_string_append_c(s, *p);
    }
    if (s->len <= SPEECH_MAX)
        return g_string_free(s, FALSE);
    /* The last sentence's end within SPEECH_MAX, else the last space, else a character's start. */
    for (i = SPEECH_MAX; i > SPEECH_MAX / 2 && cut == 0; i--)
        if (strchr(".!?", s->str[i - 1]) != NULL && (s->str[i] == ' ' || s->str[i] == '\0'))
            cut = i;
    for (i = SPEECH_MAX; i > 0 && cut == 0; i--)
        if (s->str[i] == ' ')
            cut = i;
    if (cut == 0) {
        cut = SPEECH_MAX;
        while (cut > 0 && ((unsigned char)s->str[cut] & 0xC0) == 0x80)
            cut--;
    }
    g_string_truncate(s, cut);
    return g_string_free(s, FALSE);
}
