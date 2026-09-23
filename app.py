"""Entrypoint: poller thread + Flask API + Telegram command loop."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from monitor import (
    Monitor,
    demo as monitor_demo,
    extract_token,
    fetch_booking,
    redact_payload,
)
from notifier import TelegramBot

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DASHBOARD_DIR = BASE_DIR / "dashboard"

DEFAULT_CONFIG: dict[str, Any] = {
    "bot_token": "",
    "chat_id": "",
    "poll_interval": 8,
    "port": 8600,
}


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update(raw)
        except (OSError, ValueError) as exc:
            print(f"[config] load failed ({type(exc).__name__}), using defaults")
    cfg["poll_interval"] = float(cfg.get("poll_interval") or 8)
    cfg["port"] = int(cfg.get("port") or 8600)
    cfg["bot_token"] = str(cfg.get("bot_token") or "")
    cfg["chat_id"] = cfg.get("chat_id") or ""
    return cfg


def create_app(monitor: Monitor, poll_interval: float) -> Flask:
    app = Flask(__name__)

    @app.get("/api/status")
    def api_status():
        status = monitor.status()
        status["poll_interval"] = poll_interval
        return jsonify(status)

    @app.post("/api/links")
    def api_add_link():
        body = request.get_json(silent=True) or {}
        url = (body.get("url") or "").strip()
        label = body.get("label")
        label = str(label).strip() if label else None
        if not url:
            return jsonify({"error": "url is required"}), 400
        try:
            link = monitor.add_link(url, label)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(link), 201

    @app.delete("/api/links/<token>")
    def api_delete_link(token: str):
        if not monitor.remove_link(token):
            return jsonify({"error": "not found"}), 404
        return jsonify({"ok": True, "token": token})

    @app.get("/")
    def index():
        return send_from_directory(str(DASHBOARD_DIR), "index.html")

    @app.get("/<path:filename>")
    def static_files(filename: str):
        target = DASHBOARD_DIR / filename
        if target.is_file():
            return send_from_directory(str(DASHBOARD_DIR), filename)
        return jsonify({"error": "not found"}), 404

    return app


def selftest() -> int:
    """python app.py --selftest — extract + one real poll + detect_changes."""
    import base64
    sample = base64.b64decode(
        b"aHR0cHM6Ly9zaGFyZWxvY2F0aW9uLmdyYWIuY29tL28vdmdxQlhlcDl6c3hPS0FTU1NSUmY="
    ).decode("utf-8")
    token = extract_token(sample)
    assert token == "vgqBXep9zsxOKASSSRRf", f"bad token: {token!r}"
    print(f"[selftest] extract_token OK -> {token}")

    try:
        payload = fetch_booking(token, timeout=10)
        safe = redact_payload(payload)
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
        print(f"[selftest] poll OK -> {json.dumps(summary, default=str)[:400]}")
    except Exception as exc:
        print(f"[selftest] poll handled -> {type(exc).__name__}: {exc}")

    monitor_demo()
    print("[selftest] ALL PASSED")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return selftest()

    cfg = load_config()
    monitor = Monitor(poll_interval=cfg["poll_interval"])
    bot = TelegramBot(cfg["bot_token"], cfg["chat_id"], monitor)
    monitor.on_events = bot.on_events
    monitor.start()
    bot.start()

    DASHBOARD_DIR.mkdir(exist_ok=True)
    stub = DASHBOARD_DIR / "index.html"
    if not stub.exists():
        stub.write_text(
            "<!doctype html><title>GrabTrack</title><p>dashboard placeholder</p>",
            encoding="utf-8",
        )

    app = create_app(monitor, cfg["poll_interval"])
    print(
        f"[app] http://127.0.0.1:{cfg['port']}  "
        f"interval={cfg['poll_interval']}s  "
        f"telegram={'on' if bot.enabled else 'off'}"
    )
    app.run(host="127.0.0.1", port=cfg["port"], threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
