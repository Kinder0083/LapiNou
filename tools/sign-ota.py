#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Sign a LapiNou release for lapinou-ota (R41, R42; docs/lapinou/ota.md).

    sign-ota.py --key PRIVATE --dir DIR --version vX.Y.Z [--file NAME ...]
    sign-ota.py --verify --pubkey PUBLIC --dir DIR

Signing writes DIR/manifest.json, {"version": TAG, "files": {NAME: {"sha256": HEX, "size": N}}},
and DIR/manifest.json.sig, the raw 64-byte Ed25519 signature of manifest.json's exact bytes. The
files are those named by --file, else zImage, rootfs.img.gz, usr.tar.gz and the USB key's archive
(LapinLibre-*.zip or lapinou-key-*.zip) found in DIR. --version is required: the release's tag,
vX.Y.Z, which is the manifest's version (R44); never the build's commit (BUILD_VERSION).
Each listed file is published as a release asset under the same name.

--verify checks the signature with the public key, then every listed file's size and SHA-256.
The keys are PEM files (openssl genpkey -algorithm ed25519); the work is done by the openssl
command (3.0 or later), so nothing beyond Python's standard library is needed.
Exit status: 0 good, 1 a check failed or the tool did, 2 usage.
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

MANIFEST = "manifest.json"
SIG = "manifest.json.sig"
IMAGES = ("zImage", "rootfs.img.gz", "usr.tar.gz")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}")
RELEASE = re.compile(r"v[0-9]{1,9}\.[0-9]{1,9}\.[0-9]{1,9}")
KEY_ZIP = re.compile(r"(?:LapinLibre|lapinou-key)-.+\.zip")


class Fail(Exception):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def openssl(*args):
    r = subprocess.run(["openssl", *args], capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def sign(key: Path, d: Path, version: str, names: list[str]):
    if not RELEASE.fullmatch(version):
        raise Fail(f"--version {version!r} isn't a release tag, vX.Y.Z (R44)")
    if not names:
        names = [n for n in IMAGES if (d / n).is_file()] + sorted(p.name for p in d.iterdir()
                                                                if p.is_file() and KEY_ZIP.fullmatch(p.name))
    for n in IMAGES:
        if n not in names:
            raise Fail(f"{n} is missing: the rabbit needs it")
    files = {}
    for n in names:
        if not NAME.fullmatch(n) or n in (MANIFEST, SIG):
            raise Fail(f"bad file name {n!r}")
        path = d / n
        if not path.is_file():
            raise Fail(f"no {path}")
        files[n] = {"sha256": sha256(path), "size": path.stat().st_size}
    data = (json.dumps({"version": version, "files": files}, indent=2, sort_keys=True) + "\n").encode()
    (d / MANIFEST).write_bytes(data)
    ok, out = openssl("pkeyutl", "-sign", "-inkey", str(key), "-rawin", "-in", str(d / MANIFEST), "-out", str(d / SIG))
    if not ok or (d / SIG).stat().st_size != 64:
        (d / SIG).unlink(missing_ok=True)
        raise Fail(f"signing failed (an Ed25519 private key?): {out}")
    print(f"{d / MANIFEST}: {version}, {len(files)} files; signature {d / SIG}")


def verify(pubkey: Path, d: Path):
    sig = d / SIG
    if not sig.is_file() or sig.stat().st_size != 64:
        raise Fail(f"{sig} is missing or not 64 bytes")
    if "PLACEHOLDER" in pubkey.read_text(encoding="utf-8", errors="replace"):
        raise Fail(f"{pubkey} is the placeholder, not a key")
    ok, out = openssl("pkeyutl", "-verify", "-pubin", "-inkey", str(pubkey), "-rawin", "-in", str(d / MANIFEST),
                      "-sigfile", str(sig))
    if not ok:
        raise Fail(f"bad signature: {out}")
    m = json.loads((d / MANIFEST).read_bytes())
    files = m.get("files")
    if not isinstance(m.get("version"), str) or not RELEASE.fullmatch(m["version"]) or not isinstance(files, dict):
        raise Fail("manifest.json has no valid version or files")
    bad = []
    for n, want in sorted(files.items()):
        path = d / n
        if not NAME.fullmatch(n) or not path.is_file():
            bad.append(f"{n}: missing")
        elif path.stat().st_size != want.get("size") or sha256(path) != want.get("sha256"):
            bad.append(f"{n}: size or SHA-256 differ")
    if bad:
        raise Fail("; ".join(bad))
    print(f"{d / MANIFEST}: {m['version']}, signature good, {len(files)} files match")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", type=Path, required=True)
    p.add_argument("--key", type=Path, help="the Ed25519 private key (PEM), to sign")
    p.add_argument("--version", help="the release's tag, vX.Y.Z (required to sign)")
    p.add_argument("--file", action="append", default=[], help="a file of DIR to list (repeatable)")
    p.add_argument("--verify", action="store_true")
    p.add_argument("--pubkey", type=Path, help="the Ed25519 public key (PEM), to verify")
    a = p.parse_args()
    try:
        if a.verify:
            if not a.pubkey:
                p.error("--verify needs --pubkey")
            verify(a.pubkey, a.dir)
        else:
            if not a.key:
                p.error("signing needs --key")
            if not a.version:
                p.error("signing needs --version vX.Y.Z (the release's tag)")
            sign(a.key, a.dir, a.version, a.file)
    except (Fail, OSError, ValueError) as e:
        print(f"sign-ota: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
