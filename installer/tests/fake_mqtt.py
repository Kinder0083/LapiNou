# SPDX-License-Identifier: MIT
"""Faux broker MQTT des tests de l'installateur : lit un CONNECT, note l'identifiant et le mot de
passe reçus (JSON, en hexadécimal) et répond CONNACK avec le code donné (négatif : ne répond
jamais, la connexion reste ouverte).

python3 fake_mqtt.py <fichier-port> <fichier-reçu> <code-connack>
"""

import json
import socket
import sys
import time


def read_exact(conn, n):
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def field(buf, i):
    n = int.from_bytes(buf[i : i + 2], "big")
    return buf[i + 2 : i + 2 + n], i + 2 + n


def main():
    port_file, seen_file, code = sys.argv[1], sys.argv[2], int(sys.argv[3])
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    with open(port_file, "w") as f:
        f.write(str(srv.getsockname()[1]))
    while True:
        conn, _ = srv.accept()
        with conn:
            try:
                head = read_exact(conn, 1)
            except EOFError:
                continue  # sonde de connexion, sans données
            length, mult = 0, 1
            while True:
                b = read_exact(conn, 1)[0]
                length += (b & 127) * mult
                mult *= 128
                if not b & 128:
                    break
            body = read_exact(conn, length)
            proto, i = field(body, 0)
            flags = body[i + 1]
            i += 4
            client, i = field(body, i)
            user = password = b""
            if flags & 0x80:
                user, i = field(body, i)
            if flags & 0x40:
                password, i = field(body, i)
            with open(seen_file, "w") as f:
                json.dump({"type": head[0], "proto": proto.decode(), "flags": flags,
                           "user": user.hex(), "password": password.hex()}, f)
            if code < 0:
                time.sleep(30)
                continue
            conn.sendall(bytes([0x20, 2, 0, code]))


main()
