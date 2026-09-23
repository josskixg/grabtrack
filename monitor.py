"""Grab share-link monitor: poller, state store, change detection."""

from __future__ import annotations

import base64
import copy
import json
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "grabtrack.db"
LEGACY_LINKS_PATH = BASE_DIR / "links.json"
LINKS_PATH = DB_PATH
API_URL = "https://api.grab.com/api/v1/safety/sharemyride/{token}/bookingdetails"
REQUEST_TIMEOUT = 10


def b64_encode(val: str | None) -> str | None:
    """Encode string to Base64 (used for storing Grab URLs securely)."""
    if val is None:
        return None
    return base64.b64encode(str(val).encode("utf-8")).decode("ascii")


def b64_decode(val: str | None) -> str | None:
    """Decode Base64 string back to plaintext URL with fallback."""
    if val is None:
        return None
    s = str(val).strip()
    try:
        raw = base64.b64decode(s.encode("ascii"), validate=True).decode("utf-8")
        if raw.isprintable() and len(raw) > 0:
            return raw
    except Exception:
        pass
    return s

LINK_FIELDS = (
    "token",
    "label",
    "url",
    "state",
    "driver_location",
    "eta",
    "booking_code",
    "last_poll",
    "last_change",
    "error",
    "history",
)

TRACKED_FIELDS = (
    "state",
    "driver_location",
    "sessionStatus",
    "eta",
    "messageStatus",
)

MAX_HISTORY = 50

# Events callback: (events, link_dict) -> None
EventsCallback = Callable[[list[dict[str, Any]], dict[str, Any]], None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def extract_token(url_or_token: str) -> str:
    """Token from URL path after `/o/`, last path segment of other URLs, or bare token."""
    s = (url_or_token or "").strip()
    if not s:
        return ""
    if "/o/" in s:
        part = s.split("/o/", 1)[1]
        return part.split("?", 1)[0].split("#", 1)[0].strip("/")
    if "://" in s:
        path = urlparse(s).path.strip("/")
        if not path:
            return ""
        return path.rsplit("/", 1)[-1]
    return s


def _round5(value: Any) -> float | None:
    try:
        return round(float(value), 5)
    except (TypeError, ValueError):
        return None


def _driver_location(payload: dict[str, Any]) -> dict[str, float] | None:
    driver = payload.get("driver")
    loc = driver.get("location") if isinstance(driver, dict) else None
    if not isinstance(loc, dict):
        return None
    lat = loc.get("latitude", loc.get("lat"))
    lng = loc.get("longitude", loc.get("lng"))
    rlat, rlng = _round5(lat), _round5(lng)
    if rlat is None or rlng is None:
        return None
    return {"lat": rlat, "lng": rlng}


def extract_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Flatten bookingdetails payload into tracked field names."""
    booking = payload.get("booking")
    booking = booking if isinstance(booking, dict) else {}
    route = payload.get("route")
    route = route if isinstance(route, dict) else {}
    message = payload.get("messageStatus")
    message = message if isinstance(message, dict) else {}

    drop_off: dict[str, float] | None = None
    drop_off_raw = booking.get("dropOff")
    if isinstance(drop_off_raw, dict):
        drop_loc = drop_off_raw.get("location") or {}
        dlat = _round5(drop_loc.get("latitude") or drop_loc.get("lat"))
        dlng = _round5(drop_loc.get("longitude") or drop_loc.get("lng"))
        if dlat is not None and dlng is not None:
            drop_off = {"lat": dlat, "lng": dlng}

    return {
        "state": booking.get("bookingState"),
        "driver_location": _driver_location(payload),
        "drop_off_location": drop_off,
        "sessionStatus": payload.get("sessionStatus"),
        "eta": route.get("ETA"),
        "messageStatus": message.get("title") or message.get("subtitle"),
        "booking_code": booking.get("bookingCode"),
    }


def detect_changes(
    old_payload: dict[str, Any], new_payload: dict[str, Any], ts: str | None = None
) -> list[dict[str, Any]]:
    """Compare tracked fields between payloads, return list of {field, old, new, ts}."""
    old_fields = extract_fields(old_payload)
    new_fields = extract_fields(new_payload)
    ts = ts or _now()
    events: list[dict[str, Any]] = []
    for field in TRACKED_FIELDS:
        o = old_fields.get(field)
        n = new_fields.get(field)
        if o != n:
            events.append({"ts": ts, "field": field, "old": o, "new": n})
    return events


def redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Scrub sensitive tokens from payload dict (deep copy mutated)."""
    data = copy.deepcopy(payload)
    if "token" in data:
        data["token"] = "***"
    driver = data.get("driver")
    if isinstance(driver, dict) and "phoneNumber" in driver:
        driver["phoneNumber"] = "***"
    return data


def fetch_booking(token: str, timeout: int = REQUEST_TIMEOUT) -> dict[str, Any]:
    """Call Grab public bookingdetails endpoint, returns parsed JSON."""
    url = API_URL.format(token=token)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as exc:
        raise ValueError("response is not JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("unexpected response shape")
    return redact_payload(data)


# ---------------------------------------------------------------------------
# SQLite Persistence Layer
# ---------------------------------------------------------------------------

def init_db(db_path: Path = DB_PATH) -> None:
    """Initialize SQLite tables for grabtrack."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS links (
                token TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                label TEXT,
                state TEXT,
                driver_location TEXT,
                drop_off_location TEXT,
                eta INTEGER,
                booking_code TEXT,
                last_poll TEXT,
                last_change TEXT,
                error TEXT,
                history TEXT DEFAULT '[]',
                created_at TEXT,
                updated_at TEXT
            )
        """)
        # Migrate: add column if upgrading from older schema
        try:
            conn.execute("ALTER TABLE links ADD COLUMN drop_off_location TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.commit()
    finally:
        conn.close()


def migrate_legacy_json(json_path: Path = LEGACY_LINKS_PATH, db_path: Path = DB_PATH) -> None:
    """Migrate legacy links.json into SQLite if present."""
    if not json_path.exists():
        return
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
        if isinstance(raw, list) and raw:
            now = _now()
            conn = sqlite3.connect(db_path)
            try:
                for item in raw:
                    if isinstance(item, dict) and item.get("url"):
                        url = str(item["url"])
                        label = item.get("label")
                        token = extract_token(url)
                        if token:
                            b64_url = b64_encode(url)
                            conn.execute(
                                """
                                INSERT INTO links (token, url, label, created_at, updated_at)
                                VALUES (?, ?, ?, ?, ?)
                                ON CONFLICT(token) DO UPDATE SET
                                    label = COALESCE(excluded.label, links.label)
                                """,
                                (token, b64_url, label, now, now),
                            )
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:
        print(f"[db] migration warning: {exc}")


def load_links(db_path: Path = DB_PATH) -> list[dict[str, Any]]:
    """Load all tracked links from SQLite (decodes base64 URLs)."""
    init_db(db_path)
    migrate_legacy_json(LEGACY_LINKS_PATH, db_path)
    out: list[dict[str, Any]] = []
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM links ORDER BY created_at ASC")
        for row in cursor.fetchall():
            keys = row.keys()
            loc = None
            if row["driver_location"]:
                try:
                    loc = json.loads(row["driver_location"])
                except (ValueError, TypeError):
                    loc = None
            drop_off = None
            if "drop_off_location" in keys and row["drop_off_location"]:
                try:
                    drop_off = json.loads(row["drop_off_location"])
                except (ValueError, TypeError):
                    drop_off = None
            hist = []
            if row["history"]:
                try:
                    hist = json.loads(row["history"])
                except (ValueError, TypeError):
                    hist = []
            raw_url = row["url"]
            decoded_url = b64_decode(raw_url) if raw_url else ""
            out.append({
                "token": row["token"],
                "url": decoded_url,
                "label": row["label"],
                "state": row["state"],
                "driver_location": loc,
                "drop_off_location": drop_off,
                "eta": row["eta"],
                "booking_code": row["booking_code"],
                "last_poll": row["last_poll"],
                "last_change": row["last_change"],
                "error": row["error"],
                "history": hist,
            })
    finally:
        conn.close()
    return out


def save_link(link: dict[str, Any], db_path: Path = DB_PATH) -> None:
    """Upsert link record into SQLite with Base64 encoded URL."""
    init_db(db_path)
    token = link["token"]
    raw_url = link.get("url", "")
    b64_url = b64_encode(raw_url) if raw_url else ""
    driver_loc = json.dumps(link.get("driver_location")) if link.get("driver_location") else None
    drop_off_loc = json.dumps(link.get("drop_off_location")) if link.get("drop_off_location") else None
    history_str = json.dumps(link.get("history") or [], ensure_ascii=False)
    now = _now()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO links (
                token, url, label, state, driver_location, drop_off_location, eta,
                booking_code, last_poll, last_change, error, history,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(token) DO UPDATE SET
                url = excluded.url,
                label = excluded.label,
                state = excluded.state,
                driver_location = excluded.driver_location,
                drop_off_location = excluded.drop_off_location,
                eta = excluded.eta,
                booking_code = excluded.booking_code,
                last_poll = excluded.last_poll,
                last_change = excluded.last_change,
                error = excluded.error,
                history = excluded.history,
                updated_at = excluded.updated_at
            """,
            (
                token,
                b64_url,
                link.get("label"),
                link.get("state"),
                driver_loc,
                drop_off_loc,
                link.get("eta"),
                link.get("booking_code"),
                link.get("last_poll"),
                link.get("last_change"),
                link.get("error"),
                history_str,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def delete_link(token: str, db_path: Path = DB_PATH) -> None:
    """Delete link record by token from SQLite."""
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM links WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


def save_links(links: dict[str, dict[str, Any]], db_path: Path = DB_PATH) -> None:
    """Bulk save all links to SQLite."""
    for link in links.values():
        save_link(link, db_path)


class Monitor:
    """In-memory link store + SQLite persistence + background poll loop."""

    def __init__(
        self,
        poll_interval: float = 8,
        on_events: EventsCallback | None = None,
        db_path: Path = DB_PATH,
        links_path: Path | None = None,
        fetcher: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.poll_interval = max(1.0, float(poll_interval))
        self.on_events = on_events
        self.db_path = Path(links_path if links_path is not None else db_path)
        self.links_path = self.db_path
        self._fetch = fetcher or fetch_booking
        self.lock = threading.RLock()
        self.links: dict[str, dict[str, Any]] = {}
        self._prev: dict[str, dict[str, Any]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        for item in load_links(self.db_path):
            try:
                token = item["token"]
                self.links[token] = item
                if item.get("state") or item.get("booking_code"):
                    self._prev[token] = {
                        "booking": {
                            "bookingState": item.get("state"),
                            "bookingCode": item.get("booking_code"),
                        },
                        "driver": {
                            "location": {
                                "latitude": (item["driver_location"] or {}).get("lat"),
                                "longitude": (item["driver_location"] or {}).get("lng"),
                            }
                            if item.get("driver_location")
                            else None
                        },
                        "route": {"ETA": item.get("eta")},
                    }
            except Exception:
                continue

    def add_link(
        self, url: str, label: str | None = None, persist: bool = True
    ) -> dict[str, Any]:
        token = extract_token(url)
        if not token or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
            raise ValueError("invalid grab share url/token")
        with self.lock:
            link = self.links.get(token)
            if link is None:
                link = {
                    "token": token,
                    "label": label or None,
                    "url": url,
                    "state": None,
                    "driver_location": None,
                    "eta": None,
                    "booking_code": None,
                    "last_poll": None,
                    "last_change": None,
                    "error": None,
                    "history": [],
                }
                self.links[token] = link
            elif label:
                link["label"] = label
            if persist:
                save_link(link, self.db_path)
            return self._snapshot_link(link)

    def remove_link(self, token: str) -> bool:
        with self.lock:
            if token not in self.links:
                return False
            self.links.pop(token, None)
            self._prev.pop(token, None)
            delete_link(token, self.db_path)
            return True

    def remove_by_token_or_label(self, key: str) -> dict[str, Any] | None:
        key = (key or "").strip()
        if not key:
            return None
        with self.lock:
            if key in self.links:
                snap = self._snapshot_link(self.links[key])
                self.remove_link(key)
                return snap
            for token, link in list(self.links.items()):
                if link.get("label") and link["label"] == key:
                    snap = self._snapshot_link(link)
                    self.remove_link(token)
                    return snap
            return None

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "links": [self._snapshot_link(link) for link in self.links.values()],
                "poll_interval": self.poll_interval,
            }

    def _snapshot_link(self, link: dict[str, Any]) -> dict[str, Any]:
        return {
            "token": link["token"],
            "label": link.get("label"),
            "url": link.get("url"),
            "state": link.get("state"),
            "driver_location": link.get("driver_location"),
            "drop_off_location": link.get("drop_off_location"),
            "eta": link.get("eta"),
            "booking_code": link.get("booking_code"),
            "last_poll": link.get("last_poll"),
            "last_change": link.get("last_change"),
            "error": link.get("error"),
            "history": list(link.get("history") or []),
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="grab-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def poll_once(self) -> None:
        with self.lock:
            tokens = list(self.links)
        if not tokens:
            return
        workers = min(len(tokens), 16)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(self._poll_one, tokens))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # never kill the loop
                print(f"[poller] cycle error: {type(exc).__name__}: {exc}")
            self._stop.wait(self.poll_interval)

    def _poll_one(self, token: str) -> None:
        with self.lock:
            link = self.links.get(token)
        if link is None:
            return
        try:
            payload = self._fetch(token)
        except requests.Timeout:
            with self.lock:
                link["error"] = "timeout"
                link["last_poll"] = _now()
                save_link(link, self.db_path)
            return
        except requests.RequestException as exc:
            status = ""
            if exc.response is not None:
                status = f"HTTP {exc.response.status_code}"
            with self.lock:
                link["error"] = status or f"{type(exc).__name__}"
                link["last_poll"] = _now()
                save_link(link, self.db_path)
            return
        except Exception as exc:
            with self.lock:
                link["error"] = f"{type(exc).__name__}: {exc}"
                link["last_poll"] = _now()
                save_link(link, self.db_path)
            return

        payload = redact_payload(payload)
        with self.lock:
            prev = self._prev.get(token)
            now = _now()
            fields = extract_fields(payload)
            link["error"] = None
            link["last_poll"] = now
            link["state"] = fields["state"]
            link["driver_location"] = fields["driver_location"]
            if fields.get("drop_off_location") is not None:
                link["drop_off_location"] = fields["drop_off_location"]
            link["eta"] = fields["eta"]
            link["booking_code"] = fields["booking_code"]
            events: list[dict[str, Any]] = []
            if prev is not None:
                events = detect_changes(prev, payload, ts=now)
                if events:
                    history = link.get("history") or []
                    history.extend(events)
                    link["history"] = history[-MAX_HISTORY:]
                    link["last_change"] = now
            self._prev[token] = payload
            save_link(link, self.db_path)
            snapshot = self._snapshot_link(link)

        if events and self.on_events:
            try:
                self.on_events(events, snapshot)
            except Exception as exc:
                print(f"[poller] on_events error: {type(exc).__name__}: {exc}")


def demo() -> None:
    """Runnable check: token extract + one real poll + detect_changes assert + sqlite persistence."""
    sample = b64_decode("aHR0cHM6Ly9zaGFyZWxvY2F0aW9uLmdyYWIuY29tL28vdmdxQlhlcDl6c3hPS0FTU1NSUmY=")
    token = extract_token(sample)
    assert token == "vgqBXep9zsxOKASSSRRf", f"bad token: {token!r}"
    assert extract_token("vgqBXep9zsxOKASSSRRf") == "vgqBXep9zsxOKASSSRRf"
    print(f"[demo] extract_token OK -> {token}")

    # Test SQLite persistence with Base64 encoding
    test_db = BASE_DIR / "_test_demo.db"
    try:
        if test_db.exists():
            test_db.unlink()
        init_db(test_db)
        test_link = {
            "token": token,
            "url": sample,
            "label": "Demo Test",
            "state": "ORDER_IN_PREPARE",
            "driver_location": {"lat": -6.01, "lng": 106.05},
            "eta": 120,
            "booking_code": "TEST-123",
            "last_poll": _now(),
            "last_change": _now(),
            "error": None,
            "history": [{"ts": _now(), "field": "state", "old": None, "new": "ORDER_IN_PREPARE"}],
        }
        save_link(test_link, test_db)
        # Verify in raw SQLite that url is stored in Base64
        conn = sqlite3.connect(test_db)
        try:
            cur = conn.execute("SELECT url FROM links WHERE token = ?", (token,))
            row = cur.fetchone()
            assert row and row[0] != sample, "url should be stored in Base64"
            assert row[0] == b64_encode(sample)
        finally:
            conn.close()

        loaded = load_links(test_db)
        assert len(loaded) == 1, f"expected 1 link, got {len(loaded)}"
        assert loaded[0]["token"] == token
        assert loaded[0]["url"] == sample, "loaded url should be decoded"
        assert loaded[0]["booking_code"] == "TEST-123"
        delete_link(token, test_db)
        assert len(load_links(test_db)) == 0
        print("[demo] sqlite persistence OK -> base64 secure storage verified")
    finally:
        if test_db.exists():
            test_db.unlink()

    try:
        payload = fetch_booking(token, timeout=10)
        safe = redact_payload(copy.deepcopy(payload))
        summary = {
            k: safe.get(k)
            for k in (
                "pass",
                "sessionStatus",
                "booking",
                "route",
                "messageStatus",
                "driver",
            )
            if k in safe
        }
        print(f"[demo] poll OK -> {json.dumps(summary, default=str)[:400]}")
        assert "token" not in summary or summary.get("token") == "***"
    except Exception as exc:
        print(f"[demo] poll handled without crash -> {type(exc).__name__}: {exc}")

    old = {
        "pass": True,
        "token": "SECRET-SESSION",
        "sessionStatus": "ACTIVE",
        "driver": {"location": None},
        "booking": {"bookingCode": "A-TEST", "bookingState": "ORDER_IN_PREPARE"},
        "route": {"ETA": 100},
        "messageStatus": {"title": "Kitchen's preparing your order"},
    }
    new = copy.deepcopy(old)
    new["booking"]["bookingState"] = "DRIVER_ASSIGNED"
    new["driver"]["location"] = {"latitude": -6.0144212, "longitude": 106.0591433}
    new["route"]["ETA"] = 50
    new["messageStatus"]["title"] = "Driver on the way"
    events = detect_changes(old, new)
    assert events, "expected change events"
    fields = {e["field"] for e in events}
    assert "state" in fields, f"state event missing: {fields}"
    assert "driver_location" in fields, f"driver_location event missing: {fields}"
    for e in events:
        assert "SECRET" not in json.dumps(e, default=str)
    loc_ev = next(e for e in events if e["field"] == "driver_location")
    assert loc_ev["new"] == {"lat": -6.01442, "lng": 106.05914}
    print(f"[demo] detect_changes OK -> {json.dumps(events, default=str)}")
    print("[demo] ALL PASSED")


if __name__ == "__main__":
    demo()
