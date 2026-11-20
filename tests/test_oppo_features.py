"""Emby.Oppo plugin features on the Remote: "movie without menus", disc format, Atmos / DTS:X, library filters."""

from __future__ import annotations

import asyncio

from ucapi import media_player
from ucapi.api_definitions import Pagination  # noqa: F401  (ucapi import check)
from ucapi.media_player import BrowseOptions

from uc_intg_emby import browser, library_cache
from uc_intg_emby.media_player import EmbyMediaPlayer

DV = {"Id": "1", "Resolution": "4K", "Hdr": "DV"}


class FakeClient:
    _oppo_plugin = True

    def __init__(self, fmt=None, item=None, titles=None, badges=None, audio=None):
        self.fmt = fmt or {}
        self.item = item or {}
        self.titles = titles or []
        self.badges = badges or {}
        self.audio = audio or {}
        self.main_movie = []

    async def get_item(self, user_id, item_id):
        return self.item

    async def get_oppo_badges(self, ids):
        return [self.badges[i] for i in ids if i in self.badges]

    async def get_oppo_format(self, item_id):
        return self.fmt

    async def get_oppo_chapters(self, item_id):
        return []

    async def get_oppo_audio_filter(self, kind):
        return self.audio.get(kind, set())

    async def oppo_main_movie(self, item_id):
        self.main_movie.append(item_id)
        return True

    async def get_items(self, user_id, parent_id="", item_type="", start_index=0, limit=50, recursive=False):
        return {"Items": self.titles[start_index:start_index + limit], "TotalRecordCount": len(self.titles)}

    def image_source_url(self, item_id, max_height=300):
        return f"http://emby/{item_id}"


class FakeDevice:
    identifier = "emby1"
    user_id = "user"

    def __init__(self, client):
        self.client = client
        self.played = []

    def build_image_url(self, item_id):
        return f"http://img/{item_id}"

    async def play_items(self, session_id, item_ids, start_ticks=0):
        self.played.append((session_id, item_ids, start_ticks))
        return True


def movie(**extra):
    return {"Id": "1", "Name": "Dune", "Type": "Movie", "RunTimeTicks": 9300 * 10_000_000,
            "ImageTags": {"Primary": "x"}, **extra}


def page(device, media_id, page_no=1, limit=50):
    options = BrowseOptions(media_id=media_id, paging=media_player.Paging(page=page_no, limit=limit))
    return asyncio.run(browser.browse(device, options))


def titles(result):
    return [i.title for i in result.media.items]


def test_movie_without_menus_on_a_uhd_disc_with_dolby_vision_kept() -> None:
    client = FakeClient(fmt={"Kind": "uhd", "Label": "UHD Blu-ray", "MovieDisc": True},
                        item=movie(UserData={"PlaybackPositionTicks": 4320 * 10_000_000},
                                   MediaStreams=[{"Type": "Audio", "Codec": "truehd", "Profile": "TrueHD Atmos",
                                                  "DisplayTitle": "English TRUEHD 7.1"}]),
                        badges={"1": DV})
    rows = titles(page(FakeDevice(client), "item_1"))
    assert rows[:4] == ["▶ Lecture", "▶ Film sans menu", "⏯ Reprendre à 1 h 12", "⏯ Film sans menu à 1 h 12"]
    assert rows[4] == "2 h 35 · 4K · Dolby Vision · UHD Blu-ray · Atmos"


def test_no_movie_without_menus_on_dolby_vision_when_it_would_play_as_a_file() -> None:
    client = FakeClient(fmt={"Kind": "uhd", "Label": "UHD Blu-ray", "MovieDisc": False}, item=movie(), badges={"1": DV})
    assert "▶ Film sans menu" not in titles(page(FakeDevice(client), "item_1"))
    client.badges = {"1": {"Id": "1", "Resolution": "4K", "Hdr": "HDR10"}}
    assert "▶ Film sans menu" in titles(page(FakeDevice(client), "item_1"))


def test_no_movie_without_menus_on_a_dvd_a_file_or_several_discs() -> None:
    for fmt, extra in (({"Kind": "dvd", "Label": "DVD"}, {}), ({"Kind": "file", "Label": "MKV"}, {}),
                       ({"Kind": "bluray", "Label": "Blu-ray", "MovieDisc": True}, {"PartCount": 2})):
        rows = titles(page(FakeDevice(FakeClient(fmt=fmt, item=movie(**extra))), "item_1"))
        assert "▶ Film sans menu" not in rows
    rows = titles(page(FakeDevice(FakeClient(fmt={"Kind": "dvd", "Label": "DVD"}, item=movie())), "item_1"))
    assert rows[1] == "2 h 35 · DVD"  # disc label, never the file type
    rows = titles(page(FakeDevice(FakeClient(fmt={"Kind": "file", "Label": "MKV"}, item=movie(
        MediaStreams=[{"Type": "Audio", "Profile": "DTS:X", "DisplayTitle": "English DTS:X 7.1"}]))), "item_1"))
    assert rows[1] == "2 h 35 · DTS:X"


def test_playing_movie_without_menus_tells_the_plugin_first() -> None:
    client = FakeClient()
    device = FakeDevice(client)
    player = EmbyMediaPlayer.__new__(EmbyMediaPlayer)
    player._device = device
    player._emby_device_id = "oppo"
    assert asyncio.run(player._handle_play_media("s1", {"media_id": "movie_17655"}))
    assert asyncio.run(player._handle_play_media("s1", {"media_id": "movieat_17655_43200000000"}))
    assert client.main_movie == ["17655", "17655"]
    assert device.played == [("s1", ["17655"], 0), ("s1", ["17655"], 43200000000)]


def test_library_starts_with_filters_and_pages_stay_whole(tmp_path) -> None:
    library_cache.setup(str(tmp_path))
    films = [{"Id": str(n), "Name": f"Film {n:02d}", "Type": "Movie", "ImageTags": {"Primary": "x"}} for n in range(25)]
    badges = {"3": {"Id": "3", "Resolution": "4K", "Hdr": "DV"}, "4": {"Id": "4", "Resolution": "4K", "Hdr": "HDR10"}}
    client = FakeClient(titles=films, badges=badges, audio={"atmos": {"3", "7"}, "dtsx": set()})
    device = FakeDevice(client)
    asyncio.run(library_cache.CACHE.fill(device, "lib", "Movie"))
    first = page(device, "library_lib:movies", 1, 10)
    assert titles(first)[:3] == ["Filtres", "Film 00", "Film 01"] and len(first.media.items) == 10
    assert first.pagination.count == 26
    second = page(device, "library_lib:movies", 2, 10)
    assert titles(second)[0] == "Film 09" and len(second.media.items) == 10
    last = page(device, "library_lib:movies", 3, 10)
    assert titles(last)[-1] == "Film 24"
    filters = page(device, "filters_lib:movies")
    assert [(i.title, i.subtitle) for i in filters.media.items] == [
        ("Dolby Atmos", "2 titres"), ("Dolby Vision", "1 titre"), ("4K", "2 titres")]  # DTS:X: none
    assert titles(page(device, "filter_atmos_lib:movies")) == ["Film 03", "Film 07"]
    assert titles(page(device, "filter_4k_lib:movies")) == ["Film 03", "Film 04"]
    library_cache.CACHE = None
