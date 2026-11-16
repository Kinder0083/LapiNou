# SPDX-License-Identifier: MIT
"""lapinou-cloud: the mode without a server (speech to text, a language model and text to speech in
the cloud), against fakes of the three services.

The daemon's settings are the scenario's own (LAPINOU_CONF_DIR): [server] enabled=0, [cloud]
enabled=1 with the three services at a fake (tests/fakes/fake_cloud.py, on 127.0.0.1:PORT in the
daemon's network namespace, started by sandbox_setup): stt_url the whole endpoint, llm_url the
server alone (llm_kind=anthropic, so <llm_url>/v1/messages), tts_url ending in /v1 (so
/v1/audio/speech is added); the keys are the secrets cloud_stt_key, cloud_llm_key and
cloud_tts_key (test values). The microphone is a fake lapinou-mic socket (LAPINOU_MIC_SOCKET), run
here: it streams G.711 mu-law at 16 kHz, a second of a tone then silence, in real time (or the tone
without end, 8 times as fast, for the 15 s limit). Led and Multimedia are fakes on the test bus
(tests/fakes/fake_karotz_services.py), which log their calls; the scenario makes Multimedia emit
multi_playfinished. The daemon writes /tmp/lapinou-tts.mp3: the sandbox has a private /tmp (as in
controller_platform), so the scenario reads it back.

  1. A question: start_listening(), the tone, stop_listening(): the speech to text request (the
     WAV: 16 kHz, mono, 16 bits, its first second the tone decoded), the language model request
     (the persona as the system prompt, the transcript; no temperature, top_p or thinking; the
     server-side fallback for claude-opus-5-5), the text to speech request (the answer, mp3), then
     Multimedia.play of /tmp/lapinou-tts.mp3 (the service's MP3); idle once it has played.
  2. A follow-up: the history (the first question and answer) comes with the second question.
  3. say(text): the text to speech request and the playback.
  4. Speech to text answering 500: the error (the error sound, the orange LED, 3 s later the status
     LED), no language model request; the daemon goes on.
  5. The language model refusing (stop_reason "refusal"): the error, and a gentle sentence spoken
     instead of an answer.
  6. A recording that isn't stopped: cut at 15 s (480000 bytes of PCM), then answered; the late
     stop_listening() changes nothing.
  7. stop_listening() while idle: nothing.
  8. llm_kind=openai (the settings are read again at each question; llm_url ending in /v1): OpenAI's
     chat completions, the persona as the first message, the history after it.
  9. A transcript over 64 kB, then a language model answer that isn't JSON: the error each time.
 10. No llm_model: claude-haiku-5-5 at output_config.effort "low" (the default [cloud] llm_effort),
     without the server-side fallback (no "fallbacks", no anthropic-beta); then stop_reason
     "max_tokens" with no text: the error.
 11. start_listening() while the language model is answering (its answer 3 s late): that question is
     dropped (no text to speech for it, even once its answer comes), the new one is answered.
 12. say() while listening: refused, nothing sent; the question goes on.
 13. Text to speech answering 500 (a say()): the error.
Then the daemon's log: its failures, and that it never logged a key, a transcript or an answer.
"""

import importlib.util
import json
import math
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from scenarios import conf_fixture

DAEMON = "lapinou-cloud"
DEVICES = []
SERVICE = "com.mindscape.karotz.LapinouCloud"
PATH = "/com/mindscape/karotz/LapinouCloud"
IFACE = "com.mindscape.karotz.LapinouCloud"
PORT = 18080
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FAKE_CLOUD = HERE.parent / "fakes" / "fake_cloud.py"
SERVICES = HERE.parent / "fakes" / "fake_karotz_services.py"
PERSONA = ROOT / "image" / "core" / "usr" / "share" / "lapinou" / "persona_fr_court.txt"
# Test values, not keys of any service.
KEYS = {"cloud_stt_key": "test-stt-key-0001", "cloud_llm_key": "test-llm-key-0002", "cloud_tts_key": "test-tts-key-0003"}
SAID = "Il est l'heure du goûter !"
SAID_LATE = "Ceci ne doit pas être dit."
RATE = 16000

EXPECTED_WARNINGS = [
    r"^speech to text failed: HTTP 500$",
    r"^the language model refused to answer$",
    r"^speech to text failed: the answer is too long$",
    r"^the language model failed: not an answer$",
    r"^the language model failed: it stopped at max_tokens with no text$",
    r"^text to speech failed: HTTP 500$",
]

FAKE = None  # tests/fakes/fake_cloud.py as a module (its answers and MP3)


def sandbox_setup(workdir, sysroot):
    global FAKE
    spec = importlib.util.spec_from_file_location("fake_cloud", FAKE_CLOUD)
    FAKE = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(FAKE)
    w = Path(workdir)
    conf = w / "lapinou-conf"
    conf.mkdir()
    (conf / "lapinou.conf").write_text(
        "[server]\nenabled=0\n[cloud]\nenabled=1\n"
        f"stt_url=http://127.0.0.1:{PORT}/v1/audio/transcriptions\nstt_model=whisper-test\n"
        f"llm_kind=anthropic\nllm_url=http://127.0.0.1:{PORT}/\nllm_model=claude-opus-5-5\n"
        f"tts_url=http://127.0.0.1:{PORT}/v1\ntts_model=tts-test\ntts_voice=nova\n")
    (conf / "lapinou.secrets").write_text("[secrets]\n" + "".join(f"{k}={v}\n" for k, v in KEYS.items()))
    (conf / "lapinou.secrets").chmod(0o600)
    ctrl = w / "cloud"
    ctrl.mkdir()
    # A private /tmp, with the run's directory mounted back in it (where it was under /tmp).
    host_tmp = w / "host-tmp"
    (host_tmp / w.name).mkdir(parents=True)
    mount = conf_fixture.mount_tool()
    return (f"{mount} --rbind {w} {host_tmp / w.name}; {mount} --rbind {host_tmp} /tmp; "
            f"export LAPINOU_CONF_DIR={conf} LAPINOU_MIC_SOCKET={w}/mic.sock; "
            f"{sys.executable} -I {FAKE_CLOUD} {ctrl} {PORT} > /dev/null 2> {ctrl}/err & "
            f"i=0; while [ ! -e {ctrl}/ready ] && [ $i -lt 200 ]; do /bin/sleep 0.1; i=$((i + 1)); done; ")


# --- G.711 mu-law -------------------------------------------------------------------------------

def mulaw_encode(sample):
    sign = 0x80 if sample < 0 else 0
    magnitude = min(abs(sample), 32635) + 0x84
    exponent = 7
    while exponent > 0 and not magnitude & (0x4000 >> (7 - exponent)):
        exponent -= 1
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def mulaw_decode(byte):
    u = ~byte & 0xFF
    sample = (((u & 0x0F) << 3) + 0x84) << ((u >> 4) & 7)
    sample -= 0x84
    return -sample if u & 0x80 else sample


TONE = bytes(mulaw_encode(int(8000 * math.sin(2 * math.pi * 440 * i / RATE))) for i in range(RATE))


class Microphone(threading.Thread):
    """A fake lapinou-mic socket: each connection gets the tone, then silence, in real time
    (mode "speak"), or the tone again and again, 8 times as fast (mode "fast")."""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.path, self.mode, self.connections = path, "speak", 0
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        os.chmod(path, 0o666)
        self.server.listen(4)

    def run(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self.stream, args=(conn, self.mode), daemon=True).start()

    def stream(self, conn, mode):
        block = 320  # 20 ms
        sent = 0
        try:
            while True:
                if mode == "fast":
                    data, pause = TONE[sent % RATE:sent % RATE + 4 * block], 0.01
                else:
                    data = TONE[sent:sent + block] if sent < RATE else b"\xff" * block
                    pause = 0.02
                conn.sendall(data)
                sent += len(data)
                time.sleep(pause)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.server.close()


# --- helpers ---------------------------------------------------------------------------------------

def wait_until(predicate, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


class Services:
    def __init__(self, bus, workdir):
        self.bus, self.workdir = bus, Path(workdir)
        self.log, self.seen, self.proc = self.workdir / "services.log", 0, None
        self.log.touch()

    def start(self):
        self.proc = subprocess.Popen([sys.executable, "-I", str(SERVICES), self.bus.address, str(self.log)],
                                     stdout=subprocess.DEVNULL, stderr=open(self.workdir / "services.err", "a"))
        return wait_until((self.workdir / "ready").exists, 20)

    def calls(self):
        return self.log.read_text().splitlines()

    def pending(self):
        return self.calls()[self.seen:]

    def new(self):
        lines = self.calls()
        out, self.seen = lines[self.seen:], len(lines)
        return out

    def emit(self, name, member, *args):
        return self.bus.send(f"--dest=com.mindscape.karotz.{name}", f"/com/mindscape/karotz/{name}",
                             "com.lapinou.TestControl.emit", f"string:{name}", f"string:{member}",
                             f"string:{json.dumps(list(args))}")

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.proc.wait()


class Cloud:
    """The fake services' log, read as it grows."""

    def __init__(self, ctrl):
        self.ctrl, self.seen = Path(ctrl), 0

    def entries(self):
        try:
            return [json.loads(l) for l in (self.ctrl / "log").read_text().splitlines() if l.strip()]
        except (OSError, ValueError):
            return []

    def pending(self):
        return self.entries()[self.seen:]

    def new(self):
        e = self.entries()
        out, self.seen = e[self.seen:], len(e)
        return out

    def set(self, name, value=""):
        (self.ctrl / name).write_text(value)

    def clear(self, name):
        (self.ctrl / name).unlink(missing_ok=True)


def describe(e, ctrl):
    kind = e["kind"]
    head = f"  {kind.upper()} POST {e['path']} ({e['content_type']}, user agent {e['user_agent']})"
    lines = [head]
    if kind == "stt":
        lines.append(f"    Authorization is Bearer + the STT key: {e['authorization'] == 'Bearer ' + KEYS['cloud_stt_key']}")
        lines.append(f"    fields: {e.get('fields')}")
        f = e.get("file")
        if f:
            fmt = {k: v for k, v in f.items() if k not in ("filename", "content_type", "data_bytes")}
            lines.append(f"    file {f['filename']} ({f['content_type']}): {fmt}")
            lines.extend("    " + l for l in wav_check(ctrl / f"stt-{e['n']}.wav", f.get("data_bytes", 0)))
    elif kind == "llm":
        persona = persona_text()
        lines.append(f"    x-api-key is the LLM key: {e['x_api_key'] == KEYS['cloud_llm_key']}, "
                     f"Authorization: {e['authorization']}, anthropic-version: {e['anthropic_version']}, "
                     f"anthropic-beta: {e['anthropic_beta']}")
        lines.append(f"    keys: {e['keys']}; model {e['model']}, max_tokens {e['max_tokens']}, fallbacks {e['fallbacks']}, "
                     f"output_config {e.get('output_config')}")
        lines.append(f"    system is persona_fr_court.txt: {persona is not None and e['system'] == persona}")
        for m in e["messages"] or []:
            lines.append(f"    message: {json.dumps(m, ensure_ascii=False)}")
    elif kind == "chat":
        lines.append(f"    Authorization is Bearer + the LLM key: {e['authorization'] == 'Bearer ' + KEYS['cloud_llm_key']}, "
                     f"x-api-key: {e['x_api_key']}")
        lines.append(f"    keys: {e['keys']}; model {e['model']}, max_tokens {e['max_tokens']}")
        for m in e["messages"] or []:
            if m.get("role") == "system":
                lines.append(f"    message: system, persona_fr_court.txt: {m.get('content') == persona_text()}")
            else:
                lines.append(f"    message: {json.dumps(m, ensure_ascii=False)}")
    elif kind == "tts":
        lines.append(f"    Authorization is Bearer + the TTS key: {e['authorization'] == 'Bearer ' + KEYS['cloud_tts_key']}")
        lines.append(f"    keys: {e['keys']}; model {e['model']}, voice {e['voice']}, "
                     f"response_format {e['response_format']}")
        lines.append(f"    input: {e['input']}")
    if "answered" in e:
        lines.append(f"    answered HTTP {e['answered']}")
    if "error" in e:
        lines.append(f"    bad request: {e['error']}")
    return lines


def persona_text():
    """persona_fr_court.txt as it is sent: without its comment lines at the start."""
    lines = PERSONA.read_text(encoding="utf-8").splitlines(keepends=True)
    while lines and lines[0].startswith("#"):
        lines.pop(0)
    return "".join(lines).strip()


def wav_check(path, data_bytes):
    """What the WAV's samples are: its length, and whether its first second is the tone decoded."""
    try:
        pcm = path.read_bytes()[44:]
    except OSError:
        return ["no WAV kept"]
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm[:len(pcm) // 2 * 2])
    first = list(samples[:RATE]) == [mulaw_decode(b) for b in TONE]
    if data_bytes == 15 * RATE * 2:
        return [f"data: {data_bytes} bytes, 15 s exactly; it starts with the tone decoded: {first}"]
    return [f"data: at least 1 s and under 3 s: {RATE * 2 <= data_bytes < 3 * RATE * 2}; "
            f"its first second is the tone decoded: {first}"]


def drive(d, bus):
    from harness import introspection

    work = Path(d.workdir)
    services = Services(bus, work)
    cloud = Cloud(work / "cloud")
    mic = Microphone(str(work / "mic.sock"))
    mic.start()
    mp3 = work / "host-tmp" / "lapinou-tts.mp3"
    record = [f"== registered: {bus.wait_for_name(SERVICE)}", "== introspection"]
    record.extend(introspection(bus, SERVICE, PATH))

    def states():
        return [s for s in bus.signals() if s.startswith(f"{IFACE}.state(")]

    def idle_now():
        return states()[-1:] == [f'{IFACE}.state(string "idle")']

    def state_count(name):
        return sum(f'"{name}"' in s for s in states())

    def call(method, *args):
        """The method's reply: "answered" (no value), or the error."""
        out = bus.send(f"--dest={SERVICE}", PATH, f"{IFACE}.{method}", *args)
        return "answered" if out.startswith("method return") else out

    def step(title, until=None, settle=0.5, timeout=30):
        ok = wait_until(until, timeout) if until else True
        time.sleep(settle if until else 2)
        record.append(f"== {title}" + ("" if ok else " (timed out)"))
        for e in cloud.new():
            record.extend(describe(e, work / "cloud"))
        record.extend(f"  call {c}" for c in services.new())

    def played():
        return lambda: any("Multimedia.play(" in c for c in services.pending())

    def ask(seconds=1.3):
        call("start_listening")
        time.sleep(seconds)
        call("stop_listening")

    def finish_playing():
        idle = state_count("idle")
        services.emit("Multimedia", "multi_playfinished", "lapinou-cloud", 4)
        return wait_until(lambda: state_count("idle") > idle, 10)

    try:
        record.append(f"fake daemons ready: {services.start()}")

        # 1. A question
        ask()
        step("1. a question", played())
        record.append(f"the played file is the service's MP3: {mp3.exists() and mp3.read_bytes() == FAKE.MP3}")
        record.append(f"idle once played: {finish_playing()}")

        # 2. A follow-up
        ask()
        step("2. a follow-up: the history comes with it", played())
        record.append(f"idle once played: {finish_playing()}")

        # 3. say()
        record.append(f"say: {call('say', 'string:' + SAID)}")
        step("3. say()", played())
        record.append(f"idle once played: {finish_playing()}")

        # 4. Speech to text answers 500
        cloud.set("stt-status", "500")
        errors = state_count("error")
        ask()
        step("4. speech to text answers 500: the error", lambda: any("Led.pulse(" in c for c in services.pending()),
             timeout=20)
        back = wait_until(idle_now, 10)
        record.append(f"state error, then idle: {state_count('error') > errors}, {back}")
        record.append(f"still running: {d.alive()}")
        cloud.clear("stt-status")

        # 5. The language model refuses
        cloud.set("llm-refusal")
        ask()
        step("5. the language model refuses: the error and a gentle sentence",
             lambda: any("Led.pulse(" in c for c in services.pending()) and any("Multimedia.play(" in c for c in services.pending()),
             timeout=20)
        record.append(f"idle once played: {finish_playing()}")
        record.append(f"still running: {d.alive()}")
        cloud.clear("llm-refusal")

        # 6. 15 s at most
        mic.mode = "fast"
        call("start_listening")
        step("6. a recording not stopped: cut at 15 s", played(), timeout=60)
        call("stop_listening")
        record.append(f"idle once played: {finish_playing()}")
        step("the late stop_listening(): nothing")

        # 7. stop_listening() while idle
        call("stop_listening")
        step("7. stop_listening() while idle: nothing")

        # 8. OpenAI's chat completions, the settings read again
        conf = work / "lapinou-conf" / "lapinou.conf"
        conf.write_text(conf.read_text().replace("llm_kind=anthropic", "llm_kind=openai")
                        .replace(f"llm_url=http://127.0.0.1:{PORT}/", f"llm_url=http://127.0.0.1:{PORT}/v1")
                        .replace("llm_model=claude-opus-5-5", "llm_model=gpt-test"))
        mic.mode = "speak"
        ask()
        step("8. llm_kind=openai: chat completions", played())
        record.append(f"idle once played: {finish_playing()}")

        # 9. Answers that aren't answers
        for name, title in (("stt-huge", "9. a transcript longer than 64 kB: the error"),
                            ("llm-garbage", "an answer that isn't JSON: the error")):
            cloud.set(name)
            ask()
            step(title, lambda: any("Led.pulse(" in c for c in services.pending()), timeout=20)
            cloud.clear(name)
            record.append(f"idle again: {wait_until(idle_now, 10)}")

        # 10. The default model: claude-haiku-5-5 at effort low, no server-side fallback
        conf.write_text(conf.read_text().replace("llm_kind=openai", "llm_kind=anthropic")
                        .replace(f"llm_url=http://127.0.0.1:{PORT}/v1", f"llm_url=http://127.0.0.1:{PORT}")
                        .replace("llm_model=gpt-test\n", ""))
        ask()
        step("10. no llm_model: claude-haiku-5-5, effort low, no fallback", played())
        record.append(f"idle once played: {finish_playing()}")
        cloud.set("llm-cut")
        ask()
        step("max_tokens reached with no text: the error", lambda: any("Led.pulse(" in c for c in services.pending()),
             timeout=20)
        cloud.clear("llm-cut")
        record.append(f"idle again: {wait_until(idle_now, 10)}")

        # 11. A new question while the language model is answering: the first one dropped
        cloud.set("llm-delay", "3")
        call("start_listening")
        time.sleep(1.3)
        call("stop_listening")
        wait_until(lambda: any(e["kind"] == "llm" for e in cloud.pending()), 20)
        cloud.clear("llm-delay")
        ask()
        step("11. start_listening() while the language model answers: only the new question is answered", played())
        time.sleep(3.5)  # the first answer comes, to a request that is no longer there
        step("the first answer, late: nothing more")
        record.append(f"idle once played: {finish_playing()}")

        # 12. say() while listening: nothing sent
        call("start_listening")
        record.append(f"say while listening: {call('say', 'string:' + SAID_LATE)}")
        time.sleep(1.3)
        call("stop_listening")
        step("12. say() while listening: refused, the question goes on", played())
        record.append(f"idle once played: {finish_playing()}")

        # 13. Text to speech answers 500
        cloud.set("tts-status", "500")
        record.append(f"say: {call('say', 'string:' + SAID)}")
        step("13. text to speech answers 500: the error", lambda: any("Led.pulse(" in c for c in services.pending()),
             timeout=20)
        cloud.clear("tts-status")
        record.append(f"state error, then idle: {wait_until(idle_now, 10)}")
        record.append(f"still running: {d.alive()}")
        record.append(f"microphone connections: {mic.connections}")
        record.append(f"still running: {d.alive()}")

        record.append("== the daemon's log: failures")
        log = Path(d.syslog).read_text(errors="replace") if Path(d.syslog).exists() else ""
        record.extend("  " + l for l in log.splitlines() if re.search(r"failed|refused|cut at|not configured|max_tokens", l))
        secret = list(KEYS.values()) + [FAKE.TRANSCRIPT, SAID, SAID_LATE] + FAKE.ANSWERS
        record.append(f"the log has a key, a transcript, an answer or said text: {any(s in log for s in secret)}")
    finally:
        services.stop()
        mic.close()
    return record
