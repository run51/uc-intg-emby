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
TRIES = 3  # attempts per request (the Remote's Wi-Fi sometimes drops a request)
RETRY_PAUSE_S = 2.0
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
        key = self.key(device, library_id, item_types)
        old = self.lists.get(key) or {}
        items: list[dict[str, Any]] = []
        total: int | None = None  # from the first answer: a request that times out reports 0
        while total is None or len(items) < total:
            batch: list[dict[str, Any]] = []
            for attempt in range(TRIES):
                result = await client.get_items(
                    device.user_id, parent_id=library_id, item_type=item_types,
                    start_index=len(items), limit=CHUNK, recursive=True,
                )
                batch = result.get("Items") or []
                if batch:
                    if total is None:
                        total = result.get("TotalRecordCount") or len(batch)
                    break
                if attempt < TRIES - 1:
                    await asyncio.sleep(RETRY_PAUSE_S)
            if not batch:
                if total is None and not old.get("items"):
                    break  # an empty library, or no answer and nothing kept yet
                # The server stopped answering: never keep a partial list, keep the old one
                _LOG.info("Library %s: no answer from the server after %d titles, the kept list stays",
                          library_id, len(items))
                return None
            items.extend({k: v for k, v in item.items() if k not in DROP_FIELDS} for item in batch)
        ids = [i.get("Id") for i in items if i.get("Id")]
        wanted = set(ids)
        # A badges request that is not answered keeps the badges already known
        badges: dict[str, dict] = {i: b for i, b in (old.get("badges") or {}).items() if i in wanted}
        for first in range(0, len(ids), BADGE_CHUNK):
            for attempt in range(TRIES):
                got = await client.get_oppo_badges(ids[first:first + BADGE_CHUNK])
                if got is not None or not getattr(client, "_oppo_plugin", True):
                    badges.update({b["Id"]: b for b in got or [] if isinstance(b, dict) and b.get("Id")})
                    break
                if attempt < TRIES - 1:
                    await asyncio.sleep(RETRY_PAUSE_S)
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
