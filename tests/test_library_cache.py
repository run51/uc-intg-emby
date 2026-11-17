"""Library lists kept on the Remote (library_cache)."""

from __future__ import annotations

import asyncio
import time

from uc_intg_emby import images, library_cache


class FakeClient:
    def __init__(self, count: int) -> None:
        self.titles = [
            {"Id": str(n), "Name": f"Film {n:03d}", "Type": "Movie", "ImageTags": {"Primary": "x"},
             "Overview": "long text " * 50}
            for n in range(count)
        ]
        self.item_requests = 0
        self.badge_requests = 0

    async def get_items(self, user_id, parent_id="", item_type="", start_index=0, limit=50, recursive=False):
        self.item_requests += 1
        return {"Items": self.titles[start_index:start_index + limit], "TotalRecordCount": len(self.titles)}

    async def get_oppo_badges(self, ids):
        self.badge_requests += 1
        return [{"Id": i, "Resolution": "4K"} for i in ids]

    def image_source_url(self, item_id, max_height=300):
        return f"http://emby/Items/{item_id}/Images/Primary?maxHeight={max_height}"


class FakeDevice:
    identifier = "emby1"
    user_id = "user"

    def __init__(self, client) -> None:
        self.client = client


class FakePictures:
    def __init__(self) -> None:
        self.queued: list[str] = []

    def prefetch(self, urls):
        self.queued += urls
        return len(urls)


def test_fill_keeps_whole_library_badges_and_queues_posters(tmp_path, monkeypatch) -> None:
    pictures = FakePictures()
    monkeypatch.setattr(images, "CACHE", pictures)
    cache = library_cache.LibraryCache(str(tmp_path))
    client = FakeClient(450)
    device = FakeDevice(client)
    entry = asyncio.run(cache.fill(device, "3", "Movie"))
    assert len(entry["items"]) == 450 and client.item_requests == 3  # 200 + 200 + 50
    assert client.badge_requests == 5 and entry["badges"]["449"]["Resolution"] == "4K"
    assert "Overview" not in entry["items"][0]
    assert len(pictures.queued) == 450 and pictures.queued[0].endswith("/Items/0/Images/Primary?maxHeight=300")
    # kept on disk: a new cache (integration restart) has it at once
    again = library_cache.LibraryCache(str(tmp_path))
    assert len(again.get(again.key(device, "3", "Movie"))["items"]) == 450


def test_server_not_answering_keeps_the_old_list(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(images, "CACHE", FakePictures())
    cache = library_cache.LibraryCache(str(tmp_path))
    client = FakeClient(10)
    device = FakeDevice(client)
    asyncio.run(cache.fill(device, "3", "Movie"))

    async def no_answer(*_a, **_k):
        return {"Items": [], "TotalRecordCount": 0}

    client.get_items = no_answer  # timed out: the client returns an empty result
    asyncio.run(cache.fill(device, "3", "Movie"))
    assert len(cache.get(cache.key(device, "3", "Movie"))["items"]) == 10


def test_stale_after_half_an_hour(tmp_path) -> None:
    cache = library_cache.LibraryCache(str(tmp_path))
    assert not cache.stale({"at": time.time() - 60})
    assert cache.stale({"at": time.time() - library_cache.STALE_S - 1})


def test_library_page_is_cut_from_the_kept_list(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from uc_intg_emby import browser

    monkeypatch.setattr(images, "CACHE", FakePictures())
    cache = library_cache.LibraryCache(str(tmp_path))
    monkeypatch.setattr(library_cache, "CACHE", cache)
    client = FakeClient(120)
    device = FakeDevice(client)
    device.build_image_url = lambda item_id, max_height=300: f"http://remote/img/{item_id}"
    asyncio.run(cache.fill(device, "3", "Movie"))
    requests = client.item_requests

    options = SimpleNamespace(paging=SimpleNamespace(page=3, limit=20))
    result = asyncio.run(browser._browse_library(device, "3", options, "movies"))
    assert client.item_requests == requests  # nothing asked to the server
    assert result.pagination.count == 120
    assert [i.title for i in result.media.items][:2] == ["Film 040", "Film 041"]
    assert result.media.items[0].subtitle == "4K"  # OPPO badge kept with the list


def test_dropped_request_is_retried_and_a_partial_list_never_kept(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(images, "CACHE", FakePictures())
    monkeypatch.setattr(library_cache, "RETRY_PAUSE_S", 0)
    cache = library_cache.LibraryCache(str(tmp_path))
    client = FakeClient(450)
    device = FakeDevice(client)
    real = client.get_items
    drops = {"left": 1}

    async def flaky(user_id, parent_id="", item_type="", start_index=0, limit=50, recursive=False):
        if start_index == 200 and drops["left"]:
            drops["left"] -= 1
            return {"Items": [], "TotalRecordCount": 0}  # what a timed-out request looks like
        return await real(user_id, parent_id, item_type, start_index, limit, recursive)

    client.get_items = flaky
    entry = asyncio.run(cache.fill(device, "3", "Movie"))
    assert entry is not None and len(entry["items"]) == 450  # the dropped page was asked again

    drops["left"] = 99  # the server stops answering after the first page
    assert asyncio.run(cache.fill(device, "3", "Movie")) is None
    assert len(cache.get(cache.key(device, "3", "Movie"))["items"]) == 450  # old list kept, not 200

    fresh = library_cache.LibraryCache(str(tmp_path / "new"))
    assert asyncio.run(fresh.fill(device, "3", "Movie")) is None  # nothing kept rather than 200 titles
    assert fresh.get(fresh.key(device, "3", "Movie")) is None
