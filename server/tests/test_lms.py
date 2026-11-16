# SPDX-License-Identifier: MIT
import pytest
from fakes.fake_lms import FakeLms

from lapinou.brain.base import validate_args
from lapinou.config import RabbitConfig
from lapinou.tools.base import ToolBox, ToolContext
from lapinou.tools.lms import LmsTools

CTX = ToolContext(rabbit_id="lapin-a")
CUISINE, SALON, CHAMBRE = "aa:aa:aa:aa:aa:01", "aa:aa:aa:aa:aa:02", "aa:aa:aa:aa:aa:03"


@pytest.fixture
def lms():
    fake = FakeLms()
    tools = LmsTools(
        "lms.local", 9000,
        rabbits={"lapin-a": RabbitConfig(name="Lapinou", default_lms_player="Salon")},
        client=fake.client(),
    )
    return fake, tools


async def test_players(lms):
    fake, tools = lms
    names = [p.name for p in await tools.players()]
    assert names == ["Cuisine", "Salon", "Chambre des enfants"]
    assert fake.commands[0] == ("", ["players", 0, 99])


@pytest.mark.parametrize("room", ["dans la cuisine", "Cuisine", "la cuisiné", "cuisinne"])
async def test_room_resolves_to_player(lms, room):
    _, tools = lms
    player = await tools.resolve(room, "lapin-a")
    assert player.name == "Cuisine"


async def test_no_room_uses_rabbit_default(lms):
    _, tools = lms
    assert (await tools.resolve(None, "lapin-a")).name == "Salon"
    assert (await tools.resolve("", "lapin-a")).name == "Salon"


async def test_unknown_room_is_an_error_listing_rooms(lms):
    fake, tools = lms
    box = ToolBox(tools.tools())
    result = await box.call("lms_pause", {"room": "garage", "resume": False}, CTX)
    assert result.is_error
    assert "Cuisine" in result.content and "Salon" in result.content
    assert fake.sent() == []


async def test_play_favorite_by_name_in_room(lms):
    fake, tools = lms
    box = ToolBox(tools.tools())
    result = await box.call(
        "lms_play", {"room": "dans la cuisine", "query": "france inter"}, CTX
    )
    assert not result.is_error, result.content
    assert fake.sent() == [(CUISINE, ["favorites", "playlist", "play", "item_id:f1.0"])]


async def test_play_url(lms):
    fake, tools = lms
    await tools.play(await tools.resolve(None, "lapin-a"), "http://radio.example/stream.mp3")
    assert fake.sent() == [(SALON, ["playlist", "play", "http://radio.example/stream.mp3"])]


async def test_play_unknown_favorite_lists_favorites(lms):
    fake, tools = lms
    box = ToolBox(tools.tools())
    result = await box.call("lms_play", {"room": None, "query": "zzzz qqqq"}, CTX)
    assert result.is_error and "France Inter" in result.content
    assert fake.sent() == []


async def test_pause_volume_next(lms):
    fake, tools = lms
    box = ToolBox(tools.tools())
    await box.call("lms_pause", {"room": "salon", "resume": False}, CTX)
    await box.call("lms_pause", {"room": "salon", "resume": True}, CTX)
    await box.call("lms_volume", {"room": "salon", "level": 30}, CTX)
    await box.call("lms_next", {"room": "salon"}, CTX)
    assert fake.sent() == [
        (SALON, ["pause", "1"]),
        (SALON, ["pause", "0"]),
        (SALON, ["mixer", "volume", "30"]),
        (SALON, ["playlist", "index", "+1"]),
    ]


async def test_now_playing(lms):
    _, tools = lms
    box = ToolBox(tools.tools())
    result = await box.call("lms_now_playing", {"room": None}, CTX)
    assert "Le journal" in result.content and "Salon" in result.content


async def test_sync_rooms(lms):
    fake, tools = lms
    box = ToolBox(tools.tools())
    await box.call("lms_group", {"rooms": ["cuisine", "chambre des enfants"]}, CTX)
    assert fake.sent() == [(CUISINE, ["sync", CHAMBRE])]
    await box.call("lms_group", {"rooms": ["chambre"]}, CTX)
    assert fake.sent()[-1] == (CHAMBRE, ["sync", "-"])


async def test_lms_down_is_a_spoken_error(lms):
    fake, tools = lms
    fake.down = True
    box = ToolBox(tools.tools())
    result = await box.call("lms_next", {"room": None}, CTX)
    assert result.is_error and "Lyrion" in result.content


def test_schemas_are_strict(lms):
    _, tools = lms
    for tool in tools.tools():
        spec = tool.spec()
        props = spec.input_schema["properties"]
        assert spec.input_schema["additionalProperties"] is False
        assert set(spec.input_schema["required"]) == set(props)
    assert validate_args(
        next(t for t in tools.tools() if t.name == "lms_volume").spec().input_schema,
        {"room": None, "level": 30},
    ) is None
