"""Emby media browser. :copyright: (c) 2026 by Meir Miyara. :license: MPL-2.0"""
from __future__ import annotations

import logging
import textwrap
from typing import TYPE_CHECKING, Any

from ucapi import StatusCodes
from ucapi.api_definitions import Pagination
from ucapi.media_player import (
    BrowseMediaItem,
    BrowseOptions,
    BrowseResults,
    MediaClass,
    MediaContentType,
    SearchOptions,
    SearchResults,
)

if TYPE_CHECKING:
    from uc_intg_emby.device import EmbyServer

_LOG = logging.getLogger(__name__)

EMBY_TYPE_TO_MEDIA_CLASS: dict[str, MediaClass] = {
    "Movie": MediaClass.MOVIE,
    "Series": MediaClass.TV_SHOW,
    "Season": MediaClass.SEASON,
    "Episode": MediaClass.EPISODE,
    "Audio": MediaClass.TRACK,
    "MusicAlbum": MediaClass.ALBUM,
    "MusicArtist": MediaClass.ARTIST,
    "Folder": MediaClass.DIRECTORY,
    "CollectionFolder": MediaClass.DIRECTORY,
    "Playlist": MediaClass.PLAYLIST,
    "BoxSet": MediaClass.DIRECTORY,
    "MusicVideo": MediaClass.VIDEO,
    "Video": MediaClass.VIDEO,
}

PLAYABLE_TYPES = {"Movie", "Episode", "Audio", "MusicVideo", "Video"}
BROWSABLE_TYPES = {"Series", "Season", "MusicAlbum", "MusicArtist", "Folder", "CollectionFolder", "BoxSet", "Playlist"}

# Libraries listed flat (all their items, alphabetically) instead of following the folder tree,
# keyed by Emby CollectionType. A movie library then shows the movies, not the disk folders.
FLAT_LIBRARY_ITEM_TYPES = {"movies": "Movie", "musicvideos": "MusicVideo,Video"}

# Video items get an info line (runtime, resolution, HDR, 3D) and open a "title page" when browsed:
# play, resume, summary, audio tracks and chapters. The Remote has no detail screen of its own.
TITLE_PAGE_TYPES = {"Movie", "Episode", "MusicVideo", "Video"}
TICKS_PER_SECOND = 10_000_000
HDR_LABELS = {"DV": "Dolby Vision"}
OVERVIEW_LINE_WIDTH = 60
OVERVIEW_MAX_LINES = 20
MAX_AUDIO_TRACKS = 3

# Shortcuts shown above the libraries when they are not empty, like the Emby home screen.
CONTINUE_ID = "continue"
FAVORITES_ID = "favorites"


async def browse(device: EmbyServer, options: BrowseOptions) -> BrowseResults | StatusCodes:
    if not device.client or not device.user_id:
        return StatusCodes.SERVICE_UNAVAILABLE

    media_id = options.media_id if hasattr(options, "media_id") else None

    if not media_id or media_id == "root":
        return await _browse_root(device)

    if media_id.startswith("library_"):
        library_id, _, library_type = media_id[8:].partition(":")
        return await _browse_library(device, library_id, options, library_type)

    if media_id.startswith("folder_"):
        folder_id = media_id[7:]
        return await _browse_folder(device, folder_id, options)

    if media_id.startswith("item_"):
        return await _browse_title_page(device, media_id[5:], options)

    if media_id.startswith("chapters_"):
        return await _browse_chapters(device, media_id[9:], options)

    if media_id.startswith("summary_"):
        return await _browse_summary(device, media_id[8:], options)

    if media_id in (CONTINUE_ID, FAVORITES_ID):
        return await _browse_shortcut(device, media_id, options)

    return StatusCodes.NOT_FOUND


async def search(device: EmbyServer, options: SearchOptions) -> SearchResults | StatusCodes:
    if not device.client or not device.user_id:
        return StatusCodes.SERVICE_UNAVAILABLE

    query = options.query if hasattr(options, "query") else ""
    if not query:
        return SearchResults(media=[], pagination=Pagination(page=1, limit=0, count=0))

    items = await device.client.search_items(device.user_id, query, limit=30)
    badges = await _badges_for(device, items)
    results = []
    for item in items:
        media_item = _item_to_browse_item(device, item, badges.get(item.get("Id", "")))
        if media_item:
            results.append(media_item)

    return SearchResults(
        media=results,
        pagination=Pagination(page=1, limit=len(results), count=len(results)),
    )


async def _browse_root(device: EmbyServer) -> BrowseResults:
    libraries = await device.client.get_libraries(device.user_id)

    items = []
    resume = await device.client.get_resume_items(device.user_id, limit=1)
    if resume.get("TotalRecordCount"):
        first = (resume.get("Items") or [{}])[0]
        items.append(BrowseMediaItem(
            title="Reprendre", media_class=MediaClass.DIRECTORY, media_type="continue",
            media_id=CONTINUE_ID, can_browse=True,
            thumbnail=device.build_image_url(first["Id"]) if first.get("Id") else None,
        ))
    favorites = await device.client.get_favorite_items(device.user_id, limit=1)
    if favorites.get("TotalRecordCount"):
        first = (favorites.get("Items") or [{}])[0]
        items.append(BrowseMediaItem(
            title="Favoris", media_class=MediaClass.DIRECTORY, media_type="favorites",
            media_id=FAVORITES_ID, can_browse=True,
            thumbnail=device.build_image_url(first["Id"]) if first.get("Id") else None,
        ))

    for lib in libraries:
        lib_id = lib.get("Id", "")
        lib_name = lib.get("Name", "Unknown")
        lib_type = lib.get("CollectionType", "")

        media_class = MediaClass.DIRECTORY
        if lib_type == "movies":
            media_class = MediaClass.MOVIE
        elif lib_type == "tvshows":
            media_class = MediaClass.TV_SHOW
        elif lib_type == "music":
            media_class = MediaClass.ALBUM

        thumbnail = device.build_image_url(lib_id) if lib_id else ""

        items.append(BrowseMediaItem(
            title=lib_name,
            media_class=media_class,
            media_type="library",
            media_id=f"library_{lib_id}:{lib_type}" if lib_type in FLAT_LIBRARY_ITEM_TYPES else f"library_{lib_id}",
            can_browse=True,
            can_play=False,
            thumbnail=thumbnail or None,
        ))

    return BrowseResults(
        media=BrowseMediaItem(
            title=device.server_name or "Emby",
            media_class=MediaClass.DIRECTORY,
            media_type="root",
            media_id="root",
            can_browse=True,
            can_search=True,
            items=items,
        ),
        pagination=Pagination(page=1, limit=len(items), count=len(items)),
    )


def _paging(options: BrowseOptions) -> tuple[int, int]:
    paging = getattr(options, "paging", None)
    page = getattr(paging, "page", None) or 1
    limit = getattr(paging, "limit", None) or 50
    return page, limit


async def _browse_library(
    device: EmbyServer, library_id: str, options: BrowseOptions, library_type: str = ""
) -> BrowseResults:
    page, limit = _paging(options)
    start_index = (page - 1) * limit
    item_types = FLAT_LIBRARY_ITEM_TYPES.get(library_type, "")
    result = await device.client.get_items(
        device.user_id, parent_id=library_id, item_type=item_types,
        start_index=start_index, limit=limit, recursive=bool(item_types),
    )

    total = result.get("TotalRecordCount", 0)
    emby_items = result.get("Items", [])
    badges = await _badges_for(device, emby_items)

    items = []
    for item in emby_items:
        media_item = _item_to_browse_item(device, item, badges.get(item.get("Id", "")))
        if media_item:
            items.append(media_item)

    return BrowseResults(
        media=BrowseMediaItem(
            title="Library",
            media_class=MediaClass.DIRECTORY,
            media_type="library",
            media_id=f"library_{library_id}",
            can_browse=True,
            can_search=True,
            items=items,
        ),
        pagination=Pagination(page=page, limit=limit, count=total),
    )


async def _browse_folder(device: EmbyServer, folder_id: str, options: BrowseOptions) -> BrowseResults:
    page, limit = _paging(options)
    start_index = (page - 1) * limit
    result = await device.client.get_items(device.user_id, parent_id=folder_id, start_index=start_index, limit=limit)

    total = result.get("TotalRecordCount", 0)
    emby_items = result.get("Items", [])
    badges = await _badges_for(device, emby_items)

    items = []
    for item in emby_items:
        media_item = _item_to_browse_item(device, item, badges.get(item.get("Id", "")))
        if media_item:
            items.append(media_item)

    return BrowseResults(
        media=BrowseMediaItem(
            title="Folder",
            media_class=MediaClass.DIRECTORY,
            media_type="folder",
            media_id=f"folder_{folder_id}",
            can_browse=True,
            items=items,
        ),
        pagination=Pagination(page=page, limit=limit, count=total),
    )


async def _browse_shortcut(device: EmbyServer, shortcut: str, options: BrowseOptions) -> BrowseResults:
    """"Reprendre" (started videos, most recent first) or "Favoris"."""
    page, limit = _paging(options)
    start_index = (page - 1) * limit
    if shortcut == CONTINUE_ID:
        result = await device.client.get_resume_items(device.user_id, start_index, limit)
        title = "Reprendre"
    else:
        result = await device.client.get_favorite_items(device.user_id, start_index, limit)
        title = "Favoris"
    emby_items = result.get("Items", [])
    badges = await _badges_for(device, emby_items)

    items = []
    for item in emby_items:
        media_item = _item_to_browse_item(device, item, badges.get(item.get("Id", "")))
        if not media_item:
            continue
        position = (item.get("UserData") or {}).get("PlaybackPositionTicks") or 0
        if shortcut == CONTINUE_ID and position > 0:
            info = f"Reprendre à {_fmt_duration(position // TICKS_PER_SECOND)}"
            media_item.subtitle = f"{info} · {media_item.subtitle}" if media_item.subtitle else info
        items.append(media_item)

    return BrowseResults(
        media=BrowseMediaItem(title=title, media_class=MediaClass.DIRECTORY, media_type=shortcut,
                              media_id=shortcut, can_browse=True, items=items),
        pagination=Pagination(page=page, limit=limit, count=result.get("TotalRecordCount", len(items))),
    )


async def _browse_title_page(device: EmbyServer, item_id: str, options: BrowseOptions) -> BrowseResults | StatusCodes:
    """A movie's page: play, resume, info, summary, audio tracks and chapters, as list rows."""
    item = await device.client.get_item(device.user_id, item_id)
    if not item:
        return StatusCodes.NOT_FOUND
    badges = await _badges_for(device, [item])
    badge = badges.get(item_id)
    poster = device.build_image_url(item_id) if "Primary" in item.get("ImageTags", {}) else None

    rows: list[BrowseMediaItem] = [
        BrowseMediaItem(title="▶ Lecture", media_class=MediaClass.MOVIE, media_type="play",
                        media_id=f"item_{item_id}", can_play=True, thumbnail=poster),
    ]
    position = (item.get("UserData") or {}).get("PlaybackPositionTicks") or 0
    if position > 0:
        rows.append(BrowseMediaItem(
            title=f"⏯ Reprendre à {_fmt_duration(position // TICKS_PER_SECOND)}",
            media_class=MediaClass.MOVIE, media_type="resume",
            media_id=f"resume_{item_id}_{position}", can_play=True, thumbnail=poster,
        ))

    details = [part for part in (
        _info_line(item, badge),
        ", ".join(item.get("Genres") or [])[:200],
        f"★ {item['CommunityRating']:.1f}" if item.get("CommunityRating") else "",
    ) if part]
    if details:
        rows.append(_text_row(item_id, "info", " · ".join(details)))

    if (item.get("Overview") or "").strip():
        rows.append(BrowseMediaItem(
            title="Résumé", media_class=MediaClass.DIRECTORY, media_type="summary",
            media_id=f"summary_{item_id}", can_browse=True, thumbnail=poster,
        ))

    audio = [s.get("DisplayTitle") for s in item.get("MediaStreams") or [] if s.get("Type") == "Audio" and s.get("DisplayTitle")]
    if audio:
        more = f" (+{len(audio) - MAX_AUDIO_TRACKS})" if len(audio) > MAX_AUDIO_TRACKS else ""
        rows.append(_text_row(item_id, "audio", "Audio : " + ", ".join(audio[:MAX_AUDIO_TRACKS]) + more))

    chapters = await _chapters_for(device, item_id, item)
    if chapters:
        rows.append(BrowseMediaItem(
            title=f"Chapitres ({len(chapters)})", media_class=MediaClass.DIRECTORY, media_type="chapters",
            media_id=f"chapters_{item_id}", can_browse=True, thumbnail=chapters[min(1, len(chapters) - 1)][2] or poster,
        ))

    title = _title_for(item)
    return _paged_container(
        BrowseMediaItem(
            title=title, subtitle=_info_line(item, badge) or None,
            media_class=EMBY_TYPE_TO_MEDIA_CLASS.get(item.get("Type", ""), MediaClass.VIDEO),
            media_type=item.get("Type", "video").lower(), media_id=f"item_{item_id}",
            can_browse=True, can_play=True, thumbnail=poster,
            duration=_duration_seconds(item),
        ),
        rows, options,
    )


async def _browse_summary(device: EmbyServer, item_id: str, options: BrowseOptions) -> BrowseResults | StatusCodes:
    """The summary of a title, wrapped in list rows (the Remote cannot show a paragraph)."""
    item = await device.client.get_item(device.user_id, item_id)
    if not item:
        return StatusCodes.NOT_FOUND
    lines = textwrap.wrap((item.get("Overview") or "").strip(), OVERVIEW_LINE_WIDTH)
    if len(lines) > OVERVIEW_MAX_LINES:
        lines = lines[:OVERVIEW_MAX_LINES]
        lines[-1] = lines[-1].rstrip(" .,;") + "…"
    rows = [_text_row(item_id, f"txt{index}", line) for index, line in enumerate(lines)]
    return _paged_container(
        BrowseMediaItem(title="Résumé", media_class=MediaClass.DIRECTORY, media_type="summary",
                        media_id=f"summary_{item_id}", can_browse=True),
        rows, options,
    )


async def _browse_chapters(device: EmbyServer, item_id: str, options: BrowseOptions) -> BrowseResults | StatusCodes:
    chapters = await _chapters_for(device, item_id)
    if not chapters:
        return StatusCodes.NOT_FOUND
    rows = [
        BrowseMediaItem(
            title=f"{index + 1} · {_fmt_clock(seconds)}" + (f" · {name}" if name else ""),
            media_class=MediaClass.VIDEO, media_type="chapter",
            media_id=f"chapter_{item_id}_{int(seconds * TICKS_PER_SECOND)}",
            can_play=True, thumbnail=image or None,
        )
        for index, (seconds, name, image) in enumerate(chapters)
    ]
    return _paged_container(
        BrowseMediaItem(title="Chapitres", media_class=MediaClass.DIRECTORY, media_type="chapters",
                        media_id=f"chapters_{item_id}", can_browse=True),
        rows, options,
    )


async def _chapters_for(device: EmbyServer, item_id: str, item: dict | None = None) -> list[tuple[float, str, str]]:
    """Chapters as (start seconds, name, image URL). Prefers the OPPO plugin's real Blu-ray marks."""
    chapters = await device.client.get_oppo_chapters(item_id)
    if chapters:
        return [
            (float(c.get("Seconds") or 0), _chapter_name(c.get("Name")),
             device.client.oppo_chapter_image_url(item_id, c.get("Index", i)))
            for i, c in enumerate(chapters)
        ]
    if item is None:
        item = await device.client.get_item(device.user_id, item_id) or {}
    return [
        ((c.get("StartPositionTicks") or 0) / TICKS_PER_SECOND, _chapter_name(c.get("Name")),
         device.client.chapter_image_url(item_id, i, c.get("ImageTag")) if c.get("ImageTag") else "")
        for i, c in enumerate(item.get("Chapters") or [])
    ]


def _chapter_name(name: str | None) -> str:
    """Chapter name, or "" for generic ones ("Chapitre 4", "Chapter 04") that only repeat the number."""
    name = (name or "").strip()
    words = name.split()
    if len(words) == 2 and words[1].isdigit() and words[0].lower() in {"chapitre", "chapter", "kapitel", "capitolo", "capítulo"}:
        return ""
    return name[:200]


def _paged_container(container: BrowseMediaItem, rows: list[BrowseMediaItem], options: BrowseOptions) -> BrowseResults:
    page, limit = _paging(options)
    start = (page - 1) * limit
    container.items = rows[start:start + limit]
    return BrowseResults(media=container, pagination=Pagination(page=page, limit=limit, count=len(rows)))


def _text_row(item_id: str, key: str, text: str) -> BrowseMediaItem:
    """A plain text line of a title page (not playable, not browsable)."""
    return BrowseMediaItem(title=text[:255], media_type="text", media_id=f"text_{item_id}_{key}")


async def _badges_for(device: EmbyServer, emby_items: list[dict]) -> dict[str, dict]:
    """Resolution, HDR and 3D per item id from the OPPO plugin, when it is installed."""
    ids = [i.get("Id") for i in emby_items if i.get("Id") and i.get("Type") in TITLE_PAGE_TYPES]
    if not ids:
        return {}
    badges = await device.client.get_oppo_badges(ids) or []
    return {b.get("Id"): b for b in badges if isinstance(b, dict) and b.get("Id")}


def _duration_seconds(item: dict) -> int | None:
    ticks = item.get("RunTimeTicks") or 0
    return int(ticks // TICKS_PER_SECOND) if ticks else None


def _info_line(item: dict, badge: dict | None) -> str:
    """Runtime, resolution, HDR and 3D, e.g. "2 h 50 · 4K · Dolby Vision"."""
    parts = []
    seconds = _duration_seconds(item)
    if seconds:
        parts.append(_fmt_duration(seconds))
    if badge:
        if badge.get("Resolution"):
            parts.append(badge["Resolution"])
        hdr = badge.get("Hdr") or ""
        if hdr:
            parts.append(HDR_LABELS.get(hdr, hdr))
        if badge.get("Is3D"):
            parts.append("3D")
    return " · ".join(parts)


def _fmt_duration(seconds: int) -> str:
    hours, minutes = divmod(int(seconds) // 60, 60)
    return f"{hours} h {minutes:02d}" if hours else f"{minutes} min"


def _fmt_clock(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _title_for(item: dict) -> str:
    item_name = item.get("Name", "")
    item_type = item.get("Type", "")
    if item_type == "Episode":
        series = item.get("SeriesName", "")
        season_num = item.get("ParentIndexNumber")
        episode_num = item.get("IndexNumber")
        if series and season_num is not None and episode_num is not None:
            return f"{series} - S{season_num:02d}E{episode_num:02d} - {item_name}"
    elif item_type == "Movie":
        year = item.get("ProductionYear")
        if year:
            return f"{item_name} ({year})"
    return item_name


def _item_to_browse_item(device: EmbyServer, item: dict, badge: dict | None = None) -> BrowseMediaItem | None:
    item_id = item.get("Id", "")
    item_name = item.get("Name", "")
    item_type = item.get("Type", "")

    if not item_id or not item_name:
        return None

    media_class = EMBY_TYPE_TO_MEDIA_CLASS.get(item_type, MediaClass.VIDEO)
    is_playable = item_type in PLAYABLE_TYPES
    is_browsable = item_type in BROWSABLE_TYPES

    if is_playable:
        media_id = f"item_{item_id}"
        # Videos also open their title page (summary, resume, chapters).
        is_browsable = item_type in TITLE_PAGE_TYPES
    elif is_browsable:
        media_id = f"folder_{item_id}"
    else:
        media_id = f"folder_{item_id}"
        is_browsable = True

    thumbnail = ""
    if "Primary" in item.get("ImageTags", {}):
        thumbnail = device.build_image_url(item_id)

    subtitle = _info_line(item, badge) if item_type in TITLE_PAGE_TYPES else ""

    return BrowseMediaItem(
        title=_title_for(item),
        subtitle=subtitle or None,
        media_class=media_class,
        media_type=item_type.lower(),
        media_id=media_id,
        can_browse=is_browsable,
        can_play=is_playable,
        thumbnail=thumbnail or None,
        duration=_duration_seconds(item) if item_type in TITLE_PAGE_TYPES else None,
    )
