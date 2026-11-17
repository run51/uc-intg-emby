"""Library lists kept on the Remote: browsing does not wait for the Emby server.
:license: MPL-2.0

The integration runs on the Remote, whose Wi-Fi can be weak: a request to the Emby server sometimes takes
seconds, and the Remote's interface gives up a page after about 10 s ("cannot load the media content").
The lists of the flat libraries (movies, music videos), with the resolution/HDR badges of the OPPO plugin,
are therefore downloaded in the background, kept in memory and on disk, and pages are cut from them at
once. A list older than STALE_S is still served, and refreshed in the background. Every poster of these
lists is queued in the picture cache, so the pictures are on the Remote before they are shown.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from uc_intg_emby import images

if TYPE_CHECKING:
    from uc_intg_emby.device import EmbyServer

_LOG = logging.getLogger(__name__)

STALE_S = 30 * 60  # a list older than this is refreshed in the background when it is browsed
REFRESH_S = 6 * 3600  # all lists are refreshed this often while the integration runs
WARM_UP_DELAY_S = 20  # after connecting, before the first download (the Remote has just woken up)
CHUNK = 200  # titles per request
BADGE_CHUNK = 100  # ids per OPPO badges request
DROP_FIELDS = ("Overview",)  # not shown in lists (the title page asks for the item itself)


class LibraryCache:
    def __init__(self, folder: str) -> None:
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.lists: dict[str, dict[str, Any]] = {}
        self.running: dict[str, asyncio.Task] = {}
        self._load()

    @staticmethod
    def key(device: EmbyServer, library_id: str, item_types: str) -> str:
        return f"{device.identifier}_{device.user_id}_{library_id}_{item_types}".replace("/", "_").replace(",", "+")

    def get(self, key: str) -> dict[str, Any] | None:
        return self.lists.get(key)

    def stale(self, entry: dict[str, Any]) -> bool:
        return time.time() - entry.get("at", 0) > STALE_S

    def refresh_soon(self, device: EmbyServer, library_id: str, item_types: str) -> None:
        """Download the list in the background (once at a time)."""
        key = self.key(device, library_id, item_types)
        task = self.running.get(key)
        if task and not task.done():
            return
        self.running[key] = asyncio.get_running_loop().create_task(self.fill(device, library_id, item_types))

    async def fill(self, device: EmbyServer, library_id: str, item_types: str) -> dict[str, Any] | None:
        """Download a whole flat library and its badges; keep it, save it and queue its posters."""
        client = device.client
        if client is None:
            return None
        items: list[dict[str, Any]] = []
        start = 0
        while True:
            result = await client.get_items(
                device.user_id, parent_id=library_id, item_type=item_types,
                start_index=start, limit=CHUNK, recursive=True,
            )
            batch = result.get("Items") or []
            total = result.get("TotalRecordCount", 0)
            if not batch:
                if start < total:  # the server stopped answering midway: keep the old list
                    return None
                break
            items.extend({k: v for k, v in item.items() if k not in DROP_FIELDS} for item in batch)
            start += len(batch)
            if start >= total:
                break
        key = self.key(device, library_id, item_types)
        if not items and (self.lists.get(key) or {}).get("items"):
            # A request that timed out looks like an empty library: never replace a list by nothing
            _LOG.info("Library %s: no answer from the server, the kept list stays", library_id)
            return None
        badges: dict[str, dict] = {}
        ids = [i.get("Id") for i in items if i.get("Id")]
        for first in range(0, len(ids), BADGE_CHUNK):
            got = await client.get_oppo_badges(ids[first:first + BADGE_CHUNK]) or []
            badges.update({b["Id"]: b for b in got if isinstance(b, dict) and b.get("Id")})
        entry = {"at": time.time(), "items": items, "badges": badges}
        self.lists[key] = entry
        self._save(key, entry)
        posters = [client.image_source_url(i["Id"]) for i in items if "Primary" in (i.get("ImageTags") or {})]
        queued = images.CACHE.prefetch(posters) if images.CACHE else 0
        _LOG.info("Library %s: %d titles kept, %d posters queued", library_id, len(items), queued)
        return entry

    async def warm_up(self, device: EmbyServer, flat_types: dict[str, str]) -> None:
        """At start and every REFRESH_S: download the flat libraries of the server."""
        await asyncio.sleep(WARM_UP_DELAY_S)
        while True:
            client = device.client
            if client is not None:
                try:
                    for lib in await client.get_libraries(device.user_id):
                        item_types = flat_types.get(lib.get("CollectionType", ""), "")
                        if lib.get("Id") and item_types:
                            await self.fill(device, lib["Id"], item_types)
                except Exception:  # noqa: BLE001 - the next round tries again
                    _LOG.exception("Library download failed")
            await asyncio.sleep(REFRESH_S)

    def _path(self, key: str) -> str:
        return os.path.join(self.folder, f"{key}.json")

    def _save(self, key: str, entry: dict[str, Any]) -> None:
        tmp = self._path(key) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as file:
                json.dump(entry, file, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self._path(key))
        except OSError as ex:
            _LOG.warning("Library list not saved: %s", ex)

    def _load(self) -> None:
        for name in os.listdir(self.folder):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.folder, name), encoding="utf-8") as file:
                    entry = json.load(file)
                if isinstance(entry.get("items"), list):
                    self.lists[name[:-5]] = entry
            except (OSError, ValueError):
                continue
        if self.lists:
            _LOG.info("Library lists loaded: %d", len(self.lists))


CACHE: LibraryCache | None = None


def setup(folder: str) -> None:
    global CACHE
    if CACHE is None:
        CACHE = LibraryCache(folder)
