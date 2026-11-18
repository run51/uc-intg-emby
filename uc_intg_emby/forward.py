"""Volume keys of the player screens forwarded to another device of the Remote.
:license: MPL-2.0

In the media browser and the player screen, the Remote sends the volume keys to the player shown (here the
Emby session), whatever the activity maps them to. When the configuration names an amplifier's volume (a
number entity, or a media player) and the Remote API key, the keys are sent to that entity through the
Remote's own API instead, so the volume keys keep working while browsing.
"""

from __future__ import annotations

import logging
import os

import aiohttp

_LOG = logging.getLogger(__name__)

REMOTE_URL = os.getenv("UC_REMOTE_API_URL", "http://127.0.0.1")  # the integration runs on the Remote
TIMEOUT_S = 5


def volume_command(entity_id: str, up: bool) -> str:
    """Command of the target entity for one volume step."""
    if ".number." in entity_id:
        return "number.increment" if up else "number.decrement"
    return "media_player.volume_up" if up else "media_player.volume_down"


async def send(api_key: str, entity_id: str, cmd_id: str) -> bool:
    """Send one command to an entity of the Remote; True when it was accepted."""
    if not api_key or not entity_id:
        return False
    url = f"{REMOTE_URL}/api/entities/{entity_id}/command"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_S)) as session:
            async with session.put(
                url, json={"cmd_id": cmd_id}, headers={"Authorization": f"Bearer {api_key}"}
            ) as resp:
                if resp.status == 200:
                    return True
                _LOG.warning("Forwarded %s to %s refused: HTTP %d", cmd_id, entity_id, resp.status)
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOG.warning("Forwarded %s to %s failed: %s", cmd_id, entity_id, err)
    return False
