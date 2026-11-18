"""Stop shown at once, faster polling while playing, volume keys forwarded to an amplifier."""

from __future__ import annotations

import asyncio
import time

from ucapi import StatusCodes, media_player

from uc_intg_emby import const, forward
from uc_intg_emby.config import EmbyDeviceConfig
from uc_intg_emby.device import EmbyServer
from uc_intg_emby.media_player import CLEAR_PLAYBACK, EmbyMediaPlayer

SESSION = {
    "Id": "s1", "DeviceId": "oppo", "DeviceName": "OPPO", "Client": "Emby.Oppo",
    "SupportedCommands": ["VolumeUp", "VolumeDown"],
    "NowPlayingItem": {"Id": "42", "Name": "Avatar", "Type": "Movie", "RunTimeTicks": 100 * 10_000_000},
    "PlayState": {"PositionTicks": 16 * 10_000_000, "IsPaused": False},
}


def make_server() -> EmbyServer:
    server = EmbyServer.__new__(EmbyServer)
    server._sessions = {"oppo": dict(SESSION)}
    server._cleared = {}
    server._state = "ON"
    return server


def make_player(server: EmbyServer, **cfg) -> EmbyMediaPlayer:
    config = EmbyDeviceConfig(identifier="emby", name="Emby", server_url="http://e", api_key="k", **cfg)
    player = EmbyMediaPlayer.__new__(EmbyMediaPlayer)
    player._device = server
    player._device_config = config
    player._emby_device_id = "oppo"
    player._supported_commands = SESSION["SupportedCommands"]
    player.updates = []
    player.update = player.updates.append
    return player


def test_stop_from_the_remote_hides_the_title_until_the_server_catches_up() -> None:
    server = make_server()
    assert server.now_playing("oppo")["Id"] == "42"
    server.clear_playing("oppo")
    assert server.now_playing("oppo") is None  # the server still reports it: hidden
    server._sessions["oppo"] = {**SESSION, "NowPlayingItem": None}
    assert server.now_playing("oppo") is None and "oppo" not in server._cleared  # caught up
    server._sessions["oppo"] = dict(SESSION)
    assert server.now_playing("oppo")["Id"] == "42"  # played again later: shown


def test_hidden_title_comes_back_after_the_hold_or_for_another_title() -> None:
    server = make_server()
    server.clear_playing("oppo")
    server._sessions["oppo"] = {**SESSION, "NowPlayingItem": {"Id": "7", "Name": "Other"}}
    assert server.now_playing("oppo")["Id"] == "7"
    server._sessions["oppo"] = dict(SESSION)
    server.clear_playing("oppo")
    server._cleared["oppo"] = ("42", time.monotonic() - const.CLEARED_HOLD_S - 1)
    assert server.now_playing("oppo")["Id"] == "42"  # the server never confirmed the stop


def test_clear_playback_shows_stopped_without_asking_the_server() -> None:
    server = make_server()
    player = make_player(server)
    status = asyncio.run(player._handle_command(None, CLEAR_PLAYBACK, None))
    assert status == StatusCodes.OK
    last = player.updates[-1]
    assert last[media_player.Attributes.STATE] == media_player.States.ON
    assert last[media_player.Attributes.MEDIA_TITLE] == "" and last[media_player.Attributes.MEDIA_POSITION] == 0


def test_volume_keys_forwarded_to_the_amplifier(monkeypatch) -> None:
    sent = []

    async def fake_send(api_key, entity_id, cmd_id):
        sent.append((api_key, entity_id, cmd_id))
        return True

    monkeypatch.setattr(forward, "send", fake_send)
    server = make_server()
    player = make_player(server, remote_api_key="rk", volume_entity="denon_rs232.main.number.denon.volume",
                         mute_entity="denon_rs232.main.media_player.denon")
    for cmd in (media_player.Commands.VOLUME_UP, media_player.Commands.VOLUME_DOWN, media_player.Commands.MUTE_TOGGLE):
        assert asyncio.run(player._handle_command(None, cmd, None)) == StatusCodes.OK
    assert sent == [
        ("rk", "denon_rs232.main.number.denon.volume", "number.increment"),
        ("rk", "denon_rs232.main.number.denon.volume", "number.decrement"),
        ("rk", "denon_rs232.main.media_player.denon", "media_player.mute_toggle"),
    ]
    assert forward.volume_command("denon_rs232.main.media_player.denon", True) == "media_player.volume_up"
