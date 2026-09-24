import asyncio

import pytest

from xr18_mcp import osc_client
from xr18_mcp.osc_client import MixerInfo, OscClient, build

from . import fake_mixer


@pytest.fixture
async def client(fake):
    c = OscClient("127.0.0.1", fake.port, timeout=0.05)
    await c.open()
    yield c
    c.close()


async def test_replies_matched_and_lost_packets_retried(client, fake):
    fake.drop_first = {"/ch/01/mix/fader"}
    fake.values["/ch/01/mix/fader"] = 0.25
    fake.values["/ch/02/mix/fader"] = 0.5
    res = await client.get_many(["/ch/01/mix/fader", "/ch/02/mix/fader", "/nope", "/ch/02/mix/fader"])
    assert res == {"/ch/01/mix/fader": [0.25], "/ch/02/mix/fader": [0.5], "/nope": None}


async def test_node_requests(client, fake):
    fake.values["/ch/01/config/name"] = "Kick"
    text = await client.node("/ch/01/config")
    assert text.startswith('/ch/01/config "Kick"')
    many = await client.node_many(["ch/01/config", "/ch/02/config", "no/such"])
    assert many["ch/02/config"].startswith("/ch/02/config") and many["no/such"] is None


async def test_info_and_short_reply(client, fake):
    assert await client.info() == MixerInfo("127.0.0.1", "FAKE-XR18", "XR18", "1.25")
    fake.answer_xinfo = False
    assert await client.info() is None


async def test_set_many_paces_bursts(client, fake):
    client.window = 4
    await client.set_many([(f"/ch/{i:02d}/mix/fader", 0.5) for i in range(1, 11)])
    assert len(fake.sets) == 10


async def test_sample_meters(client, fake):
    fake.meters["/meters/1"] = [-12.0] * 40
    frames = await client.sample_meters("/meters/1", 0.1)
    assert fake.subscriptions == ["/meters/1"]
    assert len(frames) == fake.meter_frames
    assert client._meter_queues == []  # listener removed afterwards


async def test_sample_meters_renews_subscription(client, fake, monkeypatch):
    monkeypatch.setattr(osc_client, "METER_RENEW_SECONDS", 0.05)
    await client.sample_meters("/meters/2", 0.18)
    assert len(fake.subscriptions) >= 3
    assert set(fake.subscriptions) == {"/meters/2"}


async def test_ignores_garbage_and_foreign_messages(client):
    client._on_datagram(b"not osc at all")
    client._on_datagram(build("/unrequested", 1))  # nobody waiting: dropped silently
    client._on_datagram(build("node", ""))  # empty node text
    client._on_datagram(build("/meters/1", b"\x00\x00\x00\x00"))  # meters without a listener
    _Proto = osc_client._Proto(client)
    _Proto.error_received(OSError("port unreachable"))  # must not raise
    assert client.is_open


async def test_close_and_reopen(fake):
    c = OscClient("127.0.0.1", fake.port)
    assert not c.is_open
    await c.open()
    assert c.is_open
    c.close()
    c.close()  # idempotent
    assert not c.is_open


async def test_discover_finds_mixer_on_fallback_address(fake, monkeypatch):
    monkeypatch.setattr(osc_client, "XAIR_PORT", fake.port)
    monkeypatch.setattr(osc_client, "AP_MODE_IP", "127.0.0.1")
    found = await asyncio.to_thread(osc_client.discover, 0.3)
    assert found == [MixerInfo("127.0.0.1", "FAKE-XR18", "XR18", "1.25")]


def test_discover_skips_garbage_replies(monkeypatch):
    replies = [(b"garbage", ("10.0.0.9", 10024)), (build("/xinfo", "10.0.0.5", "M", "XR18", "1.25"), ("10.0.0.5", 10024)),
               (build("/other", 1), ("10.0.0.6", 10024))]

    class Sock:
        def __init__(self, *a):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def setsockopt(self, *a):
            pass

        def settimeout(self, *a):
            pass

        def connect(self, *a):
            pass

        def getsockname(self):
            return ("127.0.0.1", 5555)  # loopback only -> no subnet broadcast

        def sendto(self, *a):
            pass

        def recvfrom(self, *a):
            if replies:
                return replies.pop(0)
            raise TimeoutError

    monkeypatch.setattr(osc_client.socket, "socket", Sock)
    assert osc_client._subnet_broadcast() is None
    assert osc_client.discover(timeout=0.1) == [MixerInfo("10.0.0.5", "M", "XR18", "1.25")]


def test_build_encodes_types():
    from pythonosc.osc_message import OscMessage

    msg = OscMessage(build("/x", 1, 0.5, "s", b"\x01"))
    assert msg.address == "/x" and list(msg.params) == [1, 0.5, "s", b"\x01"]


async def test_fake_start_signature():
    proto, transport = await fake_mixer.start(answer_xinfo=False)
    assert proto.port > 0 and not proto.answer_xinfo
    transport.close()
