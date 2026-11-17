# SPDX-License-Identifier: MIT
"""A fake of the three cloud services lapinou-cloud calls, for tests/scenarios/lapinou_cloud.py.

    fake_cloud.py CTRL PORT

Listens on 127.0.0.1:PORT (HTTP, in the daemon's network namespace) and answers:
  POST /v1/audio/transcriptions  speech to text, OpenAI's form (multipart: model, language, file):
                                 {"text": <CTRL/stt-text, or TRANSCRIPT>}
  POST /v1/messages              Anthropic's Messages API: a thinking block, then the text block
                                 ANSWERS[n] (n counts the requests); with CTRL/llm-refusal, a
                                 refusal (stop_reason "refusal", no content)
  POST /v1/chat/completions      OpenAI's chat completions: the message ANSWERS[n] (n counts the
                                 requests to either language model)
  POST /v1/audio/speech          text to speech, OpenAI's form (JSON): MP3 bytes (MP3)
With CTRL/<name>-status (stt, llm, chat or tts) holding an HTTP status, that endpoint answers it instead;
with CTRL/stt-huge, a transcript of 200 kB; with CTRL/llm-garbage, JSON cut short (either language model);
with CTRL/llm-cut, Anthropic's stop_reason "max_tokens" with no text; with CTRL/llm-delay holding a number
of seconds, Anthropic's answer that much later.
Each request is logged to CTRL/log as one JSON object (what the scenario records: the headers that
matter, the fields, the WAV's format; the WAV itself is kept as CTRL/stt-<n>.wav). CTRL/ready is
written once it listens.
"""

import email.parser
import email.policy
import json
import os
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TRANSCRIPT = "Pourquoi le ciel est bleu ?"
ANSWERS = ["Le ciel est bleu parce que l'air disperse surtout la lumière bleue du soleil.",
           "Oui, et le soir il devient orange, car la lumière traverse plus d'air."]
# A few bytes that start like an MP3 file (an ID3 tag), enough for the scenario to compare.
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + bytes(range(256)) * 8
CTRL = None
lock = threading.Lock()
counts = {"stt": 0, "llm": 0, "chat": 0, "tts": 0}


def note(entry):
    with lock:
        with open(os.path.join(CTRL, "log"), "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def control(name):
    try:
        with open(os.path.join(CTRL, name)) as f:
            return f.read().strip()
    except OSError:
        return None


def wav_format(data):
    """The WAV's format as the scenario records it, or what is wrong with it."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:16] != b"WAVEfmt ":
        return {"error": "not a WAV file"}
    riff_size = struct.unpack("<I", data[4:8])[0]
    fmt_size, tag, channels, rate, byte_rate, align, bits = struct.unpack("<IHHIIHH", data[16:36])
    if data[36:40] != b"data":
        return {"error": "no data chunk after fmt"}
    size = struct.unpack("<I", data[40:44])[0]
    return {"riff_size_ok": riff_size == len(data) - 8, "fmt_size": fmt_size, "pcm": tag == 1,
            "channels": channels, "rate": rate, "byte_rate": byte_rate, "align": align, "bits": bits,
            "data_bytes": size, "data_bytes_ok": size == len(data) - 44}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        kind = {"/v1/audio/transcriptions": "stt", "/v1/messages": "llm", "/v1/chat/completions": "chat",
                "/v1/audio/speech": "tts"}.get(self.path)
        if kind is None:
            note({"kind": "unknown", "path": self.path})
            return self.reply(404, {"error": "not found"})
        with lock:
            counts[kind] += 1
            n = counts[kind]
        entry = {"kind": kind, "n": n, "path": self.path, "content_type": self.headers.get("Content-Type", "").split(";")[0],
                 "authorization": self.headers.get("Authorization"), "user_agent": self.headers.get("User-Agent")}
        try:
            getattr(self, kind)(entry, body, n)
        except OSError:  # the client has gone (a cancelled request)
            pass
        except (ValueError, KeyError, TypeError) as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            note(entry)
            self.reply(400, {"error": str(e)})

    def failing(self, entry, kind):
        status = control(f"{kind}-status")
        if status:
            entry["answered"] = int(status)
            note(entry)
            self.reply(int(status), {"error": {"message": "fake failure"}})
            return True
        return False

    def stt(self, entry, body, n):
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + self.headers.get("Content-Type", "").encode() + b"\r\n\r\n" + body)
        if not msg.is_multipart():
            raise ValueError("not multipart")
        fields, files = {}, {}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True)
            if part.get_filename():
                files[name] = {"filename": part.get_filename(), "content_type": part.get_content_type(), "data": payload}
            else:
                fields[name] = payload.decode()
        entry["fields"] = fields
        wav = files.get("file")
        if wav:
            entry["file"] = {"filename": wav["filename"], "content_type": wav["content_type"], **wav_format(wav["data"])}
            with open(os.path.join(CTRL, f"stt-{n}.wav"), "wb") as f:
                f.write(wav["data"])
        if self.failing(entry, "stt"):
            return
        note(entry)
        if control("stt-huge") is not None:  # longer than any transcript lapinou-cloud takes
            return self.reply(200, {"text": "bla " * 50000})
        self.reply(200, {"text": control("stt-text") or TRANSCRIPT})

    def llm(self, entry, body, n):
        req = json.loads(body)
        entry.update({"x_api_key": self.headers.get("x-api-key"), "anthropic_version": self.headers.get("anthropic-version"),
                      "anthropic_beta": self.headers.get("anthropic-beta"), "keys": sorted(req), "model": req.get("model"),
                      "max_tokens": req.get("max_tokens"), "fallbacks": req.get("fallbacks"),
                      "output_config": req.get("output_config"),
                      "system": req.get("system"), "messages": req.get("messages")})
        if self.failing(entry, "llm"):
            return
        note(entry)
        delay = control("llm-delay")
        if delay:
            time.sleep(float(delay))
        if control("llm-garbage") is not None:
            return self.reply(200, b'{"content": [{"type": "text", "text": "pas fini', "application/json")
        if control("llm-cut") is not None:
            return self.reply(200, {"id": f"msg_{n}", "type": "message", "role": "assistant", "model": req.get("model"),
                                    "content": [{"type": "thinking", "thinking": "", "signature": "c2ln"}],
                                    "stop_reason": "max_tokens"})
        if control("llm-refusal") is not None:
            return self.reply(200, {"id": f"msg_{n}", "type": "message", "role": "assistant", "model": req.get("model"),
                                    "content": [], "stop_reason": "refusal",
                                    "stop_details": {"type": "refusal", "category": None, "explanation": None}})
        self.reply(200, {"id": f"msg_{n}", "type": "message", "role": "assistant", "model": req.get("model"),
                         "content": [{"type": "thinking", "thinking": "", "signature": "c2ln"},
                                     {"type": "text", "text": ANSWERS[(n - 1) % len(ANSWERS)]}],
                         "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 20}})

    def chat(self, entry, body, n):
        req = json.loads(body)
        entry.update({"x_api_key": self.headers.get("x-api-key"), "keys": sorted(req), "model": req.get("model"),
                      "max_tokens": req.get("max_tokens"), "messages": req.get("messages")})
        if self.failing(entry, "chat"):
            return
        note(entry)
        if control("llm-garbage") is not None:
            return self.reply(200, b'{"choices": [{"message": {"content": "pas fini', "application/json")
        with lock:
            answered = counts["llm"] + counts["chat"]
        self.reply(200, {"id": f"chatcmpl-{n}", "object": "chat.completion", "model": req.get("model"),
                         "choices": [{"index": 0, "finish_reason": "stop",
                                      "message": {"role": "assistant", "content": ANSWERS[(answered - 1) % len(ANSWERS)],
                                                  "refusal": None}}]})

    def tts(self, entry, body, n):
        req = json.loads(body)
        entry.update({"keys": sorted(req), "model": req.get("model"), "voice": req.get("voice"),
                      "input": req.get("input"), "response_format": req.get("response_format")})
        if self.failing(entry, "tts"):
            return
        note(entry)
        self.reply(200, MP3, "audio/mpeg")


def main():
    global CTRL
    CTRL, port = sys.argv[1], int(sys.argv[2])
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    open(os.path.join(CTRL, "ready"), "w").close()
    server.serve_forever()


if __name__ == "__main__":
    main()
