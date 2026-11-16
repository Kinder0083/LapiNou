/* SPDX-License-Identifier: MIT */
/*
 * The cloud services' requests and answers, for lapinou-cloud (no network here: the daemon sends
 * what these make and gives back what it receives):
 *  - speech to text, OpenAI's /v1/audio/transcriptions: the recording as a WAV file (16 kHz, mono,
 *    PCM16) made from the microphone's G.711 mu-law; the answer {"text": ...};
 *  - the language model: Anthropic's Messages API (/v1/messages) or OpenAI's chat completions
 *    (/v1/chat/completions), with the persona as the system prompt and the conversation's history;
 *  - text to speech, OpenAI's /v1/audio/speech: {model, voice, input, response_format: "mp3"}.
 */
#ifndef LAPINOU_CLOUD_PROVIDERS_H
#define LAPINOU_CLOUD_PROVIDERS_H

#include <glib.h>
#include <time.h>

typedef enum { LLM_ANTHROPIC, LLM_OPENAI } LlmKind;

typedef enum {
    ANSWER_OK,      /* *text is the answer (not empty) */
    ANSWER_REFUSED, /* the model declined (Anthropic's stop_reason "refusal", OpenAI's refusal) */
    ANSWER_CUT,     /* no text: the model stopped at max_tokens (Anthropic) or length (OpenAI) */
    ANSWER_BAD,     /* not an answer: not JSON, not the expected shape, no text */
} AnswerKind;

/* The WAV file (a 44-byte header, then little-endian PCM16) of `n` mu-law bytes at 16 kHz. */
GString *wav_from_mulaw(const guint8 *mulaw, gsize n);
gint16 mulaw_to_pcm(guint8 byte);

/* An endpoint from a setting: `url` without its trailing '/', then `path` (e.g.
 * "/v1/audio/speech") added unless `url` already ends with it; when `url` ends with "/v1", only
 * the rest of `path`. NULL if `url` isn't http:// or https://. Free with g_free(). */
char *endpoint(const char *url, const char *path);

/* --- the conversation --------------------------------------------------------------------- */

/* The history kept in RAM: user and assistant turns, in order, forgotten HISTORY_S after the
 * last answer and kept to HISTORY_TURNS turns and HISTORY_BYTES bytes of text (the oldest
 * question and answer go first). */
#define HISTORY_S 300
#define HISTORY_TURNS 12
#define HISTORY_BYTES 12000

typedef struct {
    GPtrArray *turns; /* Turn* */
    time_t last;      /* when the last answer was added */
} History;

typedef struct {
    gboolean user;
    char *text;
} Turn;

void history_init(History *h);
/* Forget everything when the last answer is older than HISTORY_S at `now`. */
void history_expire(History *h, time_t now);
/* A question and its answer, at `now`. */
void history_add(History *h, const char *question, const char *answer, time_t now);
void history_clear(History *h);

/* --- the requests --------------------------------------------------------------------------- */

/* Whether an Anthropic model takes the server-side fallback (the beta header
 * ANTHROPIC_FALLBACK_BETA and "fallbacks": "default"). */
#define ANTHROPIC_VERSION "2023-06-01"
#define ANTHROPIC_FALLBACK_BETA "server-side-fallback-2026-07-01"
#define LLM_MAX_TOKENS 2048
gboolean anthropic_has_fallback(const char *model);

/* The language model's request body: the persona `system`, the history, then `question`. For
 * Anthropic, `effort` (when not NULL or empty) goes as "output_config": {"effort": ...}. */
GString *llm_request(LlmKind kind, const char *model, const char *effort, const char *system, const History *h,
                     const char *question);
/* The text to speech request body. */
GString *tts_request(const char *model, const char *voice, const char *input);

/* --- the answers ---------------------------------------------------------------------------- */

/* The transcript of a speech to text answer, trimmed (it may be empty: nothing was heard), or
 * NULL if the answer isn't one. */
char *stt_answer(const char *body, gsize len);
/* The language model's answer: its text blocks (Anthropic) or its message (OpenAI). */
AnswerKind llm_answer(LlmKind kind, const char *body, gsize len, char **text);

/* The answer as it is to be spoken: without markdown's * # ` _ marks and with its blank space
 * squeezed, cut to SPEECH_MAX bytes at a sentence's end (or a word's) when it is longer. */
#define SPEECH_MAX 1500
char *speech_text(const char *answer);

#endif
