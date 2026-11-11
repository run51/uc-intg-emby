"""OPPO audio track and subtitle pickers (select entities). :license: MPL-2.0

The lists come from the Emby.Oppo plugin (/Oppo/Remote/State?Tracks=true), which reads the OPPO's own
audio and subtitle menus; picking an entry asks the plugin to select it on the player.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from ucapi import StatusCodes, select
from ucapi_framework import SelectEntity

from uc_intg_emby.config import EmbyDeviceConfig
from uc_intg_emby.device import EmbyServer

_LOG = logging.getLogger(__name__)

PLACEHOLDER = "—"
NO_SUBTITLES = "Aucun"
# Refresh while a film plays: soon after it starts, then calmly (each refresh costs the OPPO a few queries).
FIRST_REFRESH = 8.0
RETRY_EMPTY = 10.0
REFRESH_PLAYING = 60.0
REFRESH_AFTER_PICK = 2.5


def track_label(track: dict[str, Any], subtitles: bool) -> str:
    """Same labels as the plugin's web remote: "1/5 Français…" → "Français…", subtitle 0 → none."""
    index = track.get("Index", 0)
    if subtitles and index == 0:
        return NO_SUBTITLES
    name = re.sub(r"^\s*\d+\s*/\s*\d+\s*", "", track.get("Name") or "").strip()
    return name or f"Piste {index}"


def build_options(tracks: list[dict[str, Any]], subtitles: bool) -> tuple[list[str], dict[str, int], str]:
    """Option labels (made unique), label → OPPO index, and the label of the selected track."""
    labels: list[str] = []
    indexes: dict[str, int] = {}
    current = ""
    for track in tracks:
        label = track_label(track, subtitles)
        if label in indexes:
            label = f"{label} ({track.get('Index', len(labels))})"
        labels.append(label)
        indexes[label] = int(track.get("Index", 0))
        if track.get("Selected"):
            current = label
    return labels, indexes, current


class OppoTracks:
    """Audio and subtitle menus of the OPPO, refreshed only while a video plays on its Emby session."""

    def __init__(self, device: EmbyServer) -> None:
        self._device = device
        self.audio: list[dict[str, Any]] = []
        self.subtitles: list[dict[str, Any]] = []
        self._item_id = ""
        self._next_at = 0.0
        self._busy = False
        self._started = 0.0

    def _oppo_video(self) -> dict[str, Any] | None:
        for device_id, session in self._device.sessions.items():
            item = session.get("NowPlayingItem")
            # The plugin's own session ("emby-oppo-<ip>", client "OPPO"), not the OPPO's DLNA renderer.
            is_oppo = device_id.lower().startswith("emby-oppo") or session.get("Client") == "OPPO"
            if is_oppo and item and item.get("MediaType") == "Video":
                return item
        return None

    def schedule(self, delay: float) -> None:
        self._next_at = min(self._next_at, time.monotonic() + delay) if self._next_at else time.monotonic() + delay

    async def poll(self) -> bool:
        """Called on every device poll; returns True when the lists changed."""
        item = self._oppo_video()
        if not item:
            changed = bool(self.audio or self.subtitles or self._item_id)
            self.audio, self.subtitles, self._item_id, self._next_at = [], [], "", 0.0
            return changed
        if item.get("Id", "") != self._item_id:
            self._item_id = item.get("Id", "")
            changed = bool(self.audio or self.subtitles)
            self.audio, self.subtitles = [], []
            self._next_at = time.monotonic() + FIRST_REFRESH
            self._started = time.monotonic()
            return changed
        if self._busy or not self._next_at or time.monotonic() < self._next_at:
            return False
        return await self.refresh()

    async def refresh(self) -> bool:
        client = self._device.client
        if not client or self._busy:
            return False
        self._busy = True
        try:
            state = await client.get_oppo_tracks()
        finally:
            self._busy = False
        audio = (state or {}).get("Audio") or []
        subtitles = (state or {}).get("Subtitles") or []
        # Right after a start the OPPO reports only the current audio track and no subtitle menu yet
        # (seen on a Blu-ray folder: 1 track and 0 subtitles, then 14 and 23 a minute later): ask again soon.
        # A film may really have one track and no subtitles: give up the quick retries after two minutes.
        complete = bool(audio) and (bool(subtitles) or len(audio) > 1)
        complete = complete or time.monotonic() - self._started > 120
        self._next_at = time.monotonic() + (REFRESH_PLAYING if complete else RETRY_EMPTY)
        if not self._item_id:
            return False
        changed = audio != self.audio or subtitles != self.subtitles
        self.audio, self.subtitles = audio, subtitles
        if changed:
            _LOG.info("OPPO menus: %d audio track(s), %d subtitle entr(y/ies)", len(audio), len(subtitles))
        return changed

    async def pick(self, subtitles: bool, index: int) -> bool:
        client = self._device.client
        if not client:
            return False
        ok = await client.select_oppo_track(subtitles, index)
        if ok:
            for track in self.subtitles if subtitles else self.audio:
                track["Selected"] = track.get("Index") == index
        self._next_at = time.monotonic() + REFRESH_AFTER_PICK
        return ok


class OppoTrackSelect(SelectEntity):
    """Select box listing the OPPO's audio tracks or subtitles of the film playing."""

    SUBTITLES = False

    def __init__(self, device_config: EmbyDeviceConfig, device: EmbyServer) -> None:
        self._device = device
        self._indexes: dict[str, int] = {}
        kind = "subtitles" if self.SUBTITLES else "audio"
        super().__init__(
            f"select.{device_config.identifier}.oppo_{kind}",
            {"fr": "Sous-titres", "en": "Subtitles"} if self.SUBTITLES else {"fr": "Langue", "en": "Audio"},
            attributes={
                select.Attributes.STATE: select.States.UNKNOWN,
                # Empty at first so that the first sync really sends the placeholder to the remote.
                select.Attributes.OPTIONS: [],
                select.Attributes.CURRENT_OPTION: "",
            },
            cmd_handler=self._handle_command,
        )
        self.subscribe_to_device(device)

    def _tracks(self) -> list[dict[str, Any]]:
        tracks = self._device.oppo_tracks
        return (tracks.subtitles if self.SUBTITLES else tracks.audio) if tracks else []

    async def sync_state(self) -> None:
        if self._device.state == "UNAVAILABLE":
            self.update({select.Attributes.STATE: select.States.UNAVAILABLE})
            return
        labels, self._indexes, current = build_options(self._tracks(), self.SUBTITLES)
        if not labels:
            labels, current = [PLACEHOLDER], PLACEHOLDER
        self.update({
            select.Attributes.STATE: select.States.ON,
            select.Attributes.OPTIONS: labels,
            select.Attributes.CURRENT_OPTION: current or labels[0],
        })

    async def _handle_command(self, entity: Any, cmd_id: str, params: dict[str, Any] | None) -> StatusCodes:
        labels = list(self._indexes)
        if not labels:
            return StatusCodes.SERVICE_UNAVAILABLE
        current = self.attributes.get(select.Attributes.CURRENT_OPTION)
        pos = labels.index(current) if current in labels else 0
        if cmd_id == select.Commands.SELECT_OPTION:
            option = (params or {}).get("option", "")
        elif cmd_id == select.Commands.SELECT_FIRST:
            option = labels[0]
        elif cmd_id == select.Commands.SELECT_LAST:
            option = labels[-1]
        elif cmd_id == select.Commands.SELECT_NEXT:
            option = labels[(pos + 1) % len(labels)] if (params or {}).get("cycle", True) or pos + 1 < len(labels) else current
        elif cmd_id == select.Commands.SELECT_PREVIOUS:
            option = labels[pos - 1] if (params or {}).get("cycle", True) or pos > 0 else current
        else:
            return StatusCodes.NOT_IMPLEMENTED
        if option not in self._indexes:
            return StatusCodes.BAD_REQUEST
        self.update({select.Attributes.CURRENT_OPTION: option})
        ok = await self._device.oppo_tracks.pick(self.SUBTITLES, self._indexes[option])
        if not ok:
            _LOG.warning("[%s] The OPPO refused %s", self.id, option)
            await self.sync_state()
            return StatusCodes.SERVER_ERROR
        return StatusCodes.OK


class OppoAudioSelect(OppoTrackSelect):
    SUBTITLES = False


class OppoSubtitleSelect(OppoTrackSelect):
    SUBTITLES = True

