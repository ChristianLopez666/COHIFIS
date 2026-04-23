from __future__ import annotations

"""
COHIFIS / Hydra-Boardroom
app.py

Reestructura del backend legacy de leads para convertir a Vicky en el
cerebro operativo conversacional sobre Flask + Boardroom Engine v1.

Supuestos explícitos:
- Los módulos Boardroom validados pueden vivir en `boardroom/` o en raíz del repo.
  Por compatibilidad se intenta importar primero desde `boardroom.*` y después desde
  módulos root con los nombres ya validados.
- La memoria de largo plazo usa Valkey cuando `VALKEY_URL` o `REDIS_URL` están
  disponibles; en ausencia de infraestructura externa cae en un cliente en memoria
  compatible con el contrato Boardroom.
- La integración DENUE vía Google Drive se deja preparada como placeholder funcional,
  sin prometer lectura real hasta contar con credenciales, ID de archivo y dictamen
  de auditoría sobre el esquema de datos.

Riesgos explícitos:
- `google-generativeai` puede no estar instalado o la API key puede no existir en
  todos los ambientes. El servicio responde con un fallback ejecutivo y trazable
  para no romper el flujo HTTP.
- El SDK de Google Drive puede no estar presente en contenedores mínimos; por eso la
  conexión DENUE no es obligatoria para responder chat.
- Este archivo conecta módulos Boardroom v1 a endpoints HTTP, pero no autoaprueba
  despliegues ni altera decisiones de negocio.
"""

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse
from uuid import uuid4

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# =============================================================================
# IMPORTS BOARDROOM - compatibilidad boardroom/* y módulos root
# =============================================================================
try:
    from boardroom.policy_envelope import (
        Agent,
        AuditBypassCounterScope,
        AuditTimeoutAction,
        Channel,
        Criticality,
        FallbackMode,
        NotificationChannel,
        PolicyEnvelope,
        Source,
        SystemOfRecord,
        TaskType,
    )
    from boardroom.task import Task, TaskState
    from boardroom.authority_matrix import (
        AuthorityActor,
        AuthorityEvent,
        AuthorityEventType,
        AuthorityLevel,
        EscalationReason,
        EventSeverity,
    )
    from boardroom.agent_contracts import AgentContractRegistry
    from boardroom.activation_criteria import ActivationCriteriaEngine
    from boardroom.fallback_manager import FallbackManager
    from boardroom.notifier import BoardroomNotifier, NotificationConfig
    from boardroom.valkey_store import (
        AgentTraceRecord,
        HealthMetricRecord,
        InMemoryValkeyClient,
        TraceStage,
        TraceStatus,
        ValkeyStore,
        ValkeyStoreConfig,
    )
    from boardroom.sheets_ledger import (
        LedgerIncident,
        IncidentCode,
        SheetsLedger,
        SheetsLedgerConfig,
        SheetsLedgerConfigurationError,
    )
    from boardroom.flow_gpt_claude import GPTClaudeFlow
    from boardroom.flow_gemini_gpt_claude import GeminiGPTClaudeFlow
    from boardroom.flow_gemma_decision_layer import GemmaDecisionLayerFlow
    from boardroom.boardroom_acceptance import (
        BoardroomAcceptanceRunner,
        DELIVERABLE_NEEDS_CLARIFICATION,
        DELIVERABLE_READY_FOR_AUDIT,
        REVIEW_READY_THRESHOLD,
    )
except ImportError:
    from policy_envelope import (
        Agent,
        AuditBypassCounterScope,
        AuditTimeoutAction,
        Channel,
        Criticality,
        FallbackMode,
        NotificationChannel,
        PolicyEnvelope,
        Source,
        SystemOfRecord,
        TaskType,
    )
    from task import Task, TaskState
    from authority_matrix import (
        AuthorityActor,
        AuthorityEvent,
        AuthorityEventType,
        AuthorityLevel,
        EscalationReason,
        EventSeverity,
    )
    from agent_contracts import AgentContractRegistry
    from activation_criteria import ActivationCriteriaEngine
    from fallback_manager import FallbackManager
    from notifier import BoardroomNotifier, NotificationConfig
    from valkey_store import (
        AgentTraceRecord,
        HealthMetricRecord,
        InMemoryValkeyClient,
        TraceStage,
        TraceStatus,
        ValkeyStore,
        ValkeyStoreConfig,
    )
    from sheets_ledger import (
        LedgerIncident,
        IncidentCode,
        SheetsLedger,
        SheetsLedgerConfig,
        SheetsLedgerConfigurationError,
    )
    from flow_gpt_claude import GPTClaudeFlow
    from flow_gemini_gpt_claude import GeminiGPTClaudeFlow
    from flow_gemma_decision_layer import GemmaDecisionLayerFlow
    from boardroom_acceptance import (
        BoardroomAcceptanceRunner,
        DELIVERABLE_NEEDS_CLARIFICATION,
        DELIVERABLE_READY_FOR_AUDIT,
        REVIEW_READY_THRESHOLD,
    )

try:
    import google.generativeai as genai  # type: ignore
except ImportError:  # pragma: no cover - depende del entorno
    genai = None

try:
    from google.oauth2 import service_account  # type: ignore
    from googleapiclient.discovery import build  # type: ignore
except ImportError:  # pragma: no cover - depende del entorno
    service_account = None
    build = None


# =============================================================================
# LOGGING
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("cohifis.vicky")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)


def _status_from_confidence(confidence: float) -> str:
    return (
        DELIVERABLE_READY_FOR_AUDIT
        if float(confidence) > float(REVIEW_READY_THRESHOLD)
        else DELIVERABLE_NEEDS_CLARIFICATION
    )


# =============================================================================
# DATACLASSES DE CONFIGURACIÓN Y CONTRATOS HTTP
# =============================================================================
@dataclass(slots=True)
class AppConfig:
    app_name: str = "COHIFIS Vicky Boardroom"
    environment: str = field(default_factory=lambda: os.getenv("FLASK_ENV", "production"))
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "5000")))

    allowed_origins: Tuple[str, ...] = field(default_factory=tuple)

    whatsapp_api_token: str = field(default_factory=lambda: os.getenv("WHATSAPP_API_TOKEN", "").strip())
    whatsapp_phone_id: str = field(default_factory=lambda: os.getenv("WHATSAPP_PHONE_ID", "").strip())
    whatsapp_api_version: str = field(default_factory=lambda: os.getenv("WHATSAPP_API_VERSION", "v17.0").strip())
    admin_phone: str = field(default_factory=lambda: os.getenv("ADMIN_PHONE", "").strip())

    valkey_url: Optional[str] = field(
        default_factory=lambda: (os.getenv("VALKEY_URL", "").strip() or os.getenv("REDIS_URL", "").strip() or None)
    )

    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", "").strip())
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-1.5-pro").strip())

    google_credentials_json: str = field(default_factory=lambda: os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip())
    boardroom_ledger_sheet_id: str = field(
        default_factory=lambda: (
            os.getenv("BOARDROOM_LEDGER_SHEET_ID", "").strip()
            or "15VE354to56g0Z8gQeCwFJ8HaKbrnEOgPtjdKAzX1bwQ"
        )
    )
    denue_drive_file_id: str = field(default_factory=lambda: os.getenv("DENUE_DRIVE_FILE_ID", "").strip())

    rate_limit_default: str = field(default_factory=lambda: os.getenv("RATE_LIMIT_DEFAULT", "200 per hour").strip())
    rate_limit_chat: str = field(default_factory=lambda: os.getenv("RATE_LIMIT_CHAT", "20 per minute").strip())
    rate_limit_lead: str = field(default_factory=lambda: os.getenv("RATE_LIMIT_LEAD", "10 per minute").strip())

    request_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))
    )
    memory_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("MEMORY_TTL_SECONDS", str(30 * 24 * 60 * 60)))
    )

    notifier_dry_run: bool = field(
        default_factory=lambda: os.getenv("NOTIFIER_DRY_RUN", "true").strip().lower() == "true"
    )
    sheets_dry_run: bool = field(
        default_factory=lambda: os.getenv("SHEETS_DRY_RUN", "true").strip().lower() == "true"
    )
    run_acceptance_on_health: bool = field(
        default_factory=lambda: os.getenv("RUN_ACCEPTANCE_ON_HEALTH", "false").strip().lower() == "true"
    )

    def __post_init__(self) -> None:
        if not isinstance(self.app_name, str) or not self.app_name.strip():
            raise ValueError("app_name must be a non-empty string")
        if not isinstance(self.environment, str) or not self.environment.strip():
            raise ValueError("environment must be a non-empty string")
        if not isinstance(self.port, int) or self.port < 1:
            raise ValueError("port must be an integer >= 1")
        if not isinstance(self.request_timeout_seconds, int) or self.request_timeout_seconds < 1:
            raise ValueError("request_timeout_seconds must be an integer >= 1")
        if not isinstance(self.memory_ttl_seconds, int) or self.memory_ttl_seconds < 60:
            raise ValueError("memory_ttl_seconds must be an integer >= 60")

        self.app_name = self.app_name.strip()
        self.environment = self.environment.strip().lower()
        self.whatsapp_api_version = self.whatsapp_api_version.strip().strip("/")

        origins = list(self.allowed_origins)
        env_origins_raw = os.getenv("ALLOWED_ORIGINS", "").strip()
        if env_origins_raw:
            origins.extend(origin.strip() for origin in env_origins_raw.split(",") if origin.strip())

        sanitized_origins: List[str] = []
        seen = set()
        for origin in origins or ["https://cohifis.com.mx"]:
            if not origin.startswith(("http://", "https://")):
                continue
            normalized = origin.strip().rstrip("/")
            if normalized and normalized not in seen:
                sanitized_origins.append(normalized)
                seen.add(normalized)

        # Regla obligatoria de seguridad: siempre incluir dominio principal COHIFIS
        if "https://cohifis.com.mx" not in seen:
            sanitized_origins.append("https://cohifis.com.mx")

        self.allowed_origins = tuple(sanitized_origins)

    @property
    def whatsapp_api_url(self) -> str:
        return f"https://graph.facebook.com/{self.whatsapp_api_version}/{self.whatsapp_phone_id}/messages"

    @property
    def limiter_storage_uri(self) -> str:
        return self.valkey_url or "memory://"

    @property
    def whatsapp_configured(self) -> bool:
        return bool(self.whatsapp_api_token and self.whatsapp_phone_id)

    @property
    def notifier_configured(self) -> bool:
        return bool(self.whatsapp_api_token and self.whatsapp_phone_id and self.admin_phone)

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls()

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["allowed_origins"] = list(self.allowed_origins)
        data["whatsapp_api_token"] = "***redacted***" if self.whatsapp_api_token else ""
        data["gemini_api_key"] = "***redacted***" if self.gemini_api_key else ""
        data["google_credentials_json"] = "***redacted***" if self.google_credentials_json else ""
        data["whatsapp_api_url"] = self.whatsapp_api_url
        data["whatsapp_configured"] = self.whatsapp_configured
        data["notifier_configured"] = self.notifier_configured
        data["limiter_storage_uri"] = self.limiter_storage_uri
        return data

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True)


@dataclass(slots=True)
class ChatContext:
    segmento: str = "general"
    city: str = ""
    customer_name: Optional[str] = None
    phone: Optional[str] = None
    customer_id: Optional[str] = None
    conversation_id: Optional[str] = None
    source_channel: str = "web"
    speech_enabled: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.segmento, str) or not self.segmento.strip():
            raise ValueError("segmento must be a non-empty string")
        if not isinstance(self.city, str):
            raise ValueError("city must be a string")
        if not isinstance(self.source_channel, str) or not self.source_channel.strip():
            raise ValueError("source_channel must be a non-empty string")
        if not isinstance(self.speech_enabled, bool):
            raise ValueError("speech_enabled must be a boolean")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be a dict")

        self.segmento = self.segmento.strip().lower()
        self.city = self.city.strip()
        self.source_channel = self.source_channel.strip().lower()
        self.customer_name = self._normalize_optional(self.customer_name)
        self.phone = self._normalize_optional(self.phone)
        self.customer_id = self._normalize_optional(self.customer_id)
        self.conversation_id = self._normalize_optional(self.conversation_id)

    @staticmethod
    def _normalize_optional(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("optional value must be a string if provided")
        clean = value.strip()
        return clean or None

    @classmethod
    def from_mapping(cls, data: Optional[Mapping[str, Any]]) -> "ChatContext":
        raw = dict(data or {})
        return cls(
            segmento=str(raw.get("segmento", "general") or "general"),
            city=str(raw.get("city", "") or ""),
            customer_name=raw.get("customer_name") or raw.get("name"),
            phone=raw.get("phone"),
            customer_id=raw.get("customer_id"),
            conversation_id=raw.get("conversation_id"),
            source_channel=str(raw.get("source_channel", raw.get("channel", "web")) or "web"),
            speech_enabled=bool(raw.get("speech_enabled", False)),
            metadata={k: v for k, v in raw.items() if k not in {
                "segmento", "city", "customer_name", "name", "phone",
                "customer_id", "conversation_id", "source_channel", "channel", "speech_enabled"
            }},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "segmento": self.segmento,
            "city": self.city,
            "customer_name": self.customer_name,
            "phone": self.phone,
            "customer_id": self.customer_id,
            "conversation_id": self.conversation_id,
            "source_channel": self.source_channel,
            "speech_enabled": self.speech_enabled,
            "metadata": self.metadata,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True)


@dataclass(slots=True)
class ChatRequestPayload:
    message: str
    context: ChatContext = field(default_factory=ChatContext)
    request_id: str = field(default_factory=lambda: str(uuid4()))

    def __post_init__(self) -> None:
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("message must be a non-empty string")
        if not isinstance(self.context, ChatContext):
            raise ValueError("context must be a ChatContext")
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        self.message = self.message.strip()
        self.request_id = self.request_id.strip()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ChatRequestPayload":
        if not isinstance(data, Mapping):
            raise ValueError("payload must be a mapping")
        return cls(
            message=str(data.get("message", "") or ""),
            context=ChatContext.from_mapping(data.get("context")),
            request_id=str(data.get("request_id", "") or str(uuid4())),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message": self.message,
            "context": self.context.to_dict(),
            "request_id": self.request_id,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True)


@dataclass(slots=True)
class ProductRecommendation:
    code: str
    title: str
    priority: int
    rationale: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("code must be a non-empty string")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("title must be a non-empty string")
        if not isinstance(self.priority, int) or self.priority < 1:
            raise ValueError("priority must be an integer >= 1")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ValueError("rationale must be a non-empty string")

        self.code = self.code.strip()
        self.title = self.title.strip()
        self.rationale = self.rationale.strip()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "priority": self.priority,
            "rationale": self.rationale,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True)


@dataclass(slots=True)
class ConversationMemory:
    conversation_id: str
    customer_name: Optional[str] = None
    customer_need: Optional[str] = None
    segmento: str = "general"
    city: str = ""
    phone: Optional[str] = None
    turns: List[Dict[str, str]] = field(default_factory=list)
    last_products: List[str] = field(default_factory=list)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not isinstance(self.conversation_id, str) or not self.conversation_id.strip():
            raise ValueError("conversation_id must be a non-empty string")
        if not isinstance(self.segmento, str) or not self.segmento.strip():
            raise ValueError("segmento must be a non-empty string")
        if not isinstance(self.city, str):
            raise ValueError("city must be a string")
        if not isinstance(self.turns, list):
            raise ValueError("turns must be a list")
        if not isinstance(self.last_products, list):
            raise ValueError("last_products must be a list")
        if not isinstance(self.updated_at, datetime) or self.updated_at.tzinfo is None:
            raise ValueError("updated_at must be a timezone-aware datetime")

        self.conversation_id = self.conversation_id.strip()
        self.segmento = self.segmento.strip().lower()
        self.city = self.city.strip()
        self.customer_name = self._normalize_optional(self.customer_name)
        self.customer_need = self._normalize_optional(self.customer_need)
        self.phone = self._normalize_optional(self.phone)

    @staticmethod
    def _normalize_optional(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("optional value must be a string if provided")
        clean = value.strip()
        return clean or None

    def register_turn(self, role: str, content: str) -> None:
        if not isinstance(role, str) or not role.strip():
            raise ValueError("role must be a non-empty string")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        self.turns.append({"role": role.strip(), "content": content.strip()})
        self.turns = self.turns[-12:]
        self.updated_at = utc_now()

    def apply_context(self, context: ChatContext) -> None:
        if context.customer_name and not self.customer_name:
            self.customer_name = context.customer_name
        if context.city:
            self.city = context.city
        if context.phone:
            self.phone = normalize_phone(context.phone)
        if context.segmento:
            self.segmento = context.segmento
        self.updated_at = utc_now()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "conversation_id": self.conversation_id,
            "customer_name": self.customer_name,
            "customer_need": self.customer_need,
            "segmento": self.segmento,
            "city": self.city,
            "phone": self.phone,
            "turns": self.turns,
            "last_products": self.last_products,
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ConversationMemory":
        return cls(
            conversation_id=str(data["conversation_id"]),
            customer_name=data.get("customer_name"),
            customer_need=data.get("customer_need"),
            segmento=str(data.get("segmento", "general") or "general"),
            city=str(data.get("city", "") or ""),
            phone=data.get("phone"),
            turns=list(data.get("turns", [])),
            last_products=list(data.get("last_products", [])),
            updated_at=datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else utc_now(),
        )

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True)


@dataclass(slots=True)
class ChatResponseEnvelope:
    ok: bool
    request_id: str
    conversation_id: str
    task_id: str
    answer: str
    selected_flow: str
    selected_actor: str
    deliverable_status: str
    confidence: float
    product_priority: List[Dict[str, Any]] = field(default_factory=list)
    memory: Dict[str, Any] = field(default_factory=dict)
    routing: Dict[str, Any] = field(default_factory=dict)
    boardroom: Dict[str, Any] = field(default_factory=dict)
    denue: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not isinstance(self.ok, bool):
            raise ValueError("ok must be a boolean")
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise ValueError("request_id must be a non-empty string")
        if not isinstance(self.conversation_id, str) or not self.conversation_id.strip():
            raise ValueError("conversation_id must be a non-empty string")
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(self.answer, str) or not self.answer.strip():
            raise ValueError("answer must be a non-empty string")
        if not isinstance(self.selected_flow, str) or not self.selected_flow.strip():
            raise ValueError("selected_flow must be a non-empty string")
        if not isinstance(self.selected_actor, str) or not self.selected_actor.strip():
            raise ValueError("selected_actor must be a non-empty string")
        if self.deliverable_status not in {DELIVERABLE_READY_FOR_AUDIT, DELIVERABLE_NEEDS_CLARIFICATION}:
            raise ValueError("deliverable_status is invalid")
        if not isinstance(self.confidence, (int, float)) or not (0.0 <= float(self.confidence) <= 1.0):
            raise ValueError("confidence must be between 0.0 and 1.0")
        if not isinstance(self.product_priority, list):
            raise ValueError("product_priority must be a list")
        if not isinstance(self.memory, dict):
            raise ValueError("memory must be a dict")
        if not isinstance(self.routing, dict):
            raise ValueError("routing must be a dict")
        if not isinstance(self.boardroom, dict):
            raise ValueError("boardroom must be a dict")
        if not isinstance(self.denue, dict):
            raise ValueError("denue must be a dict")
        if not isinstance(self.timestamp, datetime) or self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")

        self.request_id = self.request_id.strip()
        self.conversation_id = self.conversation_id.strip()
        self.task_id = self.task_id.strip()
        self.answer = self.answer.strip()
        self.selected_flow = self.selected_flow.strip()
        self.selected_actor = self.selected_actor.strip()
        self.confidence = round(float(self.confidence), 4)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "request_id": self.request_id,
            "conversation_id": self.conversation_id,
            "task_id": self.task_id,
            "answer": self.answer,
            "selected_flow": self.selected_flow,
            "selected_actor": self.selected_actor,
            "deliverable_status": self.deliverable_status,
            "confidence": self.confidence,
            "product_priority": self.product_priority,
            "memory": self.memory,
            "routing": self.routing,
            "boardroom": self.boardroom,
            "denue": self.denue,
            "timestamp": self.timestamp.isoformat(),
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True)


# =============================================================================
# HELPERS DE NEGOCIO / SEGMENTACIÓN
# =============================================================================
IMSS_SEGMENT_KEYWORDS = frozenset({"imss", "ley 73", "jubilación", "pensión"})

@dataclass(slots=True)
class ProductAuthority:
    credito_imss_cat: float = 29.3
    credito_imss_cat_competencia: float = 75.19
    credito_imss_monto_min: float = 40_000.0
    credito_imss_monto_max: float = 650_000.0
    credito_imss_gancho: str = "VRIM Plus de regalo en créditos de $50,000 o más"
    credito_pyme_tasa_alta_eficiencia: float = 18.0
    credito_pyme_tasa_flexible: float = 36.0
    tpv_tasa_basica: float = 1.05
    tpv_tasa_premium: float = 1.35
    inburpyme_bono_por_15_negocios: float = 40_000.0
    vrim_gancho: str = "Membresía médica Inbursa"
    vrim_aplica_regalo_con_imss: bool = True

    def __post_init__(self) -> None:
        for field_name in (
            "credito_imss_cat",
            "credito_imss_cat_competencia",
            "credito_imss_monto_min",
            "credito_imss_monto_max",
            "credito_pyme_tasa_alta_eficiencia",
            "credito_pyme_tasa_flexible",
            "tpv_tasa_basica",
            "tpv_tasa_premium",
            "inburpyme_bono_por_15_negocios",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, (int, float)) or float(value) <= 0:
                raise ValueError(f"{field_name} must be a positive number")
            setattr(self, field_name, float(value))

        for field_name in ("credito_imss_gancho", "vrim_gancho"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
            setattr(self, field_name, value.strip())

        if not isinstance(self.vrim_aplica_regalo_con_imss, bool):
            raise ValueError("vrim_aplica_regalo_con_imss must be a boolean")

    def cat_advantage_imss(self) -> str:
        diff = round(self.credito_imss_cat_competencia - self.credito_imss_cat, 2)
        return (
            f"Con Inbursa pagas CAT {self.credito_imss_cat}% "
            f"vs {self.credito_imss_cat_competencia}% de la competencia. "
            f"Eso es {diff}% menos en tu crédito."
        )

    def tpv_pitch(self) -> str:
        return (
            f"Terminal punto de venta desde {self.tpv_tasa_basica}% "
            f"por transacción. Sin mensualidad fija."
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "credito_imss_cat": self.credito_imss_cat,
            "credito_imss_cat_competencia": self.credito_imss_cat_competencia,
            "credito_imss_monto_min": self.credito_imss_monto_min,
            "credito_imss_monto_max": self.credito_imss_monto_max,
            "credito_imss_gancho": self.credito_imss_gancho,
            "credito_pyme_tasa_alta_eficiencia": self.credito_pyme_tasa_alta_eficiencia,
            "credito_pyme_tasa_flexible": self.credito_pyme_tasa_flexible,
            "tpv_tasa_basica": self.tpv_tasa_basica,
            "tpv_tasa_premium": self.tpv_tasa_premium,
            "inburpyme_bono_por_15_negocios": self.inburpyme_bono_por_15_negocios,
            "vrim_gancho": self.vrim_gancho,
            "vrim_aplica_regalo_con_imss": self.vrim_aplica_regalo_con_imss,
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True)


PRODUCT_AUTHORITY = ProductAuthority()

PRODUCT_CATALOG: Dict[str, List[ProductRecommendation]] = {
    "imss": [
        ProductRecommendation(
            code="credito_imss_ley_73",
            title="Crédito IMSS Ley 73",
            priority=1,
            rationale=(
                f"CAT {PRODUCT_AUTHORITY.credito_imss_cat}% vs "
                f"{PRODUCT_AUTHORITY.credito_imss_cat_competencia}% competencia. "
                f"{PRODUCT_AUTHORITY.credito_imss_gancho}."
            ),
        ),
        ProductRecommendation(
            code="seguro_vida",
            title="Seguro de Vida",
            priority=2,
            rationale="Complementa protección familiar y planeación patrimonial.",
        ),
        ProductRecommendation(
            code="plan_retiro",
            title="Planeación para Retiro",
            priority=3,
            rationale="Fortalece continuidad financiera y orden patrimonial.",
        ),
    ],
    "general": [
        ProductRecommendation(
            code="seguro_vida",
            title="Seguro de Vida",
            priority=1,
            rationale="Producto transversal para protección patrimonial y familiar.",
        ),
        ProductRecommendation(
            code="seguro_auto",
            title="Seguro Auto",
            priority=2,
            rationale="Cobertura rápida para necesidades frecuentes del cliente.",
        ),
        ProductRecommendation(
            code="credito_personal",
            title="Crédito Personal",
            priority=3,
            rationale="Opción útil cuando el cliente busca liquidez o consolidación.",
        ),
    ],
}


def get_allowed_origins(config: AppConfig) -> List[str]:
    return list(config.allowed_origins)


def origin_is_allowed(origin: Optional[str], config: AppConfig) -> bool:
    if not origin:
        return config.environment == "development"

    try:
        incoming = urlparse(origin).netloc.lower()
    except Exception:
        logger.warning("No se pudo parsear origin=%s", origin)
        return False

    allowed_domains = []
    for allowed in config.allowed_origins:
        try:
            allowed_domains.append(urlparse(allowed).netloc.lower())
        except Exception:
            continue

    return incoming in allowed_domains


def normalize_phone(phone: str) -> str:
    clean_phone = re.sub(r"\D", "", str(phone or ""))
    if len(clean_phone) == 10:
        return f"52{clean_phone}"
    return clean_phone


_RFC_PF_RE = re.compile(r"^[A-ZÑ&]{4}\d{6}[A-Z0-9]{3}$", re.IGNORECASE)
_RFC_PM_RE = re.compile(r"^[A-ZÑ&]{3}\d{6}[A-Z0-9]{3}$", re.IGNORECASE)


def validate_rfc(rfc: str) -> Tuple[bool, str]:
    if not rfc or not isinstance(rfc, str):
        return False, "RFC requerido para leads empresariales"
    clean = re.sub(r"\s+", "", rfc.upper())
    if _RFC_PF_RE.match(clean) or _RFC_PM_RE.match(clean):
        return True, clean
    return False, (
        f"RFC inválido: '{rfc}'. "
        "Formato esperado: XXXX000000XXX (física) o XXX000000XXX (moral)"
    )


def extract_customer_name(message: str) -> Optional[str]:
    patterns = [
        r"\bme llamo\s+([A-Za-zÁÉÍÓÚÑáéíóúñ ]{2,60})",
        r"\bsoy\s+([A-Za-zÁÉÍÓÚÑáéíóúñ ]{2,60})",
        r"\bmi nombre es\s+([A-Za-zÁÉÍÓÚÑáéíóúñ ]{2,60})",
    ]
    for pattern in patterns:
        match = re.search(pattern, message, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip(" .,:;")
    return None


def extract_customer_need(message: str) -> Optional[str]:
    patterns = [
        r"\bnecesito\s+(.+)",
        r"\bquiero\s+(.+)",
        r"\bbusco\s+(.+)",
        r"\bme interesa\s+(.+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, message, flags=re.IGNORECASE)
        if match:
            need = match.group(1).strip(" .,:;")
            return need[:200]
    return None


def infer_segment(message: str, context: ChatContext) -> str:
    raw = f"{context.segmento} {message}".lower()
    if any(keyword in raw for keyword in IMSS_SEGMENT_KEYWORDS):
        return "imss"
    return context.segmento or "general"


def build_product_priority(segmento: str) -> List[ProductRecommendation]:
    products = PRODUCT_CATALOG.get(segmento, PRODUCT_CATALOG["general"])
    return sorted(products, key=lambda item: item.priority)


def compute_request_confidence(message: str, context: ChatContext) -> float:
    score = 0.72
    if len(message.strip()) >= 10:
        score += 0.08
    if context.segmento:
        score += 0.05
    if context.city:
        score += 0.04
    if context.customer_name or extract_customer_name(message):
        score += 0.04
    if extract_customer_need(message):
        score += 0.03
    return min(round(score, 4), 0.96)


# =============================================================================
# MEMORIA DE LARGO PLAZO
# =============================================================================
@dataclass(slots=True)
class ConversationMemoryStore:
    valkey_store: ValkeyStore
    ttl_seconds: int = 30 * 24 * 60 * 60
    key_prefix: str = "vicky:memory"

    def __post_init__(self) -> None:
        if not isinstance(self.valkey_store, ValkeyStore):
            raise ValueError("valkey_store must be a ValkeyStore")
        if not isinstance(self.ttl_seconds, int) or self.ttl_seconds < 60:
            raise ValueError("ttl_seconds must be an integer >= 60")
        if not isinstance(self.key_prefix, str) or not self.key_prefix.strip():
            raise ValueError("key_prefix must be a non-empty string")
        self.key_prefix = self.key_prefix.strip()

    def _key(self, conversation_id: str) -> str:
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id must be a non-empty string")
        return f"{self.key_prefix}:{conversation_id.strip()}"

    def load(self, conversation_id: str) -> Optional[ConversationMemory]:
        raw = self.valkey_store.client.get(self._key(conversation_id))
        if raw is None:
            return None
        try:
            return ConversationMemory.from_dict(json.loads(raw))
        except Exception as exc:
            logger.warning("No se pudo reconstruir memoria conversation_id=%s error=%s", conversation_id, exc)
            return None

    def save(self, memory: ConversationMemory) -> ConversationMemory:
        self.valkey_store.client.set(
            self._key(memory.conversation_id),
            memory.to_json(indent=None),
            ex=self.ttl_seconds,
        )
        return memory


# =============================================================================
# CONECTOR GOOGLE DRIVE / DENUE PLACEHOLDER
# =============================================================================
@dataclass(slots=True)
class VickySECOMDriveConnector:
    config: AppConfig

    def __post_init__(self) -> None:
        if not isinstance(self.config, AppConfig):
            raise ValueError("config must be an AppConfig")

    def _build_drive_service(self) -> Optional[Any]:
        if not self.config.google_credentials_json or service_account is None or build is None:
            return None
        try:
            info = json.loads(self.config.google_credentials_json)
            credentials = service_account.Credentials.from_service_account_info(
                info,
                scopes=["https://www.googleapis.com/auth/drive.readonly"],
            )
            return build("drive", "v3", credentials=credentials, cache_discovery=False)
        except Exception as exc:
            logger.warning("Drive service no disponible: %s", exc)
            return None

    def read_denue_placeholder(self, *, city: str, segmento: str) -> Dict[str, Any]:
        """
        Placeholder estructural para Vicky SECOM / Google Drive.
        No promete lectura real de DENUE hasta recibir file_id, permisos y dictamen.
        """
        service = self._build_drive_service()
        service_available = service is not None and bool(self.config.denue_drive_file_id)

        result = {
            "enabled": service_available,
            "status": "placeholder",
            "city": city,
            "segmento": segmento,
            "drive_file_id_present": bool(self.config.denue_drive_file_id),
            "records": [],
            "notes": [
                "Estructura lista para conectar Google Drive API.",
                "Pendiente lectura real de base DENUE desde archivo gobernado por Vicky SECOM.",
            ],
        }

        # Riesgo controlado:
        # aún con service account válido, no se consulta un archivo real si no existe DENUE_DRIVE_FILE_ID.
        return result


# =============================================================================
# CLIENTE GEMINI / FALLBACK EJECUTIVO
# =============================================================================
@dataclass(slots=True)
class GeminiExecutiveClient:
    config: AppConfig

    def __post_init__(self) -> None:
        if not isinstance(self.config, AppConfig):
            raise ValueError("config must be an AppConfig")

    def is_available(self) -> bool:
        return bool(genai is not None and self.config.gemini_api_key)

    def generate(self, prompt: str) -> Tuple[str, Dict[str, Any]]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")

        if not self.is_available():
            raise RuntimeError("Gemini no configurado o SDK no disponible")

        start = time.perf_counter()
        genai.configure(api_key=self.config.gemini_api_key)
        model = genai.GenerativeModel(self.config.gemini_model)
        response = model.generate_content(prompt)
        latency_ms = int((time.perf_counter() - start) * 1000)

        text = getattr(response, "text", "") or ""
        if not text and getattr(response, "candidates", None):
            parts: List[str] = []
            for candidate in response.candidates:
                content = getattr(candidate, "content", None)
                if not content:
                    continue
                for part in getattr(content, "parts", []) or []:
                    part_text = getattr(part, "text", None)
                    if part_text:
                        parts.append(part_text)
            text = "\n".join(parts)

        if not text.strip():
            raise RuntimeError("Gemini devolvió respuesta vacía")

        return text.strip(), {
            "provider": "gemini",
            "model": self.config.gemini_model,
            "latency_ms": latency_ms,
        }

    def executive_fallback(
        self,
        *,
        message: str,
        memory: ConversationMemory,
        products: Sequence[ProductRecommendation],
        city: str,
    ) -> str:
        salutation = f"Hola {memory.customer_name}," if memory.customer_name else "Hola,"
        city_phrase = f" en {city}" if city else ""
        focus = products[0].title if products else "la opción financiera adecuada"
        need_phrase = (
            f" Entiendo que tu prioridad es {memory.customer_need}."
            if memory.customer_need
            else " Quiero ayudarte a ubicar la alternativa financiera más conveniente."
        )
        return (
            f"{salutation} gracias por escribir a COHIFIS{city_phrase}.{need_phrase} "
            f"Por el contexto que compartes, conviene revisar primero {focus}. "
            f"Si me confirmas tu edad, régimen o necesidad puntual, te preparo una orientación más precisa, "
            f"ejecutiva y aterrizada a tu perfil."
        )

    def build_prompt(
        self,
        *,
        request_payload: ChatRequestPayload,
        memory: ConversationMemory,
        products: Sequence[ProductRecommendation],
        denue_context: Mapping[str, Any],
    ) -> str:
        product_lines = "\n".join(
            f"- {item.priority}. {item.title}: {item.rationale}" for item in products
        ) or "- Sin prioridad específica"

        authority_context = (
            f"Datos financieros verificados COHIFIS:\n"
            f"- Crédito IMSS: CAT {PRODUCT_AUTHORITY.credito_imss_cat}% "
            f"(vs {PRODUCT_AUTHORITY.credito_imss_cat_competencia}% competencia)\n"
            f"- Monto IMSS: ${PRODUCT_AUTHORITY.credito_imss_monto_min:,.0f} "
            f"a ${PRODUCT_AUTHORITY.credito_imss_monto_max:,.0f}\n"
            f"- Gancho IMSS: {PRODUCT_AUTHORITY.credito_imss_gancho}\n"
            f"- TPV: desde {PRODUCT_AUTHORITY.tpv_tasa_basica}% por transacción\n"
            f"- PyME tasa eficiente: {PRODUCT_AUTHORITY.credito_pyme_tasa_alta_eficiencia}%\n"
            f"- Bono Inburpyme: ${PRODUCT_AUTHORITY.inburpyme_bono_por_15_negocios:,.0f} "
            f"por cada 15 negocios afiliados"
        )

        denue_json = _safe_json(denue_context)
        memory_json = _safe_json(memory.to_dict())
        tone = (
            "Responde en tono ejecutivo, profesional y persuasivo."
            if not request_payload.context.speech_enabled
            else "Responde con claridad, cercanía y precisión operativa."
        )

        return f"""
Eres Vicky, asistente ejecutiva de COHIFIS. Tu función es orientar con criterio comercial serio,
sin inventar aprobaciones, sin prometer resultados garantizados y sin sustituir validación humana.
{tone}

Contexto del cliente:
- Segmento: {request_payload.context.segmento}
- Ciudad: {request_payload.context.city or "No especificada"}
- Canal: {request_payload.context.source_channel}
- Nombre conocido: {memory.customer_name or "No identificado"}
- Necesidad conocida: {memory.customer_need or "No identificada"}

Memoria vigente:
{memory_json}

Prioridad de productos:
{product_lines}

{authority_context}

Contexto DENUE / Vicky SECOM:
{denue_json}

Mensaje del cliente:
{request_payload.message}

Instrucciones:
1. Responde en español.
2. Mantén la respuesta breve, accionable y comercialmente útil.
3. Si el segmento es IMSS, prioriza Crédito IMSS Ley 73 antes que otros productos.
4. Si faltan datos clave, pide solo los mínimos necesarios.
5. Nunca apruebes despliegues, dinero ni decisiones finales.
6. Si el cliente está listo para avanzar o pregunta qué sigue, lista los requisitos según segmento:
   - IMSS: INE vigente, Comprobante de domicilio (máx 3 meses), 3 últimos estados de cuenta de pensión, CURP.
   - Empresarial/B2B: INE del representante legal, RFC válido, Estados financieros recientes, Comprobante domicilio fiscal.
   - General: INE vigente, Comprobante de domicilio, Estado de cuenta bancario reciente.
   Finaliza indicando: "Ten estos documentos listos en PDF o foto clara. Christian López te contactará por WhatsApp para recibirlos digitalmente y agendar tu cierre sin que tengas que salir de casa."
7. Triage estricto por segmento:
   - Si segmento es "imss": NUNCA mencionar tasas empresariales, TPV, RFC ni productos B2B.
   - Si segmento es "empresarial" o "b2b": NUNCA mencionar Ley 73, pensión IMSS ni VRIM como producto principal.
   - Siempre respetar el segmento declarado en el contexto.
""".strip()


# =============================================================================
# SERVICIOS WHATSAPP / PERSISTENCIA LEGACY
# =============================================================================
def send_whatsapp_template(config: AppConfig, phone: str, name: str, product_name: str) -> bool:
    if not config.whatsapp_configured:
        logger.warning("WhatsApp no configurado para templates")
        return False

    payload = {
        "messaging_product": "whatsapp",
        "to": normalize_phone(phone),
        "type": "template",
        "template": {
            "name": "lead_bienvenida",
            "language": {"code": "es_MX"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": name},
                        {"type": "text", "text": product_name},
                    ],
                }
            ],
        },
    }
    headers = {
        "Authorization": f"Bearer {config.whatsapp_api_token}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(
            config.whatsapp_api_url,
            headers=headers,
            json=payload,
            timeout=config.request_timeout_seconds,
        )
        if response.status_code == 200:
            return True
        logger.error("WhatsApp template error status=%s body=%s", response.status_code, response.text[:500])
        return False
    except Exception as exc:
        logger.error("WhatsApp template exception: %s", exc)
        return False


def send_whatsapp_text(config: AppConfig, phone: str, text: str) -> bool:
    if not config.whatsapp_configured:
        logger.warning("WhatsApp no configurado para textos")
        return False

    payload = {
        "messaging_product": "whatsapp",
        "to": normalize_phone(phone),
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }
    headers = {
        "Authorization": f"Bearer {config.whatsapp_api_token}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(
            config.whatsapp_api_url,
            headers=headers,
            json=payload,
            timeout=config.request_timeout_seconds,
        )
        if response.status_code == 200:
            return True
        logger.error("WhatsApp text error status=%s body=%s", response.status_code, response.text[:500])
        return False
    except Exception as exc:
        logger.error("WhatsApp text exception: %s", exc)
        return False


def validate_lead_payload(data: Mapping[str, Any]) -> Tuple[bool, str]:
    if not isinstance(data, Mapping):
        return False, "Payload debe ser objeto JSON"
    lead = data.get("lead")
    if not isinstance(lead, Mapping):
        return False, "Falta objeto 'lead'"

    required_fields = ("name", "phone", "service")
    for field_name in required_fields:
        if not str(lead.get(field_name, "") or "").strip():
            return False, f"Campo requerido faltante: {field_name}"

    phone = re.sub(r"\D", "", str(lead.get("phone", "")))
    if len(phone) != 10:
        return False, "Teléfono debe tener 10 dígitos"

    service_lower = str(lead.get("service", "") or "").lower()
    is_b2b = any(k in service_lower for k in (
        "empresarial", "pyme", "tpv", "terminal", "financiamiento", "b2b"
    ))
    if is_b2b:
        rfc_raw = str(lead.get("rfc", "") or "").strip()
        rfc_valid, rfc_result = validate_rfc(rfc_raw)
        if not rfc_valid:
            return False, rfc_result

    return True, phone


# =============================================================================
# NÚCLEO OPERATIVO VICKY / HYDRA-BOARDROOM
# =============================================================================
@dataclass(slots=True)
class VickyBoardroomService:
    config: AppConfig
    orchestrator: Any
    valkey_store: ValkeyStore
    memory_store: ConversationMemoryStore
    gemini_client: GeminiExecutiveClient
    drive_connector: VickySECOMDriveConnector
    ledger: Optional[SheetsLedger] = None
    notifier: Optional[BoardroomNotifier] = None
    acceptance_runner: Optional[BoardroomAcceptanceRunner] = None
    flow_registry: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.config, AppConfig):
            raise ValueError("config must be an AppConfig")
        if not isinstance(self.valkey_store, ValkeyStore):
            raise ValueError("valkey_store must be a ValkeyStore")
        if not isinstance(self.memory_store, ConversationMemoryStore):
            raise ValueError("memory_store must be a ConversationMemoryStore")
        if not isinstance(self.gemini_client, GeminiExecutiveClient):
            raise ValueError("gemini_client must be a GeminiExecutiveClient")
        if not isinstance(self.drive_connector, VickySECOMDriveConnector):
            raise ValueError("drive_connector must be a VickySECOMDriveConnector")
        if self.ledger is not None and not isinstance(self.ledger, SheetsLedger):
            raise ValueError("ledger must be a SheetsLedger if provided")
        if self.notifier is not None and not isinstance(self.notifier, BoardroomNotifier):
            raise ValueError("notifier must be a BoardroomNotifier if provided")
        if self.acceptance_runner is not None and not isinstance(self.acceptance_runner, BoardroomAcceptanceRunner):
            raise ValueError("acceptance_runner must be a BoardroomAcceptanceRunner if provided")
        if not isinstance(self.flow_registry, dict):
            raise ValueError("flow_registry must be a dict")

    # ---------------------------------------------------------------------
    # Fábricas
    # ---------------------------------------------------------------------
    @classmethod
    def build(cls, config: AppConfig) -> "VickyBoardroomService":
        valkey_store = _build_valkey_store(config)
        contract_registry = AgentContractRegistry()
        activation_engine = ActivationCriteriaEngine()
        fallback_manager = FallbackManager()
        orchestrator = _build_orchestrator(
            contract_registry=contract_registry,
            activation_engine=activation_engine,
            fallback_manager=fallback_manager,
        )

        notifier = None
        if config.notifier_configured:
            try:
                notifier = BoardroomNotifier(
                    NotificationConfig(
                        meta_token=config.whatsapp_api_token,
                        waba_phone_id=config.whatsapp_phone_id,
                        approver_whatsapp=config.admin_phone,
                        valkey_url=config.valkey_url,
                        dry_run=config.notifier_dry_run,
                    )
                )
            except Exception as exc:
                logger.warning("Notifier no disponible: %s", exc)

        ledger = None
        if config.google_credentials_json and config.boardroom_ledger_sheet_id:
            try:
                ledger = SheetsLedger(
                    config=SheetsLedgerConfig(
                        sheet_id=config.boardroom_ledger_sheet_id,
                        credentials_json=config.google_credentials_json,
                        dry_run=config.sheets_dry_run,
                    ),
                    valkey_store=valkey_store,
                )
            except SheetsLedgerConfigurationError as exc:
                logger.warning("Sheets ledger no configurado: %s", exc)
            except Exception as exc:
                logger.warning("Sheets ledger no disponible: %s", exc)

        return cls(
            config=config,
            orchestrator=orchestrator,
            valkey_store=valkey_store,
            memory_store=ConversationMemoryStore(valkey_store=valkey_store, ttl_seconds=config.memory_ttl_seconds),
            gemini_client=GeminiExecutiveClient(config=config),
            drive_connector=VickySECOMDriveConnector(config=config),
            ledger=ledger,
            notifier=notifier,
            acceptance_runner=BoardroomAcceptanceRunner(project_name="COHIFIS / Hydra-Boardroom"),
            flow_registry={
                "flow_gpt_claude": GPTClaudeFlow,
                "flow_gemini_gpt_claude": GeminiGPTClaudeFlow,
                "flow_gemma_decision_layer": GemmaDecisionLayerFlow,
            },
        )

    # ---------------------------------------------------------------------
    # Público
    # ---------------------------------------------------------------------
    def handle_chat(
        self,
        *,
        payload: Mapping[str, Any],
        origin: Optional[str],
        remote_addr: str,
        user_agent: str,
    ) -> ChatResponseEnvelope:
        request_payload = ChatRequestPayload.from_mapping(payload)
        context = request_payload.context

        conversation_id = context.conversation_id or context.customer_id
        if not conversation_id and context.phone:
            conversation_id = normalize_phone(context.phone)
        if not conversation_id:
            conversation_id = str(uuid4())

        memory = self.memory_store.load(conversation_id) or ConversationMemory(conversation_id=conversation_id)
        memory.apply_context(context)
        self._learn_from_message(memory, request_payload.message)

        policy = self._build_conversation_policy(
            request_payload=request_payload,
            conversation_id=conversation_id,
        )
        memory.segmento = request_payload.context.segmento
        task = Task.from_policy_envelope(
            policy,
            trace_id=str(uuid4()),
            conversation_id=conversation_id,
            request_id=request_payload.request_id,
            owner="hydra",
            metadata={
                "remote_addr": remote_addr,
                "origin": origin,
                "user_agent": user_agent,
                "service": self.config.app_name,
            },
        )

        start = time.perf_counter()
        task.transition_to(
            TaskState.CLASSIFIED,
            trigger="http_chat_received",
            triggered_by="hydra",
            reason_code="CHAT_PAYLOAD_VALIDATED",
            current_agent="hydra",
        )
        self._persist_task(task)

        routing = self.orchestrator.route_task(task)
        task.transition_to(
            TaskState.ROUTED,
            trigger="hydra_route_selected",
            triggered_by="hydra",
            reason_code=routing.reason_code,
            current_agent=routing.selected_actor.value,
            notes=routing.summary,
        )
        self._persist_task(task)
        self._write_trace(
            task=task,
            actor=AuthorityActor.HYDRA,
            stage=TraceStage.ROUTING,
            status=TraceStatus.SUCCEEDED,
            summary=routing.summary,
            metadata={"routing": routing.to_dict()},
        )

        if routing.disposition.name in {"ESCALATE", "HOLD", "FAIL"}:
            return self._handle_non_execute_route(
                task=task,
                routing=routing,
                conversation_id=conversation_id,
                memory=memory,
            )

        task.transition_to(
            TaskState.RUNNING,
            trigger="assistant_execution_started",
            triggered_by=routing.selected_actor.value,
            reason_code="VICKY_EXECUTION_STARTED",
            current_agent=routing.selected_actor.value,
        )
        self._persist_task(task)

        denue_context = self.drive_connector.read_denue_placeholder(
            city=memory.city,
            segmento=memory.segmento,
        )
        product_priority = build_product_priority(memory.segmento)
        prompt = self.gemini_client.build_prompt(
            request_payload=request_payload,
            memory=memory,
            products=product_priority,
            denue_context=denue_context,
        )

        provider_metadata: Dict[str, Any] = {"provider": "fallback"}
        try:
            answer, provider_metadata = self.gemini_client.generate(prompt)
            trace_status = TraceStatus.SUCCEEDED
        except Exception as exc:
            logger.warning("Gemini no disponible, usando fallback ejecutivo: %s", exc)
            answer = self.gemini_client.executive_fallback(
                message=request_payload.message,
                memory=memory,
                products=product_priority,
                city=memory.city,
            )
            trace_status = TraceStatus.SKIPPED

        memory.last_products = [item.title for item in product_priority]
        memory.register_turn("user", request_payload.message)
        memory.register_turn("assistant", answer)
        self.memory_store.save(memory)

        task.transition_to(
            TaskState.DONE,
            trigger="assistant_execution_completed",
            triggered_by="vicky",
            reason_code="CHAT_RESOLVED",
            current_agent=routing.selected_actor.value,
            final_resolution="resolved_with_vicky",
        )
        self._persist_task(task)

        latency_ms = int((time.perf_counter() - start) * 1000)
        self._write_trace(
            task=task,
            actor=AuthorityActor.GEMINI if provider_metadata.get("provider") == "gemini" else AuthorityActor.GPT,
            stage=TraceStage.EXECUTION,
            status=trace_status,
            summary="Respuesta conversacional generada por Vicky.",
            latency_ms=latency_ms,
            metadata=provider_metadata,
        )
        self._write_metric(
            task_id=task.task_id,
            metric_name="chat_request_latency_ms",
            value=float(latency_ms),
            tags={"segmento": memory.segmento, "provider": provider_metadata.get("provider", "fallback")},
        )

        confidence = policy.confidence
        return ChatResponseEnvelope(
            ok=True,
            request_id=request_payload.request_id,
            conversation_id=conversation_id,
            task_id=task.task_id,
            answer=answer,
            selected_flow=routing.selected_flow,
            selected_actor=routing.selected_actor.value,
            deliverable_status=_status_from_confidence(confidence),
            confidence=confidence,
            product_priority=[item.to_dict() for item in product_priority],
            memory=memory.to_dict(),
            routing=routing.to_dict(),
            boardroom={
                "task_state": task.current_state.value,
                "task_type": task.task_type,
                "requires_audit": task.policy_envelope.requires_audit,
                "requires_human_approval": task.policy_envelope.requires_human_approval,
                "flow_registry": sorted(self.flow_registry.keys()),
            },
            denue=denue_context,
        )

    def handle_lead(
        self,
        *,
        payload: Mapping[str, Any],
        origin: Optional[str],
        remote_addr: str,
        user_agent: str,
    ) -> Dict[str, Any]:
        is_valid, validation = validate_lead_payload(payload)
        if not is_valid:
            raise ValueError(validation)

        lead = dict(payload["lead"])
        normalized_phone = normalize_phone(validation)
        lead_data = {
            "name": str(lead["name"]).strip(),
            "phone": normalized_phone,
            "email": str(lead.get("email", "") or "").strip(),
            "service": str(lead["service"]).strip(),
            "notes": str(lead.get("notes", "") or "").strip(),
            "tags": list(lead.get("tags", []) or []),
            "metadata": {
                "origin": origin,
                "ip": remote_addr,
                "user_agent": user_agent,
                "received_via": "web_form",
                "timestamp": utc_now().isoformat(),
                "brand": "COHIFIS",
            },
        }

        logger.info(_safe_json({"type": "lead_received", "source": "web_form", "data": lead_data}))
        whatsapp_sent = send_whatsapp_template(
            self.config,
            normalized_phone,
            lead_data["name"],
            lead_data["service"],
        )

        advisor_notified = False
        if whatsapp_sent and self.config.admin_phone:
            advisor_notified = send_whatsapp_text(
                self.config,
                self.config.admin_phone,
                (
                    f"🚨 NUEVO LEAD COHIFIS\n"
                    f"Nombre: {lead_data['name']}\n"
                    f"Teléfono: {lead_data['phone']}\n"
                    f"Servicio: {lead_data['service']}\n"
                    f"Email: {lead_data['email'] or 'No proporcionado'}"
                ),
            )

        GOLDEN_LEAD_THRESHOLD = 1_000_000.0
        lead_amount_raw = str(lead.get("amount", "") or "").replace(",", "").replace("$", "").strip()
        try:
            lead_amount = float(lead_amount_raw) if lead_amount_raw else 0.0
        except ValueError:
            lead_amount = 0.0

        if lead_amount >= GOLDEN_LEAD_THRESHOLD and self.config.admin_phone:
            golden_msg = (
                f"🏆 ALERTA ORO — LEAD PRIORITARIO\n"
                f"{'─' * 30}\n"
                f"Nombre:   {lead_data['name']}\n"
                f"Teléfono: {lead_data['phone']}\n"
                f"Servicio: {lead_data['service']}\n"
                f"Monto:    ${lead_amount:,.0f} MXN\n"
                f"{'─' * 30}\n"
                f"⚡ REQUIERE ATENCIÓN INMEDIATA"
            )
            send_whatsapp_text(self.config, self.config.admin_phone, golden_msg)
            logger.info(
                "golden_lead_alert sent phone=%s amount=%s",
                lead_data["phone"],
                lead_amount,
            )

        # Integración ligera con memoria conversacional:
        conv_id = normalized_phone
        memory = self.memory_store.load(conv_id) or ConversationMemory(conversation_id=conv_id)
        memory.customer_name = lead_data["name"]
        memory.customer_need = lead_data["service"]
        memory.phone = normalized_phone
        memory.register_turn("system", f"Lead web capturado para servicio {lead_data['service']}")
        self.memory_store.save(memory)

        return {
            "ok": True,
            "lead_id": f"lead_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "whatsapp_sent": whatsapp_sent,
            "advisor_notified": advisor_notified,
            "persisted": True,
            "message": "Lead COHIFIS recibido correctamente",
            "next_step": (
                "Revisa WhatsApp para continuar"
                if whatsapp_sent
                else "El asesor dará seguimiento directo"
            ),
        }

    def health(self) -> Dict[str, Any]:
        acceptance_summary: Dict[str, Any] = {
            "enabled": self.acceptance_runner is not None,
            "executed": False,
        }
        if self.config.run_acceptance_on_health and self.acceptance_runner is not None:
            try:
                report = self.acceptance_runner.run()
                acceptance_summary = {
                    "enabled": True,
                    "executed": True,
                    "acceptance_result": report.acceptance_result,
                    "confidence": report.confidence,
                    "deliverable_status": report.deliverable_status,
                }
            except Exception as exc:
                acceptance_summary = {
                    "enabled": True,
                    "executed": False,
                    "error": str(exc),
                }

        return {
            "status": "healthy",
            "timestamp": utc_now().isoformat(),
            "service": self.config.app_name,
            "brand": "COHIFIS",
            "version": "boardroom-flask-v1",
            "features": {
                "allowed_origins": list(self.config.allowed_origins),
                "whatsapp_configured": self.config.whatsapp_configured,
                "valkey_storage": bool(self.config.valkey_url),
                "gemini_available": self.gemini_client.is_available(),
                "ledger_available": self.ledger is not None,
                "denue_drive_placeholder": True,
                "flow_registry": sorted(self.flow_registry.keys()),
            },
            "governance": {
                "gpt_builds": True,
                "claude_audits": True,
                "don_chiwy_approves": True,
                "deliverable_reference_status": _status_from_confidence(0.91),
                "review_ready_threshold": REVIEW_READY_THRESHOLD,
            },
            "acceptance": acceptance_summary,
        }

    # ---------------------------------------------------------------------
    # Internos
    # ---------------------------------------------------------------------
    def _learn_from_message(self, memory: ConversationMemory, message: str) -> None:
        extracted_name = extract_customer_name(message)
        extracted_need = extract_customer_need(message)
        if extracted_name and not memory.customer_name:
            memory.customer_name = extracted_name
        if extracted_need:
            memory.customer_need = extracted_need

    def _build_conversation_policy(
        self,
        *,
        request_payload: ChatRequestPayload,
        conversation_id: str,
    ) -> PolicyEnvelope:
        segmento = infer_segment(request_payload.message, request_payload.context)
        request_payload.context.segmento = segmento
        confidence = compute_request_confidence(request_payload.message, request_payload.context)

        return PolicyEnvelope(
            task_id=str(uuid4()),
            source=Source.CLIENT,
            channel=Channel.WEB if request_payload.context.source_channel == "web" else Channel.WHATSAPP,
            task_type=TaskType.CONVERSATION,
            intent=f"vicky_chat_{segmento}",
            criticality=Criticality.LOW if segmento != "imss" else Criticality.MEDIUM,
            confidence=confidence,
            requires_google_state=False,
            requires_audit=False,
            requires_human_approval=False,
            fast_path=True,
            allowed_agents=[Agent.GEMMA, Agent.GEMINI, Agent.GPT, Agent.CLAUDE],
            system_of_record=SystemOfRecord.GOOGLE if request_payload.context.city else SystemOfRecord.INTERNAL,
            fallback_mode=FallbackMode.SAFE_REPLY,
            audit_timeout_seconds=300,
            audit_timeout_action=AuditTimeoutAction.HOLD,
            audit_required_for_release=False,
            audit_bypass_consecutive_limit=5,
            audit_bypass_counter_scope=AuditBypassCounterScope.GLOBAL,
            notify_on_waiting_audit=True,
            notify_on_audit_timeout=True,
            notify_on_escalation=True,
            notify_on_rejection=True,
            notify_on_conflict=True,
            notify_on_override_required=True,
            notify_on_override_applied=True,
            notification_channel=NotificationChannel.BOTH,
            requires_ack_on_escalation=False,
        )

    def _handle_non_execute_route(
        self,
        *,
        task: Task,
        routing: Any,
        conversation_id: str,
        memory: ConversationMemory,
    ) -> ChatResponseEnvelope:
        target_state = {
            "ESCALATE": TaskState.ESCALATED,
            "HOLD": TaskState.HOLD,
            "FAIL": TaskState.FAILED,
        }.get(routing.disposition.name, TaskState.HOLD)

        task.transition_to(
            target_state,
            trigger="hydra_non_execute_route",
            triggered_by="hydra",
            reason_code=routing.reason_code,
            current_agent=routing.selected_actor.value,
            final_resolution="non_execute_route",
        )
        self._persist_task(task)

        self._emit_authority_event(
            task=task,
            summary=routing.summary,
            event_type=AuthorityEventType.ESCALATION if target_state == TaskState.ESCALATED else AuthorityEventType.HOLD,
            severity=EventSeverity.MEDIUM if target_state != TaskState.FAILED else EventSeverity.HIGH,
            reason_code=EscalationReason.HUMAN_APPROVAL_REQUIRED
            if target_state == TaskState.ESCALATED
            else EscalationReason.LOW_CONFIDENCE,
        )

        answer = (
            f"Hola {memory.customer_name}," if memory.customer_name else "Hola,"
        ) + (
            " para orientarte bien necesito un poco más de información antes de continuar."
        )
        confidence = task.policy_envelope.confidence
        return ChatResponseEnvelope(
            ok=True,
            request_id=task.request_id or str(uuid4()),
            conversation_id=conversation_id,
            task_id=task.task_id,
            answer=answer,
            selected_flow=routing.selected_flow,
            selected_actor=routing.selected_actor.value,
            deliverable_status=_status_from_confidence(confidence),
            confidence=confidence,
            product_priority=[item.to_dict() for item in build_product_priority(memory.segmento)],
            memory=memory.to_dict(),
            routing=routing.to_dict(),
            boardroom={"task_state": task.current_state.value, "task_type": task.task_type},
            denue={"status": "skipped"},
        )

    def _emit_authority_event(
        self,
        *,
        task: Task,
        summary: str,
        event_type: AuthorityEventType,
        severity: EventSeverity,
        reason_code: EscalationReason,
    ) -> Optional[AuthorityEvent]:
        event = AuthorityEvent(
            task_id=task.task_id,
            from_level=AuthorityLevel.ORCHESTRATOR,
            from_agent=AuthorityActor.HYDRA,
            to_level=AuthorityLevel.DON_CHIWY if event_type == AuthorityEventType.ESCALATION else AuthorityLevel.ORCHESTRATOR,
            to_agent=AuthorityActor.DON_CHIWY if event_type == AuthorityEventType.ESCALATION else AuthorityActor.HYDRA,
            event_type=event_type,
            severity=severity,
            reason_code=reason_code,
            summary=summary,
            requires_ack=False,
            metadata={"task_state": task.current_state.value},
        )
        try:
            self.valkey_store.append_authority_event(event)
        except Exception as exc:
            logger.warning("No se pudo persistir authority event: %s", exc)

        if self.ledger is not None:
            try:
                self.ledger.append_authority_event(event)
            except Exception as exc:
                logger.warning("No se pudo escribir authority event en Sheets: %s", exc)

        if self.notifier is not None:
            try:
                self.notifier.notify_authority_event(task, event)
            except Exception as exc:
                logger.warning("No se pudo notificar authority event: %s", exc)
        return event

    def _persist_task(self, task: Task) -> None:
        try:
            self.valkey_store.write_task_ledger(task)
        except Exception as exc:
            logger.warning("No se pudo escribir task ledger en Valkey: %s", exc)

        if self.ledger is not None:
            try:
                self.ledger.append_task_ledger(task)
                if task.state_history:
                    self.ledger.append_state_transition(task, task.state_history[-1])
            except Exception as exc:
                logger.warning("No se pudo escribir task ledger en Sheets: %s", exc)
                try:
                    incident = LedgerIncident(
                        incident_code=IncidentCode.LEDGER_WRITE_FAILED,
                        task_id=task.task_id,
                        summary="No se pudo persistir task ledger en Google Sheets.",
                        severity=EventSeverity.HIGH,
                        source="app.py",
                        metadata={"error": str(exc)},
                    )
                    self.ledger.append_incident(incident)
                except Exception:
                    logger.warning("No se pudo registrar incidente de ledger")

    def _write_trace(
        self,
        *,
        task: Task,
        actor: AuthorityActor,
        stage: TraceStage,
        status: TraceStatus,
        summary: str,
        latency_ms: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            trace = AgentTraceRecord(
                task_id=task.task_id,
                actor=actor,
                stage=stage,
                status=status,
                summary=summary,
                latency_ms=latency_ms,
                input_ref=task.request_id,
                output_ref=task.trace_id,
                metadata=metadata or {},
            )
            self.valkey_store.append_agent_trace(trace)
        except Exception as exc:
            logger.warning("No se pudo escribir trace en Valkey: %s", exc)

    def _write_metric(
        self,
        *,
        task_id: str,
        metric_name: str,
        value: float,
        tags: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            metric = HealthMetricRecord(
                window="rolling_30d",
                metric_name=metric_name,
                value=float(value),
                source="app.py",
                task_id=task_id,
                tags=tags or {},
            )
            self.valkey_store.write_health_metric(metric)
        except Exception as exc:
            logger.warning("No se pudo escribir health metric: %s", exc)


# =============================================================================
# FÁBRICAS DE INFRAESTRUCTURA
# =============================================================================
def _build_orchestrator(
    *,
    contract_registry: AgentContractRegistry,
    activation_engine: ActivationCriteriaEngine,
    fallback_manager: FallbackManager,
) -> Any:
    # Compatibilidad defensiva con firmas variantes del módulo.
    try:
        from hydra_orchestrator import HydraOrchestrator  # type: ignore
    except ImportError:
        from hydra_orchestrator import HydraOrchestrator  # pragma: no cover

    try:
        return HydraOrchestrator(
            contract_validator=contract_registry,
            activation_criteria=activation_engine,
            fallback_manager=fallback_manager,
            minimum_confidence=0.75,
            default_owner="hydra",
        )
    except TypeError:
        return HydraOrchestrator()


def _build_valkey_store(config: AppConfig) -> ValkeyStore:
    if config.valkey_url:
        return ValkeyStore(ValkeyStoreConfig(redis_url=config.valkey_url))
    return ValkeyStore(
        config=ValkeyStoreConfig(redis_url="redis://memory.local"),
        client=InMemoryValkeyClient(),
    )


# =============================================================================
# APP FACTORY
# =============================================================================
limiter = Limiter(key_func=get_remote_address, default_limits=[])


def create_app(config: Optional[AppConfig] = None) -> Flask:
    resolved_config = config or AppConfig.from_env()
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    app.config["RATELIMIT_STORAGE_URI"] = resolved_config.limiter_storage_uri

    CORS(
        app,
        origins=get_allowed_origins(resolved_config),
        supports_credentials=True,
        allow_headers=["Content-Type", "Authorization"],
        methods=["GET", "POST", "OPTIONS"],
    )

    limiter.init_app(app)

    service = VickyBoardroomService.build(resolved_config)
    app.extensions["vicky_service"] = service
    app.extensions["vicky_config"] = resolved_config

    @app.before_request
    def enforce_origin_policy() -> Optional[Any]:
        if request.method == "OPTIONS":
            return None
        if request.path.startswith("/api/"):
            origin = request.headers.get("Origin")
            if not origin_is_allowed(origin, resolved_config):
                logger.warning("Origin no autorizado path=%s origin=%s", request.path, origin)
                return jsonify({"error": "Origen no autorizado"}), 403
        return None

    @app.route("/api/health", methods=["GET"])
    def health() -> Any:
        return jsonify(service.health()), 200

    @app.route("/api/v1/chat", methods=["POST", "OPTIONS"])
    @app.route("/api/v1/vicky-chat", methods=["POST", "OPTIONS"])
    @limiter.limit(resolved_config.rate_limit_chat)
    def vicky_chat() -> Any:
        if request.method == "OPTIONS":
            return jsonify({}), 200
        if not request.is_json:
            return jsonify({"error": "Content-Type debe ser application/json"}), 415

        payload = request.get_json(silent=True)
        if not isinstance(payload, Mapping):
            return jsonify({"error": "JSON malformado"}), 400

        try:
            response = service.handle_chat(
                payload=payload,
                origin=request.headers.get("Origin"),
                remote_addr=request.remote_addr or "unknown",
                user_agent=request.headers.get("User-Agent", ""),
            )
            return jsonify(response.to_dict()), 200
        except ValueError as exc:
            logger.warning("Error de validación chat: %s", exc)
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception("Error no controlado en /api/v1/chat: %s", exc)
            return jsonify({"error": "Error interno del servidor"}), 500

    @app.route("/api/web-lead", methods=["POST", "OPTIONS"])
    @limiter.limit(resolved_config.rate_limit_lead)
    def web_lead() -> Any:
        if request.method == "OPTIONS":
            return jsonify({}), 200
        if not request.is_json:
            return jsonify({"error": "Content-Type debe ser application/json"}), 415

        payload = request.get_json(silent=True)
        if not isinstance(payload, Mapping):
            return jsonify({"error": "JSON malformado"}), 400

        try:
            response = service.handle_lead(
                payload=payload,
                origin=request.headers.get("Origin"),
                remote_addr=request.remote_addr or "unknown",
                user_agent=request.headers.get("User-Agent", ""),
            )
            return jsonify(response), 201
        except ValueError as exc:
            logger.warning("Lead inválido: %s", exc)
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception("Error no controlado en /api/web-lead: %s", exc)
            return jsonify({"error": "Error interno del servidor"}), 500

    @app.errorhandler(404)
    def not_found_handler(_: Any) -> Any:
        return jsonify({"error": "Ruta no encontrada"}), 404

    @app.errorhandler(429)
    def ratelimit_handler(_: Any) -> Any:
        return jsonify({
            "error": "Demasiadas solicitudes",
            "message": "Por favor intenta de nuevo en unos minutos",
        }), 429

    @app.errorhandler(500)
    def internal_error_handler(_: Any) -> Any:
        return jsonify({"error": "Error interno del servidor"}), 500

    return app


app = create_app()


# =============================================================================
# EJEMPLO FUNCIONAL
# =============================================================================
if __name__ == "__main__":
    local_config = AppConfig.from_env()
    local_service = app.extensions["vicky_service"]

    demo_payload = {
        "message": "Hola, me llamo Carlos y quiero información para mi pensión. Soy del segmento IMSS en Los Mochis.",
        "context": {
            "segmento": "imss",
            "city": "Los Mochis",
            "customer_name": "Carlos",
            "source_channel": "web",
            "speech_enabled": False,
        },
    }

    # READY_FOR_AUDIT
    demo_result = local_service.handle_chat(
        payload=demo_payload,
        origin="https://cohifis.com.mx",
        remote_addr="127.0.0.1",
        user_agent="local-example/1.0",
    )
    print(demo_result.to_json())

    if os.getenv("RENDER", "").strip().lower() != "true":
        app.run(host="0.0.0.0", port=local_config.port, debug=False)
