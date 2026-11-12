"""Image cache of the integration: posters and chapter pictures kept on the Remote, served by a small web server.
:license: MPL-2.0

The Remote's interface keeps pictures in memory for a while only and downloads them again over Wi-Fi when
it needs them later (or after a restart). The integration keeps a small copy of each picture and serves it
from the Remote itself.

The Remote's processor is small and shared with its interface: pictures are downloaded by ONE background
worker, one after the other, gently, and kept as they are. A picture not cached yet is not waited for: the
interface is sent to its original address (as without the cache) and the picture is queued for later.
"""

import contextlib
import hashlib
import http.server
import logging
import os
import queue
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_LOG = logging.getLogger(__name__)

MAX_DOWNLOAD = 3 * 1024 * 1024
USER_AGENT = "uc-intg-emby"
PAUSE_S = 0.3  # between two downloads (the pictures come from the Emby server on the local network)
PORT = 9320  # the Remote refuses 8000-9200 to custom integrations
BACKOFF_S = 120.0  # after "429 Too Many Requests" (imgur, which hosts most logos, limits the rate)


def tls_context() -> ssl.SSLContext:
    """Certificates of certifi: the Python packed for the Remote has no system certificate store (the logos
    are mostly on https://i.imgur.com)."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


TLS = tls_context()


def kind_of(data: bytes) -> str:
    """Content type of a picture kept as downloaded. Pictures are NOT made smaller on the Remote: decoding
    and encoding them (Pillow) held the integration for seconds on its small processor, long enough for
    the Remote to drop the connection; the interface scales them itself, as it did without the cache."""
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class ImageCache:
    def __init__(self, folder: str, max_bytes: int = 100 * 1024 * 1024) -> None:
        self.folder = folder
        self.max_bytes = max_bytes
        os.makedirs(folder, exist_ok=True)
        self.base = ""  # "http://<remote>:9300" set by the driver
        self.queue: queue.Queue[str] = queue.Queue()
        self.pending: set[str] = set()
        self.guard = threading.Lock()
        self.done = 0
        threading.Thread(target=self.worker, daemon=True, name="image-cache").start()

    @staticmethod
    def key(url: str) -> str:
        return hashlib.sha1(url.encode()).hexdigest()[:20]

    def url_for(self, url: str) -> str:
        """Address of a picture through the cache ("" and icon:// addresses unchanged)."""
        if not self.base or not url or not url.startswith(("http://", "https://")):
            return url
        return f"{self.base}/img/{self.key(url)}?u={urllib.parse.quote(url, safe='')}"

    def path(self, key: str) -> str:
        return os.path.join(self.folder, key)

    def cached(self, url: str) -> bool:
        return os.path.exists(self.path(self.key(url)))

    def get(self, url: str) -> tuple[bytes, str] | None:
        """(picture, content type) when cached; else None and the picture is queued (never waits)."""
        if not url.startswith(("http://", "https://")):
            return None
        path = self.path(self.key(url))
        if os.path.exists(path):
            with contextlib.suppress(OSError):
                os.utime(path)  # most recently used: kept when the cache is trimmed
            return self.read(path)
        self.enqueue(url)
        return None

    def enqueue(self, url: str) -> None:
        with self.guard:
            if url in self.pending:
                return
            self.pending.add(url)
        self.queue.put(url)

    def prefetch(self, urls: list[str]) -> int:
        """Queue the pictures not cached yet (the channel logos, at start and at night)."""
        missing = [u for u in dict.fromkeys(urls) if u and not self.cached(u)]
        for url in missing:
            self.enqueue(url)
        return len(missing)

    def worker(self) -> None:
        while True:
            url = self.queue.get()
            try:
                if not self.cached(url):
                    refused = self.fetch(url)
                    time.sleep(BACKOFF_S if refused else PAUSE_S)
                if self.queue.empty():
                    self.trim()
                    if self.done:
                        _LOG.info("Image cache: %d pictures downloaded", self.done)
                        self.done = 0
            except Exception:  # noqa: BLE001 - the worker must keep running
                _LOG.exception("Image cache worker")
            finally:
                with self.guard:
                    self.pending.discard(url)

    def fetch(self, url: str) -> bool:
        """Download, make smaller and keep a picture; True when the site answered "too many requests"."""
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20, context=TLS) as resp:
                data = resp.read(MAX_DOWNLOAD + 1)
        except urllib.error.HTTPError as ex:
            _LOG.debug("Picture %s not downloaded: %s", url, ex)
            return ex.code == 429
        except (OSError, ValueError) as ex:
            _LOG.debug("Picture %s not downloaded: %s", url, ex)
            return False
        if not data or len(data) > MAX_DOWNLOAD:
            return False
        path = self.path(self.key(url))
        tmp = path + ".tmp"
        with open(tmp, "wb") as file:
            file.write(data)
        os.replace(tmp, path)
        self.done += 1
        return False

    @staticmethod
    def read(path: str) -> tuple[bytes, str] | None:
        try:
            with open(path, "rb") as file:
                data = file.read()
        except OSError:
            return None
        return data, kind_of(data)

    def trim(self) -> None:
        """Drop the pictures used least recently above the size limit."""
        files = []
        for name in os.listdir(self.folder):
            path = self.path(name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, path))
        total = sum(f[1] for f in files)
        for _, size, path in sorted(files):
            if total <= self.max_bytes:
                break
            try:
                os.remove(path)
                total -= size
            except OSError:
                pass


def serve(cache: ImageCache, port: int) -> http.server.ThreadingHTTPServer:
    """GET /img/<key>?u=<original address>: the cached picture, else a redirect to the original (queued)."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            try:
                original = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("u", [""])[0]
                found = cache.get(original) if self.path.startswith("/img/") else None
                if found:
                    self.send_response(200)
                    self.send_header("Content-Type", found[1])
                    self.send_header("Content-Length", str(len(found[0])))
                    self.send_header("Cache-Control", "max-age=31536000, immutable")
                    self.end_headers()
                    self.wfile.write(found[0])
                elif original.startswith(("http://", "https://")):
                    self.send_response(302)
                    self.send_header("Location", original)
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self.send_error(404)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the interface gave up a picture (list scrolled away)

    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="image-server").start()
    _LOG.info("Image cache served on port %d", port)
    return server


CACHE: ImageCache | None = None


def lan_address() -> str:
    """Address of the Remote on the local network (the pictures are also shown by the web configurator)."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # no packet is sent: only the route is looked up
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def setup(folder: str, port: int = PORT) -> None:
    """Start the image cache and its web server (pictures then go through the Remote)."""
    global CACHE
    if CACHE is not None:
        return
    cache = ImageCache(folder)
    try:
        serve(cache, port)
    except OSError as ex:  # pictures then come straight from the Emby server
        _LOG.warning("Image cache not served on port %d: %s", port, ex)
        return
    cache.base = f"http://{lan_address()}:{port}"
    CACHE = cache


def wrap(url: str) -> str:
    """Address of a picture through the cache (unchanged when the cache is not running)."""
    return CACHE.url_for(url) if CACHE and url else url
