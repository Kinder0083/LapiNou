# SPDX-License-Identifier: MIT
"""Passerelle audio : trames LapinouAudio forgées à la main (sans protoc)."""

from __future__ import annotations

import asyncio
import struct
import sys

import numpy as np
import pytest

from lapinou.rabbit import audio_gateway
from lapinou.rabbit.audio_gateway import AudioGateway, mulaw_decode

asr_server = sys.modules["asr_server"]
varint = asr_server.varint


def _key(number: int, wire: int) -> bytes:
    return varint(number << 3 | wire)


def _s(number: int, text: str) -> bytes:
    data = text.encode()
    return _key(number, 2) + varint(len(data)) + data


def _b(number: int, data: bytes) -> bytes:
    return _key(number, 2) + varint(len(data)) + data


def _u(number: int, value: int) -> bytes:
    return _key(number, 0) + varint(value)


def hello(rabbit_id: str, rate: int = 16000) -> bytes:
    return _s(1, rabbit_id) + _s(2, "fw-1") + _u(3, rate)


def frame(rabbit_id, seq, mulaw=None, first=None, mark=None, hello_msg=None) -> bytes:
    body = _s(1, rabbit_id) + _u(2, seq)
    if mulaw is not None:
        body += _b(3, mulaw)
    if first is not None:
        body += _u(4, first)
    if mark is not None:
        pid, at, start = mark
        body += _b(5, _s(1, pid) + _u(2, at) + _u(3, int(start)))
    if hello_msg is not None:
        body += _b(6, hello_msg)
    return struct.pack(">I", len(body)) + body


@pytest.fixture
async def gateway():
    gw = AudioGateway("127.0.0.1", 0)
    await gw.start()
    yield gw
    await gw.stop()


async def connect(gw):
    _, writer = await asyncio.open_connection("127.0.0.1", gw.port)
    return writer


async def take(stream, count):
    out = []
    async for chunk in stream.frames():
        out.append(chunk)
        if len(out) == count:
            return out


def test_mulaw_all_256_values():
    data = bytes(range(256))
    expected = [asr_server.mulaw_sample(u) for u in range(256)]
    pcm = mulaw_decode(data)
    assert pcm.dtype == np.int16
    assert pcm.tolist() == expected


async def test_audio_decoded_with_first_sample(gateway):
    w = await connect(gateway)
    w.write(frame("r1", 0, bytes([0xFF, 0x00]), 0, hello_msg=hello("r1")))
    w.write(frame("r1", 1, bytes([0x80]), 2))
    await w.drain()
    stream = await asyncio.wait_for(gateway.stream("r1"), 2)
    chunks = await asyncio.wait_for(take(stream, 2), 2)
    assert chunks[0].first_sample == 0 and chunks[0].pcm.tolist() == [0, -32124]
    assert chunks[1].first_sample == 2 and chunks[1].pcm.tolist() == [32124]
    assert stream.firmware_version == "fw-1" and stream.gaps == 0
    w.close()


async def test_seq_jump_counted(gateway):
    w = await connect(gateway)
    w.write(frame("r1", 0, b"\xff", 0, hello_msg=hello("r1")))
    w.write(frame("r1", 1, b"\xff", 1))
    w.write(frame("r1", 4, b"\xff", 4))
    await w.drain()
    stream = await asyncio.wait_for(gateway.stream("r1"), 2)
    await asyncio.wait_for(take(stream, 3), 2)
    assert stream.gaps == 1 and stream.lost_frames == 2
    w.close()


async def test_playback_marks(gateway):
    w = await connect(gateway)
    w.write(frame("r1", 0, hello_msg=hello("r1"), mark=("p1", 1000, True)))
    w.write(frame("r1", 1, b"\xff", 0))
    await w.drain()
    stream = await asyncio.wait_for(gateway.stream("r1"), 2)
    await asyncio.wait_for(take(stream, 1), 2)
    assert stream.playback_marks == {"p1": (1000, None)}
    w.write(frame("r1", 2, mark=("p1", 5000, False)))
    w.write(frame("r1", 3, b"\xff", 1))
    await w.drain()
    await asyncio.wait_for(take(stream, 1), 2)
    assert stream.playback_marks == {"p1": (1000, 5000)}
    w.close()


async def test_two_rabbits_separate(gateway):
    a, b = await connect(gateway), await connect(gateway)
    a.write(frame("A", 0, b"\xff", 0, hello_msg=hello("A")))
    b.write(frame("B", 0, b"\x80", 0, hello_msg=hello("B")))
    await a.drain()
    await b.drain()
    sa = await asyncio.wait_for(gateway.stream("A"), 2)
    sb = await asyncio.wait_for(gateway.stream("B"), 2)
    ca = (await asyncio.wait_for(take(sa, 1), 2))[0]
    cb = (await asyncio.wait_for(take(sb, 1), 2))[0]
    assert sa is not sb
    assert ca.pcm.tolist() == [0] and cb.pcm.tolist() == [32124]
    a.close()
    b.close()


async def test_bad_sample_rate_closes_connection(gateway):
    reader, writer = await asyncio.open_connection("127.0.0.1", gateway.port)
    writer.write(frame("r1", 0, b"\xff", 0, hello_msg=hello("r1", rate=8000)))
    await writer.drain()
    assert await asyncio.wait_for(reader.read(), 2) == b""
    assert "r1" not in gateway.streams


async def test_oversized_frame_closes_connection(gateway):
    reader, writer = await asyncio.open_connection("127.0.0.1", gateway.port)
    writer.write(struct.pack(">I", audio_gateway.MAX_FRAME + 1))
    await writer.drain()
    assert await asyncio.wait_for(reader.read(), 2) == b""


async def test_first_frame_without_hello_closes(gateway):
    reader, writer = await asyncio.open_connection("127.0.0.1", gateway.port)
    writer.write(frame("r1", 0, b"\xff", 0))
    await writer.drain()
    assert await asyncio.wait_for(reader.read(), 2) == b""
    assert "r1" not in gateway.streams


async def test_reconnect_keeps_stream_and_no_gap(gateway):
    w = await connect(gateway)
    w.write(frame("r1", 7, b"\xff", 0, hello_msg=hello("r1")))
    await w.drain()
    stream = await asyncio.wait_for(gateway.stream("r1"), 2)
    await asyncio.wait_for(take(stream, 1), 2)
    w.close()
    w2 = await connect(gateway)
    w2.write(frame("r1", 100, b"\xff", 50, hello_msg=hello("r1")))
    await w2.drain()
    chunk = (await asyncio.wait_for(take(stream, 1), 2))[0]
    assert chunk.first_sample == 50 and stream.gaps == 0
    w2.close()
