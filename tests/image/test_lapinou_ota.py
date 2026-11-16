# SPDX-License-Identifier: MIT
"""lapinou-ota, LapiNou's signed updates (userland/src/usr/lapinou-ota), on a simulated Karotz.

The script runs in the image's own BusyBox sh under qemu, with the image's curl and openssl (the
test sysroot's, under qemu-arm), against fake update sources on 127.0.0.1: a LapiNou server's
/ota/ (plain HTTP) and GitHub (the Releases API and its storage host, over HTTPS with a test
certificate). Its absolute paths are moved into a sandbox, as test_usbkey does for the key's
autorun; erasing, writing the flash and rebooting are stubs that record what they would have done.
The release is signed with a throwaway Ed25519 pair made here by tools/sign-ota.py.

Needs qemu-arm, the system build's BusyBox and the test sysroot ($LL_WORK/sysroot-core, made by
tests/run.sh), and the host's openssl.
"""

import gzip
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import socketserver
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(HERE))
from test_core_boot import arm_busybox  # noqa: E402

OTA = ROOT / "userland/src/usr/lapinou-ota/lapinou-ota.sh"
SIGN = ROOT / "tools/sign-ota.py"
PLACEHOLDER = ROOT / "image/core/usr/share/lapinou/ota-pubkey.pub"
INSTALL_LIB = ROOT / "image/usbkey/install-lib.sh"
RESUME = ROOT / "image/core/rootfs/karotz/scripts/ota-resume.sh"
LINUXRC = ROOT / "image/core/rootfs/linuxrc"
CONF_SH = ROOT / "image/core/usr/share/lapinou/conf.sh"
TOP_DIRS = "mnt|karotz|bin|sbin|tmp|usr|etc|dev|proc|sys"
REWRITE = re.compile(rf"(?<![\w.$/])/({TOP_DIRS})(?=[/\s\"');$]|$)", re.M)
PATH_LINE = "PATH=/bin:/sbin:/usr/bin:/usr/sbin"
APPLETS = ["sh", "awk", "sed", "grep", "cat", "cp", "mv", "rm", "mkdir", "gzip", "tar", "sha256sum", "wc", "df",
           "tail", "head", "date", "sync", "sleep", "flock", "tr", "cut", "chmod", "ls", "cmp", "kill", "dd", "touch",
           "basename", "dirname", "printf", "echo", "test", "["]
MiB = 1 << 20
OLD = "v1.0.0"
NEW = "v2.0.0"
TOKEN = "github_pat_TESTONLY_0123456789abcdef"
PROTECTED = ("mtd0", "mtd3", "mtd4", "mtd5")
STOCK_INITTAB = "::once:/karotz/scripts/karotz_init.sh\n"


def sysroot():
    from lapin import work_dir
    return work_dir() / "sysroot-core"


def missing_inputs():
    if shutil.which("qemu-arm") is None:
        return "no qemu-arm"
    if shutil.which("openssl") is None:
        return "no openssl on the host"
    if arm_busybox() is None:
        return "the image's BusyBox can't run here (./build.sh system, qemu-user)"
    s = sysroot()
    if not (s / "usr/bin/curl").is_file() or not (s / "usr/bin/openssl").is_file():
        return "no test sysroot with curl and openssl (bash tests/run.sh makes it)"
    return None


def stub(path: Path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8", newline="\n")
    path.chmod(0o755)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_release(d: Path, version=NEW):
    """A release's files in d: the three images, a USB key archive, and usr.tar.gz's contents."""
    d.mkdir(parents=True)
    # The kernel's SHA-256 has the bytes 0x00 and 0x25 ("%"): the build id's printf must keep them.
    base, n = bytes(range(256)) * 600 + b"kernel " + version.encode(), 0
    while True:
        zimage = base + b"#%d" % n
        digest = hashlib.sha256(zimage).digest()
        if 0x00 in digest and 0x25 in digest:
            break
        n += 1
    rootfs = gzip.compress(b"root filesystem " * 4000, mtime=0)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in (("etc/init.d/S40lapinlibre", b"#!/bin/sh\n# new\n"), ("karotz/bin/controller", b"new elf"),
                           ("etc/conf/karotz.conf", b"[karotz]\nfrom=release\n"),
                           ("www/index.html", b"<title>LapinLibre</title>\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    usr = gzip.compress(buf.getvalue(), mtime=0)
    for name, data in (("zImage", zimage), ("rootfs.img.gz", rootfs), ("usr.tar.gz", usr),
                       (f"lapinou-key-{version}.zip", b"PK not really a zip")):
        (d / name).write_bytes(data)


def sign_raw(d: Path, key: Path, manifest: dict):
    """A manifest written and signed as given, without sign-ota.py's checks."""
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(key), "-rawin", "-in", str(d / "manifest.json"),
                    "-out", str(d / "manifest.json.sig")], check=True, capture_output=True)


def sign(d: Path, key: Path, version=NEW):
    r = subprocess.run([sys.executable, "-I", str(SIGN), "--key", str(key), "--dir", str(d), "--version", version],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


class Handler(http.server.BaseHTTPRequestHandler):
    """Serves the server's routes: {path: bytes | ("redirect", url) | ("cut", bytes)}; logs each request."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):
        srv = self.server
        srv.log.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        route = srv.routes.get(self.path.split("?")[0])
        if route is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if isinstance(route, tuple) and route[0] == "redirect":
            self.send_response(302)
            self.send_header("Location", route[1])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if callable(route):
            route = route(self)
            if route is None:
                return
        cut = isinstance(route, tuple) and route[0] == "cut"
        data = route[1] if cut else route
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if cut:  # the connection drops half-way
            self.wfile.write(data[: len(data) // 2])
            self.wfile.flush()
            self.close_connection = True
            self.connection.shutdown(2)
            return
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    def __init__(self, routes=None, tls=None):
        super().__init__(("127.0.0.1", 0), Handler)
        self.routes = routes or {}
        self.log = []
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*tls)
            self.socket = ctx.wrap_socket(self.socket, server_side=True)
        self.port = self.server_address[1]
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.shutdown()
        self.server_close()


class OtaRabbit:
    """A sandboxed LapinLibre rabbit at version OLD, with lapinou-ota installed."""

    def __init__(self, sb: Path, pubkey: Path, cacert: Path, conf="", secrets="", api="127.0.0.1:1"):
        self.sb = sb
        busybox, root, qemu = arm_busybox(), sysroot(), shutil.which("qemu-arm")
        log = sb / "actions.log"
        for name in APPLETS:
            stub(sb / "bin" / name, f'exec {busybox} {name} "$@"')
        stub(sb / "bin/logger", "exit 0")
        stub(sb / "bin/killall", "exit 0")
        stub(sb / "karotz/bin/led", "exit 0")
        stub(sb / "sbin/flash_erase", f'echo "erase $1" >> {log}; : > "$1"')
        stub(sb / "sbin/flash_eraseall", f'echo "eraseall $1" >> {log}; : > "$1"')
        # nandwrite writes garbage while corrupt-mtdN holds a count above 0 (a write the read-back catches).
        stub(sb / "sbin/nandwrite", f'echo "write $2 $3" >> {log}\n'
                                    f'c={sb}/corrupt-$(basename "$2"); n=$(cat "$c" 2> /dev/null || echo 0)\n'
                                    f'if [ "$n" -gt 0 ]; then echo $((n - 1)) > "$c"; printf garbage > "$2"; '
                                    f'else cp "$3" "$2"; fi')
        # nanddump -q --bb=skipbad -l LEN -f FILE DEVICE: LEN bytes of the device (no OOB).
        stub(sb / "sbin/nanddump", f'len= out=\nwhile [ $# -gt 1 ]; do case "$1" in -l) len=$2; shift 2 ;; '
                                   f'-f) out=$2; shift 2 ;; *) shift ;; esac; done\n'
                                   f'echo "dump $1" >> {log}\nhead -c "$len" "$1" > "$out"')
        stub(sb / "sbin/reboot", f'echo "reboot $*" >> {log}')
        # The image's own curl and openssl; curl's arguments recorded (no secret may be in them).
        stub(sb / "usr/bin/curl", f'printf "%s\\n" "$*" >> {sb}/curl.log\n'
                                  f'exec {qemu} -cpu arm926 -L {root} {root}/usr/bin/curl "$@"')
        stub(sb / "usr/bin/openssl", f'exec {qemu} -cpu arm926 -L {root} {root}/usr/bin/openssl "$@"')
        stub(sb / "usr/etc/init.d/S40lapinlibre", f'echo "S40lapinlibre $1" >> {log}')

        (sb / "dev").mkdir()
        for n in range(7):
            (sb / f"dev/mtd{n}").write_bytes(f"old mtd{n}".encode())
        (sb / "proc/sys/vm").mkdir(parents=True)
        (sb / "proc/sys/vm/drop_caches").write_text("")
        (sb / "proc/mtd").write_text(
            "dev:    size   erasesize  name\n"
            'mtd0: 00040000 00020000 "bootloader"\nmtd1: 00300000 00020000 "Kernel"\n'
            'mtd2: 00500000 00020000 "ramdisk"\nmtd3: 00300000 00020000 "kernel rescue"\n'
            'mtd4: 00500000 00020000 "ramdisk rescue"\nmtd5: 00300000 00020000 "yaffs rescue"\n'
            'mtd6: 0ea00000 00020000 "yaffs"\n', newline="\n")
        (sb / "sys/block/ram0").mkdir(parents=True)
        (sb / "sys/block/ram0/size").write_text(f"{5 * MiB // 512}\n")
        (sb / "tmp").mkdir()

        usr = sb / "usr"
        conf_dir = usr / "etc/conf"
        conf_dir.mkdir(parents=True)
        (conf_dir / "sys_version").write_text(OLD + "\n")
        (conf_dir / "lapinlibre_build_id").write_text("0123456789abcdef\n")
        (conf_dir / "karotz.conf").write_text("[karotz]\nfrom=rabbit\nurl_update=http://127.0.0.1:1\n")
        (conf_dir / "wpa.conf").write_text("kept\n")
        (conf_dir / "lapinou.conf").write_text(conf)
        if secrets:
            (conf_dir / "lapinou.secrets").write_text(secrets)
        (usr / "etc/inittab").write_text(STOCK_INITTAB)
        (usr / "karotz/apps").mkdir(parents=True)
        (usr / "karotz/apps/myapp").write_text("kept\n")
        (usr / "karotz/uuid.conf").write_text("0e1f2a3b-4c5d-6e7f-8091-a2b3c4d5e6f7\n")
        (usr / "karotz/stale.conf").write_text("wiped\n")
        (usr / "scripts").mkdir()
        (usr / "scripts/old.sh").write_text("wiped\n")
        (usr / "etc/ssl/certs").mkdir(parents=True)
        shutil.copy(cacert, usr / "etc/ssl/certs/ca-certificates.crt")
        share = usr / "share/lapinou"
        share.mkdir(parents=True)
        shutil.copy(pubkey, share / "ota-pubkey.pub")
        self.put_script(CONF_SH, share / "conf.sh")
        # The root filesystem's install helpers and the boot's resume of an interrupted /usr.
        self.put_script(INSTALL_LIB, sb / "karotz/scripts/install-lib.sh")
        self.put_script(RESUME, sb / "karotz/scripts/ota-resume.sh")
        self.ota = usr / "karotz/bin/lapinou-ota"
        self.put_script(OTA, self.ota, api)

    def put_script(self, src: Path, dst: Path, api=None):
        text = src.read_text(encoding="utf-8")
        head, _, rest = text.partition("\n")
        rest = rest.replace(PATH_LINE, "@PATH@")
        if api:
            rest = rest.replace("api.github.com", api)
        rest = REWRITE.sub(lambda m: str(self.sb) + m.group(0), rest)
        rest = rest.replace("@PATH@", "PATH=" + ":".join(f"{self.sb}/{d}" for d in ("bin", "sbin", "usr/bin", "usr/sbin")))
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(head + "\n" + rest, encoding="utf-8", newline="\n")
        dst.chmod(0o755)

    def run(self, *args, timeout=300, script=None):
        # A session of its own, so that a simulated power cut (kill -KILL 0) stops this run only.
        return subprocess.run([str(arm_busybox()), "sh", str(script or self.ota), *args], cwd=self.sb,
                              capture_output=True, text=True, timeout=timeout, start_new_session=True,
                              env={"PATH": "/usr/bin:/bin", "HOME": str(self.sb)})

    def resume(self):
        """The boot's resume, as linuxrc runs it once /usr is mounted."""
        return self.run(script=self.sb / "karotz/scripts/ota-resume.sh")

    def cut_unpack(self):
        """The power goes during the unpack: tar writes one file, then everything stops."""
        busybox = arm_busybox()
        stub(self.sb / "bin/tar", f'if [ -e {self.sb}/cut-unpack ]; then rm -f {self.sb}/cut-unpack\n'
                                  f'echo partial > {self.sb}/usr/karotz/bin/controller; kill -KILL 0; fi\n'
                                  f'exec {busybox} tar "$@"')
        (self.sb / "cut-unpack").write_text("")

    def actions(self):
        f = self.sb / "actions.log"
        return [a.replace(str(self.sb), "") for a in f.read_text().splitlines()] if f.exists() else []

    def writes(self):
        return [a for a in self.actions() if a.startswith(("erase", "write"))]

    def version(self):
        return (self.sb / "usr/etc/conf/sys_version").read_text().strip()

    def ota_files(self):
        d = self.sb / "usr/ota"
        return sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()) if d.exists() else []


class OtaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        why = missing_inputs()
        if why:
            raise unittest.SkipTest(why)
        cls.tmp = Path(tempfile.mkdtemp(prefix="lapinou-ota-test-"))
        t = cls.tmp
        # The throwaway signing pair (never the real one), and a second one that isn't trusted.
        for name in ("test", "other"):
            subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(t / f"{name}.key.test")], check=True,
                           capture_output=True)
            pub = subprocess.run(["openssl", "pkey", "-in", str(t / f"{name}.key.test"), "-pubout"], check=True,
                                 capture_output=True).stdout
            # As the real file will be: its licence line before the key, which OpenSSL skips.
            (t / f"{name}.pub").write_bytes(b"SPDX-License-Identifier: MIT\n" + pub)
        # A test certificate for the fake GitHub hosts (127.0.0.1 and localhost).
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                        "-keyout", str(t / "tls.key"), "-out", str(t / "tls.crt"), "-days", "2", "-subj", "/CN=lapinou-test",
                        "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"], check=True, capture_output=True)
        cls.tls = (str(t / "tls.crt"), str(t / "tls.key"))
        cls.release = t / "release"
        make_release(cls.release)
        sign(cls.release, t / "test.key.test")
        cls.files = {p.name: p.read_bytes() for p in cls.release.iterdir()}

    @classmethod
    def tearDownClass(cls):
        if os.environ.get("LAPIN_TEST_KEEP"):
            print(f"sandboxes kept in {cls.tmp}")
        else:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.stop()

    def server(self, routes=None, tls=None):
        s = Server(routes, tls)
        self.servers.append(s)
        return s

    def ota_server(self, files=None, version=NEW):
        """The LapiNou server's relay (docs/lapinou/ota.md): the manifest and its signature at
        /ota/, every file (them too) at /ota/<version>/<name>."""
        files = dict(self.files if files is None else files)
        routes = {f"/ota/{version}/{name}": data for name, data in files.items()}
        for name in ("manifest.json", "manifest.json.sig"):
            routes[f"/ota/{name}"] = files[name]
        return self.server(routes)

    def rabbit(self, name, conf="", pubkey=None, **kw):
        return OtaRabbit(self.tmp / name, pubkey or self.tmp / "test.pub", Path(self.tls[0]), conf=conf, **kw)

    def server_rabbit(self, name, srv, path="/ota/", **kw):
        return self.rabbit(name, conf=f"[ota]\nurl=http://127.0.0.1:{srv.port}{path}\n", **kw)

    def assert_nothing_written(self, r):
        self.assertEqual(r.writes(), [])
        for n in range(7):
            self.assertEqual((r.sb / f"dev/mtd{n}").read_bytes(), f"old mtd{n}".encode())
        self.assertEqual((r.sb / "usr/scripts/old.sh").read_text(), "wiped\n")
        self.assertEqual(r.version(), OLD)
        self.assertNotIn("reboot -f", r.actions())
        self.assertNotIn("S40lapinlibre stop", r.actions())
        # No download left behind: only the log.
        self.assertEqual([f for f in r.ota_files() if f != "last.log"], [])

    WRITES = ["erase /dev/mtd1", "write /dev/mtd1 /usr/ota/new/zImage",
              "erase /dev/mtd2", "write /dev/mtd2 /usr/ota/new/rootfs.img.gz"]

    def assert_installed(self, r, p, files=None, version=NEW, writes=None, rebooted=True):
        if p is not None:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        files = files or self.files
        sb = r.sb
        self.assertEqual((sb / "dev/mtd1").read_bytes(), files["zImage"])
        self.assertEqual((sb / "dev/mtd2").read_bytes(), files["rootfs.img.gz"])
        self.assertEqual(r.writes(), writes or self.WRITES)
        for n in (0, 3, 4, 5, 6):
            self.assertEqual((sb / f"dev/mtd{n}").read_bytes(), f"old mtd{n}".encode(), f"mtd{n}")
        actions = r.actions()
        # The daemons are stopped before the first erase; each partition is read back after its
        # write; the reboot comes last.
        self.assertLess(actions.index("S40lapinlibre stop"), actions.index("erase /dev/mtd1"))
        for mtd in ("mtd1", "mtd2"):
            last_write = max(i for i, a in enumerate(actions) if a.startswith(f"write /dev/{mtd}"))
            self.assertEqual(actions[last_write + 1], f"dump /dev/{mtd}")
        if rebooted:
            self.assertEqual(actions[-1], "reboot -f")
        # /usr replaced as the key's install does, settings, apps and UUID kept.
        self.assertEqual((sb / "usr/etc/init.d/S40lapinlibre").read_text(), "#!/bin/sh\n# new\n")
        self.assertEqual((sb / "usr/karotz/bin/controller").read_text(), "new elf")
        self.assertFalse((sb / "usr/scripts/old.sh").exists())
        self.assertFalse((sb / "usr/karotz/stale.conf").exists())
        self.assertFalse((sb / "usr/etc/inittab").exists())
        self.assertEqual((sb / "usr/karotz/apps/myapp").read_text(), "kept\n")
        self.assertEqual((sb / "usr/karotz/uuid.conf").read_text(), "0e1f2a3b-4c5d-6e7f-8091-a2b3c4d5e6f7\n")
        self.assertEqual((sb / "usr/etc/conf/karotz.conf").read_text(),
                         "[karotz]\nfrom=rabbit\nurl_update=http://127.0.0.1:1\n")
        self.assertEqual((sb / "usr/etc/conf/wpa.conf").read_text(), "kept\n")
        self.assertEqual(r.version(), version)
        # The build id image.py gives these images, so the same build's USB key only applies settings.
        images = files
        build_id = hashlib.sha256(b"".join(hashlib.sha256(images[k]).digest()
                                           for k in sorted(("zImage", "rootfs.img.gz", "usr.tar.gz")))).hexdigest()[:16]
        self.assertEqual((sb / "usr/etc/conf/lapinlibre_build_id").read_text(), build_id + "\n")
        # The downloads are gone; the log stays.
        self.assertEqual(r.ota_files(), ["last.log"])
        self.assertIn(f"Installed {version}", (sb / "usr/ota/last.log").read_text())

    def build_id(self, files=None):
        files = files or self.files
        return hashlib.sha256(b"".join(hashlib.sha256(files[k]).digest()
                                       for k in sorted(("zImage", "rootfs.img.gz", "usr.tar.gz")))).hexdigest()[:16]

    def release_files(self, name, version, key="test.key.test"):
        d = self.tmp / name
        make_release(d, version)
        sign(d, self.tmp / key, version)
        return {p.name: p.read_bytes() for p in d.iterdir()}

    # --- With a LapiNou server ------------------------------------------------------------------

    def test_check(self):
        srv = self.ota_server()
        r = self.server_rabbit("check", srv)
        p = r.run("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout), {"installed": OLD, "available": NEW})
        self.assert_nothing_written(r)
        # A check reads the manifest and its signature, never the images.
        self.assertEqual(sorted(path for path, _ in srv.log), ["/ota/manifest.json", "/ota/manifest.json.sig"])

    def test_install_from_the_server(self):
        srv = self.ota_server()
        r = self.server_rabbit("server", srv)
        p = r.run("install")
        self.assert_installed(r, p)
        # The USB key's archive, listed by the manifest, isn't the rabbit's to download.
        self.assertNotIn(f"/ota/{NEW}/lapinou-key-{NEW}.zip", [path for path, _ in srv.log])

    def test_server_from_the_settings(self):
        # No [ota] url: the LapiNou server's relay, http://<[server] host>:8080/ota/.
        r = self.rabbit("server-host", conf="[server]\nenabled=1\nhost=127.0.0.1\n")
        p = r.run("check")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("http://127.0.0.1:8080/ota/manifest.json", (r.sb / "curl.log").read_text())

    def test_files_come_from_the_manifests_version(self):
        # The files are read from /ota/<the manifest's version>/, never the short form /ota/<name>
        # (which is the version in service, possibly newer than the manifest the rabbit read).
        srv = self.ota_server()
        r = self.server_rabbit("by-version", srv)
        self.assert_installed(r, r.run("install"))
        got = sorted(path for path, _ in srv.log)
        self.assertEqual(got, sorted(["/ota/manifest.json", "/ota/manifest.json.sig", f"/ota/{NEW}/zImage",
                                      f"/ota/{NEW}/rootfs.img.gz", f"/ota/{NEW}/usr.tar.gz"]))

    def test_same_version_installs_nothing(self):
        srv = self.ota_server()
        r = self.server_rabbit("same", srv)
        (r.sb / "usr/etc/conf/sys_version").write_text(NEW + "\n")
        p = r.run("install")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(r.writes(), [])
        self.assertNotIn("reboot -f", r.actions())
        self.assertIn("up to date", p.stderr)

    def test_bad_signature_writes_nothing(self):
        for name, sig in (("other-key", None), ("flipped", "flip"), ("short", b"\0" * 63)):
            with self.subTest(name):
                files = dict(self.files)
                if sig is None:
                    other = self.tmp / f"release-{name}"
                    shutil.copytree(self.release, other)
                    sign(other, self.tmp / "other.key.test")
                    files["manifest.json.sig"] = (other / "manifest.json.sig").read_bytes()
                elif sig == "flip":
                    s = bytearray(files["manifest.json.sig"])
                    s[10] ^= 1
                    files["manifest.json.sig"] = bytes(s)
                else:
                    files["manifest.json.sig"] = sig
                srv = self.ota_server(files)
                r = self.server_rabbit(f"bad-sig-{name}", srv)
                p = r.run("install")
                self.assertNotEqual(p.returncode, 0)
                self.assertIn("signature", p.stderr)
                self.assert_nothing_written(r)
                # Nothing is downloaded once the signature fails.
                self.assertNotIn(f"/ota/{NEW}/usr.tar.gz", [path for path, _ in srv.log])

    def test_manifest_changed_after_signing(self):
        files = dict(self.files)
        files["manifest.json"] = files["manifest.json"].replace(NEW.encode(), b"v9.9.9")
        srv = self.ota_server(files)
        r = self.server_rabbit("changed-manifest", srv)
        p = r.run("install")
        self.assertNotEqual(p.returncode, 0)
        self.assert_nothing_written(r)

    def test_changed_byte_in_usr_writes_nothing(self):
        files = dict(self.files)
        usr = bytearray(files["usr.tar.gz"])
        usr[len(usr) // 2] ^= 0xFF
        files["usr.tar.gz"] = bytes(usr)
        srv = self.ota_server(files)
        r = self.server_rabbit("changed-usr", srv)
        p = r.run("install")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("usr.tar.gz", p.stderr)
        self.assert_nothing_written(r)

    def test_cut_download_leaves_nothing(self):
        files = dict(self.files)
        files["usr.tar.gz"] = ("cut", files["usr.tar.gz"])
        srv = self.ota_server(files)
        r = self.server_rabbit("cut", srv)
        p = r.run("install")
        self.assertNotEqual(p.returncode, 0)
        self.assert_nothing_written(r)

    def test_leftovers_of_an_interrupted_run_are_removed(self):
        # A power cut during a download left a partial file: the next run starts clean.
        srv = self.ota_server()
        r = self.server_rabbit("leftover", srv)
        (r.sb / "usr/ota/new").mkdir(parents=True)
        (r.sb / "usr/ota/new/usr.tar.gz").write_bytes(b"partial")
        self.assertEqual(r.run("check").returncode, 0)
        self.assertFalse((r.sb / "usr/ota/new").exists())

    def test_too_big_for_its_partition(self):
        r = self.server_rabbit("too-big", self.ota_server())
        (r.sb / "proc/mtd").write_text('dev:    size   erasesize  name\nmtd1: 00001000 00020000 "Kernel"\n'
                                       'mtd2: 00500000 00020000 "ramdisk"\n')
        p = r.run("install")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("mtd1", p.stderr)
        self.assert_nothing_written(r)

    def test_placeholder_key_refuses(self):
        text = PLACEHOLDER.read_text()
        self.assertIn("PLACEHOLDER", text)
        self.assertNotIn("BEGIN PUBLIC KEY", text)
        srv = self.ota_server()
        r = self.server_rabbit("placeholder", srv, pubkey=PLACEHOLDER)
        for mode in ("install", "check"):
            p = r.run(mode)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("placeholder", p.stderr)
        self.assert_nothing_written(r)
        self.assertEqual(srv.log, [])

    def test_never_names_mtd0_or_the_rescue_partitions(self):
        self.assertIsNone(re.search(r"mtd[0345]\b", OTA.read_text()))
        srv = self.ota_server()
        r = self.server_rabbit("protected", srv)
        self.assert_installed(r, r.run("install"))
        for a in r.actions():
            self.assertNotRegex(a, r"mtd[0345]\b")

    def test_kernel_digest_has_nul_and_percent_bytes(self):
        # The build id is checked in every install: its input must hold the awkward bytes.
        digest = hashlib.sha256(self.files["zImage"]).digest()
        self.assertIn(0x00, digest)
        self.assertIn(0x25, digest)

    def test_https_server(self):
        # An https [ota] url: checked against the rabbit's CA certificates; curl ignores any curlrc.
        routes = dict(self.ota_server().routes)
        tls = self.server(routes, tls=self.tls)
        r = self.rabbit("https-server", conf=f"[ota]\nurl=https://127.0.0.1:{tls.port}/ota/\n")
        self.assert_installed(r, r.run("install"))
        calls = (r.sb / "curl.log").read_text().splitlines()
        self.assertTrue(calls)
        for c in calls:
            self.assertTrue(c.startswith("-q "), c)
            self.assertIn("--cacert", c)
            self.assertIn("--proto =http,https", c)

    # --- Writing the flash: read back, retried once, never past a bad partition --------------------

    def test_bad_write_is_written_again(self):
        srv = self.ota_server()
        r = self.server_rabbit("rewrite", srv)
        (r.sb / "corrupt-mtd2").write_text("1")
        p = r.run("install")
        self.assert_installed(r, p, writes=self.WRITES + self.WRITES[2:])
        self.assertIn("mtd2 reads back wrong", p.stderr)

    def test_partition_bad_twice_stops_before_usr(self):
        srv = self.ota_server()
        r = self.server_rabbit("bad-flash", srv)
        (r.sb / "corrupt-mtd1").write_text("2")
        p = r.run("install")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("mtd1 reads back wrong twice", p.stderr)
        self.assertEqual(r.writes(), self.WRITES[:2] * 2)
        self.assertNotIn("reboot -f", r.actions())
        # /usr untouched, and the checked images kept for another try.
        self.assertEqual((r.sb / "usr/scripts/old.sh").read_text(), "wiped\n")
        self.assertEqual(r.version(), OLD)
        self.assertEqual((r.sb / "usr/ota/new/usr.tar.gz").read_bytes(), self.files["usr.tar.gz"])
        self.assertEqual((r.sb / "usr/ota/new/zImage").read_bytes(), self.files["zImage"])
        self.assertFalse((r.sb / "usr/ota/resume.conf").exists())

    # --- Versions (R44) ----------------------------------------------------------------------------

    def test_downgrade_refused(self):
        files = self.release_files("release-old", "v0.9.0")
        srv = self.ota_server(files, version="v0.9.0")
        r = self.server_rabbit("downgrade", srv)
        p = r.run("install")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("lower than the installed", p.stderr)
        self.assert_nothing_written(r)
        self.assertNotIn("/ota/v0.9.0/usr.tar.gz", [path for path, _ in srv.log])
        # Given on purpose, it installs.
        r = self.server_rabbit("downgrade-allowed", srv)
        self.assert_installed(r, r.run("install", "--allow-downgrade"), files=files, version="v0.9.0")

    def test_version_order_is_numeric(self):
        # v1.10.0 is newer than v1.9.0; a dev build (0.0.0-<hash>), or a version from before R44,
        # is older than any release.
        for name, installed, version, ok in (("numeric", "v1.10.0", "v1.9.0", False),
                                             ("numeric-up", "v1.9.9", "v1.10.0", True),
                                             ("dev", "0.0.0-0123456789ab", "v1.10.0", True),
                                             ("pre-r44", "f794da0b5716", "v1.10.0", True)):
            with self.subTest(name):
                files = self.release_files(f"release-{name}", version)
                srv = self.ota_server(files, version=version)
                r = self.server_rabbit(f"order-{name}", srv)
                (r.sb / "usr/etc/conf/sys_version").write_text(installed + "\n")
                p = r.run("install")
                if ok:
                    self.assert_installed(r, p, files=files, version=version)
                else:
                    self.assertNotEqual(p.returncode, 0)
                    self.assertEqual(r.writes(), [])

    def test_same_build_skipped(self):
        srv = self.ota_server()
        r = self.server_rabbit("same-build", srv)
        (r.sb / "usr/etc/conf/lapinlibre_build_id").write_text(self.build_id() + "\n")
        p = r.run("install")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("same build", p.stderr)
        self.assertEqual(r.writes(), [])
        self.assertEqual(r.version(), OLD)

    def test_manifest_version_must_be_a_release_tag(self):
        d = self.tmp / "release-untagged"
        shutil.copytree(self.release, d)
        m = json.loads((d / "manifest.json").read_text())
        m["version"] = "2.0"
        sign_raw(d, self.tmp / "test.key.test", m)
        files = {p.name: p.read_bytes() for p in d.iterdir()}
        r = self.server_rabbit("untagged", self.ota_server(files, version="2.0"))
        p = r.run("install")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("vX.Y.Z", p.stderr)
        self.assert_nothing_written(r)

    # --- A power cut during /usr: resumed at the next boot ------------------------------------------

    def test_interrupted_unpack_is_resumed_at_boot(self):
        srv = self.ota_server()
        r = self.server_rabbit("resume", srv)
        r.cut_unpack()
        p = r.run("install")
        self.assertNotEqual(p.returncode, 0)
        # The power went half-way through /usr: partial, the marker and the checked archive kept.
        self.assertEqual((r.sb / "usr/karotz/bin/controller").read_text(), "partial\n")
        self.assertTrue((r.sb / "usr/ota/resume.conf").is_file())
        self.assertEqual((r.sb / "usr/ota/new/usr.tar.gz").read_bytes(), self.files["usr.tar.gz"])
        self.assertNotIn("reboot -f", r.actions())
        # lapinou-ota itself won't start over it (nor remove what the resume needs).
        p = r.run("check")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("interrupted", p.stderr)
        self.assertTrue((r.sb / "usr/ota/new/usr.tar.gz").is_file())
        # The next boot: linuxrc's resume finishes /usr.
        p = r.resume()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assert_installed(r, None, rebooted=False)
        self.assertFalse((r.sb / "usr/ota/resume.conf").exists())
        # Nothing to resume at the boot after.
        p = r.resume()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_resume_refuses_a_damaged_archive(self):
        srv = self.ota_server()
        r = self.server_rabbit("resume-damaged", srv)
        r.cut_unpack()
        r.run("install")
        usr = bytearray((r.sb / "usr/ota/new/usr.tar.gz").read_bytes())
        usr[100] ^= 0xFF
        (r.sb / "usr/ota/new/usr.tar.gz").write_bytes(bytes(usr))
        p = r.resume()
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("SHA-256", p.stdout + p.stderr)
        self.assertEqual((r.sb / "usr/karotz/bin/controller").read_text(), "partial\n")

    def test_linuxrc_resumes_before_usr_is_used(self):
        text = LINUXRC.read_text()
        call = text.index("/karotz/scripts/ota-resume.sh")
        self.assertLess(text.index("mount -t yaffs /dev/mtdblock6 /usr"), call)
        self.assertLess(call, text.index("for name in passwd inittab"))
        self.assertLess(call, text.index("/mnt/usbkey/autorun"))
        self.assertIsNone(re.search(r"mtd[0345]\b", RESUME.read_text()))

    # --- Without a server: GitHub's Releases API ----------------------------------------------------

    def github(self, storage_scheme="https"):
        """The fake API (127.0.0.1, HTTPS) and storage host (localhost): the release's assets each
        redirect from the API to storage."""
        storage = self.server(tls=self.tls if storage_scheme == "https" else None)
        api = self.server(tls=self.tls)
        assets = []
        for i, (name, data) in enumerate(sorted(self.files.items())):
            path = f"/repos/owner/repo/releases/assets/{100 + i}"
            assets.append({"url": f"https://127.0.0.1:{api.port}{path}", "id": 100 + i, "name": name, "label": None,
                           "uploader": {"login": "ci", "url": "https://127.0.0.1:1/users/ci"}, "size": len(data),
                           "browser_download_url": f"https://github.invalid/{name}"})
            api.routes[path] = ("redirect", f"{storage_scheme}://localhost:{storage.port}/blob/{name}?sig=x")
            storage.routes[f"/blob/{name}"] = data
        release = {"url": "https://127.0.0.1:1/x", "tag_name": NEW, "name": f"LapiNou {NEW}", "draft": False,
                   "body": 'Notes with "quotes", {braces}, [brackets], a \\ and\n"name": "zImage" in text.',
                   "author": {"login": "ci"}, "assets": assets}
        api.routes["/repos/owner/repo/releases/latest"] = json.dumps(release, indent=1).encode()
        return api, storage

    def github_rabbit(self, name, api, token=True):
        return self.rabbit(name, conf="[ota]\nrepo=owner/repo\n", api=f"127.0.0.1:{api.port}",
                           secrets=f"[secrets]\nota_token={TOKEN}\n" if token else "")

    def test_github_install(self):
        api, storage = self.github()
        r = self.github_rabbit("github", api)
        self.assert_installed(r, r.run("install"))
        # The token goes to the API, over HTTPS, and nowhere else; never in curl's arguments.
        self.assertTrue(api.log)
        for path, headers in api.log:
            self.assertEqual(headers.get("authorization"), f"Bearer {TOKEN}", path)
        self.assertTrue(storage.log)
        for path, headers in storage.log:
            self.assertNotIn("authorization", headers, path)
        self.assertNotIn(TOKEN, (r.sb / "curl.log").read_text())
        self.assertEqual(api.log[0][0], "/repos/owner/repo/releases/latest")

    def test_github_public_repo_check(self):
        api, _ = self.github()
        r = self.github_rabbit("github-public", api, token=False)
        p = r.run("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout), {"installed": OLD, "available": NEW})
        for _, headers in api.log:
            self.assertNotIn("authorization", headers)

    def test_github_refuses_plain_http(self):
        # A redirect to plain HTTP is refused before any request is made there.
        api, storage = self.github(storage_scheme="http")
        r = self.github_rabbit("github-http", api)
        p = r.run("install")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("https", p.stderr)
        self.assertEqual(storage.log, [])
        self.assert_nothing_written(r)

    def test_github_tag_must_be_the_manifests_version(self):
        api, _ = self.github()
        rel = json.loads(api.routes["/repos/owner/repo/releases/latest"])
        rel["tag_name"] = "v3.0.0"
        api.routes["/repos/owner/repo/releases/latest"] = json.dumps(rel).encode()
        r = self.github_rabbit("github-tag", api)
        p = r.run("install")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("v3.0.0", p.stderr)
        self.assert_nothing_written(r)

    def test_github_missing_asset(self):
        api, _ = self.github()
        rel = json.loads(api.routes["/repos/owner/repo/releases/latest"])
        rel["assets"] = [a for a in rel["assets"] if a["name"] != "rootfs.img.gz"]
        api.routes["/repos/owner/repo/releases/latest"] = json.dumps(rel).encode()
        r = self.github_rabbit("github-missing", api)
        p = r.run("install")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("rootfs.img.gz", p.stderr)
        self.assert_nothing_written(r)


class SignToolTest(unittest.TestCase):
    """tools/sign-ota.py: the manifest it writes, and its --verify."""

    def test_sign_and_verify(self):
        if shutil.which("openssl") is None:
            self.skipTest("no openssl")
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", str(t / "k")], check=True,
                           capture_output=True)
            subprocess.run(["openssl", "pkey", "-in", str(t / "k"), "-pubout", "-out", str(t / "p")], check=True,
                           capture_output=True)
            rel = t / "rel"
            make_release(rel)
            (rel / "notes.txt").write_text("not listed\n")
            sign(rel, t / "k")
            m = json.loads((rel / "manifest.json").read_bytes())
            self.assertEqual(m["version"], NEW)
            self.assertEqual(sorted(m["files"]), sorted(["zImage", "rootfs.img.gz", "usr.tar.gz", f"lapinou-key-{NEW}.zip"]))
            for name, f in m["files"].items():
                data = (rel / name).read_bytes()
                self.assertEqual(f, {"sha256": sha256(data), "size": len(data)})
            self.assertEqual(len((rel / "manifest.json.sig").read_bytes()), 64)
            verify = [sys.executable, "-I", str(SIGN), "--verify", "--pubkey", str(t / "p"), "--dir", str(rel)]
            r = subprocess.run(verify, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            (rel / "zImage").write_bytes(b"changed")
            r = subprocess.run(verify, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("zImage", r.stderr)
            # The version is the release's tag, vX.Y.Z, and must be given (R44).
            for args in ([], ["--version", "2.0"], ["--version", "0.0.0-abc"]):
                r = subprocess.run([sys.executable, "-I", str(SIGN), "--key", str(t / "k"), "--dir", str(rel), *args],
                                   capture_output=True, text=True)
                self.assertNotEqual(r.returncode, 0, args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
