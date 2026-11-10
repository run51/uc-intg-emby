"""Emby Media Server API client. :copyright: (c) 2026 by Meir Miyara. :license: MPL-2.0"""
import logging
import ssl
from typing import Any
from urllib.parse import quote

import aiohttp
import certifi

from uc_intg_emby.const import EMBY_API_TIMEOUT, EMBY_CONNECTION_TIMEOUT

_LOG = logging.getLogger(__name__)

SEARCH_ITEM_TYPES = "Movie,BoxSet,Series,Episode,MusicVideo,Video,MusicAlbum,MusicArtist,Audio,Playlist"


class EmbyClient:
    """Emby Media Server REST API client."""

    def __init__(self, server_url: str, api_key: str) -> None:
        self._server_url = server_url.rstrip("/")
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None
        self._oppo_plugin = True  # Emby.Oppo plugin endpoints, until a call shows they are missing

    async def connect(self) -> None:
        if self._session and not self._session.closed:
            return
        ssl_context: ssl.SSLContext | bool | None = None
        if self._server_url.startswith("https://"):
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        connector = aiohttp.TCPConnector(ssl=ssl_context) if ssl_context else None
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=EMBY_API_TIMEOUT),
            connector=connector,
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def _url(self, endpoint: str) -> str:
        sep = "&" if "?" in endpoint else "?"
        return f"{self._server_url}{endpoint}{sep}api_key={self._api_key}"

    async def _get(self, endpoint: str) -> dict | list | None:
        if not self._session:
            return None
        try:
            async with self._session.get(self._url(endpoint), timeout=aiohttp.ClientTimeout(total=EMBY_API_TIMEOUT)) as resp:
                if resp.status == 200:
                    return await resp.json()
                _LOG.warning("GET %s returned %d", endpoint, resp.status)
        except Exception as err:
            _LOG.debug("GET %s failed: %s", endpoint, err)
        return None

    async def _post(self, endpoint: str, data: dict | None = None) -> bool:
        if not self._session:
            return False
        try:
            async with self._session.post(self._url(endpoint), json=data, timeout=aiohttp.ClientTimeout(total=EMBY_API_TIMEOUT)) as resp:
                return resp.status in (200, 204)
        except Exception as err:
            _LOG.debug("POST %s failed: %s", endpoint, err)
        return False

    async def test_connection(self) -> bool:
        if not self._session:
            await self.connect()
        try:
            async with self._session.get(
                self._url("/System/Info"),
                timeout=aiohttp.ClientTimeout(total=EMBY_CONNECTION_TIMEOUT),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def get_server_info(self) -> dict | None:
        return await self._get("/System/Info")

    async def get_sessions(self, user_id: str = "") -> list[dict[str, Any]]:
        endpoint = "/Sessions"
        if user_id:
            endpoint += f"?ControllableByUserId={user_id}"
        result = await self._get(endpoint)
        return result if isinstance(result, list) else []

    async def send_playstate_command(self, session_id: str, command: str, seek_ticks: int | None = None) -> bool:
        endpoint = f"/Sessions/{session_id}/Playing/{command}"
        if seek_ticks is not None:
            endpoint += f"?SeekPositionTicks={seek_ticks}"
        return await self._post(endpoint)

    async def send_command(self, session_id: str, command: str, arguments: dict[str, Any] | None = None) -> bool:
        if not arguments:
            return await self._post(f"/Sessions/{session_id}/Command/{command}")
        return await self._post(f"/Sessions/{session_id}/Command", {"Name": command, "Arguments": arguments})

    async def play_items(
        self, session_id: str, item_ids: list[str], start_index: int = 0, user_id: str = "",
        start_ticks: int = 0,
    ) -> bool:
        # Emby documents ItemIds and PlayCommand as required query parameters: send them there,
        # and keep them in the body as well for servers that read the body.
        ids = quote(",".join(item_ids), safe=",")
        body: dict[str, Any] = {"ItemIds": item_ids, "StartIndex": start_index, "PlayCommand": "PlayNow"}
        if user_id:
            body["ControllingUserId"] = user_id
        endpoint = f"/Sessions/{session_id}/Playing?ItemIds={ids}&PlayCommand=PlayNow"
        if start_ticks > 0:
            endpoint += f"&StartPositionTicks={start_ticks}"
            body["StartPositionTicks"] = start_ticks
        return await self._post(endpoint, body)

    async def get_libraries(self, user_id: str) -> list[dict[str, Any]]:
        result = await self._get(f"/Users/{user_id}/Views")
        if isinstance(result, dict):
            return result.get("Items", [])
        return []

    async def get_items(
        self, user_id: str, parent_id: str = "", item_type: str = "",
        start_index: int = 0, limit: int = 50, sort_by: str = "SortName", recursive: bool = False,
    ) -> dict[str, Any]:
        endpoint = f"/Users/{user_id}/Items?StartIndex={start_index}&Limit={limit}&SortBy={sort_by}&SortOrder=Ascending"
        if parent_id:
            endpoint += f"&ParentId={parent_id}"
        if item_type:
            endpoint += f"&IncludeItemTypes={item_type}"
        if recursive:
            endpoint += "&Recursive=true"
        endpoint += "&Fields=PrimaryImageAspectRatio,Overview,ProductionYear&ImageTypeLimit=1"
        result = await self._get(endpoint)
        if isinstance(result, dict):
            return result
        return {"Items": [], "TotalRecordCount": 0}

    async def get_item(self, user_id: str, item_id: str) -> dict[str, Any] | None:
        """One item with its summary, genres, streams, chapters and the user's resume position."""
        result = await self._get(f"/Users/{user_id}/Items/{item_id}")
        return result if isinstance(result, dict) else None

    async def get_oppo_badges(self, item_ids: list[str]) -> list[dict[str, Any]] | None:
        """Resolution, HDR and 3D of movies, from the Emby.Oppo plugin (None if it is not installed)."""
        if not self._oppo_plugin or not item_ids:
            return None
        result = await self._get(f"/Oppo/Remote/Badges?Ids={quote(','.join(item_ids), safe=',')}")
        if not isinstance(result, list):
            self._oppo_plugin_missing()
            return None
        return result

    async def get_oppo_chapters(self, item_id: str) -> list[dict[str, Any]] | None:
        """Real Blu-ray chapter marks from the Emby.Oppo plugin (None if it is not installed)."""
        if not self._oppo_plugin:
            return None
        result = await self._get(f"/Oppo/Remote/Chapters?Id={quote(item_id)}")
        if not isinstance(result, list):
            self._oppo_plugin_missing()
            return None
        return result

    def _oppo_plugin_missing(self) -> None:
        # Stop asking until the next connection: without the plugin every call would fail.
        _LOG.info("Emby.Oppo plugin not available: no format badges or Blu-ray chapters")
        self._oppo_plugin = False

    async def search_items(self, user_id: str, search_term: str, limit: int = 30) -> list[dict[str, Any]]:
        endpoint = f"/Users/{user_id}/Items?SearchTerm={quote(search_term)}&Limit={limit}&Recursive=true"
        # Only things that can be played or opened: no tags, keywords, genres or people.
        endpoint += f"&IncludeItemTypes={SEARCH_ITEM_TYPES}"
        endpoint += "&Fields=PrimaryImageAspectRatio,ProductionYear&ImageTypeLimit=1"
        result = await self._get(endpoint)
        if isinstance(result, dict):
            return result.get("Items", [])
        return []

    async def get_users(self) -> list[dict[str, Any]]:
        result = await self._get("/Users")
        return result if isinstance(result, list) else []

    def image_url(self, item_id: str, max_height: int = 300) -> str:
        return f"{self._server_url}/Items/{item_id}/Images/Primary?maxHeight={max_height}&api_key={self._api_key}"

    def chapter_image_url(self, item_id: str, index: int, tag: str = "", max_height: int = 300) -> str:
        return (f"{self._server_url}/Items/{item_id}/Images/Chapter/{index}"
                f"?maxHeight={max_height}&tag={tag}&api_key={self._api_key}")

    def oppo_chapter_image_url(self, item_id: str, index: int) -> str:
        return f"{self._server_url}/Oppo/Remote/ChapterImage?Id={quote(item_id)}&Index={index}"

    @property
    def server_url(self) -> str:
        return self._server_url
