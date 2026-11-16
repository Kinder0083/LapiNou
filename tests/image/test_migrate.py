# SPDX-License-Identifier: MIT
"""Run tools/lapinou-migrate/migrate-from-fros.sh against the simulated Karotz of test_usbkey.py.

The rabbit is test_usbkey's sandbox, set up as a Free Rabbits OS rabbit (its /proc/mtd, /proc/mounts
and network). The PC's script talks to it through a fake ssh that runs each remote command in bash
with every absolute path moved into the sandbox, the way test_usbkey rewrites the key's scripts;
the scripts the PC sends to /dev/shm/lapinou-migrate get the same rewriting. Erasing, writing,
reading back and rebooting are test_usbkey's stubs plus a nanddump stub, all recorded.

The key is a small one made here from the repository's install scripts, so these tests need no
build; test_real_key also migrates with the built key (out/usbkey) when there is one.
"""

import gzip
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_usbkey import TEST_KEY, TOP_DIRS, Rabbit, stub  # noqa: E402

ROOT = HERE.parent.parent
MIGRATE = ROOT / "tools" / "lapinou-migrate" / "migrate-from-fros.sh"
MiB = 1 << 20
IP = "192.168.1.103"
# Free Rabbits OS's partitions, as the real rabbit lists them.
PROC_MTD = textwrap.dedent("""\
    dev:    size   erasesize  name
    mtd0: 00040000 00020000 "bootloader"
    mtd1: 00300000 00020000 "Kernel"
    mtd2: 00500000 00020000 "ramdisk"
    mtd3: 00300000 00020000 "kernel rescue"
    mtd4: 00500000 00020000 "ramdisk rescue"
    mtd5: 00300000 00020000 "yaffs rescue"
    mtd6: 0ea00000 00020000 "yaffs"
    mtd7: 000c0000 00020000 "mystery box"
    """)
# wlan0 on 192.168.1.0/24, its default route through 192.168.1.1 (little-endian hex).
PROC_ROUTE = textwrap.dedent("""\
    Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
    wlan0\t0001A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\t0\t0\t0
    wlan0\t00000000\t0101A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0
    """)
WPA = 'ctrl_interface=/var/run/wpa_supplicant\nnetwork={\nssid="maison"\npsk="motdepasse"\n}\n'
NETWORK_CONF = 'IP="192.168.1.103"\nNETMASK="255.255.255.0"\nGATEWAY="192.168.1.1"\nDNS="192.168.1.1"\n'
PROTECTED = ("/dev/mtd0", "/dev/mtd3", "/dev/mtd4", "/dev/mtd5")

# The fake ssh: "ssh [options] user@host command" runs the command in the sandbox.
FAKE_SSH = r'''#!/usr/bin/env python3
import hashlib, os, re, subprocess, sys
from pathlib import Path
sb = Path(os.environ["FAKE_RABBIT"])
def rewrite(text):
    return re.sub(r"(?<![\w.$/])/(%s)(?=[/\s\"');$]|$)", lambda m: str(sb) + m.group(0), text, flags=re.M)
args = sys.argv[1:]
while args and args[0].startswith("-"):
    args = args[2:] if args[0] in ("-o", "-i", "-p", "-l", "-F") else args[1:]
command = " ".join(args[1:])
with open(sb / "ssh.log", "a") as f:
    f.write(command + "\n")
# FAKE_DAMAGE=NAME: a file of that name sent to the rabbit arrives with its first byte changed.
damage = os.environ.get("FAKE_DAMAGE")
if damage and command.endswith("/" + damage + "'"):
    data = bytearray(sys.stdin.buffer.read())
    data[0] ^= 0xFF
    status = subprocess.run(["bash", "-c", rewrite(command)], cwd=sb, input=bytes(data)).returncode
else:
    status = subprocess.run(["bash", "-c", rewrite(command)], cwd=sb).returncode
# Scripts sent to the stage are rewritten once (a record keeps what has been).
done = sb / "rewritten.txt"
seen = set(done.read_text().split()) if done.exists() else set()
for script in (sb / "dev/shm/lapinou-migrate").glob("*.sh"):
    data = script.read_bytes()
    if hashlib.md5(data).hexdigest() in seen:
        continue
    head, _, rest = data.decode().partition("\n")
    out = (head + "\n" + rewrite(rest)).encode()
    script.write_bytes(out)
    seen.add(hashlib.md5(out).hexdigest())
done.write_text("\n".join(sorted(seen)))
sys.exit(status)
''' % TOP_DIRS


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def make_key(key: Path):
    """A small built key: the repository's install scripts, made-up images and their install.conf."""
    key.mkdir(parents=True)
    for name in ("autorun", "install-lib.sh", "applysettings.sh", "lapinlibre.txt"):
        shutil.copy(ROOT / "image" / "usbkey" / name, key / name)
    zimage = bytes(range(256)) * 4000 + b"kernel tail"  # not a whole number of pages
    rootfs = gzip.compress(b"root filesystem " * 20000, mtime=0)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in (("etc/init.d/S40lapinlibre", b"#!/bin/sh\n"), ("karotz/bin/controller", b"elf"),
                           ("www/index.html", b"<title>LapinLibre</title>\n"),
                           ("etc/conf/karotz.conf", b"[karotz]\nfrom=key\n"),
                           ("etc/conf/wpa.conf", b"from the key\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    usr = gzip.compress(buf.getvalue(), mtime=0)
    for name, data in (("zImage", zimage), ("rootfs.img.gz", rootfs), ("usr.tar.gz", usr)):
        (key / name).write_bytes(data)
    (key / "build_id").write_text("cd6052898d59aa6a\n", newline="\n")
    (key / "install.conf").write_text(
        f"BUILD_ID=cd6052898d59aa6a\nBUILD_VERSION=f794da0b5716\nKERNEL_MD5={md5(zimage)}\n"
        f"ROOTFS_MD5={md5(rootfs)}\nUSR_MD5={md5(usr)}\nINSTALL_TOOLS_MD5=0\n", newline="\n")


class FrosRabbit(Rabbit):
    """test_usbkey's sandboxed Karotz, running Free Rabbits OS, reached by the fake ssh."""

    def __init__(self, key: Path, sb: Path, network_conf=NETWORK_CONF, nanddump=None):
        super().__init__(key, sb, wpa=WPA)
        sb = self.sb
        log = sb / "actions.log"
        (sb / "proc/mtd").write_text(PROC_MTD, newline="\n")
        (sb / "proc/mounts").write_text(f"/dev/root / cramfs ro 0 0\n{sb}/dev/mtdblock6 {sb}/usr yaffs rw 0 0\n"
                                        "tmpfs /dev/shm tmpfs rw 0 0\n", newline="\n")
        # The scripts' paths are moved into the sandbox, so this one is too.
        (sb / "proc/cmdline").write_text(f"initrd=0x31800000,0x00500000 root={sb}/dev/ram rw init=/linuxrc\n",
                                         newline="\n")
        (sb / "proc/net").mkdir()
        (sb / "proc/net/route").write_text(PROC_ROUTE, newline="\n")
        (sb / "etc").mkdir(exist_ok=True)
        (sb / "etc/resolv.conf").write_text("nameserver 192.168.1.254\n", newline="\n")
        (sb / "dev/shm").mkdir()
        # Every partition, full size, as Free Rabbits OS left it.
        self.before = {}
        for n, size in ((0, 0x40000), (1, 3 * MiB), (2, 5 * MiB), (3, 3 * MiB), (4, 5 * MiB), (5, 3 * MiB)):
            data = bytes([0x30 + n]) * size
            (sb / f"dev/mtd{n}").write_bytes(data)
            self.before[n] = md5(data)
        (sb / "dev/mtdblock6").write_text("")
        if network_conf is not None:
            (sb / "usr/etc/conf/network.conf").write_text(network_conf, newline="\n")
        (sb / "usr/karotz/messages").mkdir(parents=True)
        (sb / "usr/karotz/messages/m1").write_text("kept\n", newline="\n")
        (sb / "usr/etc/conf/karotz.conf").write_text("[karotz]\nfrom=rabbit\n", newline="\n")
        # nanddump -o -b -l LEN -f FILE DEVICE: LEN bytes of the device (or what nanddump= gives).
        stub(sb / "sbin/nanddump", textwrap.dedent(f"""\
            len= out=
            while [ $# -gt 1 ]; do
                case "$1" in -l) len=$2; shift 2 ;; -f) out=$2; shift 2 ;; *) shift ;; esac
            done
            echo "dump $1" >> {log}
            {nanddump or 'head -c "$len" "$1" > "$out"'}
            """))
        fake = sb / "fake-ssh"
        fake.write_text(FAKE_SSH, newline="\n")
        fake.chmod(0o755)

    def migrate(self, key: Path, *extra, **env_extra):
        env = dict(os.environ, LAPINOU_SSH=str(self.sb / "fake-ssh"), FAKE_RABBIT=str(self.sb), **env_extra)
        return subprocess.run(["bash", str(MIGRATE), "--rabbit", IP, "--key", str(key), "--ssh-pubkey", TEST_KEY,
                               "--no-wait", *extra], capture_output=True, text=True, timeout=600, env=env)

    def writes(self):
        """The erase and write actions, with the sandbox's prefix taken off."""
        return [a.replace(str(self.sb), "") for a in self.actions() if a.startswith(("erase", "write", "flash_erase"))]

    def conf(self, name):
        return (self.sb / "usr/etc/conf" / name).read_text()


class MigrateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="lapinou-migrate-test-"))
        cls.key = cls.tmp / "key"
        make_key(cls.key)

    @classmethod
    def tearDownClass(cls):
        if os.environ.get("LAPIN_TEST_KEEP"):
            print(f"sandboxes kept in {cls.tmp}")
        else:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def rabbit(self, name, key=None, **kw):
        return FrosRabbit(key or self.key, self.tmp / name, **kw)

    def run_ok(self, r, key=None, *extra):
        p = r.migrate(key or self.key, *extra)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def assert_protected_untouched(self, r):
        for n in (0, 3, 4, 5):
            self.assertEqual(md5((r.sb / f"dev/mtd{n}").read_bytes()), r.before[n], f"mtd{n} changed")
        for line in r.writes():
            self.assertFalse(line.split()[1].startswith(PROTECTED), line)

    def assert_migrated(self, r, key=None):
        key = key or self.key
        sb = r.sb
        self.assertEqual((sb / "dev/mtd1").read_bytes(), (key / "zImage").read_bytes())
        self.assertEqual((sb / "dev/mtd2").read_bytes(), (key / "rootfs.img.gz").read_bytes())
        self.assertEqual(r.writes(), ["erase /dev/mtd1", "write /dev/mtd1 /dev/shm/lapinou-migrate/zImage",
                                      "erase /dev/mtd2", "write /dev/mtd2 /dev/shm/lapinou-migrate/rootfs.img.gz"])
        self.assertEqual(r.conf("lapinlibre_build_id"), (key / "build_id").read_text())
        self.assertTrue((sb / "usr/etc/init.d/S40lapinlibre").is_file())
        self.assertFalse((sb / "usr/scripts/old.sh").exists(), "Free Rabbits OS's /usr should be gone")
        self.assertFalse((sb / "usr/.lapinou-migrate").exists(), "the staged usr.tar.gz should be removed")
        self.assertFalse((sb / "usr/etc/inittab").exists())
        self.assertEqual((sb / "usr/karotz/apps/myapp").read_text(), "kept\n")
        self.assertEqual((sb / "usr/karotz/messages/m1").read_text(), "kept\n")
        self.assertEqual((sb / "usr/karotz/uuid.conf").read_text(), "0e1f2a3b-4c5d-6e7f-8091-a2b3c4d5e6f7\n")
        self.assertEqual(r.conf("authorized_keys"), TEST_KEY + "\n")
        self.assertIn("reboot -f", r.actions())

    def test_migrates(self):
        r = self.rabbit("migrates")
        self.run_ok(r)
        self.assert_migrated(r)
        self.assert_protected_untouched(r)
        # Each written partition was read back.
        self.assertIn("dump " + str(r.sb / "dev/mtd1"), r.actions())
        self.assertIn("dump " + str(r.sb / "dev/mtd2"), r.actions())
        # The rabbit's own karotz.conf wins over the key's.
        self.assertEqual(r.conf("karotz.conf"), "[karotz]\nfrom=rabbit\n")

    def test_refuses_mtd0_and_rescue(self):
        r = self.rabbit("protected")
        self.run_ok(r)
        self.assert_protected_untouched(r)
        # Neither script names a protected partition at all.
        for script in MIGRATE.parent.glob("*.sh"):
            self.assertIsNone(re.search(r"mtd[0345]\b", script.read_text()), script.name)

    def test_bad_checksum_writes_nothing(self):
        key = self.tmp / "bad-key"
        shutil.copytree(self.key, key)
        data = bytearray((key / "rootfs.img.gz").read_bytes())
        data[100] ^= 0xFF
        (key / "rootfs.img.gz").write_bytes(bytes(data))
        r = self.rabbit("bad-checksum")
        p = r.migrate(key)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertEqual(r.writes(), [])
        self.assertNotIn("reboot -f", r.actions())
        self.assertEqual((r.sb / "usr/scripts/old.sh").read_text(), "wiped\n")

    def test_bad_checksum_on_the_rabbit_writes_nothing(self):
        # The PC's copy checks out, but the kernel is damaged on its way to the rabbit.
        r = self.rabbit("bad-transfer")
        p = r.migrate(self.key, FAKE_DAMAGE="zImage")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertEqual(r.writes(), [])
        self.assertIn("MD5 checksum error: zImage", p.stdout)

    def test_keeps_wifi_conf(self):
        r = self.rabbit("wifi")
        self.run_ok(r)
        self.assertEqual(r.conf("wpa.conf"), WPA)
        self.assertEqual(r.conf("network.conf"), NETWORK_CONF)

    def test_fixed_address_from_the_live_network(self):
        # No network.conf (Free Rabbits OS sets its address in its own scripts): LapinLibre keeps the
        # address the rabbit has now, with its netmask, gateway and DNS.
        r = self.rabbit("live-network", network_conf=None)
        self.run_ok(r)
        self.assertEqual(r.conf("network.conf"),
                         'IP="192.168.1.103"\nNETMASK="255.255.255.0"\nGATEWAY="192.168.1.1"\nDNS="192.168.1.254"\n')
        self.assertEqual(r.conf("wpa.conf"), WPA)

    def test_dhcp(self):
        r = self.rabbit("dhcp", network_conf=None)
        self.run_ok(r, None, "--dhcp")
        self.assertEqual(r.conf("network.conf"), 'IP=""\nNETMASK=""\nGATEWAY=""\nDNS=""\n')

    def test_readback_mismatch(self):
        r = self.rabbit("readback", nanddump='head -c "$len" "$1" > "$out"; case "$1" in *mtd2) '
                                             'printf garbage | dd of="$out" conv=notrunc 2> /dev/null ;; esac')
        p = r.migrate(self.key)
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertNotIn("reboot -f", r.actions())
        # /usr untouched: Free Rabbits OS still runs.
        self.assertEqual((r.sb / "usr/scripts/old.sh").read_text(), "wiped\n")
        self.assert_protected_untouched(r)

    def test_dry_run_writes_nothing(self):
        r = self.rabbit("dry-run")
        p = r.migrate(self.key, "--dry-run")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(r.writes(), [])
        self.assertNotIn("reboot -f", r.actions())
        self.assertFalse((r.sb / "usr/.lapinou-migrate").exists())
        self.assertFalse((r.sb / "dev/shm/lapinou-migrate").exists())
        self.assertNotIn("usr.tar.gz", (r.sb / "ssh.log").read_text())
        self.assertEqual((r.sb / "usr/scripts/old.sh").read_text(), "wiped\n")
        self.assertIn("Checks passed", p.stdout)

    def test_no_ssh_key_writes_nothing(self):
        r = self.rabbit("no-ssh-key")
        env = dict(os.environ, LAPINOU_SSH=str(r.sb / "fake-ssh"), FAKE_RABBIT=str(r.sb))
        p = subprocess.run(["bash", str(MIGRATE), "--rabbit", IP, "--key", str(self.key), "--no-wait"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertFalse((r.sb / "ssh.log").exists(), "nothing should reach the rabbit")

    def test_already_lapinlibre_writes_nothing(self):
        r = self.rabbit("already")
        (r.sb / "usr/etc/conf/lapinlibre_build_id").write_text("0123456789abcdef\n")
        p = r.migrate(self.key)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertEqual(r.writes(), [])

    def test_real_key(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        from lapin import out_dir
        key = out_dir() / "usbkey"
        if not (key / "install.conf").is_file():
            self.skipTest("no built key (out/usbkey)")
        r = self.rabbit("real-key", key=key)
        self.run_ok(r, key)
        self.assert_migrated(r, key)
        self.assertTrue((r.sb / "usr/karotz/bin/controller").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
