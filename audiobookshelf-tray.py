#!/usr/bin/env python3
import argparse
import json
import locale
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

APP = "audiobookshelf-tray"
VERSION = "1.0.0"
SAVE_INTERVAL = 5.0
SYNC_INTERVAL = 15.0
STALL_SECONDS = 15.0
LOAD_GRACE_SECONDS = 20.0
MAX_RECOVERIES = 4
INSTANCE_PORT = 47654
USER_AGENT = "audiobookshelf-tray/" + VERSION
MIME_TYPES = [
    "audio/flac",
    "audio/mpeg",
    "audio/mp4",
    "audio/x-m4a",
    "audio/x-m4b",
    "audio/aac",
    "audio/ogg",
    "audio/opus",
    "audio/wav",
    "audio/webm",
    "audio/x-ms-wma",
    "audio/x-matroska",
]

log = logging.getLogger(APP)


def config_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    path = Path(os.environ.get("ABS_TRAY_HOME") or Path(base) / APP)
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_logging():
    handlers = [logging.FileHandler(config_dir() / "audiobookshelf-tray.log", encoding="utf-8")]
    if sys.stderr is not None and sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


DEFAULT_STATE = {
    "server": "",
    "username": "",
    "password": "",
    "token": "",
    "device_id": "",
    "theme": "auto",
    "notifications": True,
    "pin_icon": True,
    "item_id": "",
    "title": "",
    "author": "",
    "position": 0.0,
    "duration": 0.0,
}


class Store:
    def __init__(self, path=None):
        self.path = Path(path) if path else config_dir() / "state.json"
        self.lock = threading.Lock()
        self.data = dict(DEFAULT_STATE)
        self.load()

    def load(self):
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update({k: loaded[k] for k in DEFAULT_STATE if k in loaded})
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("could not read state, starting fresh")
        try:
            self.data["position"] = float(self.data["position"] or 0.0)
            self.data["duration"] = float(self.data["duration"] or 0.0)
        except (TypeError, ValueError):
            self.data["position"] = 0.0
            self.data["duration"] = 0.0

    def save(self):
        with self.lock:
            tmp = self.path.with_suffix(".tmp")
            try:
                tmp.write_text(json.dumps(self.data), encoding="utf-8")
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
                os.replace(tmp, self.path)
            except Exception:
                log.exception("could not save state")


def normalize_server(text):
    text = (text or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return text.rstrip("/")


class AuthError(Exception):
    pass


class ApiError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


class Api:
    def __init__(self, store):
        self.store = store
        self.login_lock = threading.Lock()

    @property
    def base(self):
        return self.store.data["server"]

    def raw(self, method, path, body=None, token=None, timeout=20, binary=False, extra=None):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        if extra:
            headers.update(extra)
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            raise ApiError(f"HTTP {exc.code}", exc.code)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ApiError(str(getattr(exc, "reason", exc)))
        if binary:
            return payload
        if not payload:
            return {}
        try:
            return json.loads(payload.decode("utf-8"))
        except ValueError:
            return {}

    def login(self):
        d = self.store.data
        if not d["server"] or not d["username"] or not d["password"]:
            raise AuthError("not signed in")
        with self.login_lock:
            if d["token"]:
                return
            try:
                res = self.raw(
                    "POST",
                    "/login",
                    {"username": d["username"], "password": d["password"]},
                    extra={"x-return-tokens": "true"},
                )
            except ApiError as exc:
                if exc.status in (400, 401, 403):
                    raise AuthError("wrong username or password")
                raise
            user = res.get("user") or {}
            token = user.get("accessToken") or user.get("token")
            if not token:
                raise AuthError("server did not return a token")
            d["token"] = token
            self.store.save()

    def call(self, method, path, body=None, timeout=20, binary=False):
        for attempt in (0, 1):
            if not self.store.data["token"]:
                self.login()
            token = self.store.data["token"]
            try:
                return self.raw(method, path, body, token, timeout, binary)
            except ApiError as exc:
                if exc.status == 401 and attempt == 0:
                    with self.login_lock:
                        if self.store.data["token"] == token:
                            self.store.data["token"] = ""
                    continue
                raise

    def libraries(self):
        res = self.call("GET", "/api/libraries")
        return [lib for lib in res.get("libraries", []) if lib.get("mediaType") == "book"]

    def search(self, query):
        found = []
        seen = set()
        q = urllib.parse.quote(query)
        for lib in self.libraries():
            res = self.call("GET", f"/api/libraries/{lib['id']}/search?q={q}&limit=10")
            for hit in res.get("book") or []:
                item = hit.get("libraryItem") or {}
                meta = (item.get("media") or {}).get("metadata") or {}
                item_id = item.get("id")
                if item_id and item_id not in seen:
                    seen.add(item_id)
                    found.append(
                        {
                            "id": item_id,
                            "title": meta.get("title") or "",
                            "author": meta.get("authorName") or "",
                        }
                    )
        return found

    def last_in_progress(self):
        res = self.call("GET", "/api/me")
        best = None
        for prog in res.get("mediaProgress") or []:
            if prog.get("isFinished") or prog.get("episodeId") or not prog.get("libraryItemId"):
                continue
            if not (prog.get("currentTime") or prog.get("progress")):
                continue
            if best is None or (prog.get("lastUpdate") or 0) > (best.get("lastUpdate") or 0):
                best = prog
        return best["libraryItemId"] if best else ""

    def play(self, item_id):
        body = {
            "deviceInfo": {
                "clientName": APP,
                "clientVersion": VERSION,
                "deviceId": self.store.data.get("device_id") or APP,
            },
            "forceDirectPlay": True,
            "forceTranscode": False,
            "supportedMimeTypes": MIME_TYPES,
            "mediaPlayer": "mpv",
        }
        return self.call("POST", f"/api/items/{item_id}/play", body, timeout=45)

    def sync(self, session_id, current, listened, duration):
        body = {"currentTime": current, "timeListened": listened, "duration": duration}
        self.call("POST", f"/api/session/{session_id}/sync", body)

    def close(self, session_id, current, listened, duration):
        body = {"currentTime": current, "timeListened": listened, "duration": duration}
        self.call("POST", f"/api/session/{session_id}/close", body, timeout=10)

    def finish(self, item_id):
        self.call("PATCH", f"/api/me/progress/{item_id}", {"isFinished": True})

    def cover(self, item_id):
        return self.call(
            "GET", f"/api/items/{item_id}/cover?width=400&format=jpeg", binary=True, timeout=15
        )


def best_match(found, query):
    q = query.strip().lower()
    for test in (
        lambda t: t == q,
        lambda t: t.startswith(q),
        lambda t: q in t,
    ):
        for entry in found:
            if test(entry["title"].lower()):
                return entry
    return found[0]


def subprocess_flags():
    return {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def prepare_libmpv():
    import ctypes.util

    if sys.platform == "win32":
        here = str(Path(__file__).resolve().parent)
        dirs = [here] + [d for d in os.environ.get("ABS_TRAY_MPV_DIR", "").split(os.pathsep) if d]
        for d in dirs:
            if os.path.isdir(d):
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                if hasattr(os, "add_dll_directory"):
                    try:
                        os.add_dll_directory(d)
                    except OSError:
                        pass
    elif sys.platform == "darwin":
        original = ctypes.util.find_library
        if not original("mpv"):
            for candidate in (
                "/opt/homebrew/lib/libmpv.dylib",
                "/usr/local/lib/libmpv.dylib",
            ):
                if os.path.exists(candidate):
                    ctypes.util.find_library = (
                        lambda name, _c=candidate, _o=original: _c
                        if name == "mpv"
                        else _o(name)
                    )
                    break


def make_player(**extra):
    prepare_libmpv()
    try:
        locale.setlocale(locale.LC_NUMERIC, "C")
    except locale.Error:
        pass
    import mpv

    opts = dict(
        vid="no",
        ytdl=False,
        idle="yes",
        input_default_bindings=False,
        input_vo_keyboard=False,
        osc=False,
        audio_display="no",
        network_timeout=20,
        stream_lavf_o="reconnect=1,reconnect_streamed=1,reconnect_delay_max=4",
        user_agent=USER_AGENT,
        loglevel="warn",
        log_handler=lambda level, prefix, text: log.warning("mpv %s %s: %s", level, prefix, text.strip()),
    )
    opts.update(extra)
    return mpv.MPV(**opts)


class Controller:
    def __init__(
        self,
        store,
        api,
        player,
        on_change=None,
        on_notify=None,
        ask_search=None,
        ask_setup=None,
        on_track=None,
    ):
        self.store = store
        self.api = api
        self.player = player
        self.on_change = on_change or (lambda: None)
        self.on_notify = on_notify or (lambda msg: None)
        self.ask_search = ask_search or (lambda: None)
        self.ask_setup = ask_setup or (lambda: False)
        self.on_track = on_track or (lambda item_id, title, author: None)
        self.lock = threading.RLock()
        self.status = "idle"
        self.gen = 0
        self.session = None
        self.session_expired = False
        self.tracks = []
        self.chapters = []
        self.track_idx = 0
        self.duration = 0.0
        self.title = ""
        self.author = ""
        self.last_pos = 0.0
        self.track_dur = None
        self.listened = 0.0
        self.tick_at = time.time()
        self.last_sync = time.time()
        self.syncing = False
        self.progress_at = time.time()
        self.progress_pos = None
        self.started_at = 0.0
        self.seen_active = False
        self.recoveries = 0
        self.resume_from = 0.0
        self.last_save = 0.0
        self.last_announced = None
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self._watchdog, daemon=True)
        self.thread.start()

    @property
    def data(self):
        return self.store.data

    def now_title(self):
        return self.title or self.data.get("title") or ""

    def percent(self):
        with self.lock:
            if self.session is not None:
                pos, dur = self.last_pos, self.duration
            else:
                pos, dur = self.data.get("position") or 0.0, self.data.get("duration") or 0.0
        if not dur or dur <= 0:
            return None
        return max(0, min(100, int(pos / dur * 100)))

    def _changed(self):
        try:
            self.on_change()
        except Exception:
            log.exception("on_change failed")

    def ensure_ready(self):
        d = self.data
        asked = False
        if not d["server"] or not d["username"] or not d["password"]:
            asked = True
            if not self.ask_setup():
                return False
        if d["token"]:
            return True
        try:
            self.api.login()
        except AuthError as exc:
            d["password"] = ""
            self.store.save()
            self.on_notify(f"Sign in failed: {exc}")
            return False
        except ApiError as exc:
            if asked:
                d["server"] = ""
                self.store.save()
            self.on_notify(f"Cannot reach the server: {exc}")
            return False
        return True

    def toggle(self):
        with self.lock:
            status = self.status
            if status == "playing":
                self.pause()
                return
            if status == "paused":
                self.resume()
                return
            if status == "loading":
                return
        threading.Thread(target=self._begin, daemon=True).start()

    def _begin(self):
        if not self.ensure_ready():
            return
        d = self.data
        item_id = ""
        try:
            item_id = self.api.last_in_progress()
        except (AuthError, ApiError) as exc:
            log.info("could not read progress: %s", exc)
        if not item_id:
            item_id = d["item_id"]
        if not item_id:
            self.ask_search()
            return
        if item_id == d["item_id"]:
            self.start_item(item_id, None, d["title"], d["author"])
        else:
            self.start_item(item_id)

    def pause(self):
        with self.lock:
            if self.status != "playing":
                return
            try:
                self.player.pause = True
            except Exception:
                log.exception("pause failed")
            self.status = "paused"
            self._remember_position()
            self.store.save()
            self._changed()
        self._sync_async(force=True)

    def resume(self):
        with self.lock:
            if self.session is None or self.session_expired:
                self.start_item(
                    self.data["item_id"], self.last_pos if self.last_pos > 0 else None
                )
                return
            try:
                self.player.pause = False
            except Exception:
                self.start_item(
                    self.data["item_id"], self.last_pos if self.last_pos > 0 else None
                )
                return
            now = time.time()
            self.status = "playing"
            self.progress_at = now
            self.progress_pos = None
            self.tick_at = now
            self._changed()

    def search_and_play(self, query):
        query = (query or "").strip()
        if not query:
            return
        if not self.ensure_ready():
            return
        with self.lock:
            self.gen += 1
            gen = self.gen
            prev = self.status
            prev_title = self.title
            self.status = "loading"
            self.title = f"Searching: {query}"
            self._changed()
        error = None
        found = []
        try:
            found = self.api.search(query)
        except (AuthError, ApiError) as exc:
            error = exc
        with self.lock:
            if gen != self.gen:
                return
            if error is not None or not found:
                self.status = prev if prev != "loading" else "idle"
                self.title = prev_title
                self._changed()
                if error is not None:
                    if isinstance(error, AuthError):
                        self.data["password"] = ""
                        self.data["token"] = ""
                        self.store.save()
                    self.on_notify(f"Search failed: {error}")
                else:
                    self.on_notify(f'No audiobook found for "{query}"')
                return
        best = best_match(found, query)
        self.start_item(best["id"], None, best["title"], best["author"])

    def start_item(self, item_id, position=None, title="", author=""):
        with self.lock:
            self.gen += 1
            gen = self.gen
            self.status = "loading"
            if title:
                self.title = title
                self.author = author
            elif not self.title:
                self.title = self.data.get("title") or ""
            self._changed()
        threading.Thread(
            target=self._open, args=(gen, item_id, position), daemon=True
        ).start()

    def _open(self, gen, item_id, position):
        session = None
        error = None
        for attempt in range(3):
            if gen != self.gen or self.stop_flag.is_set():
                return
            try:
                session = self.api.play(item_id)
                break
            except AuthError as exc:
                error = exc
                break
            except ApiError as exc:
                error = exc
                log.warning("play failed (%s): %s", attempt + 1, exc)
                if exc.status == 404:
                    break
                time.sleep(1.5 * (attempt + 1))
        with self.lock:
            if gen != self.gen or self.stop_flag.is_set():
                if session is not None:
                    self._close_async(session.get("id"), 0.0, 0.0, 0.0)
                return
            if session is None:
                self._fail(error or ApiError("could not open the book"))
                return
            try:
                self._adopt(session, item_id, position)
            except Exception as exc:
                log.exception("could not start playback")
                self._fail(exc)

    def _adopt(self, session, item_id, position):
        old = self.session
        if old is not None:
            self._close_async(old.get("id"), self.last_pos, self.listened, self.duration)
        tracks = []
        raw_tracks = sorted(session.get("audioTracks") or [], key=lambda t: t.get("index") or 0)
        for t in raw_tracks:
            url = t.get("contentUrl") or ""
            if not url:
                continue
            if not url.startswith("http"):
                url = self.data["server"] + url
            tracks.append(
                {
                    "start": float(t.get("startOffset") or 0),
                    "dur": float(t.get("duration") or 0),
                    "url": url,
                }
            )
        if not tracks:
            self._close_async(session.get("id"), 0.0, 0.0, 0.0)
            self._fail(ApiError("this book has no audio files"))
            return
        chapters = []
        for c in session.get("chapters") or []:
            try:
                chapters.append(
                    {
                        "start": float(c["start"]),
                        "end": float(c["end"]),
                        "title": str(c.get("title") or ""),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue
        chapters.sort(key=lambda c: c["start"])
        duration = float(session.get("duration") or 0) or (tracks[-1]["start"] + tracks[-1]["dur"])
        new_id = session.get("libraryItemId") or item_id
        server_pos = float(session.get("currentTime") or 0)
        pos = position
        if pos is None:
            pos = server_pos
            if pos < 1 and new_id == self.data.get("item_id") and self.data["position"] > 1:
                pos = float(self.data["position"])
        pos = max(0.0, pos)
        if duration and pos >= duration - 5:
            pos = 0.0
        self.session = session
        self.session_expired = False
        self.tracks = tracks
        self.chapters = chapters
        self.duration = duration
        self.title = session.get("displayTitle") or self.title
        self.author = session.get("displayAuthor") or self.author
        self.data["item_id"] = new_id
        self.data["title"] = self.title
        self.data["author"] = self.author
        self.data["duration"] = duration
        self.data["position"] = pos
        self.store.save()
        self.listened = 0.0
        self._play_pos(pos)
        if new_id != self.last_announced:
            self.last_announced = new_id
            try:
                self.on_track(new_id, self.title, self.author)
            except Exception:
                log.exception("track announcement failed")

    def _track_for(self, pos):
        for i in range(len(self.tracks) - 1, -1, -1):
            if self.tracks[i]["start"] <= pos:
                return i
        return 0

    def _load_track(self, idx, offset):
        track = self.tracks[idx]
        self.player.pause = False
        self.player["start"] = f"{offset:.2f}" if offset > 1 else "none"
        token = self.data.get("token") or ""
        try:
            self.player["http-header-fields"] = [f"Authorization: Bearer {token}"] if token else []
        except Exception:
            log.warning("could not set http headers")
        self.player.play(track["url"])
        now = time.time()
        self.started_at = now
        self.seen_active = False
        self.progress_at = now
        self.progress_pos = None
        self.tick_at = now
        self.track_dur = None

    def _play_pos(self, pos):
        idx = self._track_for(pos)
        self.track_idx = idx
        self._load_track(idx, pos - self.tracks[idx]["start"])
        self.status = "playing"
        self.last_pos = pos
        self.resume_from = pos
        self.last_sync = time.time()
        self._changed()

    def _fail(self, error):
        if isinstance(error, AuthError):
            self.data["password"] = ""
            self.data["token"] = ""
            self.store.save()
        if self.session is not None and self.tracks:
            try:
                self.player.pause = True
            except Exception:
                pass
            self.session_expired = True
            self.status = "paused"
            self.title = self.data.get("title") or self.title
        else:
            self.status = "idle"
            self.title = ""
        self._changed()
        self.on_notify(f"Playback failed: {error}")

    def _recover(self, reason):
        if self.status != "playing":
            return
        self.recoveries += 1
        log.warning("recovering (%s), attempt %s", reason, self.recoveries)
        if self.recoveries > MAX_RECOVERIES:
            self.recoveries = 0
            self._fail(ApiError(reason))
            return
        self.start_item(self.data["item_id"], max(0.0, self.last_pos - 1.0))

    def _finished(self):
        item_id = self.data["item_id"]
        title = self.title
        sid = self.session.get("id") if self.session else None
        self._close_async(sid, self.duration, self.listened, self.duration)
        threading.Thread(target=self._mark_finished, args=(item_id,), daemon=True).start()
        self.session = None
        self.session_expired = False
        self.tracks = []
        self.chapters = []
        self.status = "idle"
        self.title = ""
        self.author = ""
        self.last_pos = 0.0
        self.listened = 0.0
        self.data["item_id"] = ""
        self.data["title"] = ""
        self.data["author"] = ""
        self.data["position"] = 0.0
        self.store.save()
        self._changed()
        self.on_notify(f"Finished: {title}")

    def _mark_finished(self, item_id):
        try:
            self.api.finish(item_id)
        except Exception as exc:
            log.info("could not mark finished: %s", exc)

    def _close_async(self, sid, pos, listened, duration):
        if not sid:
            return
        threading.Thread(
            target=self._close_session, args=(sid, pos, listened, duration), daemon=True
        ).start()

    def _close_session(self, sid, pos, listened, duration):
        try:
            self.api.close(sid, pos, listened, duration)
        except Exception as exc:
            log.info("session close skipped: %s", exc)

    def _sync_async(self, force=False):
        with self.lock:
            if self.session is None or (self.syncing and not force):
                return
            sid = self.session.get("id")
            pos = self.last_pos
            listened = self.listened
            self.listened = 0.0
            duration = self.duration
            self.syncing = True
        threading.Thread(
            target=self._sync_worker, args=(sid, pos, listened, duration), daemon=True
        ).start()

    def _sync_worker(self, sid, pos, listened, duration):
        try:
            self.api.sync(sid, pos, listened, duration)
        except AuthError as exc:
            log.warning("sync auth failed: %s", exc)
            with self.lock:
                self.listened += listened
        except ApiError as exc:
            if exc.status == 404:
                with self.lock:
                    current = self.session.get("id") if self.session else None
                    if current == sid:
                        self.session_expired = True
                        if self.status == "playing":
                            self._recover("session expired")
            else:
                log.info("sync failed: %s", exc)
                with self.lock:
                    self.listened += listened
        finally:
            self.syncing = False

    def chapter_index(self):
        pos = self.last_pos
        for i, c in enumerate(self.chapters):
            if c["start"] <= pos < c["end"]:
                return i
        return -1

    def seek_global(self, target):
        if self.duration:
            target = min(target, max(0.0, self.duration - 1.0))
        target = max(0.0, target)
        idx = self._track_for(target)
        offset = target - self.tracks[idx]["start"]
        if idx == self.track_idx and self.status in ("playing", "paused") and not self.session_expired:
            try:
                self.player.pause = False
                self.player.seek(offset, reference="absolute")
            except Exception:
                self.track_idx = idx
                self._load_track(idx, offset)
        else:
            self.track_idx = idx
            self._load_track(idx, offset)
        now = time.time()
        self.last_pos = target
        self.resume_from = target
        self.progress_pos = None
        self.progress_at = now
        self.tick_at = now
        self.status = "playing"
        self._changed()

    def next(self):
        with self.lock:
            if self.session is None or self.status == "loading" or self.session_expired:
                return
            if not self.chapters:
                self.on_notify("This book has no chapters")
                return
            i = self.chapter_index()
            if i == -1:
                later = [c for c in self.chapters if c["start"] > self.last_pos]
                if not later:
                    return
                target = later[0]["start"]
            elif i + 1 < len(self.chapters):
                target = self.chapters[i + 1]["start"]
            else:
                return
            self.seek_global(target)
        self._sync_async(force=True)

    def previous(self):
        with self.lock:
            if self.session is None or self.status == "loading" or self.session_expired:
                return
            if not self.chapters:
                self.on_notify("This book has no chapters")
                return
            i = self.chapter_index()
            if i == -1:
                earlier = [c for c in self.chapters if c["start"] < self.last_pos]
                if not earlier:
                    return
                target = earlier[-1]["start"]
            elif i == 0:
                target = self.chapters[0]["start"]
            else:
                target = self.chapters[i - 1]["start"]
            self.seek_global(target)
        self._sync_async(force=True)

    def current_chapter_title(self):
        with self.lock:
            i = self.chapter_index()
            return self.chapters[i]["title"] if i >= 0 else ""

    def sign_out(self):
        with self.lock:
            self.gen += 1
            if self.session is not None:
                self._close_async(
                    self.session.get("id"), self.last_pos, self.listened, self.duration
                )
            try:
                self.player.command("stop")
            except Exception:
                pass
            self.session = None
            self.session_expired = False
            self.tracks = []
            self.chapters = []
            self.status = "idle"
            self.title = ""
            self.author = ""
            self.last_pos = 0.0
            self.listened = 0.0
            self.last_announced = None
            for key in ("server", "username", "password", "token", "item_id", "title", "author"):
                self.data[key] = ""
            self.data["position"] = 0.0
            self.store.save()
            self._changed()

    def _remember_position(self):
        if self.session is not None:
            self.data["position"] = float(self.last_pos or 0.0)

    def _poll(self):
        player = self.player
        try:
            idle = bool(player.idle_active)
            pos = player.time_pos
            dur = player.duration
            core_idle = bool(player.core_idle)
            paused = bool(player.pause)
        except Exception:
            return None
        return idle, pos, dur, core_idle, paused

    def _watchdog(self):
        while not self.stop_flag.wait(0.5):
            try:
                self._tick()
            except Exception:
                log.exception("watchdog error")

    def _tick(self):
        sync = False
        with self.lock:
            status = self.status
            if status in ("idle", "loading") or not self.tracks:
                return
            snap = self._poll()
            if snap is None:
                return
            idle, pos, dur, core_idle, paused = snap
            now = time.time()
            track = self.tracks[self.track_idx]
            if pos is not None:
                self.last_pos = track["start"] + float(pos)
            if dur:
                self.track_dur = float(dur)
            elapsed = now - self.tick_at
            self.tick_at = now
            if status == "paused":
                if now - self.last_save > SAVE_INTERVAL:
                    self._remember_position()
                    self.store.save()
                    self.last_save = now
                return
            if not idle:
                self.seen_active = True
            if idle:
                if self.seen_active:
                    self.seen_active = False
                    local = self.last_pos - track["start"]
                    length = self.track_dur or track["dur"]
                    ended = (length and local >= length - 3.0) or (not length and local > 1)
                    if ended:
                        self.recoveries = 0
                        if self.track_idx + 1 < len(self.tracks):
                            self.track_idx += 1
                            nxt = self.tracks[self.track_idx]
                            self.last_pos = nxt["start"]
                            self._load_track(self.track_idx, 0.0)
                        else:
                            self._finished()
                    else:
                        self._recover("stream ended early")
                elif now - self.started_at > LOAD_GRACE_SECONDS:
                    self._recover("load timeout")
                return
            if paused:
                return
            if pos is not None and (
                self.progress_pos is None or abs(pos - self.progress_pos) > 0.2
            ):
                self.progress_pos = pos
                self.progress_at = now
                self.listened += min(elapsed, 2.0)
                if self.last_pos - self.resume_from > 10:
                    self.recoveries = 0
            elif now - self.progress_at > STALL_SECONDS:
                self.progress_at = now
                self._recover("stalled")
                return
            if now - self.last_save > SAVE_INTERVAL:
                self._remember_position()
                self.store.save()
                self.last_save = now
            if now - self.last_sync >= SYNC_INTERVAL:
                self.last_sync = now
                sync = True
        if sync:
            self._sync_async()

    def shutdown(self):
        self.stop_flag.set()
        sid = None
        with self.lock:
            self._remember_position()
            self.store.save()
            if self.session is not None:
                sid = self.session.get("id")
            pos, listened, duration = self.last_pos, self.listened, self.duration
        if sid:
            t = threading.Thread(
                target=self._close_session, args=(sid, pos, listened, duration), daemon=True
            )
            t.start()
            t.join(4)
        try:
            self.player.terminate()
        except Exception:
            pass


def hide_dock_icon(activate=False):
    if sys.platform != "darwin":
        return
    try:
        import AppKit

        app = AppKit.NSApplication.sharedApplication()
        app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
        if activate:
            app.activateIgnoringOtherApps_(True)
    except Exception:
        log.info("could not hide dock icon", exc_info=True)


def form_main(title, fields, button):
    import tkinter as tk

    dark = False
    try:
        dark = detect_system_dark()
    except Exception:
        dark = False
    bg, fg, field, btn, sel = "#202020", "#f0f0f0", "#2d2d2d", "#3a3a3a", "#0a64ad"
    result = {"values": None}
    root = tk.Tk()
    hide_dock_icon(True)
    root.title(title)
    root.resizable(False, False)
    if dark:
        root.configure(bg=bg)
    entries = []
    for i, f in enumerate(fields):
        label = tk.Label(root, text=f["label"], anchor="w")
        var = tk.StringVar(value=f.get("value", ""))
        secret = bool(f.get("secret"))
        entry = tk.Entry(root, textvariable=var, width=44, show="*" if secret else "")
        if dark:
            label.configure(bg=bg, fg=fg)
            entry.configure(
                bg=field,
                fg=fg,
                insertbackground=fg,
                selectbackground=sel,
                selectforeground="#ffffff",
                relief="flat",
                highlightthickness=1,
                highlightbackground="#555555",
                highlightcolor=sel,
            )
        label.pack(padx=14, pady=(14 if i == 0 else 6, 2), anchor="w")
        entry.pack(padx=14, pady=2, ipady=2)
        entries.append((entry, var, secret))

    def accept(_event=None):
        result["values"] = [v.get() if s else v.get().strip() for _, v, s in entries]
        root.destroy()

    def cancel(_event=None):
        root.destroy()

    def make_enter(index):
        def handler(_event=None):
            if index + 1 < len(entries):
                entries[index + 1][0].focus_set()
            else:
                accept()
            return "break"

        return handler

    for i, (entry, _, _) in enumerate(entries):
        entry.bind("<Return>", make_enter(i))
    row = tk.Frame(root)
    if dark:
        row.configure(bg=bg)
    row.pack(padx=14, pady=(10, 14), anchor="e")
    cancel_btn = tk.Button(row, text="Cancel", width=8, command=cancel)
    ok_btn = tk.Button(row, text=button, width=8, command=accept)
    if dark:
        for b in (cancel_btn, ok_btn):
            b.configure(
                bg=btn,
                fg=fg,
                activebackground="#4a4a4a",
                activeforeground=fg,
                relief="flat",
                borderwidth=0,
                highlightthickness=0,
            )
    cancel_btn.pack(side="left", padx=4)
    ok_btn.pack(side="left")
    root.bind("<Escape>", cancel)
    root.update_idletasks()
    if dark and sys.platform == "win32":
        try:
            import ctypes

            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            value = ctypes.c_int(1)
            for attr in (20, 19):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)
                )
        except Exception:
            pass
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 3
    root.geometry(f"+{x}+{y}")
    root.attributes("-topmost", True)
    first = next((e for e, v, _ in entries if not v.get()), entries[0][0])
    root.after(50, lambda: (root.focus_force(), first.focus_force()))
    root.mainloop()
    if result["values"] is not None:
        sys.stdout.write(json.dumps(result["values"]))
    sys.stdout.flush()


def read_cover(path):
    if not path:
        return None
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def popup_main_mac(title, sub, cover, seconds):
    import AppKit
    import Foundation
    from PyObjCTools import AppHelper

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    data = read_cover(cover)
    width, height = 372, 96
    frame = AppKit.NSScreen.mainScreen().visibleFrame()
    x = frame.origin.x + frame.size.width - width - 20
    y = frame.origin.y + frame.size.height - height - 12
    win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        Foundation.NSMakeRect(x, y, width, height),
        AppKit.NSWindowStyleMaskBorderless,
        AppKit.NSBackingStoreBuffered,
        False,
    )
    win.setLevel_(AppKit.NSStatusWindowLevel)
    win.setOpaque_(False)
    win.setBackgroundColor_(AppKit.NSColor.clearColor())
    win.setHasShadow_(True)
    win.setIgnoresMouseEvents_(True)
    win.setReleasedWhenClosed_(False)
    content = win.contentView()
    content.setWantsLayer_(True)
    layer = content.layer()
    layer.setBackgroundColor_(
        AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.13, 0.96).CGColor()
    )
    layer.setCornerRadius_(12)
    text_x = 16
    if data:
        image = AppKit.NSImage.alloc().initWithData_(
            Foundation.NSData.dataWithBytes_length_(data, len(data))
        )
        if image is not None:
            view = AppKit.NSImageView.alloc().initWithFrame_(
                Foundation.NSMakeRect(12, 12, 72, 72)
            )
            view.setImage_(image)
            view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
            view.setWantsLayer_(True)
            view.layer().setCornerRadius_(6)
            view.layer().setMasksToBounds_(True)
            content.addSubview_(view)
            text_x = 96
    caption = AppKit.NSTextField.labelWithString_("Now playing")
    caption.setFont_(AppKit.NSFont.systemFontOfSize_(11))
    caption.setTextColor_(AppKit.NSColor.lightGrayColor())
    caption.setFrame_(Foundation.NSMakeRect(text_x, 68, width - text_x - 12, 16))
    content.addSubview_(caption)
    shown = title if len(title) <= 60 else title[:57] + "..."
    label = AppKit.NSTextField.wrappingLabelWithString_(shown)
    label.setFont_(AppKit.NSFont.boldSystemFontOfSize_(13))
    label.setTextColor_(AppKit.NSColor.whiteColor())
    label.setFrame_(Foundation.NSMakeRect(text_x, 30, width - text_x - 12, 36))
    content.addSubview_(label)
    if sub:
        author = AppKit.NSTextField.labelWithString_(sub if len(sub) <= 48 else sub[:45] + "...")
        author.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        author.setTextColor_(AppKit.NSColor.lightGrayColor())
        author.setFrame_(Foundation.NSMakeRect(text_x, 12, width - text_x - 12, 16))
        content.addSubview_(author)
    win.orderFrontRegardless()
    AppHelper.callLater(seconds, app.terminate_, None)
    app.run()


def popup_main(title, sub, cover, seconds):
    if sys.platform == "darwin":
        popup_main_mac(title, sub, cover, seconds)
        return
    import base64
    import io
    import tkinter as tk

    png = None
    data = read_cover(cover)
    if data:
        try:
            from PIL import Image, ImageOps

            img = Image.open(io.BytesIO(data)).convert("RGB")
            img = ImageOps.fit(img, (72, 72), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            png = base64.b64encode(buf.getvalue())
        except Exception:
            png = None
    width, height = 372, 96
    bg, fg, dim = "#202124", "#f1f3f4", "#9aa0a6"
    root = tk.Tk()
    root.withdraw()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=bg)
    frame = tk.Frame(root, bg=bg, highlightthickness=1, highlightbackground="#3c4043")
    frame.pack(fill="both", expand=True)
    if png:
        photo = tk.PhotoImage(data=png)
        tk.Label(frame, image=photo, bg=bg, bd=0).pack(side="left", padx=(12, 10), pady=12)
        frame.photo = photo
    text = tk.Frame(frame, bg=bg)
    text.pack(side="left", fill="both", expand=True, padx=(0, 12), pady=10)
    tk.Label(text, text="Now playing", bg=bg, fg=dim, font=("TkDefaultFont", 9), anchor="w").pack(fill="x")
    tk.Label(
        text,
        text=title if len(title) <= 60 else title[:57] + "...",
        bg=bg,
        fg=fg,
        font=("TkDefaultFont", 11, "bold"),
        anchor="nw",
        justify="left",
        wraplength=250 if png else 330,
    ).pack(fill="x")
    if sub:
        tk.Label(
            text,
            text=sub if len(sub) <= 48 else sub[:45] + "...",
            bg=bg,
            fg=dim,
            font=("TkDefaultFont", 9),
            anchor="w",
        ).pack(fill="x", side="bottom")
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{width}x{height}+{sw - width - 20}+{sh - height - 70}")
    root.deiconify()
    root.lift()
    root.update()
    root.bind("<Button-1>", lambda e: root.destroy())
    frame.bind("<Button-1>", lambda e: root.destroy())
    root.after(int(seconds * 1000), root.destroy)
    root.mainloop()


def ask_form_mac(title, fields, button):
    values = []
    for f in fields:
        hidden = " with hidden answer" if f.get("secret") else ""
        script = (
            f"text returned of (display dialog {json.dumps(f['label'], ensure_ascii=False)} "
            f"default answer {json.dumps(f.get('value', ''), ensure_ascii=False)} "
            f"with title {json.dumps(title, ensure_ascii=False)}{hidden} "
            f"buttons {{\"Cancel\", {json.dumps(button)}}} default button {json.dumps(button)})"
        )
        try:
            proc = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True, timeout=3600
            )
        except Exception:
            log.exception("dialog failed")
            return None
        if proc.returncode != 0:
            return None
        values.append(proc.stdout.rstrip("\n") if f.get("secret") else proc.stdout.strip())
    return values


def ask_form(title, fields, button="OK"):
    if sys.platform == "darwin" and len(fields) == 1:
        return ask_form_mac(title, fields, button)
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--form",
        f"--title={title}",
        f"--fields={json.dumps(fields)}",
        f"--button={button}",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3600,
            **subprocess_flags(),
        )
    except Exception:
        log.exception("dialog failed")
        return None
    if proc.returncode != 0:
        log.warning("dialog error: %s", (proc.stderr or "").strip()[-300:])
        return None
    out = proc.stdout.strip()
    if not out:
        return None
    try:
        values = json.loads(out)
    except ValueError:
        return None
    return values if isinstance(values, list) else None


ICON_COLORS = {
    "playing": (46, 184, 92),
    "paused": (240, 170, 30),
    "idle": (112, 118, 130),
    "loading": (60, 140, 230),
}


THEMES = (
    ("auto", "Auto (follow system)"),
    ("color", "Color"),
    ("light", "Black icon (for light bar)"),
    ("dark", "White icon (for dark bar)"),
)


def detect_system_dark():
    try:
        if sys.platform == "win32":
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            )
            value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
            winreg.CloseKey(key)
            return value == 0
        if sys.platform == "darwin":
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return out.stdout.strip().lower() == "dark"
        for key in ("color-scheme", "gtk-theme"):
            out = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", key],
                capture_output=True,
                text=True,
                timeout=3,
            )
            text = out.stdout.strip().lower()
            if out.returncode == 0 and text:
                if "dark" in text:
                    return True
                if key == "color-scheme" and ("light" in text or "default" in text):
                    continue
                if key == "gtk-theme":
                    return False
    except Exception:
        pass
    return True



def make_icon_image(kind, theme="color", frame=0):
    from PIL import Image, ImageDraw

    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    left, top, right, bottom = 36, 10, 220, 246
    spine = 76
    if theme == "color":
        base = ICON_COLORS[kind]
        dark = tuple(int(c * 0.68) for c in base)
        d.rounded_rectangle((left, top, right, bottom), radius=26, fill=base + (255,))
        d.rounded_rectangle((left, top, spine, bottom), radius=26, fill=dark + (255,))
        d.rectangle((spine - 26, top, spine, bottom), fill=dark + (255,))
        d.rectangle((spine, top, spine + 6, bottom), fill=base + (255,))
        d.rounded_rectangle((spine + 18, bottom - 30, right - 14, bottom - 20), radius=5, fill=(255, 255, 255, 120))
        ink = (255, 255, 255, 255)
        soft = (232, 235, 240, 255)
        outline = False
    else:
        ink = (0, 0, 0, 255) if theme == "light" else (255, 255, 255, 255)
        soft = ink
        outline = True
        d.rounded_rectangle((left, top, right, bottom), radius=26, outline=ink, width=16)
        d.line((spine, top + 8, spine, bottom - 8), fill=ink, width=14)
    cx = (spine + right) // 2 + 4
    cy = 120
    tri = [(cx - 30, cy - 44), (cx - 30, cy + 44), (cx + 38, cy)]
    bars = ((cx - 36, cy - 44, cx - 10, cy + 44), (cx + 10, cy - 44, cx + 36, cy + 44))
    dots = (cx - 34, cx, cx + 34)
    if kind == "paused":
        for box in bars:
            d.rounded_rectangle(box, radius=7, fill=ink)
    elif kind == "loading":
        for i, x in enumerate(dots):
            active = i == frame % 3
            sz = 34 if active else 20
            col = ink if active else ink[:3] + (120,)
            d.ellipse((x - sz // 2, cy - sz // 2, x + sz // 2, cy + sz // 2), fill=col)
    elif kind == "idle" and outline:
        d.polygon(tri, outline=ink, width=12)
    else:
        d.polygon(tri, fill=ink if kind == "playing" else soft)
    return img.resize((64, 64), Image.LANCZOS)


SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
    <method name="Activate"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
    <method name="SecondaryActivate"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
    <method name="Scroll"><arg type="i" name="delta" direction="in"/><arg type="s" name="orientation" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg type="s" name="status"/></signal>
  </interface>
</node>
"""

DBUSMENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{sv}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{sv})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{sv})" name="updatedProps"/>
      <arg type="a(ias)" name="removedProps"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision"/>
      <arg type="i" name="parent"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id"/>
      <arg type="u" name="timestamp"/>
    </signal>
  </interface>
</node>
"""

SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"


class SniIcon:
    def __init__(self, name, image, title, menu, on_activate):
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        self.Gio = Gio
        self.GLib = GLib
        self.name = name
        self.title_text = title
        self.menu = menu
        self.on_activate = on_activate
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = self.conn.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
        if not reply.unpack()[0]:
            raise RuntimeError("no StatusNotifierWatcher")
        self.lock = threading.Lock()
        self.pixmap = self.to_pixmap(image)
        self.nodes = {}
        self.tree = None
        self.revision = 1
        self.signature = None
        self.loop = None
        self.rebuild(emit=False)

    def to_pixmap(self, image):
        img = image.convert("RGBA")
        r, g, b, a = img.split()
        from PIL import Image

        data = Image.merge("RGBA", (a, r, g, b)).tobytes()
        return [(img.width, img.height, data)]

    def prop(self, name):
        GLib = self.GLib
        if name == "Category":
            return GLib.Variant("s", "ApplicationStatus")
        if name == "Id":
            return GLib.Variant("s", self.name)
        if name == "Title":
            return GLib.Variant("s", self.title_text)
        if name == "Status":
            return GLib.Variant("s", "Active")
        if name == "WindowId":
            return GLib.Variant("i", 0)
        if name in ("IconName", "OverlayIconName", "AttentionIconName", "AttentionMovieName"):
            return GLib.Variant("s", "")
        if name == "IconPixmap":
            return GLib.Variant("a(iiay)", self.pixmap)
        if name in ("OverlayIconPixmap", "AttentionIconPixmap"):
            return GLib.Variant("a(iiay)", [])
        if name == "ToolTip":
            return GLib.Variant("(sa(iiay)ss)", ("", [], self.title_text, ""))
        if name == "ItemIsMenu":
            return GLib.Variant("b", False)
        if name == "Menu":
            return GLib.Variant("o", MENU_PATH)
        return None

    def menu_prop(self, name):
        GLib = self.GLib
        if name == "Version":
            return GLib.Variant("u", 3)
        if name == "TextDirection":
            return GLib.Variant("s", "ltr")
        if name == "Status":
            return GLib.Variant("s", "normal")
        if name == "IconThemePath":
            return GLib.Variant("as", [])
        return None

    def build_tree(self):
        GLib = self.GLib
        import pystray

        nodes = {}
        counter = [0]

        def make(menu):
            children = []
            for item in menu:
                counter[0] += 1
                node_id = counter[0]
                if item is pystray.Menu.SEPARATOR:
                    props = {"type": GLib.Variant("s", "separator")}
                    kids = []
                else:
                    label = str(item.text).replace("_", "__")
                    props = {
                        "label": GLib.Variant("s", label),
                        "enabled": GLib.Variant("b", bool(item.enabled)),
                        "visible": GLib.Variant("b", True),
                    }
                    checked = item.checked
                    if checked is not None:
                        props["toggle-type"] = GLib.Variant(
                            "s", "radio" if item.radio else "checkmark"
                        )
                        props["toggle-state"] = GLib.Variant("i", 1 if checked else 0)
                    sub = item.submenu
                    kids = []
                    if sub is not None:
                        props["children-display"] = GLib.Variant("s", "submenu")
                        kids = make(sub)
                nodes[node_id] = (item, props)
                children.append((node_id, props, kids))
            return children

        top = make(self.menu) if self.menu is not None else []
        return nodes, (0, {"children-display": GLib.Variant("s", "submenu")}, top)

    def signature_of(self, tree):
        def sig(node):
            node_id, props, kids = node
            return (
                node_id,
                tuple(sorted((k, v.print_(False)) for k, v in props.items())),
                tuple(sig(k) for k in kids),
            )

        return sig(tree)

    def rebuild(self, emit=True):
        try:
            nodes, tree = self.build_tree()
        except Exception:
            log.exception("sni menu build failed")
            return
        signature = self.signature_of(tree)
        with self.lock:
            changed = signature != self.signature
            self.nodes = nodes
            self.tree = tree
            self.signature = signature
            if changed:
                self.revision += 1
            revision = self.revision
        if emit and changed:
            self.emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                      self.GLib.Variant("(ui)", (revision, 0)))

    def emit(self, path, iface, signal, params=None):
        try:
            self.conn.emit_signal(None, path, iface, signal, params)
        except Exception:
            log.exception("sni emit failed")

    def node_variant(self, node):
        GLib = self.GLib
        node_id, props, kids = node
        return (
            node_id,
            props,
            [GLib.Variant("(ia{sv}av)", self.node_variant(k)) for k in kids],
        )

    def find(self, node, wanted):
        if node[0] == wanted:
            return node
        for kid in node[2]:
            found = self.find(kid, wanted)
            if found:
                return found
        return None

    def flatten(self, node, out):
        out.append((node[0], node[1]))
        for kid in node[2]:
            self.flatten(kid, out)

    def on_sni_call(self, conn, sender, path, iface, method, params, invocation):
        log.info("tray call: %s", method)
        if method in ("Activate", "SecondaryActivate"):
            threading.Thread(target=self.on_activate, daemon=True).start()
        invocation.return_value(None)

    def on_sni_get(self, conn, sender, path, iface, name):
        return self.prop(name)

    def on_menu_get(self, conn, sender, path, iface, name):
        return self.menu_prop(name)

    def on_menu_call(self, conn, sender, path, iface, method, params, invocation):
        GLib = self.GLib
        try:
            if method == "GetLayout":
                parent = params.unpack()[0]
                with self.lock:
                    tree = self.tree
                    revision = self.revision
                node = self.find(tree, parent) or tree
                invocation.return_value(
                    GLib.Variant("(u(ia{sv}av))", (revision, self.node_variant(node)))
                )
            elif method == "GetGroupProperties":
                ids = params.unpack()[0]
                flat = []
                with self.lock:
                    self.flatten(self.tree, flat)
                if ids:
                    flat = [f for f in flat if f[0] in ids]
                invocation.return_value(GLib.Variant("(a(ia{sv}))", (flat,)))
            elif method == "GetProperty":
                wanted, name = params.unpack()
                flat = []
                with self.lock:
                    self.flatten(self.tree, flat)
                value = next((p.get(name) for i, p in flat if i == wanted), None)
                if value is None:
                    value = GLib.Variant("s", "")
                invocation.return_value(GLib.Variant("(v)", (value,)))
            elif method == "Event":
                node_id, event = params.unpack()[:2]
                self.fire(node_id, event)
                invocation.return_value(None)
            elif method == "EventGroup":
                for node_id, event, _, _ in params.unpack()[0]:
                    self.fire(node_id, event)
                invocation.return_value(GLib.Variant("(ai)", ([],)))
            elif method == "AboutToShow":
                if params.unpack()[0] == 0:
                    self.rebuild()
                invocation.return_value(GLib.Variant("(b)", (False,)))
            elif method == "AboutToShowGroup":
                invocation.return_value(GLib.Variant("(aiai)", ([], [])))
            else:
                invocation.return_value(None)
        except Exception:
            log.exception("sni menu call failed")
            invocation.return_value(None)

    def fire(self, node_id, event):
        if event != "clicked":
            return
        with self.lock:
            entry = self.nodes.get(node_id)
        if not entry:
            return
        item = entry[0]
        if item.submenu is not None:
            return
        threading.Thread(target=item, args=(self,), daemon=True).start()

    def register(self):
        Gio = self.Gio
        sni_info = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
        menu_info = Gio.DBusNodeInfo.new_for_xml(DBUSMENU_XML)
        self.conn.register_object(
            SNI_PATH, sni_info.interfaces[0], self.on_sni_call, self.on_sni_get, None
        )
        self.conn.register_object(
            MENU_PATH, menu_info.interfaces[0], self.on_menu_call, self.on_menu_get, None
        )
        self.bus_name = "org.kde.StatusNotifierItem-%d-1" % os.getpid()
        Gio.bus_own_name_on_connection(
            self.conn, self.bus_name, Gio.BusNameOwnerFlags.NONE, None, None
        )
        self.conn.call_sync(
            "org.kde.StatusNotifierWatcher",
            "/StatusNotifierWatcher",
            "org.kde.StatusNotifierWatcher",
            "RegisterStatusNotifierItem",
            self.GLib.Variant("(s)", (self.bus_name,)),
            None,
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )

    @property
    def icon(self):
        return None

    @icon.setter
    def icon(self, image):
        self.pixmap = self.to_pixmap(image)
        self.GLib.idle_add(
            lambda: (self.emit(SNI_PATH, "org.kde.StatusNotifierItem", "NewIcon"), False)[1]
        )

    @property
    def title(self):
        return self.title_text

    @title.setter
    def title(self, value):
        if value == self.title_text:
            return
        self.title_text = value
        self.GLib.idle_add(
            lambda: (
                self.emit(SNI_PATH, "org.kde.StatusNotifierItem", "NewTitle"),
                self.emit(SNI_PATH, "org.kde.StatusNotifierItem", "NewToolTip"),
                False,
            )[2]
        )

    @property
    def visible(self):
        return True

    @visible.setter
    def visible(self, value):
        pass

    def update_menu(self):
        self.GLib.idle_add(lambda: (self.rebuild(), False)[1])

    def notify(self, message, title=None):
        GLib = self.GLib

        def send():
            try:
                self.conn.call_sync(
                    "org.freedesktop.Notifications",
                    "/org/freedesktop/Notifications",
                    "org.freedesktop.Notifications",
                    "Notify",
                    GLib.Variant(
                        "(susssasa{sv}i)",
                        (title or self.name, 0, "", title or self.name, message, [], {}, 5000),
                    ),
                    None,
                    self.Gio.DBusCallFlags.NONE,
                    2000,
                    None,
                )
            except Exception:
                pass

        threading.Thread(target=send, daemon=True).start()

    def run(self, setup=None):
        self.register()
        self.loop = self.GLib.MainLoop()
        if setup:
            threading.Thread(target=setup, args=(self,), daemon=True).start()
        self.loop.run()

    def stop(self):
        if self.loop:
            self.GLib.idle_add(lambda: (self.loop.quit(), False)[1])


class App:
    def __init__(self):
        import pystray

        self.pystray = pystray
        self.store = Store()
        if not self.store.data["device_id"]:
            self.store.data["device_id"] = uuid.uuid4().hex
            self.store.save()
        self.api = Api(self.store)
        self.dialog_lock = threading.Lock()
        self.popup_lock = threading.Lock()
        self.images = {}
        self.frame = 0
        self.cover_item = None
        self.system_dark = detect_system_dark()
        self.player = make_player()
        self.ctl = Controller(
            self.store,
            self.api,
            self.player,
            on_change=self.refresh,
            on_notify=self.notify,
            ask_search=self.open_search,
            ask_setup=self.setup_login,
            on_track=self.show_popup,
        )
        self.popup_proc = None
        theme = self.effective_theme()
        self.images[(theme, "idle", 0)] = make_icon_image("idle", theme)
        if sys.platform == "win32":
            self.enable_dark_menus()
        self.icon = None
        if sys.platform.startswith("linux") and pystray.Icon.__module__.endswith("_appindicator"):
            try:
                self.icon = SniIcon(
                    APP, self.images[(theme, "idle", 0)], APP, self.build_menu(), self.on_toggle
                )
            except Exception:
                log.info("native tray item unavailable, using indicator", exc_info=True)
                self.icon = None
        log.info("tray: %s", "native" if self.icon is not None else "indicator fallback")
        if self.icon is None:
            self.icon = pystray.Icon(APP, self.images[(theme, "idle", 0)], APP, self.build_menu())
        threading.Thread(target=self.animate, daemon=True).start()

    def build_menu(self):
        ps = self.pystray
        return ps.Menu(
            ps.MenuItem(
                lambda item: "Pause" if self.ctl.status == "playing" else "Play",
                self.on_toggle,
                default=True,
            ),
            ps.MenuItem("Search...", self.on_search),
            ps.MenuItem("Next", self.on_next),
            ps.MenuItem("Previous", self.on_previous),
            ps.Menu.SEPARATOR,
            ps.MenuItem(
                lambda item: self.now_playing_text(),
                self.on_now_playing,
                enabled=lambda item: bool(self.store.data.get("item_id")),
            ),
            ps.MenuItem(
                "Notifications",
                ps.Menu(
                    ps.MenuItem(
                        "On",
                        self.make_notify_action(True),
                        checked=lambda item: bool(self.store.data.get("notifications", True)),
                        radio=True,
                    ),
                    ps.MenuItem(
                        "Off",
                        self.make_notify_action(False),
                        checked=lambda item: not self.store.data.get("notifications", True),
                        radio=True,
                    ),
                ),
            ),
            ps.MenuItem(
                "Always show icon",
                self.on_pin_icon,
                checked=lambda item: bool(self.store.data.get("pin_icon", True)),
                visible=sys.platform == "win32",
            ),
            ps.MenuItem(
                "Icon style",
                ps.Menu(
                    *[
                        ps.MenuItem(
                            label,
                            self.make_theme_action(key),
                            checked=lambda item, k=key: self.current_theme() == k,
                            radio=True,
                        )
                        for key, label in THEMES
                    ]
                ),
            ),
            ps.MenuItem(
                "Sign out",
                self.on_sign_out,
                enabled=lambda item: bool(self.store.data.get("server")),
            ),
            ps.Menu.SEPARATOR,
            ps.MenuItem("Quit", self.on_quit),
        )

    def setup_login(self):
        if not self.dialog_lock.acquire(blocking=False):
            return False
        try:
            d = self.store.data
            values = ask_form(
                "Audiobookshelf",
                [
                    {"label": "Site:", "value": d["server"]},
                    {"label": "Username:", "value": d["username"]},
                    {"label": "Password:", "value": "", "secret": True},
                ],
                "Sign in",
            )
            if not values or not values[0] or not values[1] or not values[2]:
                return False
            server = normalize_server(values[0])
            if not server:
                self.notify("That does not look like a valid site address")
                return False
            d["server"] = server
            d["username"] = values[1]
            d["password"] = values[2]
            d["token"] = ""
            self.store.save()
            return True
        finally:
            self.dialog_lock.release()

    def show_popup(self, item_id, title, author, force=False):
        if not force and not self.store.data.get("notifications", True):
            return
        threading.Thread(
            target=self.popup_worker, args=(item_id, title, author), daemon=True
        ).start()

    def popup_worker(self, item_id, title, author):
        cover = config_dir() / "cover.jpg"
        if self.cover_item != item_id:
            try:
                cover.write_bytes(self.api.cover(item_id))
                self.cover_item = item_id
            except Exception as exc:
                log.info("cover unavailable: %s", exc)
                try:
                    cover.unlink()
                except OSError:
                    pass
                self.cover_item = None
        cmd = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--popup",
            f"--text={title}",
            f"--sub={author}",
            f"--cover={cover if cover.exists() else ''}",
        ]
        with self.popup_lock:
            if self.popup_proc is not None and self.popup_proc.poll() is None:
                self.popup_proc.terminate()
            try:
                self.popup_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=open(config_dir() / "popup.log", "a"),
                    **subprocess_flags(),
                )
            except Exception:
                log.exception("popup failed")

    def title_with_percent(self):
        title = self.ctl.now_title()
        if not title:
            return ""
        pct = self.ctl.percent()
        return f"{title} ({pct}%)" if pct is not None else title

    def now_playing_text(self):
        title = self.title_with_percent()
        if not title:
            return "Now Playing: nothing"
        title = title.replace("&", "&&")
        if len(title) > 64:
            title = title[:61] + "..."
        return f"Now Playing: {title}"

    def tooltip(self):
        title = self.title_with_percent()
        return f"{APP}: {title}"[:120] if title else APP

    def animate(self):
        ticks = 0
        while not self.ctl.stop_flag.wait(0.3):
            ticks += 1
            if ticks % 10 == 0 and self.current_theme() == "auto":
                dark = detect_system_dark()
                if dark != self.system_dark:
                    self.system_dark = dark
                    try:
                        self.set_icon(self.current_kind(), self.frame)
                    except Exception:
                        log.exception("theme switch failed")
            if ticks % 50 == 0 and self.ctl.status in ("playing", "paused"):
                try:
                    self.icon.title = self.tooltip()
                    self.icon.update_menu()
                except Exception:
                    log.exception("menu refresh failed")
            if self.ctl.status != "loading":
                self.frame = 0
                continue
            self.frame = (self.frame + 1) % 3
            try:
                self.set_icon("loading", self.frame)
            except Exception:
                log.exception("animation failed")

    def on_now_playing(self, icon=None, item=None):
        d = self.store.data
        if d.get("item_id"):
            self.show_popup(d["item_id"], self.ctl.now_title(), d.get("author", ""), force=True)

    def notify(self, message):
        log.info("notify: %s", message)
        try:
            self.icon.notify(message, APP)
        except Exception:
            pass

    def open_search(self):
        if not self.dialog_lock.acquire(blocking=False):
            return
        try:
            values = ask_form(
                APP,
                [{"label": "Search audiobooks by title:", "value": ""}],
                "Play",
            )
        finally:
            self.dialog_lock.release()
        if values and values[0]:
            self.ctl.search_and_play(values[0])

    def search_flow(self):
        if self.ctl.ensure_ready():
            self.open_search()

    def on_toggle(self, icon=None, item=None):
        self.ctl.toggle()

    def on_search(self, icon=None, item=None):
        threading.Thread(target=self.search_flow, daemon=True).start()

    def on_next(self, icon=None, item=None):
        self.ctl.next()

    def on_previous(self, icon=None, item=None):
        self.ctl.previous()

    def on_sign_out(self, icon=None, item=None):
        self.ctl.sign_out()

    def on_quit(self, icon=None, item=None):
        self.ctl.shutdown()
        self.icon.stop()

    def enable_dark_menus(self):
        try:
            import ctypes

            uxtheme = ctypes.WinDLL("uxtheme")
            set_mode = uxtheme[135]
            set_mode.argtypes = [ctypes.c_int]
            set_mode.restype = ctypes.c_int
            set_mode(1)
            flush = uxtheme[136]
            flush()
        except Exception:
            log.exception("dark menu setup failed")

    def on_pin_icon(self, icon=None, item=None):
        self.store.data["pin_icon"] = not self.store.data.get("pin_icon", True)
        self.store.save()
        self.apply_pin_icon()

    def apply_pin_icon(self):
        if sys.platform != "win32":
            return
        try:
            import winreg

            wanted = 1 if self.store.data.get("pin_icon", True) else 0
            exes = {sys.executable, getattr(sys, "_base_executable", "") or ""}
            for name in ("python.exe", "pythonw.exe"):
                exes.add(os.path.join(sys.base_prefix, name))
                exes.add(os.path.join(os.path.dirname(sys.executable), name))
            exes = {os.path.normcase(os.path.abspath(e)) for e in exes if e}
            root = r"Control Panel\NotifyIconSettings"
            base = winreg.OpenKey(winreg.HKEY_CURRENT_USER, root)
            count = winreg.QueryInfoKey(base)[0]
            names = [winreg.EnumKey(base, i) for i in range(count)]
            winreg.CloseKey(base)
            for name in names:
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    root + "\\" + name,
                    0,
                    winreg.KEY_READ | winreg.KEY_SET_VALUE,
                )
                try:
                    path = winreg.QueryValueEx(key, "ExecutablePath")[0]
                except OSError:
                    path = ""
                if path and os.path.normcase(os.path.abspath(path)) in exes:
                    winreg.SetValueEx(key, "IsPromoted", 0, winreg.REG_DWORD, wanted)
                winreg.CloseKey(key)
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("pin icon failed")

    def pin_icon_later(self):
        for delay in (2, 6, 20, 60):
            if self.ctl.stop_flag.wait(delay):
                return
            self.apply_pin_icon()

    def make_notify_action(self, on):
        def action(icon=None, item=None):
            self.store.data["notifications"] = on
            self.store.save()

        return action

    def make_theme_action(self, key):
        def action(icon=None, item=None):
            self.store.data["theme"] = key
            if key == "auto":
                self.system_dark = detect_system_dark()
            self.store.save()
            self.refresh()

        return action

    def refresh(self):
        status = self.ctl.status
        kind = status if status in ICON_COLORS else "idle"
        try:
            self.set_icon(kind, self.frame)
            self.icon.title = self.tooltip()
            self.icon.update_menu()
        except Exception:
            log.exception("icon refresh failed")

    def current_theme(self):
        theme = self.store.data.get("theme", "auto")
        return theme if theme in dict(THEMES) else "auto"

    def effective_theme(self):
        theme = self.current_theme()
        if theme == "auto":
            return "dark" if self.system_dark else "light"
        return theme

    def current_kind(self):
        status = self.ctl.status
        return status if status in ICON_COLORS else "idle"

    def set_icon(self, kind, frame=0):
        theme = self.effective_theme()
        key = (theme, kind, frame if kind == "loading" else 0)
        if key not in self.images:
            self.images[key] = make_icon_image(kind, theme, key[2])
        self.icon.icon = self.images[key]

    def install_mac_click(self):
        import AppKit
        import objc
        from Foundation import NSObject

        app = self

        class ClickHandler(NSObject):
            def clicked_(self, sender):
                event = AppKit.NSApp.currentEvent()
                right = event is not None and (
                    event.type() == AppKit.NSEventTypeRightMouseUp
                    or bool(event.modifierFlags() & AppKit.NSEventModifierFlagControl)
                )
                if right:
                    handle = app.icon._menu_handle
                    if handle:
                        item = app.icon._status_item
                        item.setMenu_(handle[0])
                        item.button().performClick_(None)
                        item.setMenu_(None)
                else:
                    app.on_toggle()

        icon = self.icon

        def update_menu():
            callbacks = []
            nsmenu = icon._create_menu(icon.menu, callbacks)
            icon._menu_handle = (nsmenu, callbacks) if nsmenu else None

        icon._update_menu = update_menu
        self.click_handler = ClickHandler.alloc().init()

        def apply():
            try:
                item = icon._status_item
                item.setMenu_(None)
                button = item.button()
                button.setTarget_(self.click_handler)
                button.setAction_(
                    objc.selector(None, selector=b"clicked:", signature=b"v@:@")
                )
                button.sendActionOn_(
                    AppKit.NSEventMaskLeftMouseUp | AppKit.NSEventMaskRightMouseUp
                )
                update_menu()
            except Exception:
                log.exception("mac click handling failed")

        from PyObjCTools import AppHelper

        AppHelper.callAfter(apply)

    def reshow_icon(self):
        for delay in (10, 30, 60):
            if self.ctl.stop_flag.wait(delay):
                return
            try:
                self.icon.visible = False
                self.icon.visible = True
                self.refresh()
            except Exception:
                log.exception("icon re-register failed")

    def run(self):
        def setup(icon):
            if sys.platform == "darwin":
                try:
                    self.install_mac_click()
                except Exception:
                    log.exception("mac click handling failed, using default menu click")
            hide_dock_icon()
            icon.visible = True
            self.refresh()
            if sys.platform == "win32":
                threading.Thread(target=self.pin_icon_later, daemon=True).start()
            if getattr(self, "startup", False) and sys.platform == "win32":
                threading.Thread(target=self.reshow_icon, daemon=True).start()

        self.icon.run(setup)


def new_instance_socket():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return sock


def single_instance():
    sock = new_instance_socket()
    try:
        sock.bind(("127.0.0.1", INSTANCE_PORT))
        sock.listen(5)
    except OSError:
        return None
    return sock


def control_loop(sock, app):
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        try:
            conn.settimeout(2)
            data = conn.recv(64).strip()
            if data == b"quit":
                conn.sendall(b"ok")
                conn.close()
                app.on_quit()
                return
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def stop_running(wait=15.0):
    try:
        with socket.create_connection(("127.0.0.1", INSTANCE_PORT), timeout=2) as conn:
            conn.sendall(b"quit")
            conn.settimeout(3)
            conn.recv(8)
    except OSError:
        return False
    end = time.time() + wait
    while time.time() < end:
        probe = new_instance_socket()
        try:
            probe.bind(("127.0.0.1", INSTANCE_PORT))
            probe.close()
            return True
        except OSError:
            probe.close()
            time.sleep(0.2)
    return True


def selftest(seconds=8):
    print(f"{APP} {VERSION} selftest")
    store = Store()
    d = store.data
    if not d["server"] or not d["username"] or not d["password"]:
        print("FAIL: run the app once and sign in first")
        return 1
    api = Api(store)
    try:
        api.login()
        libs = api.libraries()
        print("signed in to", d["server"], f"({len(libs)} book libraries)")
        item_id = d["item_id"]
        if not item_id:
            for lib in libs:
                res = api.call("GET", f"/api/libraries/{lib['id']}/items?limit=1")
                results = res.get("results") or []
                if results:
                    item_id = results[0]["id"]
                    break
        if not item_id:
            print("FAIL: no books found")
            return 1
        session = api.play(item_id)
    except (AuthError, ApiError) as exc:
        print("FAIL:", exc)
        return 1
    tracks = session.get("audioTracks") or []
    if not tracks:
        print("FAIL: book has no audio tracks")
        return 1
    print("opened:", session.get("displayTitle"), f"({len(tracks)} tracks)")
    url = tracks[0]["contentUrl"]
    if not url.startswith("http"):
        url = d["server"] + url
    try:
        player = make_player(ao="null")
    except Exception as exc:
        print("FAIL: could not start mpv:", exc)
        return 1
    try:
        player["http-header-fields"] = [f"Authorization: Bearer {d['token']}"]
    except Exception:
        pass
    player.play(url)
    start = time.time()
    moved = None
    while time.time() - start < seconds + 15:
        time.sleep(0.5)
        pos = player.time_pos
        if pos is not None and pos > 2:
            moved = pos
            break
    player.terminate()
    try:
        api.close(session["id"], float(session.get("currentTime") or 0), 0.0, float(session.get("duration") or 0))
    except Exception as exc:
        print("close failed:", exc)
    if moved is None:
        print("FAIL: mpv did not advance")
        return 1
    print(f"PASS: playback advanced to {moved:.1f}s")
    return 0


PIP_DEPS = {
    "mpv": "python-mpv>=1.0.6",
    "pystray": "pystray>=0.19.5",
    "PIL": "Pillow>=10.0",
    "truststore": "truststore>=0.9",
}


def setup_tls():
    try:
        import truststore

        truststore.inject_into_ssl()
        return
    except Exception:
        pass
    try:
        import certifi

        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    except Exception:
        pass


def script_dir():
    return Path(__file__).resolve().parent


def venv_dir():
    return script_dir() / ".venv"


def venv_python():
    base = venv_dir() / ("Scripts" if sys.platform == "win32" else "bin")
    exe = base / ("python.exe" if sys.platform == "win32" else "python")
    if sys.platform == "win32" and Path(sys.executable).name.lower() == "pythonw.exe":
        w = base / "pythonw.exe"
        if w.exists():
            return w
    return exe


def in_venv():
    try:
        return Path(sys.prefix).resolve() == venv_dir().resolve()
    except OSError:
        return False


def say(msg):
    print(f"[audiobookshelf-tray setup] {msg}", flush=True)
    log.info(msg)


def run_cmd(cmd, **kw):
    try:
        return subprocess.run(cmd, **subprocess_flags(), **kw).returncode == 0
    except Exception:
        return False


def have_command(name):
    import shutil

    return shutil.which(name) is not None


def have_libmpv():
    import ctypes.util

    prepare_libmpv()
    if sys.platform == "win32":
        return any(ctypes.util.find_library(n) for n in ("mpv-2.dll", "libmpv-2.dll", "mpv-1.dll"))
    return bool(ctypes.util.find_library("mpv"))


def have_module(name):
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def sudo_prefix():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    if have_command("sudo"):
        return ["sudo"]
    return None


def install_system_packages():
    need_mpv = not have_libmpv()
    marker = config_dir() / ".system-setup"
    tried = marker.exists()
    need_tk = not have_module("tkinter") and not tried
    need_gi = sys.platform.startswith("linux") and not have_module("gi") and not tried
    if not (need_mpv or need_tk or need_gi):
        return
    try:
        marker.write_text("1")
    except OSError:
        pass
    missing = [n for n, f in (("libmpv", need_mpv), ("tkinter", need_tk), ("gi", need_gi)) if f]
    say("missing system components: " + ", ".join(missing))
    if sys.platform == "darwin":
        if not have_command("brew"):
            say("Homebrew not found. Install it from https://brew.sh then run again")
            return
        pkgs = []
        if need_mpv:
            pkgs.append("mpv")
        if need_tk:
            pkgs.append("python-tk")
        run_cmd(["brew", "install"] + pkgs)
    elif sys.platform == "win32":
        if need_mpv:
            install_libmpv_windows()
        if need_tk:
            say("tkinter missing: reinstall Python from python.org with tcl/tk enabled")
    else:
        pre = sudo_prefix()
        if pre is None:
            say("sudo not found, install these packages manually: mpv libmpv tkinter python3-gi")
            return
        if have_command("apt-get"):
            run_cmd(pre + ["apt-get", "update"])
            pkgs = ["mpv", "python3-tk", "python3-gi", "gir1.2-ayatanaappindicator3-0.1"]
            if not run_cmd(pre + ["apt-get", "install", "-y", "libmpv2"] + pkgs):
                run_cmd(pre + ["apt-get", "install", "-y", "libmpv1"] + pkgs)
        elif have_command("dnf"):
            run_cmd(pre + ["dnf", "install", "-y", "mpv-libs", "python3-tkinter", "python3-gobject", "libayatana-appindicator-gtk3"])
        elif have_command("pacman"):
            run_cmd(pre + ["pacman", "-S", "--noconfirm", "mpv", "tk", "python-gobject", "libayatana-appindicator"])
        elif have_command("zypper"):
            run_cmd(pre + ["zypper", "--non-interactive", "install", "mpv", "libmpv2", "python3-tk", "python3-gobject", "typelib-1_0-AyatanaAppIndicator3-0_1"])
        else:
            say("unknown package manager, install mpv, libmpv, python3-tk and python3-gi manually")
    if not have_libmpv():
        say("libmpv is still missing, playback will not work until it is installed")


GNOME_TRAY_UUID = "appindicatorsupport@rgcjonas.gmail.com"


def ensure_gnome_tray():
    if not sys.platform.startswith("linux"):
        return
    if "gnome" not in os.environ.get("XDG_CURRENT_DESKTOP", "").lower():
        return
    if not have_command("gnome-extensions"):
        return

    def listing(*flags):
        try:
            out = subprocess.run(
                ["gnome-extensions", "list", *flags],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return out.stdout
        except Exception:
            return ""

    if GNOME_TRAY_UUID in listing():
        if GNOME_TRAY_UUID not in listing("--enabled"):
            run_cmd(["gnome-extensions", "enable", GNOME_TRAY_UUID])
        return
    marker = config_dir() / ".gnome-tray"
    if marker.exists():
        return
    try:
        marker.write_text("1")
    except OSError:
        pass
    pre = sudo_prefix()
    if pre is None:
        say("GNOME needs the AppIndicator extension to show the tray icon")
        return
    say("installing the GNOME AppIndicator extension for the tray icon")
    pkg = "gnome-shell-extension-appindicator"
    if have_command("dnf"):
        done = run_cmd(pre + ["dnf", "install", "-y", pkg])
    elif have_command("apt-get"):
        done = run_cmd(pre + ["apt-get", "install", "-y", pkg])
    elif have_command("pacman"):
        done = run_cmd(pre + ["pacman", "-S", "--noconfirm", pkg])
    elif have_command("zypper"):
        done = run_cmd(pre + ["zypper", "--non-interactive", "install", pkg])
    else:
        done = False
    if not done:
        say("could not install the extension, install gnome-shell-extension-appindicator manually")
        return
    run_cmd(["gnome-extensions", "enable", GNOME_TRAY_UUID])
    say("log out and back in once, then the tray icon will appear")


def extract_7z(archive, out):
    import shutil

    attempts = []
    tar = shutil.which("tar")
    if tar:
        attempts.append([tar, "-xf", str(archive), "-C", str(out)])
    for exe in ("7z", "7za", "7zz"):
        found = shutil.which(exe)
        if found:
            attempts.append([found, "x", "-y", f"-o{out}", str(archive)])
    for exe in (
        r"C:\Program Files\7-Zip\7z.exe",
        r"C:\Program Files (x86)\7-Zip\7z.exe",
    ):
        if os.path.exists(exe):
            attempts.append([exe, "x", "-y", f"-o{out}", str(archive)])
    for cmd in attempts:
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=300)
        except Exception:
            continue
        if result.returncode == 0 and any(out.rglob("*.dll")):
            return True
    try:
        pip = [str(venv_python()) if venv_python().exists() else sys.executable, "-m", "pip"]
        run_cmd(pip + ["install", "--quiet", "py7zr"])
        import py7zr

        with py7zr.SevenZipFile(archive) as z:
            z.extractall(out)
        return True
    except Exception:
        return False


def install_libmpv_windows():
    import platform
    import shutil
    import tempfile

    say("downloading libmpv for Windows")
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest",
            headers={"User-Agent": APP},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            release = json.loads(resp.read().decode("utf-8"))
        arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
        asset = None
        for a in release.get("assets", []):
            name = a["name"]
            if name.startswith(f"mpv-dev-{arch}-") and "v3" not in name and name.endswith(".7z"):
                asset = a
                break
        if asset is None:
            raise RuntimeError("no mpv-dev archive found in latest release")
        tmp = Path(tempfile.mkdtemp())
        archive = tmp / asset["name"]
        urllib.request.urlretrieve(asset["browser_download_url"], archive)
        out = tmp / "x"
        out.mkdir()
        if not extract_7z(archive, out):
            raise RuntimeError("could not unpack the archive")
        dll = next(out.rglob("libmpv-2.dll"), None) or next(out.rglob("mpv-2.dll"), None)
        if dll is None:
            raise RuntimeError("libmpv dll not found in archive")
        shutil.copy2(dll, script_dir() / "mpv-2.dll")
        say("installed mpv-2.dll next to the script")
    except Exception as exc:
        say(f"automatic libmpv download failed ({exc}). See README for manual steps")


def bootstrap(setup_only=False):
    if not in_venv():
        vp = venv_python()
        if not vp.exists():
            say("creating virtual environment in .venv")
            import venv

            venv.EnvBuilder(
                with_pip=True, system_site_packages=sys.platform.startswith("linux")
            ).create(venv_dir())
        if not vp.exists():
            say("could not create virtual environment, continuing with current Python")
            return
        args = [str(vp), str(Path(__file__).resolve())] + sys.argv[1:]
        if sys.platform == "win32":
            sys.exit(subprocess.run(args).returncode)
        os.execv(str(vp), args)
    missing = [spec for mod, spec in PIP_DEPS.items() if not have_module(mod)]
    if missing:
        say("installing Python packages: " + ", ".join(missing))
        run_cmd(
            [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
            + missing
        )
    install_system_packages()
    ensure_gnome_tray()
    if setup_only:
        ok = all(have_module(m) for m in PIP_DEPS) and have_libmpv()
        say("ready" if ok else "setup incomplete, see messages above")
        sys.exit(0 if ok else 1)


SERVICE_LABEL = "com.justinoros.audiobookshelf-tray"


def wait_for_taskbar(timeout=120):
    try:
        import ctypes

        end = time.time() + timeout
        while time.time() < end:
            if ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None):
                break
            time.sleep(1)
        time.sleep(3)
    except Exception:
        log.exception("taskbar wait failed")


def service_command():
    py = venv_python() if venv_python().exists() else Path(sys.executable)
    if sys.platform == "win32" and py.name.lower() == "python.exe":
        w = py.with_name("pythonw.exe")
        if w.exists():
            py = w
    return str(py), str(Path(__file__).resolve())


def service_install():
    py, script = service_command()
    if sys.platform == "win32":
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, APP, 0, winreg.REG_SZ, f'"{py}" "{script}" --startup')
        winreg.CloseKey(key)
        print("installed: starts at login")
    elif sys.platform == "darwin":
        import plistlib

        path = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
        path.parent.mkdir(parents=True, exist_ok=True)
        plist = {
            "Label": SERVICE_LABEL,
            "ProgramArguments": [py, script],
            "RunAtLoad": True,
            "EnvironmentVariables": {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
            },
        }
        with open(path, "wb") as f:
            plistlib.dump(plist, f)
        uid = str(os.getuid())
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(path)], capture_output=True)
        run_cmd(["launchctl", "bootstrap", f"gui/{uid}", str(path)])
        print("installed:", path)
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        path = base / "autostart" / f"{APP}.desktop"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[Desktop Entry]\nType=Application\nName=audiobookshelf-tray\n"
            f'Exec="{py}" "{script}"\nX-GNOME-Autostart-enabled=true\n',
            encoding="utf-8",
        )
        print("installed:", path)
    return 0


def service_remove():
    if sys.platform == "win32":
        import winreg

        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_SET_VALUE,
            )
            winreg.DeleteValue(key, APP)
            winreg.CloseKey(key)
        except FileNotFoundError:
            pass
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
        path.unlink(missing_ok=True)
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        (base / "autostart" / f"{APP}.desktop").unlink(missing_ok=True)
    print("removed")
    return 0


def main():
    parser = argparse.ArgumentParser(prog=APP)
    parser.add_argument("--form", action="store_true")
    parser.add_argument("--title", default=APP)
    parser.add_argument("--fields", default="[]")
    parser.add_argument("--button", default="OK")
    parser.add_argument("--popup", action="store_true")
    parser.add_argument("--text", default="")
    parser.add_argument("--sub", default="")
    parser.add_argument("--cover", default="")
    parser.add_argument("--seconds", type=float, default=4.0)
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--startup", action="store_true")
    parser.add_argument("--install-service", action="store_true")
    parser.add_argument("--remove-service", action="store_true")
    args = parser.parse_args()
    if args.version:
        print(VERSION)
        return 0
    if args.form:
        form_main(args.title, json.loads(args.fields), args.button)
        return 0
    if args.popup:
        popup_main(args.text, args.sub, args.cover, args.seconds)
        return 0
    if args.stop:
        print("stopped" if stop_running() else "not running")
        return 0
    if args.restart:
        print("stopped old instance" if stop_running() else "no running instance, starting")
    if os.environ.get("ABS_TRAY_NO_BOOTSTRAP") != "1":
        bootstrap(args.setup)
    elif args.setup:
        return 0
    setup_tls()
    if args.install_service:
        return service_install()
    if args.remove_service:
        return service_remove()
    if args.selftest:
        return selftest()
    setup_logging()
    lock = single_instance()
    if lock is None:
        log.info("already running")
        return 0
    try:
        if args.startup and sys.platform == "win32":
            wait_for_taskbar()
        app = App()
        app.startup = bool(args.startup)
        threading.Thread(target=control_loop, args=(lock, app), daemon=True).start()
        app.run()
    except Exception:
        log.exception("fatal")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
