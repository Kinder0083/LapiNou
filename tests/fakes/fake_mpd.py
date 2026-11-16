# SPDX-License-Identifier: MIT
"""A fake MPD for lapinou-mqtt's scenario: the volume commands of MPD's protocol.

    fake_mpd.py CTRL_DIR PORT

Listens on 127.0.0.1:PORT (in the daemon's network namespace), writes CTRL_DIR/mpd-ready once it
does, greets "OK MPD 0.15.0", answers password, setvol, status (with the volume set last, 75 at
first) and close, and logs each command it reads to CTRL_DIR/mpd.log, one per line. While
CTRL_DIR/mpd-refuse exists, setvol is answered with an ACK.
"""

import os
import socket
import sys
import threading

LOCK = threading.Lock()
VOLUME = [75]


def client(conn, ctrl):
    with conn, conn.makefile("rwb") as f:
        f.write(b"OK MPD 0.15.0\n")
        f.flush()
        for raw in f:
            line = raw.decode(errors="replace").rstrip("\n")
            with LOCK, open(os.path.join(ctrl, "mpd.log"), "a") as log:
                log.write(line + "\n")
            if line == "close":
                return
            if line.startswith("setvol "):
                if os.path.exists(os.path.join(ctrl, "mpd-refuse")):
                    f.write(b"ACK [52@0] {setvol} problems setting volume\n")
                else:
                    VOLUME[0] = int(line.split()[1])
                    f.write(b"OK\n")
            elif line == "status":
                f.write(f"volume: {VOLUME[0]}\nstate: stop\nOK\n".encode())
            elif line.startswith("password "):
                f.write(b"OK\n")
            else:
                f.write(b"ACK [5@0] {} unknown command\n")
            f.flush()


def main(ctrl, port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    open(os.path.join(ctrl, "mpd-ready"), "w").close()
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=client, args=(conn, ctrl), daemon=True).start()


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
