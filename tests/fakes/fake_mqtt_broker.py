# SPDX-License-Identifier: MIT
"""A small MQTT 3.1.1 broker for lapinou-mqtt's scenario (tests/scenarios/lapinou_mqtt.py).

    fake_mqtt_broker.py CTRL_DIR PORT

Listens on 127.0.0.1:PORT (run inside the daemon's network namespace, like fake_audio_server.py),
and writes CTRL_DIR/ready once it does. It keeps retained messages and subscriptions (the + and #
wildcards), answers CONNECT, PUBLISH (QoS 0 and 1), SUBSCRIBE, UNSUBSCRIBE, PINGREQ and
DISCONNECT, and publishes a client's will when its connection ends without a DISCONNECT. What it
sees is logged to CTRL_DIR/log, one JSON object per line (pings are not logged: their number
depends on timing):

  {"ev": "connect", "conn": N, "protocol": [name, level], "clean": bool, "keepalive": s,
   "client_id": ..., "username": ... or null, "password_b64": ... or null,
   "will": {"topic", "payload_b64", "qos", "retain"} or null}
  {"ev": "publish", "conn": N, "topic": ..., "payload_b64": ..., "qos": q, "retain": bool}
  {"ev": "subscribe", "conn": N, "topics": [[filter, qos], ...]}
  {"ev": "unsubscribe", ...}, {"ev": "disconnect", "conn": N}, {"ev": "closed", "conn": N}
  {"ev": "will", "conn": N, "topic": ..., "payload_b64": ...}
  {"ev": "bad", "conn": N, "why": ...}      a packet this broker doesn't take

The scenario publishes to the daemon through the control socket CTRL_DIR/ctl.sock (a Unix
socket, so it is reachable from outside the network namespace): one JSON object per line,
{"topic": ..., "payload_b64": ..., "retain": bool}, answered "ok"; the message goes to every
matching subscription (at QoS 0), and is kept when retained. {"op": "drop"} ends every client's
connection (their wills are published, as after a network drop); {"op": "exit"} stops the broker
at once.
"""

import base64
import json
import os
import socket
import sys
import threading

LOCK = threading.Lock()
LOG = None
RETAINED = {}          # topic -> payload
SUBSCRIBERS = {}       # conn number -> (socket, [filters])
NEXT_CONN = [0]


def log(event):
    with LOCK:
        LOG.write(json.dumps(event) + "\n")
        LOG.flush()


def b64(data):
    return base64.b64encode(data).decode() if data is not None else None


def matches(filt, topic):
    f, t = filt.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(f) == len(t)


def encode_length(n):
    out = bytearray()
    while True:
        b, n = n % 128, n // 128
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def utf8(s):
    data = s.encode()
    return len(data).to_bytes(2, "big") + data


def publish_packet(topic, payload, retain=False):
    body = utf8(topic) + payload
    return bytes([0x30 | (1 if retain else 0)]) + encode_length(len(body)) + body


def deliver(topic, payload, exclude=None):
    """Send a message to every matching subscription (at QoS 0), once per connection."""
    with LOCK:
        targets = [s for n, (s, filters) in SUBSCRIBERS.items()
                   if n != exclude and any(matches(f, topic) for f in filters)]
    for s in targets:
        try:
            s.sendall(publish_packet(topic, payload))
        except OSError:
            pass


def store(topic, payload, retain):
    if retain:
        with LOCK:
            if payload:
                RETAINED[topic] = payload
            else:
                RETAINED.pop(topic, None)


def read_exact(conn, n):
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def read_packet(conn):
    first = read_exact(conn, 1)[0]
    mult, length = 1, 0
    while True:
        b = read_exact(conn, 1)[0]
        length += (b & 0x7F) * mult
        if not b & 0x80:
            break
        mult *= 128
    return first, read_exact(conn, length)


def take_string(data, pos):
    n = int.from_bytes(data[pos:pos + 2], "big")
    return data[pos + 2:pos + 2 + n], pos + 2 + n


def client(conn, number):
    will = None
    clean_end = False
    try:
        first, body = read_packet(conn)
        if first >> 4 != 1:
            log({"ev": "bad", "conn": number, "why": "first packet is not CONNECT"})
            return
        name, pos = take_string(body, 0)
        level, flags = body[pos], body[pos + 1]
        keepalive = int.from_bytes(body[pos + 2:pos + 4], "big")
        pos += 4
        client_id, pos = take_string(body, pos)
        if flags & 0x04:
            wt, pos = take_string(body, pos)
            wp, pos = take_string(body, pos)
            will = {"topic": wt.decode(errors="replace"), "payload": wp, "qos": (flags >> 3) & 3,
                    "retain": bool(flags & 0x20)}
        username = password = None
        if flags & 0x80:
            username, pos = take_string(body, pos)
        if flags & 0x40:
            password, pos = take_string(body, pos)
        log({"ev": "connect", "conn": number, "protocol": [name.decode(errors="replace"), level],
             "clean": bool(flags & 0x02), "keepalive": keepalive, "client_id": client_id.decode(errors="replace"),
             "username": username.decode(errors="replace") if username is not None else None,
             "password_b64": b64(password),
             "will": None if will is None else {"topic": will["topic"], "payload_b64": b64(will["payload"]),
                                                "qos": will["qos"], "retain": will["retain"]}})
        conn.sendall(b"\x20\x02\x00\x00")
        with LOCK:
            SUBSCRIBERS[number] = (conn, [])
        while True:
            first, body = read_packet(conn)
            kind = first >> 4
            if kind == 3:  # PUBLISH
                qos, retain = (first >> 1) & 3, bool(first & 1)
                topic, pos = take_string(body, 0)
                packet_id = None
                if qos:
                    packet_id, pos = body[pos:pos + 2], pos + 2
                payload = body[pos:]
                topic = topic.decode(errors="replace")
                log({"ev": "publish", "conn": number, "topic": topic, "payload_b64": b64(payload), "qos": qos,
                     "retain": retain})
                if qos == 1:
                    conn.sendall(b"\x40\x02" + packet_id)
                elif qos == 2:
                    log({"ev": "bad", "conn": number, "why": "QoS 2"})
                store(topic, payload, retain)
                deliver(topic, payload, exclude=number)
            elif kind == 8:  # SUBSCRIBE
                packet_id, pos = body[0:2], 2
                topics = []
                while pos < len(body):
                    f, pos = take_string(body, pos)
                    topics.append([f.decode(errors="replace"), body[pos]])
                    pos += 1
                log({"ev": "subscribe", "conn": number, "topics": topics})
                with LOCK:
                    SUBSCRIBERS[number][1].extend(t for t, _ in topics)
                    retained = [(t, p) for t, p in sorted(RETAINED.items()) if any(matches(f, t) for f, _ in topics)]
                conn.sendall(bytes([0x90, 2 + len(topics)]) + packet_id + bytes(min(q, 1) for _, q in topics))
                for t, p in retained:
                    conn.sendall(publish_packet(t, p, retain=True))
            elif kind == 10:  # UNSUBSCRIBE
                packet_id, pos = body[0:2], 2
                topics = []
                while pos < len(body):
                    f, pos = take_string(body, pos)
                    topics.append(f.decode(errors="replace"))
                log({"ev": "unsubscribe", "conn": number, "topics": topics})
                with LOCK:
                    SUBSCRIBERS[number] = (conn, [f for f in SUBSCRIBERS[number][1] if f not in topics])
                conn.sendall(b"\xb0\x02" + packet_id)
            elif kind == 12:  # PINGREQ
                conn.sendall(b"\xd0\x00")
            elif kind == 14:  # DISCONNECT
                log({"ev": "disconnect", "conn": number})
                clean_end = True
                return
            elif kind == 4:  # PUBACK (the broker sends at QoS 0: never expected)
                pass
            else:
                log({"ev": "bad", "conn": number, "why": f"packet type {kind}"})
    except (EOFError, OSError):
        pass
    finally:
        with LOCK:
            SUBSCRIBERS.pop(number, None)
        try:
            conn.close()
        except OSError:
            pass
        log({"ev": "closed", "conn": number})
        if will is not None and not clean_end:
            log({"ev": "will", "conn": number, "topic": will["topic"], "payload_b64": b64(will["payload"])})
            store(will["topic"], will["payload"], will["retain"])
            deliver(will["topic"], will["payload"])


def control(path):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    if os.path.exists(path):
        os.unlink(path)
    srv.bind(path)
    srv.listen(4)
    while True:
        c, _ = srv.accept()
        with c, c.makefile("rwb") as f:
            for line in f:
                msg = json.loads(line)
                if msg.get("op") == "exit":  # the broker stopped at once, as by a crash
                    os._exit(0)
                if msg.get("op") == "drop":  # every client's connection ended, as by a network drop
                    with LOCK:
                        socks = [s for s, _ in SUBSCRIBERS.values()]
                    for s in socks:
                        try:
                            s.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                    f.write(b"ok\n")
                    f.flush()
                    continue
                payload = base64.b64decode(msg["payload_b64"])
                store(msg["topic"], payload, msg.get("retain", False))
                deliver(msg["topic"], payload)
                f.write(b"ok\n")
                f.flush()


def main(ctrl, port):
    global LOG
    LOG = open(os.path.join(ctrl, "log"), "a")
    threading.Thread(target=control, args=(os.path.join(ctrl, "ctl.sock"),), daemon=True).start()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    open(os.path.join(ctrl, "ready"), "w").close()
    while True:
        conn, _ = srv.accept()
        with LOCK:
            NEXT_CONN[0] += 1
            number = NEXT_CONN[0]
        threading.Thread(target=client, args=(conn, number), daemon=True).start()


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
