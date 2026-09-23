"""Telegram Bot API via raw HTTPS (requests only — no bot framework)."""

from __future__ import annotations

import html
import threading
from typing import Any

import requests

API_BASE = "https://api.telegram.org/bot{token}/{method}"
REQUEST_TIMEOUT = 15
PARSE_MODE = "HTML"

STATE_LABELS = {
    "ORDER_IN_PREPARE": "⏳ Pesanan sedang disiapkan",
    "DRIVER_ASSIGNED": "🛵 Driver assigned — menuju pickup",
    "ON_THE_WAY": "🛵 Menuju pickup",
    "ON_THE_WAY_TO_PICKUP": "🛵 Menuju pickup",
    "ON_THE_WAY_TO_DROPOFF": "📦 Menuju tujuan",
    "PICKED_UP": "📦 Sudah diambil — menuju tujuan",
    "ARRIVED": "📍 Driver sudah tiba",
    "COMPLETED": "✅ Pesanan selesai",
    "CANCELLED": "❌ Pesanan dibatalkan",
    "CANCELED": "❌ Pesanan dibatalkan",
    "CANCEL": "❌ Pesanan dibatalkan",
    "CANCEL_BY_CUSTOMER": "❌ Dibatalkan pelanggan",
    "CANCEL_BY_DRIVER": "❌ Dibatalkan driver",
    "CANCEL_BY_MERCHANT": "❌ Dibatalkan merchant",
    "REJECTED": "❌ Pesanan ditolak",
    "FAILED": "⚠️ Pesanan gagal",
}

# field -> (emoji, human title)
_FIELD_META = {
    "state": ("🔄", "Status pesanan"),
    "driver_location": ("📍", "Lokasi driver"),
    "eta": ("⏱", "ETA"),
    "sessionStatus": ("🔐", "Status sesi"),
    "messageStatus": ("💬", "Pesan"),
    "booking_code": ("🧾", "Kode booking"),
}


def _esc(value: Any) -> str:
    if value is None:
        return "—"
    return html.escape(str(value), quote=False)


def _fmt_value(field: str | None, value: Any) -> str:
    if value is None:
        return "—"
    if field == "driver_location" and isinstance(value, dict):
        lat, lng = value.get("lat"), value.get("lng")
        if lat is not None and lng is not None:
            return f"{lat}, {lng}"
    if field == "state":
        return state_label(value)
    return str(value)


def state_label(state: Any) -> str:
    if state is None:
        return "(belum ada data)"
    s = str(state)
    return STATE_LABELS.get(s, s)


def format_event(link: dict[str, Any], event: dict[str, Any]) -> str:
    """Multi-line HTML alert: bold label, emoji field title, old → new delta."""
    name = link.get("label") or link.get("token") or "?"
    code = link.get("booking_code")
    field = event.get("field")
    old = event.get("old")
    new = event.get("new")
    emoji, title = _FIELD_META.get(str(field), ("🔔", str(field)))
    head = f"<b>{_esc(name)}</b>"
    if code:
        head += f" · <code>{_esc(code)}</code>"
    old_s = _fmt_value(str(field) if field else None, old)
    new_s = _fmt_value(str(field) if field else None, new)
    msg = (
        f"{head}\n"
        f"{emoji} <b>{_esc(title)}</b>\n"
        f"<code>{_esc(old_s)}</code> → <code>{_esc(new_s)}</code>"
    )
    if field == "driver_location" and isinstance(new, dict):
        lat = new.get("lat") or new.get("latitude")
        lng = new.get("lng") or new.get("longitude")
        if lat is not None and lng is not None:
            osm_url = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lng}#map=16/{lat}/{lng}"
            msg += f'\n🗺 <a href="{osm_url}">Lihat di OpenStreetMap</a>'
    return msg


def send_message(bot_token: str, chat_id: Any, text: str) -> bool:
    """POST sendMessage (HTML). Empty bot_token → silent no-op (never raises)."""
    if not bot_token or chat_id in (None, ""):
        return False
    try:
        resp = requests.post(
            API_BASE.format(token=bot_token, method="sendMessage"),
            json={"chat_id": chat_id, "text": text, "parse_mode": PARSE_MODE},
            timeout=REQUEST_TIMEOUT,
        )
        return bool(resp.ok)
    except requests.RequestException as exc:
        # never echo bot_token
        print(f"[telegram] send failed: {type(exc).__name__}")
        return False


class TelegramBot:
    """Command loop: /add /list /stop /ping + push on monitor events."""

    def __init__(self, bot_token: str, chat_id: Any, monitor: Any) -> None:
        self.bot_token = bot_token or ""
        self.chat_id = chat_id
        self.monitor = monitor
        self._offset = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token)

    def send(self, text: str, chat_id: Any = None) -> bool:
        target = chat_id if chat_id not in (None, "") else self.chat_id
        return send_message(self.bot_token, target, text)

    def start(self) -> None:
        if not self.enabled:
            print(
                "[telegram] bot_token empty — Telegram disabled (dashboard-only mode)"
            )
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="grab-telegram", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def on_events(self, events: list[dict[str, Any]], link: dict[str, Any]) -> None:
        if not self.enabled:
            return
        for ev in events:
            self.send(format_event(link, ev))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                updates = self._get_updates()
            except requests.RequestException as exc:
                print(f"[telegram] poll failed: {type(exc).__name__}")
                self._stop.wait(3)
                continue
            except Exception as exc:
                print(f"[telegram] poll error: {type(exc).__name__}: {exc}")
                self._stop.wait(3)
                continue
            for upd in updates:
                self._handle_update(upd)

    def _get_updates(self) -> list[dict[str, Any]]:
        resp = requests.post(
            API_BASE.format(token=self.bot_token, method="getUpdates"),
            json={"offset": self._offset, "timeout": 30},
            timeout=REQUEST_TIMEOUT + 20,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or not data.get("ok"):
            return []
        results = data.get("result") or []
        out = []
        for upd in results:
            if isinstance(upd, dict):
                if "update_id" in upd:
                    self._offset = int(upd["update_id"]) + 1
                out.append(upd)
        return out

    def _handle_update(self, upd: dict[str, Any]) -> None:
        msg = upd.get("message")
        if not isinstance(msg, dict):
            return
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        chat_id = (msg.get("chat") or {}).get("id", self.chat_id)
        self._dispatch(chat_id, text)

    def _dispatch(self, chat_id: Any, text: str) -> None:
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@", 1)[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        try:
            if cmd == "/ping":
                self.send("🏓 Pong — bot aktif.", chat_id)
            elif cmd == "/add":
                self._cmd_add(chat_id, rest)
            elif cmd == "/list":
                self._cmd_list(chat_id)
            elif cmd == "/stop":
                self._cmd_stop(chat_id, rest)
            elif cmd == "/help":
                self.send(
                    "🤖 <b>GrabTrack bot</b>\n"
                    "• <code>/add &lt;url&gt; [label]</code> — tambah link\n"
                    "• <code>/list</code> — daftar link\n"
                    "• <code>/stop &lt;token|label&gt;</code> — berhenti monitor\n"
                    "• <code>/ping</code> — cek bot\n"
                    "• <code>/help</code> — bantuan ini",
                    chat_id,
                )
            else:
                self.send(
                    "Perintah: /add · /list · /stop · /ping · /help",
                    chat_id,
                )
        except Exception as exc:
            self.send(f"⚠️ Error: {_esc(type(exc).__name__)}: {_esc(exc)}", chat_id)

    def _cmd_add(self, chat_id: Any, rest: str) -> None:
        if not rest:
            self.send("Pakai: <code>/add &lt;url_grab&gt; [label]</code>", chat_id)
            return
        bits = rest.split(maxsplit=1)
        url = bits[0]
        label = bits[1].strip() if len(bits) > 1 else None
        try:
            link = self.monitor.add_link(url, label)
        except ValueError as exc:
            self.send(f"❌ {_esc(exc)}", chat_id)
            return
        shown = link.get("label") or link.get("token")
        self.send(f"✅ Ditambahkan: <b>{_esc(shown)}</b>", chat_id)

    def _cmd_list(self, chat_id: Any) -> None:
        links = self.monitor.status().get("links") or []
        if not links:
            self.send("Belum ada link.", chat_id)
            return
        lines = []
        for link in links:
            name = link.get("label") or link.get("token")
            st = state_label(link.get("state")) if link.get("state") else "…"
            err = link.get("error")
            line = f"• <b>{_esc(name)}</b> — {_esc(st)}"
            if err:
                line += f" · ⚠️ {_esc(err)}"
            lines.append(line)
        self.send("📋 <b>Daftar link</b>\n" + "\n".join(lines), chat_id)

    def _cmd_stop(self, chat_id: Any, rest: str) -> None:
        if not rest:
            self.send("Pakai: <code>/stop &lt;token|label&gt;</code>", chat_id)
            return
        removed = self.monitor.remove_by_token_or_label(rest)
        if removed is None:
            self.send(f"❌ Tidak ditemukan: {_esc(rest)}", chat_id)
            return
        name = removed.get("label") or removed.get("token")
        self.send(f"🛑 Dihentikan: <b>{_esc(name)}</b>", chat_id)


if __name__ == "__main__":
    # tiny self-check: import + format_event structure
    ev = format_event(
        {"label": "Test & Co", "booking_code": "G<1>"},
        {"field": "state", "old": "ORDER_IN_PREPARE", "new": "SOME_RAW"},
    )
    assert "<b>Test &amp; Co</b>" in ev and "<code>G&lt;1&gt;</code>" in ev
    assert "⏳" in ev and "SOME_RAW" in ev
    assert state_label(None) == "(belum ada data)"
    assert state_label("CANCEL_BY_DRIVER").startswith("❌")
    assert send_message("", "x", "hi") is False  # empty token → silent no-op
    print("[notifier] self-check OK")
