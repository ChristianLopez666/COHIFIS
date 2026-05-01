from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("cohifis")

limiter = Limiter(key_func=get_remote_address, default_limits=[])


@dataclass(slots=True)
class AppConfig:
    app_name: str = "COHIFIS Vicky"
    environment: str = field(default_factory=lambda: os.getenv("FLASK_ENV", "production"))
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "5000")))

    allowed_origins: Tuple[str, ...] = field(default_factory=tuple)

    whatsapp_api_token: str = field(default_factory=lambda: os.getenv("WHATSAPP_API_TOKEN", "").strip())
    whatsapp_verify_token: str = field(default_factory=lambda: os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip())
    whatsapp_phone_id: str = field(default_factory=lambda: os.getenv("WHATSAPP_PHONE_ID", "").strip())
    whatsapp_api_version: str = field(default_factory=lambda: os.getenv("WHATSAPP_API_VERSION", "v17.0").strip())

    rate_limit_chat: str = field(default_factory=lambda: os.getenv("RATE_LIMIT_CHAT", "20 per minute").strip())
    rate_limit_lead: str = field(default_factory=lambda: os.getenv("RATE_LIMIT_LEAD", "10 per minute").strip())

    boardroom_engine_url: str = field(default_factory=lambda: os.getenv("BOARDROOM_ENGINE_URL", "https://boardroom-engine.onrender.com/api/decision/process").strip())
    boardroom_api_token: str = field(default_factory=lambda: os.getenv("BOARDROOM_API_TOKEN", "").strip())
    boardroom_timeout_seconds: int = field(default_factory=lambda: int(os.getenv("BOARDROOM_TIMEOUT_SECONDS", "15")))

    def __post_init__(self) -> None:
        origins = list(self.allowed_origins)
        env_origins_raw = os.getenv("ALLOWED_ORIGINS", "").strip()
        if env_origins_raw:
            origins.extend(origin.strip() for origin in env_origins_raw.split(",") if origin.strip())

        sanitized = []
        seen = set()
        for origin in origins or ["https://cohifis.com.mx"]:
            if not origin.startswith(("http://", "https://")):
                continue
            normalized = origin.rstrip("/")
            if normalized not in seen:
                sanitized.append(normalized)
                seen.add(normalized)

        if "https://cohifis.com.mx" not in seen:
            sanitized.append("https://cohifis.com.mx")
            seen.add("https://cohifis.com.mx")
        if "https://cohifis-web.onrender.com" not in seen:
            sanitized.append("https://cohifis-web.onrender.com")

        self.allowed_origins = tuple(sanitized)

    @property
    def whatsapp_api_url(self) -> str:
        return f"https://graph.facebook.com/{self.whatsapp_api_version}/{self.whatsapp_phone_id}/messages"

    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.whatsapp_api_token and self.whatsapp_phone_id)


def origin_is_allowed(origin: Optional[str], config: AppConfig) -> bool:
    if not origin:
        return config.environment == "development"
    incoming = urlparse(origin).netloc.lower()
    allowed = [urlparse(item).netloc.lower() for item in config.allowed_origins]
    return incoming in allowed


def send_whatsapp_text(config: AppConfig, phone: str, text: str) -> bool:
    if not config.whatsapp_configured:
        return False
    headers = {
        "Authorization": f"Bearer {config.whatsapp_api_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    try:
        response = requests.post(config.whatsapp_api_url, headers=headers, json=payload, timeout=config.boardroom_timeout_seconds)
        return bool(response.ok)
    except Exception:
        return False


def proxy_boardroom(config: AppConfig, *, mensaje: str, telefono: str, interes: str, source: str = "cohifis_web") -> str:
    headers = {"Content-Type": "application/json"}
    if config.boardroom_api_token:
        headers["X-Boardroom-Token"] = config.boardroom_api_token
        headers["Authorization"] = f"Bearer {config.boardroom_api_token}"

    payload = {
        "source": source,
        "telefono": telefono,
        "mensaje": mensaje,
        "interes": interes or "general",
    }

    try:
        response = requests.post(
            config.boardroom_engine_url,
            headers=headers,
            json=payload,
            timeout=config.boardroom_timeout_seconds,
        )
        data = response.json() if response.content else {}
        if isinstance(data, Mapping):
            answer = str(data.get("answer") or data.get("response") or "").strip()
            if answer:
                return answer
    except Exception as exc:
        logger.exception("Error Boardroom proxy: %s", exc)

    return (
        "Gracias por tu mensaje. Te puedo orientar sobre Seguro de Vida Temporal, "
        "coberturas y opciones según tu perfil. ¿Qué te gustaría revisar primero?"
    )


def create_app(config: Optional[AppConfig] = None) -> Flask:
    cfg = config or AppConfig()
    app = Flask(__name__)

    CORS(
        app,
        origins=list(cfg.allowed_origins),
        supports_credentials=True,
        allow_headers=["Content-Type", "Authorization"],
        methods=["GET", "POST", "OPTIONS"],
    )

    limiter.init_app(app)

    @app.before_request
    def enforce_origin_policy() -> Optional[Any]:
        if request.method == "OPTIONS":
            return None
        if request.path == "/api/whatsapp/webhook":
            return None
        if request.path.startswith("/api/"):
            if not origin_is_allowed(request.headers.get("Origin"), cfg):
                return jsonify({"error": "Origen no autorizado"}), 403
        return None

    @app.route("/api/health", methods=["GET"])
    def health() -> Any:
        return jsonify({"ok": True, "service": cfg.app_name, "allowed_origins": list(cfg.allowed_origins)}), 200

    @app.route("/api/v1/chat", methods=["POST", "OPTIONS"])
    @app.route("/api/v1/vicky-chat", methods=["POST", "OPTIONS"])
    @limiter.limit(cfg.rate_limit_chat)
    def vicky_chat() -> Any:
        if request.method == "OPTIONS":
            return jsonify({}), 200
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, Mapping):
            return jsonify({"error": "JSON malformado"}), 400
        context = payload.get("context") if isinstance(payload.get("context"), Mapping) else {}
        mensaje = str(payload.get("message", "")).strip()
        telefono = str(context.get("phone", "") or context.get("customer_id", "")).strip()
        interes = str(context.get("product_context", "") or context.get("segmento", "general")).strip() or "general"
        answer = proxy_boardroom(cfg, mensaje=mensaje, telefono=telefono, interes=interes)
        return jsonify({"answer": answer}), 200

    @app.route("/api/web-lead", methods=["POST", "OPTIONS"])
    @limiter.limit(cfg.rate_limit_lead)
    def web_lead() -> Any:
        if request.method == "OPTIONS":
            return jsonify({}), 200
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, Mapping):
            return jsonify({"error": "JSON malformado"}), 400
        mensaje = str(payload.get("message", "")).strip()
        telefono = str(payload.get("phone", "")).strip()
        interes = str(payload.get("product", "general")).strip() or "general"
        answer = proxy_boardroom(cfg, mensaje=mensaje, telefono=telefono, interes=interes)
        return jsonify({"ok": True, "answer": answer}), 201

    @app.route("/api/whatsapp/webhook", methods=["GET", "POST"])
    def whatsapp_webhook() -> Any:
        if request.method == "GET":
            mode = request.args.get("hub.mode", "")
            token = request.args.get("hub.verify_token", "")
            challenge = request.args.get("hub.challenge", "")
            if mode == "subscribe" and token and token == cfg.whatsapp_verify_token:
                return challenge, 200
            return jsonify({"error": "Verificación inválida"}), 403

        payload = request.get_json(silent=True) or {}
        try:
            value = payload["entry"][0]["changes"][0]["value"]
            messages = value.get("messages") or []
            if not isinstance(messages, list) or not messages:
                return jsonify({"status": "ignored"}), 200
            msg = messages[0] or {}
            telefono = str(msg.get("from", "")).strip()
            mensaje = str((msg.get("text") or {}).get("body", "")).strip()
            if not telefono or not mensaje:
                return jsonify({"status": "ignored"}), 200
            answer = proxy_boardroom(cfg, mensaje=mensaje, telefono=telefono, interes="general")
            if telefono:
                send_whatsapp_text(cfg, telefono, answer)
            return jsonify({"status": "processed"}), 200
        except Exception as exc:
            logger.exception("Webhook error: %s", exc)
            return jsonify({"status": "ignored"}), 200

    return app


app = create_app()

if __name__ == "__main__":
    config = AppConfig()
    app.run(host="0.0.0.0", port=config.port)
