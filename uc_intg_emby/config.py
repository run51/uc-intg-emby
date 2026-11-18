"""Emby configuration. :copyright: (c) 2026 by Meir Miyara. :license: MPL-2.0"""
from dataclasses import dataclass, field
from ucapi_framework import BaseConfigManager


@dataclass
class EmbyDeviceConfig:
    """Emby server device configuration."""

    identifier: str
    name: str
    server_url: str
    api_key: str
    user_id: str = ""
    server_version: str = ""
    # Volume keys of the player screens forwarded to another device of the Remote (an amplifier):
    # Remote API key, number or media player entity for volume up/down, media player entity for mute.
    remote_api_key: str = ""
    volume_entity: str = ""
    mute_entity: str = ""
