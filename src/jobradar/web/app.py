"""Local Flask adapter; dialogue, permissions and storage remain in services."""

import hmac
import json
import os
import secrets
import sqlite3
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, session, stream_with_context

from jobradar.orchestration.service import Orchestrator
from jobradar.profiles import ProfileError


def session_secret(env_path: Path):
    """Persist the signing key in the ignored .env so cookies survive restarts."""
    name = "JOBRADAR_SESSION_SECRET"
    if os.environ.get(name):
        return os.environ[name]
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == name and value.strip().strip("\"'"):
                return value.strip().strip("\"'")
    value = secrets.token_hex(32)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    with env_path.open("a", encoding="utf-8") as stream:
        stream.write(f"\n{name}={value}\n")
    return value


def create_app(database, settings, *, secret_key=None, client_factory=None):
    from jobradar.storage.search import recover_interrupted
    recover_interrupted(database)
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret_key or session_secret(settings.config_path.parent.parent / ".env"),
        MAX_CONTENT_LENGTH=256 * 1024,
        SESSION_COOKIE_NAME="jobradar_v01", SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict", TRUSTED_HOSTS=["localhost", "127.0.0.1", "[::1]"],
    )
    app.json.ensure_ascii = False
    instances, guard = {}, threading.Lock()
    app.extensions["jobradar_orchestrators"] = instances

    def orchestrator():
        with guard:
            session_id = session.get("chat_id")
            if session_id not in instances:
                instance = Orchestrator(database, settings, session_id=session_id,
                                        client=client_factory() if client_factory else None)
                instance.agent.manager.get_state()
                instances[instance.session_id] = instance
                session["chat_id"] = instance.session_id
            session.setdefault("csrf", secrets.token_urlsafe(32))
            session.permanent = True
            return instances[session["chat_id"]]

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        return response

    @app.errorhandler(ProfileError)
    @app.errorhandler(sqlite3.Error)
    def storage_error(error):
        return jsonify(error={"code":"SESSION_UNAVAILABLE", "message":"Сессия или база данных недоступны."}), 503

    @app.errorhandler(413)
    def too_large(error):
        return jsonify(error={"code":"MESSAGE_TOO_LONG", "message":"Сообщение слишком длинное."}), 413

    @app.get("/")
    def home():
        return render_template("index.html")

    @app.get("/api/state")
    def state():
        service = orchestrator()
        with service.lock:
            active = service.agent.manager.get_state()
            return jsonify(
                state={"active_context_id":active["active_context_id"],
                       "profession":active["profile"]["profile_data"].get("profession") if active["profile"] else None,
                       "context_name":active["context"]["name"] if active["context"] else None},
                profiles=service.agent.manager.list_profiles()["profiles"],
                history=service.agent.history.public_page(), csrf_token=session["csrf"],
            )

    @app.get("/api/history")
    def history():
        raw = request.args.get("before")
        if raw is not None and (not raw.isdecimal() or not 0 < int(raw) <= 2**63 - 1):
            return jsonify(error={"code":"VALIDATION_ERROR", "message":"Некорректная страница истории."}), 400
        service = orchestrator()
        with service.lock:
            return jsonify(service.agent.history.public_page(before_id=int(raw) if raw else None))

    @app.post("/api/chat")
    def chat():
        token = request.headers.get("X-JobRadar-CSRF", "")
        origin = request.headers.get("Origin")
        if (not token.isascii() or not session.get("csrf") or not hmac.compare_digest(token, session["csrf"])
                or (origin is not None and origin != request.host_url.rstrip("/"))):
            return jsonify(error={"code":"ACCESS_DENIED", "message":"Обновите страницу и повторите отправку."}), 403
        body = request.get_json(silent=True)
        if (not isinstance(body, dict) or set(body) != {"message"}
                or not isinstance(body["message"], str) or not body["message"].strip()):
            return jsonify(error={"code":"VALIDATION_ERROR", "message":"Введите текст сообщения."}), 400
        if len(body["message"]) > 60000:
            return jsonify(error={"code":"MESSAGE_TOO_LONG", "message":"Сообщение должно быть не длиннее 60 000 символов."}), 400
        service = orchestrator()
        # Synchronous execution, without workers or job queues. A second tab does
        # not enqueue another turn while the first one is awaiting DeepSeek.
        if not service.lock.acquire(blocking=False):
            return jsonify(error={"code":"CHAT_BUSY", "message":"Дождитесь ответа на предыдущее сообщение."}), 409
        if request.accept_mimetypes.best == "application/x-ndjson":
            released = False
            def release():
                nonlocal released
                if not released:
                    released = True
                    service.lock.release()
            @stream_with_context
            def stream():
                try:
                    yield json.dumps({"event":"accepted","data":{}},ensure_ascii=False) + "\n"
                    for event in service.events(body["message"].strip()):
                        if event["event"] == "answer":
                            result = event["data"]
                            event = {"event":"answer","data":{"answer":result["answer"],
                                     **({"error":result["error"]} if "error" in result else {})}}
                        yield json.dumps(event,ensure_ascii=False) + "\n"
                finally:
                    release()
            response = Response(stream(),mimetype="application/x-ndjson")
            response.call_on_close(release)
            return response
        try:
            result = service.chat(body["message"].strip())
            if "error" in result:
                return jsonify(error={**result["error"], "message":result["answer"]}), 503
            return jsonify(answer=result["answer"])
        finally:
            service.lock.release()

    return app
