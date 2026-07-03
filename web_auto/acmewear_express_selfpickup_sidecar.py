"""Read-only detector/minimizer for ACMEWEAR PP2 Express and self-pickup rows."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


LANE_ID = "ACMEWEAR_PP2_EXPRESS_SELFPICKUP_V1"
SCHEMA_VERSION = "acmewear_express_selfpickup_sidecar.v1"
ACMEWEAR_MERCHANT_ID = "30137883"
PP2_PICKUP_POINT_ID = "30137883_PP2"
ALMATY_TZ = timezone(timedelta(hours=5))
DEFAULT_RUN_ROOT = Path("runs/acmewear_express_selfpickup_sidecar")
DEFAULT_ORDER_API_URL = "https://kaspi.kz/shop/api/v2/orders"
DEFAULT_FETCH_STATES: tuple[str, ...] = ("NEW", "KASPI_DELIVERY", "PICKUP")
ACTIONABLE_PERSIST_CLASSIFICATIONS: frozenset[str] = frozenset(
    {
        "KASPI_EXPRESS_DELIVERY_API_MATCH",
        "PP2_SELF_SERVING_PICKUP_PROVEN",
    }
)
WATCH_HEARTBEAT_VERSION = "acmewear_express_selfpickup_watch.v1"
TELEGRAM_ALERT_LEDGER_VERSION = "acmewear_express_telegram_send_ledger.v1"
TELEGRAM_ALERT_TEMPLATE_VERSION = "acmewear_pp2_express_alert.v1"
TELEGRAM_LABEL_TEMPLATE_VERSION = "acmewear_pp2_express_label_send.v1"
PREPARE_LABEL_VERSION = "acmewear_express_prepare_label.v1"
PICKUP_COMPLETION_LEDGER_VERSION = "acmewear_pp2_pickup_completion_review.v1"
PICKUP_COMPLETION_API_PLAN_VERSION = "acmewear_pp2_pickup_completion_api_request_plan.v1"
PICKUP_COMPLETION_API_EXECUTION_VERSION = "acmewear_pp2_pickup_completion_api_execution.v1"
EXPRESS_ASSEMBLY_LEDGER_VERSION = "acmewear_pp2_express_assembly_review.v1"
READINESS_REVIEW_VERSION = "acmewear_express_selfpickup_readiness.v1"
OPERATOR_QUEUE_VERSION = "acmewear_express_selfpickup_operator_queue.v1"
SHIFT_PACKET_VERSION = "acmewear_express_selfpickup_shift_packet.v1"
SELF_PICKUP_SAMPLE_SCAN_VERSION = "acmewear_pp2_selfpickup_sample_scan.v1"
PICKUP_COMPLETION_OFFICIAL_DOC_URL = "https://guide.kaspi.kz/partner/ru/shop/api/orders/q3212"
DEFAULT_ON_DEMAND_LABEL_APPROVAL_POLICY = Path(
    "config/owner_decisions/acmewear_express_label_on_demand_persistent_approval_2026_06_24.json"
)
DEFAULT_TELEGRAM_LABEL_LEDGER_CSV = Path("data/acmewear_express_telegram_label_ledger.csv")
DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV = "KASPI_TOKEN_ACMEWEAR"
LEGACY_ACMEWEAR_KASPI_TOKEN_ENV = "ACMEWEAR_KASPI_API_TOKEN"
ACMEWEAR_KASPI_TOKEN_ENV_ALIASES: tuple[str, ...] = (
    DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV,
    LEGACY_ACMEWEAR_KASPI_TOKEN_ENV,
)
DEFAULT_TELEGRAM_BOT_TOKEN_ENV = "ACMEWEAR_EXPRESS_TELEGRAM_BOT_TOKEN"
GENERIC_TELEGRAM_BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
WAYBILL_TELEGRAM_BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN_WAYBILL"
DEFAULT_TELEGRAM_ALERT_CHAT_ID_ENV = "ACMEWEAR_EXPRESS_TELEGRAM_CHAT_ID"
GENERIC_TELEGRAM_ALERT_CHAT_ID_ENV = "TELEGRAM_CHAT_ID"
DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV = "ACMEWEAR_EXPRESS_TELEGRAM_PRINT_CHAT_ID"
GENERIC_TELEGRAM_PRINT_CHAT_ID_ENV = "TELEGRAM_WAYBILL_CHAT_ID"
TELEGRAM_BOT_TOKEN_ENV_ALIASES: tuple[str, ...] = (
    DEFAULT_TELEGRAM_BOT_TOKEN_ENV,
    GENERIC_TELEGRAM_BOT_TOKEN_ENV,
    WAYBILL_TELEGRAM_BOT_TOKEN_ENV,
)
TELEGRAM_ALERT_CHAT_ID_ENV_ALIASES: tuple[str, ...] = (
    DEFAULT_TELEGRAM_ALERT_CHAT_ID_ENV,
    GENERIC_TELEGRAM_ALERT_CHAT_ID_ENV,
)
TELEGRAM_PRINT_CHAT_ID_ENV_ALIASES: tuple[str, ...] = (
    DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV,
    GENERIC_TELEGRAM_PRINT_CHAT_ID_ENV,
)

READINESS_MATRIX_COLUMNS: tuple[str, ...] = (
    "check_id",
    "domain",
    "gate",
    "decision",
    "evidence_path",
    "blocker",
    "next_step",
)

SELF_PICKUP_SAMPLE_SCAN_COLUMNS: tuple[str, ...] = (
    "scan_version",
    "lane_id",
    "local_ref",
    "order_hash_prefix",
    "delivery_kind",
    "classification",
    "classification_confidence",
    "pickup_point_id",
    "is_kaspi_delivery",
    "pp2_pickup_point_seen",
    "seller_self_pickup_proven",
    "unlock_pickup_completion_review",
    "reason",
)

OPERATOR_QUEUE_COLUMNS: tuple[str, ...] = (
    "queue_version",
    "lane_id",
    "idempotency_key",
    "order_hash",
    "local_ref",
    "store_code",
    "merchant_id",
    "delivery_kind",
    "classification",
    "pickup_point_id",
    "detected_at",
    "created_at",
    "delivery_slot_label",
    "courier_planning_at",
    "sku_key",
    "merchant_article",
    "ordered_size",
    "my_size",
    "owner_size_override",
    "size_status",
    "waybill_status",
    "telegram_alert_status",
    "telegram_label_status",
    "printed_status",
    "close_status",
    "operator_priority",
    "operator_next_action",
    "operator_reason",
    "mutation_gate",
    "telegram_gate",
    "print_gate",
    "blockers",
    "warnings",
)

PICKUP_COMPLETION_LEDGER_COLUMNS: tuple[str, ...] = (
    "ledger_version",
    "lane_id",
    "sidecar_idempotency_key",
    "order_hash",
    "local_ref",
    "store_code",
    "merchant_id",
    "event_type",
    "pickup_completion_key",
    "review_status",
    "delivery_kind_at_review",
    "classification_at_review",
    "pickup_point_id",
    "size_status_at_review",
    "my_size",
    "owner_size_override",
    "security_code_provided",
    "security_code_sha256",
    "customer_arrived_status",
    "manual_handoff_status",
    "kaspi_completion_status",
    "blockers",
    "warnings",
    "created_at",
    "created_by",
    "notes",
)

PICKUP_COMPLETION_API_EXECUTION_COLUMNS: tuple[str, ...] = (
    "execution_version",
    "lane_id",
    "idempotency_key",
    "local_ref",
    "order_hash",
    "step",
    "dry_run",
    "execute_requested",
    "execution_status",
    "http_status",
    "response_bytes",
    "api_plan_gate",
    "api_plan_path",
    "owner_approval_ref",
    "effective_token_env",
    "order_id_env",
    "order_id_env_present",
    "order_code_env",
    "order_code_env_present",
    "security_code_env",
    "security_code_env_present",
    "created_at",
    "blockers",
    "warnings",
    "notes",
)

EXPRESS_ASSEMBLY_LEDGER_COLUMNS: tuple[str, ...] = (
    "ledger_version",
    "lane_id",
    "sidecar_idempotency_key",
    "order_hash",
    "local_ref",
    "store_code",
    "merchant_id",
    "event_type",
    "express_assembly_key",
    "review_status",
    "delivery_kind_at_review",
    "classification_at_review",
    "pickup_point_id",
    "size_status_at_review",
    "my_size",
    "owner_size_override",
    "waybill_status_at_review",
    "waybill_present",
    "cropped_label_sha256",
    "telegram_label_status",
    "printed_status",
    "label_printed_confirmed",
    "operator_physically_ready",
    "owner_assembly_approval_ref",
    "kaspi_assembly_status",
    "blockers",
    "warnings",
    "created_at",
    "created_by",
    "notes",
)

TELEGRAM_ALERT_LEDGER_COLUMNS: tuple[str, ...] = (
    "ledger_version",
    "lane_id",
    "sidecar_idempotency_key",
    "order_hash",
    "local_ref",
    "store_code",
    "merchant_id",
    "send_kind",
    "event_type",
    "idempotency_key",
    "template_version",
    "payload_sha256",
    "message_preview_redacted",
    "chat_config_ref",
    "send_config_ref",
    "send_approval_ref",
    "dry_run",
    "send_status",
    "waybill_status_at_send",
    "size_status_at_send",
    "assemble_status_at_send",
    "attachment_required",
    "attachment_kind",
    "attachment_sha256",
    "attachment_page_count",
    "attachment_width_mm",
    "attachment_height_mm",
    "sensitive_scan_status",
    "telegram_message_id",
    "telegram_media_group_id",
    "sent_at",
    "first_seen_at",
    "last_attempt_at",
    "attempt_count",
    "last_error_class",
    "created_by",
    "notes",
)

ALERT_QUEUE_COLUMNS: tuple[str, ...] = (
    "idempotency_key",
    "order_hash",
    "local_ref",
    "detected_at",
    "sidecar_state",
    "classification",
    "delivery_kind",
    "pickup_point_id",
    "express_flag",
    "delivery_slot_label",
    "courier_planning_at",
    "sku_key",
    "merchant_article",
    "ordered_size",
    "size_status",
    "assemble_status",
    "blockers",
    "warnings",
    "telegram_alert_status",
    "alert_reason",
)

STATE_MACHINE: tuple[str, ...] = (
    "DETECTED",
    "NEEDS_SIZE",
    "SIZE_READY",
    "ASSEMBLE_READY",
    "ASSEMBLED",
    "WAYBILL_READY",
    "TELEGRAM_LABEL_SENT",
    "PRINTED",
    "CLOSED",
)

SIDE_CAR_COLUMNS: tuple[str, ...] = (
    "schema_version",
    "lane_id",
    "idempotency_key",
    "order_hash",
    "local_ref",
    "store_code",
    "merchant_id",
    "source_endpoint_family",
    "detected_at",
    "created_at",
    "last_seen_at",
    "sidecar_state",
    "classification",
    "delivery_kind",
    "classification_confidence",
    "state",
    "status",
    "delivery_type",
    "delivery_mode",
    "is_kaspi_delivery",
    "pickup_point_id",
    "pickup_point_proof_status",
    "express_flag",
    "express_signal_source",
    "delivery_slot_from",
    "delivery_slot_to",
    "delivery_slot_label",
    "planned_delivery_at",
    "courier_planning_at",
    "sla_minutes_to_courier_planning",
    "sla_deadline_at",
    "sku_key",
    "merchant_article",
    "ordered_size",
    "height_cm",
    "weight_kg",
    "my_size",
    "owner_size_override",
    "size_source",
    "size_status",
    "assemble_status",
    "waybill_status",
    "waybill_present",
    "cropped_label_path",
    "cropped_label_sha256",
    "telegram_alert_status",
    "telegram_label_status",
    "printed_status",
    "close_status",
    "missing_size_blocker",
    "missing_waybill_blocker",
    "ambiguous_delivery_mode_blocker",
    "unapproved_mutation_blocker",
    "blockers",
    "warnings",
    "row_hash",
)

SIDE_CAR_SCHEMA_ROWS: tuple[dict[str, str], ...] = tuple(
    {
        "column": column,
        "type": "text",
        "required": "yes"
        if column
        in {
            "schema_version",
            "lane_id",
            "idempotency_key",
            "order_hash",
            "local_ref",
            "merchant_id",
            "detected_at",
            "sidecar_state",
            "classification",
            "delivery_kind",
            "blockers",
            "row_hash",
        }
        else "no",
        "allowed_values": {
            "sidecar_state": "|".join(STATE_MACHINE),
            "delivery_kind": "EXPRESS_DELIVERY|SELLER_SELF_PICKUP|KASPI_DELIVERY|NORMAL_DELIVERY|MANUAL_REVIEW",
            "size_status": "NEEDS_SIZE|SIZE_READY_FROM_RULE|SIZE_READY_OWNER_OVERRIDE|SIZE_BLOCKED_CUSTOMER_NOT_CONTACTED|SIZE_BLOCKED_AMBIGUOUS",
            "assemble_status": "BLOCKED_SIZE_MISSING|BLOCKED_MUTATION_UNAPPROVED|ASSEMBLE_READY|ASSEMBLED_MANUAL|ASSEMBLED_API_APPROVED|ASSEMBLE_FAILED",
            "waybill_status": "WAYBILL_PRESENT_REDACTED|WAYBILL_MISSING|WAYBILL_NOT_APPLICABLE",
            "telegram_alert_status": "NOT_SENT_DRY_RUN_ONLY|ALERT_READY|SENT|SKIPPED",
            "telegram_label_status": "NOT_SENT_DRY_RUN_ONLY|LABEL_READY|SENT|SKIPPED",
            "printed_status": "NOT_PRINTED|PRINT_READY|PRINTED|SKIPPED",
            "close_status": "OPEN|CLOSED|CANCELLED|MANUAL_ONLY",
        }.get(column, ""),
        "description": {
            "idempotency_key": "Stable sha256 over lane and order_hash; falls back to local_ref only when order_hash is unavailable.",
            "order_hash": "Saved order identifier hash only. Raw order id/code must never be persisted.",
            "delivery_kind": "Top-level delivery family; Express delivery is separate from seller self-pickup.",
            "classification": "Detector result from the read-only classification ladder.",
            "courier_planning_at": "Kaspi courier planning timestamp when present.",
            "sla_minutes_to_courier_planning": "Optional computed minutes from detected_at to courier_planning_at.",
            "missing_size_blocker": "Set when MY_SIZE and owner_size_override are both empty.",
            "missing_waybill_blocker": "Set when an already-present redacted waybill signal is absent.",
            "ambiguous_delivery_mode_blocker": "Set when the delivery signal cannot be classified safely.",
            "unapproved_mutation_blocker": "Set until an exact owner-approved order mutation lane exists.",
            "blockers": "Semicolon-delimited blocker list for this row.",
            "warnings": "Semicolon-delimited non-blocking detector warnings.",
            "row_hash": "Stable sha256 over the minimized sidecar row excluding row_hash itself.",
        }.get(column, ""),
        "sensitive_policy": {
            "order_hash": "hash_only",
            "cropped_label_path": "repo_relative_no_raw_url",
            "cropped_label_sha256": "hash_only",
        }.get(column, "no_customer_pii_no_tokens_no_raw_urls"),
    }
    for column in SIDE_CAR_COLUMNS
)


@dataclass(frozen=True)
class ClassificationResult:
    classification: str
    delivery_kind: str
    confidence: str
    express_flag: bool
    express_signal_source: str
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class KaspiOrderFetchError(RuntimeError):
    """Raised when the read-only order-list fetch cannot complete safely."""


def _clean(value: Any) -> str:
    return " ".join(str(value or "").replace("\u00a0", " ").split())


def _path_get(data: Mapping[str, Any], path: str) -> Any:
    if path in data:
        return data[path]
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _first(data: Mapping[str, Any], *paths: str) -> Any:
    for path in paths:
        value = _path_get(data, path)
        if value not in (None, ""):
            return value
    return None


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = _clean(value).lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n", "none", "null", ""}:
        return False
    return None


def _is_pp2_pickup_point(value: Any) -> bool:
    return _clean(value) == PP2_PICKUP_POINT_ID


def _pickup_point_id(row: Mapping[str, Any]) -> str:
    return _clean(
        _first(
            row,
            "pickupPointId",
            "pickup_point_id",
            "attributes.pickupPointId",
            "deliveryPointOfService.id",
            "deliveryPointOfService.code",
            "attributes.deliveryPointOfService.id",
            "attributes.deliveryPointOfService.code",
            "attributes.pointOfService.id",
            "attributes.pointOfService.code",
        )
    )


def _pickup_point_proof_status(row: Mapping[str, Any], *, is_kaspi_delivery: bool | None = None) -> str:
    explicit = _clean(
        _first(
            row,
            "pickup_point_proof_status",
            "point_of_service_proof_status",
            "pp2_point_proof_status",
        )
    )
    if explicit:
        return explicit
    if is_kaspi_delivery is False and _is_pp2_pickup_point(_pickup_point_id(row)):
        return "PP2_PROVEN"
    return ""


def _slot_parts(row: Mapping[str, Any]) -> tuple[str, str, str]:
    slot = _first(row, "deliverySlot_safe", "delivery_slot_safe", "deliverySlot", "delivery_slot", "attributes.deliverySlot")
    if isinstance(slot, Mapping):
        start = _clean(_first(slot, "from", "start", "slotFrom"))
        end = _clean(_first(slot, "to", "end", "slotTo"))
        label = f"{start} - {end}".strip(" -") if start or end else ""
        return start, end, label
    label = _clean(slot)
    if " - " in label:
        start, end = [part.strip() for part in label.split(" - ", 1)]
        return start, end, label
    return "", "", label


def _datetime_minutes(start: str, end: str) -> str:
    if not start or not end:
        return ""
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=ALMATY_TZ)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=ALMATY_TZ)
    minutes = round((end_dt - start_dt).total_seconds() / 60)
    return str(minutes)


def _hash_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_order_hash(row: Mapping[str, Any], *, local_ref: str) -> str:
    existing = _clean(_first(row, "order_hash", "order_ref_hash", "id_hash", "code_hash"))
    if existing:
        if existing.startswith("sha256:"):
            return existing
        return _hash_text(existing)

    raw_ref = _clean(_first(row, "id", "order_id", "orderCode", "order_code", "code"))
    if raw_ref:
        return _hash_text(raw_ref)

    fallback = "|".join(
        (
            local_ref,
            _clean(_first(row, "merchant_id", "attributes.merchantId")),
            _clean(_first(row, "created_at", "attributes.creationDate")),
            _clean(_first(row, "deliveryMode", "delivery_mode", "attributes.deliveryMode")),
            _clean(_first(row, "pickupPointId", "pickup_point_id", "attributes.pickupPointId")),
        )
    )
    return _hash_text(fallback)


def _is_valid_sha256_hash(value: str) -> bool:
    text = _clean(value)
    return text.startswith("sha256:") and len(text) == len("sha256:") + 64


def build_idempotency_key(*, order_hash: str, local_ref: str, lane_id: str = LANE_ID) -> str:
    """Build a rerun-safe sidecar key without depending on rolling row refs."""
    if _is_valid_sha256_hash(order_hash):
        return _hash_text(f"{lane_id}|{order_hash}")
    return _hash_text(f"{lane_id}|{order_hash}|{local_ref}")


def classify_order(row: Mapping[str, Any]) -> ClassificationResult:
    delivery_mode = _clean(_first(row, "deliveryMode", "delivery_mode", "attributes.deliveryMode"))
    is_kaspi_delivery = _parse_bool(_first(row, "isKaspiDelivery", "is_kaspi_delivery", "attributes.isKaspiDelivery"))
    pickup_point_id = _pickup_point_id(row)
    nested_express = _parse_bool(
        _first(row, "kaspiDelivery_safe.express", "attributes.kaspiDelivery.express", "kaspiDelivery.express")
    )
    attributes_express = _parse_bool(_first(row, "attributes_express", "attributes.express", "express"))
    slot_from, slot_to, slot_label = _slot_parts(row)
    courier_planning_at = _clean(
        _first(
            row,
            "courier_planning_at",
            "kaspiDelivery_safe.courierTransmissionPlanningDate",
            "attributes.kaspiDelivery.courierTransmissionPlanningDate",
            "kaspiDelivery.courierTransmissionPlanningDate",
        )
    )

    express_flag = bool(nested_express or attributes_express)
    express_source = ""
    if nested_express is True:
        express_source = "kaspiDelivery.express"
    elif attributes_express is True:
        express_source = "attributes.express"

    seller_pickup_shape = (
        (delivery_mode == "DELIVERY_PICKUP" and is_kaspi_delivery is False)
        or delivery_mode == "DELIVERY_REGIONAL_PICKUP"
    )
    if express_flag and seller_pickup_shape:
        return ClassificationResult(
            classification="MANUAL_AMBIGUOUS_EXPRESS_PICKUP_CONFLICT",
            delivery_kind="MANUAL_REVIEW",
            confidence="LOW",
            express_flag=True,
            express_signal_source=express_source,
            blockers=("ambiguous_delivery_mode",),
        )

    if nested_express is True and delivery_mode == "DELIVERY_LOCAL" and slot_label and courier_planning_at:
        warnings = () if _is_pp2_pickup_point(pickup_point_id) else ("pp2_pickup_point_not_seen",)
        return ClassificationResult(
            classification="KASPI_EXPRESS_DELIVERY_API_MATCH",
            delivery_kind="EXPRESS_DELIVERY",
            confidence="HIGH",
            express_flag=True,
            express_signal_source=express_source,
            warnings=warnings,
        )

    if express_flag:
        blockers: list[str] = []
        warnings: list[str] = []
        if delivery_mode != "DELIVERY_LOCAL":
            blockers.append("ambiguous_delivery_mode")
        if not slot_label:
            warnings.append("delivery_slot_missing")
        if not courier_planning_at:
            warnings.append("courier_planning_missing")
        if not _is_pp2_pickup_point(pickup_point_id):
            warnings.append("pp2_pickup_point_not_seen")
        return ClassificationResult(
            classification="EXPRESS_DELIVERY_CANDIDATE",
            delivery_kind="EXPRESS_DELIVERY",
            confidence="MEDIUM",
            express_flag=True,
            express_signal_source=express_source,
            blockers=tuple(blockers),
            warnings=tuple(warnings),
        )

    if is_kaspi_delivery is True:
        if delivery_mode in {"DELIVERY_PICKUP", "DELIVERY_REGIONAL_TODOOR"}:
            return ClassificationResult(
                classification="KASPI_DELIVERY_PICKUP_OR_POSTOMAT",
                delivery_kind="KASPI_DELIVERY",
                confidence="HIGH",
                express_flag=False,
                express_signal_source="",
            )
        return ClassificationResult(
            classification="NORMAL_KASPI_DELIVERY",
            delivery_kind="NORMAL_DELIVERY",
            confidence="HIGH",
            express_flag=False,
            express_signal_source="",
        )

    if delivery_mode in {"DELIVERY_PICKUP", "DELIVERY_REGIONAL_PICKUP"} and is_kaspi_delivery is None:
        return ClassificationResult(
            classification="MANUAL_AMBIGUOUS_MISSING_KASPI_DELIVERY_FLAG",
            delivery_kind="MANUAL_REVIEW",
            confidence="LOW",
            express_flag=False,
            express_signal_source="",
            blockers=("ambiguous_delivery_mode",),
        )

    if seller_pickup_shape:
        proof_status = _pickup_point_proof_status(row, is_kaspi_delivery=is_kaspi_delivery)
        if proof_status in {"PP2_PROVEN", "PP2_ADDRESS_PROVEN", "POINT_OF_SERVICE_PP2_PROVEN"}:
            return ClassificationResult(
                classification="PP2_SELF_SERVING_PICKUP_PROVEN",
                delivery_kind="SELLER_SELF_PICKUP",
                confidence="HIGH",
                express_flag=False,
                express_signal_source="",
            )
        return ClassificationResult(
            classification="MANUAL_AMBIGUOUS_PICKUP_POINT_NOT_PROVEN",
            delivery_kind="SELLER_SELF_PICKUP",
            confidence="LOW",
            express_flag=False,
            express_signal_source="",
            blockers=("ambiguous_delivery_mode",),
            warnings=("seller_self_pickup_candidate_without_pp2_point_proof",),
        )

    return ClassificationResult(
        classification="MANUAL_AMBIGUOUS_NOT_SELF_PICKUP",
        delivery_kind="MANUAL_REVIEW",
        confidence="LOW",
        express_flag=False,
        express_signal_source="",
        blockers=("ambiguous_delivery_mode",),
    )


def _waybill_status(row: Mapping[str, Any]) -> tuple[str, str]:
    value = _first(
        row,
        "waybill_status",
        "kaspiDelivery_safe.waybill",
        "attributes.kaspiDelivery.waybill",
        "kaspiDelivery.waybill",
        "waybill",
    )
    text = _clean(value)
    if text and text != "redacted_absent":
        return "WAYBILL_PRESENT_REDACTED", "true"
    return "WAYBILL_MISSING", "false"


def build_sidecar_row(
    row: Mapping[str, Any],
    *,
    local_ref: str = "candidate_1",
    detected_at: str | None = None,
) -> dict[str, str]:
    detected_at = detected_at or datetime.now(ALMATY_TZ).isoformat(timespec="seconds")
    classification = classify_order(row)
    order_hash = normalize_order_hash(row, local_ref=local_ref)
    is_kaspi_delivery = _parse_bool(_first(row, "isKaspiDelivery", "is_kaspi_delivery", "attributes.isKaspiDelivery"))
    slot_from, slot_to, slot_label = _slot_parts(row)
    courier_planning_at = _clean(
        _first(
            row,
            "courier_planning_at",
            "kaspiDelivery_safe.courierTransmissionPlanningDate",
            "attributes.kaspiDelivery.courierTransmissionPlanningDate",
            "kaspiDelivery.courierTransmissionPlanningDate",
        )
    )
    planned_delivery_at = _clean(_first(row, "plannedDeliveryDate", "planned_delivery_at", "attributes.plannedDeliveryDate"))
    my_size = _clean(_first(row, "my_size", "MY_SIZE"))
    owner_override = _clean(_first(row, "owner_size_override"))
    size_ready = bool(my_size or owner_override)
    size_status = "SIZE_READY_OWNER_OVERRIDE" if owner_override else "SIZE_READY_FROM_RULE" if my_size else "NEEDS_SIZE"
    sidecar_state = "SIZE_READY" if size_ready else "NEEDS_SIZE"
    assemble_status = "BLOCKED_MUTATION_UNAPPROVED" if size_ready else "BLOCKED_SIZE_MISSING"
    waybill_status, waybill_present = _waybill_status(row)
    if classification.delivery_kind == "SELLER_SELF_PICKUP":
        waybill_status = "WAYBILL_NOT_APPLICABLE"
        waybill_present = "false"

    missing_size_blocker = "" if size_ready else "missing_size"
    missing_waybill_blocker = (
        ""
        if waybill_present == "true" or classification.delivery_kind == "SELLER_SELF_PICKUP"
        else "missing_waybill"
    )
    ambiguous_delivery_mode_blocker = (
        "ambiguous_delivery_mode" if "ambiguous_delivery_mode" in classification.blockers else ""
    )
    mutation_approval = _clean(_first(row, "mutation_approval_status"))
    unapproved_mutation_blocker = "" if mutation_approval == "APPROVED" else "unapproved_mutation"
    blockers = [
        item
        for item in (
            missing_size_blocker,
            missing_waybill_blocker,
            ambiguous_delivery_mode_blocker,
            unapproved_mutation_blocker,
        )
        if item
    ]
    warnings = list(classification.warnings)

    minimized: dict[str, str] = {
        "schema_version": SCHEMA_VERSION,
        "lane_id": LANE_ID,
        "idempotency_key": build_idempotency_key(order_hash=order_hash, local_ref=local_ref),
        "order_hash": order_hash,
        "local_ref": local_ref,
        "store_code": _clean(_first(row, "store_code", "store", "store_label")) or "ACMEWEAR",
        "merchant_id": _clean(_first(row, "merchant_id", "attributes.merchantId")) or ACMEWEAR_MERCHANT_ID,
        "source_endpoint_family": _clean(_first(row, "api_endpoint", "source_endpoint_family")) or "GET /shop/api/v2/orders",
        "detected_at": detected_at,
        "created_at": _clean(_first(row, "created_at", "attributes.creationDate")),
        "last_seen_at": detected_at,
        "sidecar_state": sidecar_state,
        "classification": classification.classification,
        "delivery_kind": classification.delivery_kind,
        "classification_confidence": classification.confidence,
        "state": _clean(_first(row, "state", "attributes.state")),
        "status": _clean(_first(row, "status", "attributes.status")),
        "delivery_type": _clean(_first(row, "deliveryType", "delivery_type", "attributes.deliveryType")),
        "delivery_mode": _clean(_first(row, "deliveryMode", "delivery_mode", "attributes.deliveryMode")),
        "is_kaspi_delivery": str(is_kaspi_delivery),
        "pickup_point_id": _pickup_point_id(row),
        "pickup_point_proof_status": _pickup_point_proof_status(row, is_kaspi_delivery=is_kaspi_delivery),
        "express_flag": str(classification.express_flag).lower(),
        "express_signal_source": classification.express_signal_source,
        "delivery_slot_from": slot_from,
        "delivery_slot_to": slot_to,
        "delivery_slot_label": slot_label,
        "planned_delivery_at": planned_delivery_at,
        "courier_planning_at": courier_planning_at,
        "sla_minutes_to_courier_planning": _datetime_minutes(detected_at, courier_planning_at),
        "sla_deadline_at": courier_planning_at,
        "sku_key": _clean(_first(row, "sku_key")),
        "merchant_article": _clean(_first(row, "merchant_article", "merchantArticle", "offer_code")),
        "ordered_size": _clean(_first(row, "ordered_size", "offer_size")),
        "height_cm": _clean(_first(row, "height_cm")),
        "weight_kg": _clean(_first(row, "weight_kg")),
        "my_size": my_size,
        "owner_size_override": owner_override,
        "size_source": _clean(_first(row, "size_source")),
        "size_status": size_status,
        "assemble_status": assemble_status,
        "waybill_status": waybill_status,
        "waybill_present": waybill_present,
        "cropped_label_path": "",
        "cropped_label_sha256": "",
        "telegram_alert_status": "NOT_SENT_DRY_RUN_ONLY",
        "telegram_label_status": "NOT_SENT_DRY_RUN_ONLY",
        "printed_status": "NOT_PRINTED",
        "close_status": "OPEN",
        "missing_size_blocker": missing_size_blocker,
        "missing_waybill_blocker": missing_waybill_blocker,
        "ambiguous_delivery_mode_blocker": ambiguous_delivery_mode_blocker,
        "unapproved_mutation_blocker": unapproved_mutation_blocker,
        "blockers": ";".join(blockers),
        "warnings": ";".join(warnings),
        "row_hash": "",
    }
    minimized["row_hash"] = _hash_text(
        json.dumps({k: minimized[k] for k in SIDE_CAR_COLUMNS if k != "row_hash"}, sort_keys=True)
    )
    return minimized


def _sidecar_merge_key(row: Mapping[str, str]) -> str:
    lane_id = _clean(row.get("lane_id")) or LANE_ID
    order_hash = _clean(row.get("order_hash"))
    if _is_valid_sha256_hash(order_hash):
        return f"{lane_id}|order_hash|{order_hash}"
    idempotency_key = _clean(row.get("idempotency_key"))
    if idempotency_key:
        return f"{lane_id}|idempotency|{idempotency_key}"
    return f"{lane_id}|local_ref|{_clean(row.get('local_ref'))}"


def _recompute_sidecar_row_hash(row: Mapping[str, str]) -> dict[str, str]:
    normalized = {column: _clean(row.get(column)) for column in SIDE_CAR_COLUMNS}
    normalized["row_hash"] = _hash_text(
        json.dumps({k: normalized[k] for k in SIDE_CAR_COLUMNS if k != "row_hash"}, sort_keys=True)
    )
    return normalized


def _normalize_sidecar_storage_row(row: Mapping[str, str]) -> dict[str, str]:
    normalized = {column: _clean(row.get(column)) for column in SIDE_CAR_COLUMNS}
    if _is_valid_sha256_hash(normalized.get("order_hash", "")):
        normalized["idempotency_key"] = build_idempotency_key(
            order_hash=normalized["order_hash"],
            local_ref=normalized["local_ref"],
        )
    return _recompute_sidecar_row_hash(normalized)


def _merge_same_order_sidecar_rows(
    prior: Mapping[str, str],
    incoming: Mapping[str, str],
) -> dict[str, str]:
    """Refresh API-observed fields without churning the operator-facing row id."""
    merged = {column: _clean(incoming.get(column)) for column in SIDE_CAR_COLUMNS}
    prior_order_hash = _clean(prior.get("order_hash"))
    incoming_order_hash = _clean(incoming.get("order_hash"))
    if _is_valid_sha256_hash(prior_order_hash) and prior_order_hash == incoming_order_hash:
        if _clean(prior.get("local_ref")):
            merged["local_ref"] = _clean(prior.get("local_ref"))
        if _clean(prior.get("detected_at")):
            merged["detected_at"] = _clean(prior.get("detected_at"))
    return _recompute_sidecar_row_hash(merged)


def dedupe_sidecar_rows(rows: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    by_key: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for row in rows:
        normalized = _normalize_sidecar_storage_row(row)
        key = _sidecar_merge_key(normalized)
        if key not in by_key:
            order.append(key)
        prior = by_key.get(key)
        by_key[key] = _merge_same_order_sidecar_rows(prior, normalized) if prior else normalized
    return [by_key[key] for key in order]


def read_sidecar_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for row in reader:
            rows.append({column: _clean(row.get(column)) for column in SIDE_CAR_COLUMNS})
        return rows


def merge_sidecar_rows(
    existing_rows: Iterable[Mapping[str, str]],
    incoming_rows: Iterable[Mapping[str, str]],
) -> tuple[list[dict[str, str]], dict[str, int]]:
    existing_by_key: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for row in existing_rows:
        normalized = _normalize_sidecar_storage_row(row)
        key = _sidecar_merge_key(normalized)
        if not key:
            continue
        if key not in existing_by_key:
            order.append(key)
            existing_by_key[key] = normalized
        else:
            existing_by_key[key] = _merge_same_order_sidecar_rows(existing_by_key[key], normalized)

    inserted = 0
    updated = 0
    unchanged = 0
    for row in incoming_rows:
        normalized = _normalize_sidecar_storage_row(row)
        key = _sidecar_merge_key(normalized)
        if not key:
            continue
        prior = existing_by_key.get(key)
        if prior is None:
            order.append(key)
            inserted += 1
            existing_by_key[key] = normalized
        elif prior == normalized:
            unchanged += 1
        else:
            updated += 1
            existing_by_key[key] = _merge_same_order_sidecar_rows(prior, normalized)

    return [existing_by_key[key] for key in order], {
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "total_after": len(order),
    }


def build_alert_queue_rows(rows: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    queue: list[dict[str, str]] = []
    for row in rows:
        delivery_kind = _clean(row.get("delivery_kind"))
        if delivery_kind not in {"EXPRESS_DELIVERY", "SELLER_SELF_PICKUP"}:
            continue
        if _clean(row.get("close_status")) in {"CLOSED", "CANCELLED"}:
            continue
        blockers = _clean(row.get("blockers"))
        if "ambiguous_delivery_mode" in blockers:
            alert_reason = "manual_review_delivery_mode"
        elif "missing_size" in blockers:
            alert_reason = "contact_customer_for_size"
        elif "unapproved_mutation" in blockers:
            alert_reason = "size_ready_but_assembly_unapproved"
        else:
            alert_reason = "operator_review"
        queue.append(
            {
                "idempotency_key": _clean(row.get("idempotency_key")),
                "order_hash": _clean(row.get("order_hash")),
                "local_ref": _clean(row.get("local_ref")),
                "detected_at": _clean(row.get("detected_at")),
                "sidecar_state": _clean(row.get("sidecar_state")),
                "classification": _clean(row.get("classification")),
                "delivery_kind": delivery_kind,
                "pickup_point_id": _clean(row.get("pickup_point_id")),
                "express_flag": _clean(row.get("express_flag")),
                "delivery_slot_label": _clean(row.get("delivery_slot_label")),
                "courier_planning_at": _clean(row.get("courier_planning_at")),
                "sku_key": _clean(row.get("sku_key")),
                "merchant_article": _clean(row.get("merchant_article")),
                "ordered_size": _clean(row.get("ordered_size")),
                "size_status": _clean(row.get("size_status")),
                "assemble_status": _clean(row.get("assemble_status")),
                "blockers": blockers,
                "warnings": _clean(row.get("warnings")),
                "telegram_alert_status": _clean(row.get("telegram_alert_status")),
                "alert_reason": alert_reason,
            }
        )
    return queue


def _order_hash_short(order_hash: str) -> str:
    text = _clean(order_hash)
    if text.startswith("sha256:"):
        text = text.split(":", 1)[1]
    return text[:8] if text else "unknown"


def build_telegram_alert_idempotency_key(
    row: Mapping[str, str],
    *,
    event_type: str = "immediate_alert",
    template_version: str = TELEGRAM_ALERT_TEMPLATE_VERSION,
) -> str:
    return _hash_text(
        "|".join(
            (
                LANE_ID,
                _clean(row.get("order_hash")),
                "alert",
                _clean(event_type) or "immediate_alert",
                template_version,
            )
        )
    )


def build_telegram_label_idempotency_key(
    *,
    order_hash: str,
    attachment_sha256: str,
    template_version: str = TELEGRAM_LABEL_TEMPLATE_VERSION,
) -> str:
    return _hash_text(
        "|".join(
            (
                LANE_ID,
                _clean(order_hash),
                "label",
                "label_send",
                template_version,
                _clean(attachment_sha256),
            )
        )
    )


def _telegram_required_action(row: Mapping[str, str]) -> str:
    blockers = _clean(row.get("blockers"))
    alert_reason = _clean(row.get("alert_reason"))
    if "ambiguous_delivery_mode" in blockers or alert_reason == "manual_review_delivery_mode":
        return "verify delivery mode / PP2 before any assembly."
    if "missing_size" in blockers or alert_reason == "contact_customer_for_size":
        return "contact customer for height/weight and resolve MY_SIZE or owner_size_override before assembly."
    if "unapproved_mutation" in blockers or alert_reason == "size_ready_but_assembly_unapproved":
        return "size is ready; assembly remains a separate approved/manual gate."
    return "operator review; do not assemble unless size and mutation gates are safe."


def render_telegram_alert_message(row: Mapping[str, str]) -> str:
    product = _clean(row.get("sku_key")) or _clean(row.get("merchant_article")) or "unknown"
    return "\n".join(
        (
            "ACMEWEAR PP2 special order",
            f"Ref: {_clean(row.get('local_ref')) or 'unknown'} / {_order_hash_short(_clean(row.get('order_hash')))}",
            f"Type: {_clean(row.get('delivery_kind')) or 'unknown'} / {_clean(row.get('classification')) or 'unknown'}",
            f"Slot: {_clean(row.get('delivery_slot_label')) or 'unknown'}",
            f"Courier planning: {_clean(row.get('courier_planning_at')) or 'unknown'}",
            f"Product: {product}",
            f"Ordered size: {_clean(row.get('ordered_size')) or 'unknown'}",
            f"Size gate: {_clean(row.get('size_status')) or 'unknown'}",
            f"Assembly: {_clean(row.get('assemble_status')) or 'unknown'}",
            f"Required action: {_telegram_required_action(row)}",
            "Label: alert only; product-label PDF send waits for waybill-ready plus separate approval/config.",
        )
    )


def read_telegram_alert_ledger(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return [{column: _clean(row.get(column)) for column in TELEGRAM_ALERT_LEDGER_COLUMNS} for row in reader]


def _sent_telegram_alert_keys(rows: Iterable[Mapping[str, str]]) -> set[str]:
    return {
        _clean(row.get("idempotency_key"))
        for row in rows
        if _clean(row.get("send_kind")) == "alert" and _clean(row.get("send_status")) == "SENT"
    }


def _sent_telegram_label_keys(rows: Iterable[Mapping[str, str]]) -> set[str]:
    return {
        _clean(row.get("idempotency_key"))
        for row in rows
        if _clean(row.get("send_kind")) == "label" and _clean(row.get("send_status")) == "SENT"
    }


def _telegram_ledger_row(
    row: Mapping[str, str],
    *,
    send_status: str,
    dry_run: bool,
    chat_config_ref: str,
    now_text: str,
    telegram_message_id: str = "",
    last_error_class: str = "",
    attempt_count: str = "0",
) -> dict[str, str]:
    message = render_telegram_alert_message(row)
    return {
        "ledger_version": TELEGRAM_ALERT_LEDGER_VERSION,
        "lane_id": LANE_ID,
        "sidecar_idempotency_key": _clean(row.get("idempotency_key")),
        "order_hash": _clean(row.get("order_hash")),
        "local_ref": _clean(row.get("local_ref")),
        "store_code": "ACMEWEAR",
        "merchant_id": ACMEWEAR_MERCHANT_ID,
        "send_kind": "alert",
        "event_type": "immediate_alert",
        "idempotency_key": build_telegram_alert_idempotency_key(row),
        "template_version": TELEGRAM_ALERT_TEMPLATE_VERSION,
        "payload_sha256": _hash_text(message),
        "message_preview_redacted": message,
        "chat_config_ref": _clean(chat_config_ref) or "ACMEWEAR_EXPRESS_TELEGRAM_CHAT",
        "send_config_ref": _clean(chat_config_ref) or "ACMEWEAR_EXPRESS_TELEGRAM_CHAT",
        "send_approval_ref": "explicit_cli_send_flag" if not dry_run else "",
        "dry_run": str(dry_run).lower(),
        "send_status": send_status,
        "waybill_status_at_send": "",
        "size_status_at_send": _clean(row.get("size_status")),
        "assemble_status_at_send": _clean(row.get("assemble_status")),
        "attachment_required": "false",
        "attachment_kind": "none",
        "attachment_sha256": "",
        "attachment_page_count": "",
        "attachment_width_mm": "",
        "attachment_height_mm": "",
        "sensitive_scan_status": "PASS",
        "telegram_message_id": telegram_message_id,
        "telegram_media_group_id": "",
        "sent_at": now_text if send_status == "SENT" else "",
        "first_seen_at": now_text,
        "last_attempt_at": now_text if not dry_run else "",
        "attempt_count": attempt_count,
        "last_error_class": last_error_class,
        "created_by": "web-auto acmewear-express-sidecar telegram-alert",
        "notes": "no_customer_pii_no_tokens_no_raw_chat_id",
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _build_multipart_form_data(
    *,
    fields: Mapping[str, str],
    file_field: str,
    file_name: str,
    file_bytes: bytes,
    content_type: str,
) -> tuple[bytes, str]:
    boundary = "----webauto-acmewear-express-label-boundary"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    parts.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8"),
            file_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _send_telegram_message(
    *,
    bot_token: str,
    chat_id: str,
    text: str,
    timeout_seconds: int,
    urlopen_func: Any = None,
) -> str:
    opener = urlopen_func or urllib.request.urlopen
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram sendMessage failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("Telegram sendMessage failed") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Telegram sendMessage response was not valid JSON") from exc
    if not isinstance(payload, Mapping) or payload.get("ok") is not True:
        raise RuntimeError("Telegram sendMessage returned ok=false")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise RuntimeError("Telegram sendMessage response missing result")
    return _clean(result.get("message_id"))


def _send_telegram_document(
    *,
    bot_token: str,
    chat_id: str,
    document_path: Path,
    caption: str,
    timeout_seconds: int,
    urlopen_func: Any = None,
) -> str:
    opener = urlopen_func or urllib.request.urlopen
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    body, content_type = _build_multipart_form_data(
        fields={
            "chat_id": chat_id,
            "caption": caption,
            "disable_content_type_detection": "true",
        },
        file_field="document",
        file_name="acmewear_express_product_label.pdf",
        file_bytes=document_path.read_bytes(),
        content_type="application/pdf",
    )
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Telegram sendDocument failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("Telegram sendDocument failed") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("Telegram sendDocument response was not valid JSON") from exc
    if not isinstance(payload, Mapping) or payload.get("ok") is not True:
        raise RuntimeError("Telegram sendDocument returned ok=false")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise RuntimeError("Telegram sendDocument response missing result")
    return _clean(result.get("message_id"))


def run_telegram_alerts_from_queue(
    *,
    alert_queue_csv: Path,
    ledger_csv: Path,
    run_dir: Path,
    send: bool = False,
    bot_token: str = "",
    chat_id: str = "",
    chat_config_ref: str = "ACMEWEAR_EXPRESS_TELEGRAM_CHAT",
    timeout_seconds: int = 20,
    max_alerts: int | None = None,
    now_text: str | None = None,
    urlopen_func: Any = None,
) -> dict[str, Any]:
    if send and (not bot_token or not chat_id):
        raise ValueError("live Telegram send requires bot token and chat id")
    if not alert_queue_csv.exists():
        raise ValueError(f"Alert queue CSV not found: {alert_queue_csv}")

    now_text = now_text or datetime.now(ALMATY_TZ).isoformat(timespec="seconds")
    with alert_queue_csv.open("r", encoding="utf-8", newline="") as handle:
        alert_rows = [{key: _clean(value) for key, value in row.items()} for row in csv.DictReader(handle)]
    if max_alerts is not None:
        alert_rows = alert_rows[: max(0, int(max_alerts))]

    prior_ledger_rows = read_telegram_alert_ledger(ledger_csv)
    sent_keys = _sent_telegram_alert_keys(prior_ledger_rows)
    preview_rows: list[dict[str, str]] = []
    persistent_rows = list(prior_ledger_rows)
    network_send_attempts = 0
    sent_rows = 0
    failed_rows = 0
    skipped_duplicate_rows = 0

    for row in alert_rows:
        alert_key = build_telegram_alert_idempotency_key(row)
        if alert_key in sent_keys:
            skipped_duplicate_rows += 1
            preview_rows.append(
                _telegram_ledger_row(
                    row,
                    send_status="SKIPPED_DUPLICATE",
                    dry_run=not send,
                    chat_config_ref=chat_config_ref,
                    now_text=now_text,
                )
            )
            continue

        if not send:
            preview_rows.append(
                _telegram_ledger_row(
                    row,
                    send_status="DRY_RUN_NOT_SENT",
                    dry_run=True,
                    chat_config_ref=chat_config_ref,
                    now_text=now_text,
                )
            )
            continue

        network_send_attempts += 1
        try:
            telegram_message_id = _send_telegram_message(
                bot_token=bot_token,
                chat_id=chat_id,
                text=render_telegram_alert_message(row),
                timeout_seconds=timeout_seconds,
                urlopen_func=urlopen_func,
            )
        except RuntimeError as exc:
            failed_rows += 1
            ledger_row = _telegram_ledger_row(
                row,
                send_status="FAILED",
                dry_run=False,
                chat_config_ref=chat_config_ref,
                now_text=now_text,
                last_error_class=exc.__class__.__name__,
                attempt_count="1",
            )
        else:
            sent_rows += 1
            ledger_row = _telegram_ledger_row(
                row,
                send_status="SENT",
                dry_run=False,
                chat_config_ref=chat_config_ref,
                now_text=now_text,
                telegram_message_id=telegram_message_id,
                attempt_count="1",
            )
        preview_rows.append(ledger_row)
        persistent_rows.append(ledger_row)

    run_dir.mkdir(parents=True, exist_ok=True)
    payloads_path = run_dir / "telegram_alert_payloads_review.csv"
    ledger_preview_path = run_dir / "telegram_alert_ledger_preview.csv"
    summary_path = run_dir / "telegram_alert_summary.json"
    write_csv(payloads_path, TELEGRAM_ALERT_LEDGER_COLUMNS, preview_rows)
    write_csv(ledger_preview_path, TELEGRAM_ALERT_LEDGER_COLUMNS, preview_rows)
    if send:
        write_csv(ledger_csv, TELEGRAM_ALERT_LEDGER_COLUMNS, persistent_rows)

    summary = {
        "status": "success" if failed_rows == 0 else "partial_failure",
        "lane_id": LANE_ID,
        "ledger_version": TELEGRAM_ALERT_LEDGER_VERSION,
        "template_version": TELEGRAM_ALERT_TEMPLATE_VERSION,
        "send_mode": "live_send" if send else "dry_run",
        "run_dir": str(run_dir),
        "alert_queue_csv": str(alert_queue_csv),
        "ledger_csv": str(ledger_csv),
        "alert_queue_rows": len(alert_rows),
        "preview_rows": len(preview_rows),
        "sent_rows": sent_rows,
        "failed_rows": failed_rows,
        "skipped_duplicate_rows": skipped_duplicate_rows,
        "network_send_attempts": network_send_attempts,
        "payloads_review_path": str(payloads_path),
        "ledger_preview_path": str(ledger_preview_path),
        "external_writes": {
            "kaspi_order_mutation": False,
            "telegram_send": bool(send and sent_rows > 0),
            "print_job": False,
            "autonomous_business_write": False,
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def _telegram_label_ledger_row(
    *,
    order_hash: str,
    local_ref: str,
    sidecar_idempotency_key: str,
    attachment_sha256: str,
    caption: str,
    send_status: str,
    dry_run: bool,
    chat_config_ref: str,
    now_text: str,
    telegram_message_id: str = "",
    last_error_class: str = "",
    attempt_count: str = "0",
) -> dict[str, str]:
    return {
        "ledger_version": TELEGRAM_ALERT_LEDGER_VERSION,
        "lane_id": LANE_ID,
        "sidecar_idempotency_key": _clean(sidecar_idempotency_key),
        "order_hash": _clean(order_hash),
        "local_ref": _clean(local_ref),
        "store_code": "ACMEWEAR",
        "merchant_id": ACMEWEAR_MERCHANT_ID,
        "send_kind": "label",
        "event_type": "label_send",
        "idempotency_key": build_telegram_label_idempotency_key(
            order_hash=order_hash,
            attachment_sha256=attachment_sha256,
        ),
        "template_version": TELEGRAM_LABEL_TEMPLATE_VERSION,
        "payload_sha256": _hash_text(caption),
        "message_preview_redacted": caption,
        "chat_config_ref": _clean(chat_config_ref) or "ACMEWEAR_EXPRESS_TELEGRAM_PRINT_CHAT",
        "send_config_ref": _clean(chat_config_ref) or "ACMEWEAR_EXPRESS_TELEGRAM_PRINT_CHAT",
        "send_approval_ref": "explicit_cli_send_flag" if not dry_run else "",
        "dry_run": str(dry_run).lower(),
        "send_status": send_status,
        "waybill_status_at_send": "WAYBILL_PRESENT_REDACTED",
        "size_status_at_send": "",
        "assemble_status_at_send": "",
        "attachment_required": "true",
        "attachment_kind": "cropped_label_pdf",
        "attachment_sha256": attachment_sha256,
        "attachment_page_count": "1",
        "attachment_width_mm": "75",
        "attachment_height_mm": "120",
        "sensitive_scan_status": "PASS",
        "telegram_message_id": telegram_message_id,
        "telegram_media_group_id": "",
        "sent_at": now_text if send_status == "SENT" else "",
        "first_seen_at": now_text,
        "last_attempt_at": now_text if not dry_run else "",
        "attempt_count": attempt_count,
        "last_error_class": last_error_class,
        "created_by": "web-auto acmewear-express-sidecar telegram-label",
        "notes": "no_customer_pii_no_tokens_no_raw_chat_id_no_raw_file_path",
    }


def run_telegram_label_from_pdf(
    *,
    cropped_label_pdf: Path,
    order_hash: str,
    local_ref: str,
    ledger_csv: Path,
    run_dir: Path,
    send: bool = False,
    bot_token: str = "",
    chat_id: str = "",
    chat_config_ref: str = "ACMEWEAR_EXPRESS_TELEGRAM_PRINT_CHAT",
    sidecar_idempotency_key: str = "",
    caption: str = "ACMEWEAR PP2 product label",
    timeout_seconds: int = 20,
    now_text: str | None = None,
    urlopen_func: Any = None,
) -> dict[str, Any]:
    if send and (not bot_token or not chat_id):
        raise ValueError("live Telegram label send requires bot token and chat id")
    if not cropped_label_pdf.exists():
        raise ValueError("cropped label PDF not found")
    if cropped_label_pdf.suffix.lower() != ".pdf":
        raise ValueError("cropped label must be a PDF")
    if not _clean(order_hash).startswith("sha256:"):
        raise ValueError("--order-hash must be a sha256:... value, not a raw order id")

    now_text = now_text or datetime.now(ALMATY_TZ).isoformat(timespec="seconds")
    attachment_sha256 = _file_sha256(cropped_label_pdf)
    prior_ledger_rows = read_telegram_alert_ledger(ledger_csv)
    sent_keys = _sent_telegram_label_keys(prior_ledger_rows)
    idempotency_key = build_telegram_label_idempotency_key(
        order_hash=order_hash,
        attachment_sha256=attachment_sha256,
    )
    preview_rows: list[dict[str, str]] = []
    persistent_rows = list(prior_ledger_rows)
    skipped_duplicate_rows = 0
    sent_rows = 0
    failed_rows = 0
    network_send_attempts = 0

    if idempotency_key in sent_keys:
        skipped_duplicate_rows = 1
        preview_rows.append(
            _telegram_label_ledger_row(
                order_hash=order_hash,
                local_ref=local_ref,
                sidecar_idempotency_key=sidecar_idempotency_key,
                attachment_sha256=attachment_sha256,
                caption=caption,
                send_status="SKIPPED_DUPLICATE",
                dry_run=not send,
                chat_config_ref=chat_config_ref,
                now_text=now_text,
            )
        )
    elif not send:
        preview_rows.append(
            _telegram_label_ledger_row(
                order_hash=order_hash,
                local_ref=local_ref,
                sidecar_idempotency_key=sidecar_idempotency_key,
                attachment_sha256=attachment_sha256,
                caption=caption,
                send_status="DRY_RUN_NOT_SENT",
                dry_run=True,
                chat_config_ref=chat_config_ref,
                now_text=now_text,
            )
        )
    else:
        network_send_attempts = 1
        try:
            telegram_message_id = _send_telegram_document(
                bot_token=bot_token,
                chat_id=chat_id,
                document_path=cropped_label_pdf,
                caption=caption,
                timeout_seconds=timeout_seconds,
                urlopen_func=urlopen_func,
            )
        except RuntimeError as exc:
            failed_rows = 1
            ledger_row = _telegram_label_ledger_row(
                order_hash=order_hash,
                local_ref=local_ref,
                sidecar_idempotency_key=sidecar_idempotency_key,
                attachment_sha256=attachment_sha256,
                caption=caption,
                send_status="FAILED",
                dry_run=False,
                chat_config_ref=chat_config_ref,
                now_text=now_text,
                last_error_class=exc.__class__.__name__,
                attempt_count="1",
            )
        else:
            sent_rows = 1
            ledger_row = _telegram_label_ledger_row(
                order_hash=order_hash,
                local_ref=local_ref,
                sidecar_idempotency_key=sidecar_idempotency_key,
                attachment_sha256=attachment_sha256,
                caption=caption,
                send_status="SENT",
                dry_run=False,
                chat_config_ref=chat_config_ref,
                now_text=now_text,
                telegram_message_id=telegram_message_id,
                attempt_count="1",
            )
        preview_rows.append(ledger_row)
        persistent_rows.append(ledger_row)

    run_dir.mkdir(parents=True, exist_ok=True)
    label_preview_path = run_dir / "telegram_label_ledger_preview.csv"
    summary_path = run_dir / "telegram_label_summary.json"
    write_csv(label_preview_path, TELEGRAM_ALERT_LEDGER_COLUMNS, preview_rows)
    if send:
        write_csv(ledger_csv, TELEGRAM_ALERT_LEDGER_COLUMNS, persistent_rows)

    summary = {
        "status": "success" if failed_rows == 0 else "partial_failure",
        "lane_id": LANE_ID,
        "ledger_version": TELEGRAM_ALERT_LEDGER_VERSION,
        "template_version": TELEGRAM_LABEL_TEMPLATE_VERSION,
        "send_mode": "live_send" if send else "dry_run",
        "run_dir": str(run_dir),
        "ledger_csv": str(ledger_csv),
        "attachment_sha256": attachment_sha256,
        "attachment_kind": "cropped_label_pdf",
        "attachment_page_count": 1,
        "attachment_width_mm": 75,
        "attachment_height_mm": 120,
        "sent_rows": sent_rows,
        "failed_rows": failed_rows,
        "skipped_duplicate_rows": skipped_duplicate_rows,
        "network_send_attempts": network_send_attempts,
        "ledger_preview_path": str(label_preview_path),
        "external_writes": {
            "kaspi_order_mutation": False,
            "telegram_send": bool(send and sent_rows > 0),
            "print_job": False,
            "autonomous_business_write": False,
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def _load_prepare_label_policy(policy_path: Path) -> tuple[dict[str, Any], list[str]]:
    blockers: list[str] = []
    if not policy_path.exists():
        return {}, ["owner_policy_file_missing"]
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}, ["owner_policy_invalid_json"]
    if not isinstance(policy, dict):
        return {}, ["owner_policy_invalid_shape"]
    if policy.get("status") != "active":
        blockers.append("owner_policy_not_active")
    if policy.get("approved_by_owner") is not True:
        blockers.append("owner_policy_not_owner_approved")
    if policy.get("human_owner_approval_required_per_run") is not False:
        blockers.append("owner_policy_still_requires_per_run_approval")
    if policy.get("lane_id") != LANE_ID:
        blockers.append("owner_policy_lane_mismatch")
    scope = policy.get("scope") if isinstance(policy.get("scope"), Mapping) else {}
    if ACMEWEAR_MERCHANT_ID not in set(scope.get("merchant_account_ids") or []):
        blockers.append("owner_policy_missing_acmewear_merchant")
    if scope.get("allowed_telegram_bot_token_env") != WAYBILL_TELEGRAM_BOT_TOKEN_ENV:
        blockers.append("owner_policy_wrong_waybill_bot_env")
    if scope.get("allowed_telegram_chat_id_env") != GENERIC_TELEGRAM_PRINT_CHAT_ID_ENV:
        blockers.append("owner_policy_wrong_waybill_chat_env")
    return policy, blockers


def _sent_label_row_for_order_hash(ledger_csv: Path, order_hash: str) -> dict[str, str] | None:
    for row in reversed(read_telegram_alert_ledger(ledger_csv)):
        if (
            _clean(row.get("send_kind")) == "label"
            and _clean(row.get("send_status")) == "SENT"
            and _clean(row.get("order_hash")) == _clean(order_hash)
        ):
            return row
    return None


def _waybill_url_from_order(row: Mapping[str, Any]) -> str:
    return _clean(_first(row, "attributes.kaspiDelivery.waybill", "kaspiDelivery.waybill", "waybill"))


def _prepare_label_caption(owner_size: str) -> str:
    size = _clean(owner_size)
    if size:
        return f"ACMEWEAR Express delivery print label. Size: {size}"
    return "ACMEWEAR Express delivery print label"


def _safe_prepare_label_summary(
    *,
    run_dir: Path,
    gate: str,
    status: str,
    order_hash: str = "",
    local_ref: str = "",
    owner_size_override: str = "",
    size_optional: bool = True,
    error: str = "",
    external_writes: Mapping[str, bool] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema_version": PREPARE_LABEL_VERSION,
        "status": status,
        "gate": gate,
        "lane_id": LANE_ID,
        "store": "ACMEWEAR",
        "merchant_id": ACMEWEAR_MERCHANT_ID,
        "order_hash": _clean(order_hash),
        "order_hash_prefix": _order_hash_short(_clean(order_hash)),
        "local_ref": _clean(local_ref),
        "owner_size_override": _clean(owner_size_override),
        "size_optional": bool(size_optional),
        "error": _clean(error),
        "run_dir": str(run_dir),
        "external_writes": dict(
            external_writes
            or {
                "kaspi_order_mutation": False,
                "telegram_send": False,
                "print_job": False,
                "autonomous_business_write": False,
            }
        ),
    }
    summary.update(extra)
    summary_path = run_dir / "prepare_label_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def _default_crop_platform_waybill(
    *,
    input_pdf: Path,
    output_pdf: Path,
    repo_root: Path | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    root = repo_root or Path.cwd()
    script = root / "scripts" / "crop_kaspi_express_waybill_product_label.py"
    if not script.exists():
        raise RuntimeError(f"Express waybill cropper not found: {script}")
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--json",
            "--input",
            str(input_pdf),
            "--output",
            str(output_pdf),
        ],
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"Express waybill cropper failed with exit {completed.returncode}: {detail}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Express waybill cropper did not return JSON") from exc
    if not isinstance(result, Mapping):
        raise RuntimeError("Express waybill cropper returned invalid metadata")
    label_mm = result.get("label_mm")
    if list(label_mm or []) != [75.0, 120.0]:
        raise RuntimeError("Cropped Express label is not 75x120mm")
    return dict(result)


def _select_prepare_label_target(
    *,
    rows: Sequence[Mapping[str, Any]],
    target_order_hash: str = "",
    owner_size_override: str = "",
    generated_at: str,
    local_ref_prefix: str = "api",
) -> tuple[dict[str, Any] | None, list[dict[str, str]], list[dict[str, str]]]:
    candidates: list[dict[str, Any]] = []
    all_sidecars: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=1):
        local_ref = f"{local_ref_prefix}_{index:03d}"
        mutable_row = dict(row)
        if owner_size_override:
            mutable_row["owner_size_override"] = owner_size_override
        sidecar = build_sidecar_row(mutable_row, local_ref=local_ref, detected_at=generated_at)
        all_sidecars.append(sidecar)
        if _clean(target_order_hash) and _clean(sidecar.get("order_hash")) != _clean(target_order_hash):
            continue
        if (
            _clean(sidecar.get("classification")) == "KASPI_EXPRESS_DELIVERY_API_MATCH"
            and _clean(sidecar.get("delivery_kind")) == "EXPRESS_DELIVERY"
            and _clean(sidecar.get("express_flag")) == "true"
        ):
            candidates.append(
                {
                    "row": row,
                    "sidecar": sidecar,
                    "local_ref": local_ref,
                    "order_hash": _clean(sidecar.get("order_hash")),
                    "waybill_url": _waybill_url_from_order(row),
                }
            )
    return (candidates[0] if len(candidates) == 1 else None), candidates, all_sidecars


def run_prepare_express_label(
    *,
    token: str,
    run_dir: Path,
    send_telegram: bool = False,
    bot_token: str = "",
    chat_id: str = "",
    chat_config_ref: str = "ACMEWEAR_EXPRESS_TELEGRAM_PRINT_CHAT",
    order_code: str = "",
    order_hash: str = "",
    owner_size_override: str = "",
    size_optional: bool = True,
    force_resend: bool = False,
    approval_policy_path: Path = DEFAULT_ON_DEMAND_LABEL_APPROVAL_POLICY,
    ledger_csv: Path = DEFAULT_TELEGRAM_LABEL_LEDGER_CSV,
    lookback_hours: int = 96,
    states: Sequence[str] | None = None,
    created_from: str = "",
    created_to: str = "",
    page_size: int = 100,
    max_pages: int = 5,
    timeout_seconds: int = 20,
    now_text: str | None = None,
    api_urlopen_func: Any = None,
    waybill_urlopen_func: Any = None,
    telegram_urlopen_func: Any = None,
    crop_func: Any = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    now_text = now_text or datetime.now(ALMATY_TZ).isoformat(timespec="seconds")
    run_dir.mkdir(parents=True, exist_ok=True)
    policy, policy_blockers = _load_prepare_label_policy(approval_policy_path)
    if policy_blockers:
        return _safe_prepare_label_summary(
            run_dir=run_dir,
            gate="YELLOW_OWNER_POLICY_BLOCKED",
            status="blocked",
            owner_size_override=owner_size_override,
            size_optional=size_optional,
            error=";".join(policy_blockers),
            approval_policy_path=str(approval_policy_path),
            policy_blockers=policy_blockers,
        )
    if send_telegram and (not bot_token or not chat_id):
        return _safe_prepare_label_summary(
            run_dir=run_dir,
            gate="YELLOW_TELEGRAM_CONFIG_MISSING",
            status="blocked",
            owner_size_override=owner_size_override,
            size_optional=size_optional,
            error="live Telegram send requires bot token and chat id",
            approval_policy_path=str(approval_policy_path),
        )
    if not token:
        return _safe_prepare_label_summary(
            run_dir=run_dir,
            gate="YELLOW_KASPI_TOKEN_MISSING",
            status="blocked",
            owner_size_override=owner_size_override,
            size_optional=size_optional,
            error="missing ACMEWEAR Kaspi token",
            approval_policy_path=str(approval_policy_path),
        )

    try:
        if _clean(order_code):
            rows, query_log = fetch_order_api_rows_by_code(
                token=token,
                order_code=order_code,
                timeout_seconds=timeout_seconds,
                urlopen_func=api_urlopen_func,
            )
            local_ref_prefix = "api_code"
        else:
            resolved_from, resolved_to = _resolve_created_bounds_with_lookback(
                created_from=created_from,
                created_to=created_to,
                lookback_hours=lookback_hours,
                now=datetime.fromisoformat(now_text) if now_text else None,
            )
            rows, query_log = fetch_order_api_rows(
                token=token,
                states=states or ("KASPI_DELIVERY", "NEW", "PICKUP"),
                created_from=resolved_from,
                created_to=resolved_to,
                page_size=page_size,
                max_pages=max_pages,
                timeout_seconds=timeout_seconds,
                urlopen_func=api_urlopen_func,
            )
            local_ref_prefix = "api"
    except (KaspiOrderFetchError, ValueError) as exc:
        return _safe_prepare_label_summary(
            run_dir=run_dir,
            gate="YELLOW_ORDER_READ_BLOCKED",
            status="blocked",
            order_hash=order_hash,
            owner_size_override=owner_size_override,
            size_optional=size_optional,
            error=str(exc),
            approval_policy_path=str(approval_policy_path),
        )

    target, candidates, sidecars = _select_prepare_label_target(
        rows=rows,
        target_order_hash=order_hash,
        owner_size_override=_clean(owner_size_override),
        generated_at=now_text,
        local_ref_prefix=local_ref_prefix,
    )
    if not target:
        candidate_refs = [
            {
                "order_hash": _clean(candidate.get("order_hash")),
                "order_hash_prefix": _order_hash_short(_clean(candidate.get("order_hash"))),
                "local_ref": _clean(candidate.get("local_ref")),
            }
            for candidate in candidates
        ]
        gate = "YELLOW_MULTIPLE_ACTIVE_EXPRESS_ORDERS" if len(candidates) > 1 else "YELLOW_ACTIVE_EXPRESS_ORDER_NOT_FOUND"
        return _safe_prepare_label_summary(
            run_dir=run_dir,
            gate=gate,
            status="blocked",
            order_hash=order_hash,
            owner_size_override=owner_size_override,
            size_optional=size_optional,
            error="select exactly one active Express order with --order-code or --order-hash",
            approval_policy_path=str(approval_policy_path),
            source_rows=len(rows),
            express_candidate_rows=len(candidates),
            candidate_refs=candidate_refs,
            query_log=query_log,
            incoming_sidecar_rows=len(sidecars),
        )

    sidecar = target["sidecar"]
    selected_order_hash = _clean(target["order_hash"])
    selected_local_ref = _clean(target["local_ref"])
    waybill_url = _clean(target.get("waybill_url"))
    hard_blockers: list[str] = []
    if _clean(sidecar.get("merchant_id")) not in {"", ACMEWEAR_MERCHANT_ID}:
        hard_blockers.append("merchant_not_acmewear")
    if _clean(sidecar.get("state")) not in {"NEW", "KASPI_DELIVERY", "PICKUP"}:
        hard_blockers.append("order_not_active")
    if not waybill_url:
        hard_blockers.append("existing_waybill_missing")
    if not size_optional and not (_clean(sidecar.get("owner_size_override")) or _clean(sidecar.get("my_size"))):
        hard_blockers.append("size_required_but_missing")
    if hard_blockers:
        return _safe_prepare_label_summary(
            run_dir=run_dir,
            gate="YELLOW_" + "_".join(hard_blockers).upper(),
            status="blocked",
            order_hash=selected_order_hash,
            local_ref=selected_local_ref,
            owner_size_override=owner_size_override,
            size_optional=size_optional,
            error=";".join(hard_blockers),
            approval_policy_path=str(approval_policy_path),
            query_log=query_log,
            api_state=_clean(sidecar.get("state")),
            api_status=_clean(sidecar.get("status")),
            delivery_kind=_clean(sidecar.get("delivery_kind")),
            classification=_clean(sidecar.get("classification")),
            waybill_present=bool(waybill_url),
            size_status=_clean(sidecar.get("size_status")),
            blockers_after_owner_override=_clean(sidecar.get("blockers")),
        )

    prior_sent = _sent_label_row_for_order_hash(ledger_csv, selected_order_hash)
    if prior_sent and not force_resend:
        return _safe_prepare_label_summary(
            run_dir=run_dir,
            gate="GREEN_ALREADY_SENT",
            status="success",
            order_hash=selected_order_hash,
            local_ref=selected_local_ref,
            owner_size_override=owner_size_override,
            size_optional=size_optional,
            approval_policy_path=str(approval_policy_path),
            already_sent=True,
            duplicate_guard="order_hash",
            telegram_message_id=_clean(prior_sent.get("telegram_message_id")),
            attachment_sha256=_clean(prior_sent.get("attachment_sha256")),
            sent_at=_clean(prior_sent.get("sent_at")),
            network_send_attempts=0,
            source_rows=len(rows),
            query_log=query_log,
            api_state=_clean(sidecar.get("state")),
            api_status=_clean(sidecar.get("status")),
            delivery_kind=_clean(sidecar.get("delivery_kind")),
            classification=_clean(sidecar.get("classification")),
            waybill_present=True,
            size_status=_clean(sidecar.get("size_status")),
            blockers_after_owner_override=_clean(sidecar.get("blockers")),
        )

    hash12 = selected_order_hash.split(":", 1)[1][:12]
    platform_pdf = run_dir / f"acmewear_express_waybill_platform_orderhash_{hash12}.pdf"
    cropped_pdf = run_dir / f"acmewear_express_waybill_label_orderhash_{hash12}_75x120.pdf"
    try:
        download_summary = _download_waybill_pdf(
            waybill_url=waybill_url,
            token=token,
            output_path=platform_pdf,
            timeout_seconds=max(timeout_seconds, 45),
            urlopen_func=waybill_urlopen_func,
        )
        cropper = crop_func or _default_crop_platform_waybill
        crop_summary = cropper(
            input_pdf=platform_pdf,
            output_pdf=cropped_pdf,
            repo_root=repo_root,
            timeout_seconds=max(timeout_seconds, 60),
        )
        telegram_summary = run_telegram_label_from_pdf(
            cropped_label_pdf=cropped_pdf,
            order_hash=selected_order_hash,
            local_ref=f"express_{datetime.fromisoformat(now_text).strftime('%Y%m%d')}_{hash12}",
            ledger_csv=ledger_csv,
            run_dir=run_dir / "telegram_label",
            send=send_telegram,
            bot_token=bot_token,
            chat_id=chat_id,
            chat_config_ref=chat_config_ref,
            sidecar_idempotency_key=_clean(sidecar.get("idempotency_key")),
            caption=_prepare_label_caption(owner_size_override),
            timeout_seconds=timeout_seconds,
            now_text=now_text,
            urlopen_func=telegram_urlopen_func,
        )
    except (OSError, RuntimeError, ValueError, KaspiOrderFetchError) as exc:
        return _safe_prepare_label_summary(
            run_dir=run_dir,
            gate="YELLOW_PREPARE_LABEL_EXECUTION_BLOCKED",
            status="blocked",
            order_hash=selected_order_hash,
            local_ref=selected_local_ref,
            owner_size_override=owner_size_override,
            size_optional=size_optional,
            error=str(exc),
            approval_policy_path=str(approval_policy_path),
            query_log=query_log,
            external_writes={
                "kaspi_order_mutation": False,
                "telegram_send": False,
                "print_job": False,
                "autonomous_business_write": False,
            },
        )

    telegram_message_id = ""
    label_preview_text = _clean(telegram_summary.get("ledger_preview_path"))
    label_preview_path = Path(label_preview_text) if label_preview_text else None
    if label_preview_path and label_preview_path.exists():
        preview_rows = read_telegram_alert_ledger(label_preview_path)
        if preview_rows:
            telegram_message_id = _clean(preview_rows[0].get("telegram_message_id"))

    if int(telegram_summary.get("sent_rows") or 0) == 1:
        gate = "GREEN_TELEGRAM_LABEL_SENT"
    elif int(telegram_summary.get("skipped_duplicate_rows") or 0) == 1:
        gate = "GREEN_ALREADY_SENT"
    elif send_telegram:
        gate = "YELLOW_TELEGRAM_LABEL_SEND_FAILED"
    else:
        gate = "GREEN_PREPARE_LABEL_DRY_RUN"

    return _safe_prepare_label_summary(
        run_dir=run_dir,
        gate=gate,
        status="success" if gate.startswith("GREEN") else "blocked",
        order_hash=selected_order_hash,
        local_ref=selected_local_ref,
        owner_size_override=owner_size_override,
        size_optional=size_optional,
        approval_policy_path=str(approval_policy_path),
        source_rows=len(rows),
        query_log=query_log,
        api_state=_clean(sidecar.get("state")),
        api_status=_clean(sidecar.get("status")),
        delivery_kind=_clean(sidecar.get("delivery_kind")),
        classification=_clean(sidecar.get("classification")),
        pickup_point_id=_clean(sidecar.get("pickup_point_id")),
        delivery_slot_label=_clean(sidecar.get("delivery_slot_label")),
        waybill_present=True,
        downloaded_existing_waybill_only=True,
        waybill_generation_called=False,
        order_mutation_called=False,
        print_job_called=False,
        autonomous_business_write_called=False,
        size_status=_clean(sidecar.get("size_status")),
        blockers_after_owner_override=_clean(sidecar.get("blockers")),
        ignored_blockers_for_label_send=["missing_size", "unapproved_mutation"] if size_optional else ["unapproved_mutation"],
        platform_pdf_sha256=download_summary.get("pdf_sha256"),
        platform_pdf_bytes=download_summary.get("pdf_bytes"),
        platform_original_run_path=str(platform_pdf),
        cropped_label_run_path=str(cropped_pdf),
        cropped_label_sha256=_file_sha256(cropped_pdf),
        crop_summary=crop_summary,
        telegram_summary_path=telegram_summary.get("summary_path"),
        telegram_message_id=telegram_message_id,
        telegram_send_summary={
            "send_mode": telegram_summary.get("send_mode"),
            "sent_rows": telegram_summary.get("sent_rows"),
            "failed_rows": telegram_summary.get("failed_rows"),
            "skipped_duplicate_rows": telegram_summary.get("skipped_duplicate_rows"),
            "network_send_attempts": telegram_summary.get("network_send_attempts"),
            "attachment_page_count": telegram_summary.get("attachment_page_count"),
            "attachment_width_mm": telegram_summary.get("attachment_width_mm"),
            "attachment_height_mm": telegram_summary.get("attachment_height_mm"),
        },
        external_writes=telegram_summary.get("external_writes"),
    )


def read_pickup_completion_ledger(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return [{column: _clean(row.get(column)) for column in PICKUP_COMPLETION_LEDGER_COLUMNS} for row in reader]


def build_pickup_completion_key(
    *,
    order_hash: str,
    security_code_sha256: str,
    event_type: str = "pickup_completion_review",
) -> str:
    return _hash_text(
        "|".join(
            (
                LANE_ID,
                _clean(order_hash),
                "pickup_completion",
                _clean(event_type) or "pickup_completion_review",
                _clean(security_code_sha256),
            )
        )
    )


def _normalize_security_code_hash(*, security_code: str = "", security_code_sha256: str = "") -> tuple[str, bool]:
    existing = _clean(security_code_sha256)
    if existing:
        if not existing.startswith("sha256:"):
            raise ValueError("--security-code-sha256 must be a sha256:... value")
        return existing, True
    raw_code = _clean(security_code)
    if not raw_code:
        return "", False
    return _hash_text(raw_code), True


def _select_one_sidecar_row(
    rows: Sequence[Mapping[str, str]],
    *,
    order_hash: str = "",
    local_ref: str = "",
    sidecar_idempotency_key: str = "",
) -> dict[str, str]:
    if not any(_clean(value) for value in (order_hash, local_ref, sidecar_idempotency_key)):
        raise ValueError("sidecar row review requires --order-hash, --local-ref, or --sidecar-idempotency-key")
    matches: list[dict[str, str]] = []
    for row in rows:
        if _clean(order_hash) and _clean(row.get("order_hash")) != _clean(order_hash):
            continue
        if _clean(local_ref) and _clean(row.get("local_ref")) != _clean(local_ref):
            continue
        if _clean(sidecar_idempotency_key) and _clean(row.get("idempotency_key")) != _clean(sidecar_idempotency_key):
            continue
        matches.append({column: _clean(row.get(column)) for column in SIDE_CAR_COLUMNS})
    if not matches:
        raise ValueError("no matching sidecar row found for sidecar row review")
    if len(matches) > 1:
        raise ValueError("sidecar row review matched multiple sidecar rows; add a stricter identifier")
    return matches[0]


def build_pickup_completion_review_row(
    row: Mapping[str, str],
    *,
    security_code: str = "",
    security_code_sha256: str = "",
    customer_arrived: bool = False,
    manual_handoff_confirmed: bool = False,
    now_text: str | None = None,
) -> dict[str, str]:
    now_text = now_text or datetime.now(ALMATY_TZ).isoformat(timespec="seconds")
    security_hash, security_code_provided = _normalize_security_code_hash(
        security_code=security_code,
        security_code_sha256=security_code_sha256,
    )
    delivery_kind = _clean(row.get("delivery_kind"))
    classification = _clean(row.get("classification"))
    pickup_point_id = _clean(row.get("pickup_point_id"))
    my_size = _clean(row.get("my_size"))
    owner_size_override = _clean(row.get("owner_size_override"))
    size_ready = bool(my_size or owner_size_override) or _clean(row.get("size_status")).startswith("SIZE_READY")

    blockers: list[str] = []
    warnings: list[str] = []
    if delivery_kind != "SELLER_SELF_PICKUP":
        blockers.append("not_seller_self_pickup")
    if classification != "PP2_SELF_SERVING_PICKUP_PROVEN":
        blockers.append("pp2_selfpickup_not_proven")
    if pickup_point_id != PP2_PICKUP_POINT_ID:
        blockers.append("pp2_pickup_point_missing")
    if not size_ready:
        blockers.append("missing_size")
    if not security_code_provided:
        blockers.append("missing_security_code")
    if not customer_arrived:
        blockers.append("customer_not_arrived")
    if not manual_handoff_confirmed:
        blockers.append("manual_handoff_not_confirmed")

    source_blockers = _clean(row.get("blockers"))
    if source_blockers:
        warnings.append(f"source_sidecar_blockers={source_blockers}")
    source_warnings = _clean(row.get("warnings"))
    if source_warnings:
        warnings.append(f"source_sidecar_warnings={source_warnings}")

    review_status = "READY_FOR_MANUAL_PICKUP_COMPLETION" if not blockers else "BLOCKED"
    kaspi_completion_status = (
        "NOT_EXECUTED_REVIEW_ONLY_READY_FOR_MANUAL_OR_APPROVED_API"
        if not blockers
        else "NOT_EXECUTED_REVIEW_ONLY_BLOCKED"
    )
    return {
        "ledger_version": PICKUP_COMPLETION_LEDGER_VERSION,
        "lane_id": LANE_ID,
        "sidecar_idempotency_key": _clean(row.get("idempotency_key")),
        "order_hash": _clean(row.get("order_hash")),
        "local_ref": _clean(row.get("local_ref")),
        "store_code": _clean(row.get("store_code")) or "ACMEWEAR",
        "merchant_id": _clean(row.get("merchant_id")) or ACMEWEAR_MERCHANT_ID,
        "event_type": "pickup_completion_review",
        "pickup_completion_key": build_pickup_completion_key(
            order_hash=_clean(row.get("order_hash")),
            security_code_sha256=security_hash,
        ),
        "review_status": review_status,
        "delivery_kind_at_review": delivery_kind,
        "classification_at_review": classification,
        "pickup_point_id": pickup_point_id,
        "size_status_at_review": _clean(row.get("size_status")),
        "my_size": my_size,
        "owner_size_override": owner_size_override,
        "security_code_provided": str(security_code_provided).lower(),
        "security_code_sha256": security_hash,
        "customer_arrived_status": "ARRIVED_CONFIRMED" if customer_arrived else "NOT_CONFIRMED",
        "manual_handoff_status": "HANDED_TO_CUSTOMER_CONFIRMED" if manual_handoff_confirmed else "NOT_CONFIRMED",
        "kaspi_completion_status": kaspi_completion_status,
        "blockers": ";".join(blockers),
        "warnings": ";".join(warnings),
        "created_at": now_text,
        "created_by": "web-auto acmewear-express-sidecar pickup-completion-review",
        "notes": "review_only_no_kaspi_mutation_no_raw_security_code_no_customer_pii",
    }


def _render_pickup_completion_markdown(summary: Mapping[str, Any], review_row: Mapping[str, str]) -> str:
    lines = [
        "# ACMEWEAR PP2 Seller Self-Pickup Completion Review",
        "",
        f"- Status: `{summary.get('review_status')}`",
        f"- Run dir: `{summary.get('run_dir')}`",
        f"- Sidecar: `{summary.get('sidecar_csv')}`",
        f"- Local ref: `{review_row.get('local_ref')}`",
        f"- Order hash: `{review_row.get('order_hash')}`",
        f"- Delivery proof: `{review_row.get('classification_at_review')}`",
        f"- Pickup point: `{review_row.get('pickup_point_id')}`",
        f"- Product size: `{review_row.get('my_size') or review_row.get('owner_size_override') or '-'}`",
        f"- Security code provided: `{review_row.get('security_code_provided')}`",
        f"- Customer arrival: `{review_row.get('customer_arrived_status')}`",
        f"- Manual handoff: `{review_row.get('manual_handoff_status')}`",
        f"- Blockers: `{review_row.get('blockers') or '-'}`",
        "",
        "## Operator Checklist",
        "",
        "1. Confirm the row is `PP2_SELF_SERVING_PICKUP_PROVEN`.",
        "2. Confirm the final size is resolved before handing over the product.",
        "3. Confirm the customer is physically present at PP2.",
        "4. Confirm the security code was provided and hashed, never stored raw.",
        "5. Hand over the exact sized product before any pickup-completion action.",
        "",
        "## Guardrail",
        "",
        "- This command is review-only. It does not complete pickup in Kaspi.",
        "- `READY_FOR_MANUAL_PICKUP_COMPLETION` means the operator can manually complete only after separately deciding to perform that live Kaspi action.",
        "- Automated API/UI pickup completion remains a separate future write gate.",
        "",
    ]
    return "\n".join(lines)


def _env_present(env: Mapping[str, str] | None, name: str) -> bool:
    if not name:
        return False
    source = env if env is not None else {}
    return bool(_clean(source.get(name)))


def build_pickup_completion_api_request_plan(
    review_row: Mapping[str, str],
    *,
    owner_approval_ref: str = "",
    order_id_env: str = "ACMEWEAR_PICKUP_ORDER_ID",
    order_code_env: str = "ACMEWEAR_PICKUP_ORDER_CODE",
    security_code_env: str = "ACMEWEAR_PICKUP_SECURITY_CODE",
    env: Mapping[str, str] | None = None,
    now_text: str | None = None,
) -> dict[str, Any]:
    """Build a redacted pickup-completion POST plan without executing it."""

    now_text = now_text or datetime.now(ALMATY_TZ).isoformat(timespec="seconds")
    owner_approval_ref = _clean(owner_approval_ref)
    order_id_env = _clean(order_id_env) or "ACMEWEAR_PICKUP_ORDER_ID"
    order_code_env = _clean(order_code_env) or "ACMEWEAR_PICKUP_ORDER_CODE"
    security_code_env = _clean(security_code_env) or "ACMEWEAR_PICKUP_SECURITY_CODE"
    order_id_present = _env_present(env, order_id_env)
    order_code_present = _env_present(env, order_code_env)
    security_code_present = review_row.get("security_code_provided") == "true"
    review_blockers = _split_semicolon_text(str(review_row.get("blockers", "")))
    blockers_without_security_code = [blocker for blocker in review_blockers if blocker != "missing_security_code"]
    review_ready_for_code_request = not blockers_without_security_code
    completion_ready = review_row.get("review_status") == "READY_FOR_MANUAL_PICKUP_COMPLETION"

    blockers: list[str] = []
    if not review_ready_for_code_request:
        blockers.append("pickup_review_not_ready")
    if not owner_approval_ref:
        blockers.append("missing_owner_approval_ref")
    if not order_id_present:
        blockers.append(f"missing_env:{order_id_env}")
    if not order_code_present:
        blockers.append(f"missing_env:{order_code_env}")
    if completion_ready and not security_code_present:
        blockers.append(f"missing_env_or_hash:{security_code_env}")

    if blockers:
        gate = "BLOCKED"
    elif completion_ready:
        gate = "READY_FOR_OWNER_APPROVED_API_PILOT"
    elif review_ready_for_code_request and not security_code_present:
        gate = "READY_FOR_OWNER_APPROVED_SECURITY_CODE_REQUEST"
    else:
        gate = "BLOCKED"

    send_code_step_blockers: list[str] = []
    if not review_ready_for_code_request:
        send_code_step_blockers.append("pickup_review_not_ready")
    if not owner_approval_ref:
        send_code_step_blockers.append("missing_owner_approval_ref")
    if not order_id_present:
        send_code_step_blockers.append(f"missing_env:{order_id_env}")
    if not order_code_present:
        send_code_step_blockers.append(f"missing_env:{order_code_env}")
    complete_step_blockers = [
        *send_code_step_blockers,
        *([] if security_code_present else [f"missing_env_or_hash:{security_code_env}"]),
    ]
    send_code_request = {
        "method": "POST",
        "url": DEFAULT_ORDER_API_URL,
        "headers_redacted": {
            "Content-Type": "application/vnd.api+json",
            "X-Auth-Token": "<KASPI_TOKEN_ACMEWEAR or legacy ACMEWEAR_KASPI_API_TOKEN>",
            "X-Security-Code": "<empty string; sends code to customer app>",
            "X-Send-Code": "true",
        },
        "body_template_redacted": {
            "data": {
                "type": "orders",
                "id": f"<raw order id from ${order_id_env}; never persist>",
                "attributes": {
                    "code": f"<raw order code from ${order_code_env}; never persist>",
                    "status": "COMPLETED",
                },
            }
        },
    }
    completion_request = {
        "method": "POST",
        "url": DEFAULT_ORDER_API_URL,
        "headers_redacted": {
            "Content-Type": "application/vnd.api+json",
            "X-Auth-Token": "<KASPI_TOKEN_ACMEWEAR or legacy ACMEWEAR_KASPI_API_TOKEN>",
            "X-Security-Code": f"<raw customer code from ${security_code_env}; never persist>",
            "X-Send-Code": "true",
        },
        "body_template_redacted": {
            "data": {
                "type": "orders",
                "id": f"<raw order id from ${order_id_env}; never persist>",
                "attributes": {
                    "code": f"<raw order code from ${order_code_env}; never persist>",
                    "status": "COMPLETED",
                },
            }
        },
    }
    return {
        "plan_version": PICKUP_COMPLETION_API_PLAN_VERSION,
        "lane_id": LANE_ID,
        "created_at": now_text,
        "gate": gate,
        "execute_allowed": False,
        "would_mutate_kaspi": True,
        "live_execution_requires_separate_command_and_owner_approval": True,
        "owner_approval_ref": owner_approval_ref,
        "blockers": blockers,
        "source_review_blockers": review_blockers,
        "source_review_status": review_row.get("review_status", ""),
        "source_sidecar_idempotency_key": review_row.get("sidecar_idempotency_key", ""),
        "order_hash": review_row.get("order_hash", ""),
        "local_ref": review_row.get("local_ref", ""),
        "merchant_id": review_row.get("merchant_id", ""),
        "pickup_point_id": review_row.get("pickup_point_id", ""),
        "final_size": review_row.get("my_size") or review_row.get("owner_size_override") or "",
        "order_id_env": order_id_env,
        "order_id_env_present": order_id_present,
        "order_code_env": order_code_env,
        "order_code_env_present": order_code_present,
        "security_code_env": security_code_env,
        "security_code_provided": security_code_present,
        "security_code_sha256": review_row.get("security_code_sha256", ""),
        "official_doc_url": PICKUP_COMPLETION_OFFICIAL_DOC_URL,
        "flow_steps": [
            {
                "step": "send_customer_code",
                "purpose": "Send pickup completion security code to the customer's Kaspi mobile app.",
                "execute_allowed": False,
                "gate": "READY_FOR_OWNER_APPROVED_SECURITY_CODE_REQUEST" if not send_code_step_blockers else "BLOCKED",
                "blockers": send_code_step_blockers,
            },
            {
                "step": "complete_with_security_code",
                "purpose": "Complete the already-handed-off seller self-pickup order after customer provides the code.",
                "execute_allowed": False,
                "gate": "READY_FOR_OWNER_APPROVED_API_PILOT" if not complete_step_blockers else "BLOCKED",
                "blockers": complete_step_blockers,
            },
        ],
        "request_sequence": {
            "send_customer_code": send_code_request,
            "complete_with_security_code": completion_request,
        },
        "request": completion_request,
        "guardrails": [
            "This artifact does not execute the POST.",
            "Kaspi's documented pickup-completion flow is two-step: first send the code with empty X-Security-Code, then complete with the customer-provided code.",
            "Raw order id, order code, auth token, and security code must not be persisted.",
            "Use only for a proven PP2 seller self-pickup row after customer arrival and handoff.",
            "After any future approved live POST, perform fresh Kaspi readback before marking closed.",
        ],
    }


def _render_pickup_completion_api_plan_markdown(plan: Mapping[str, Any]) -> str:
    request = plan.get("request") if isinstance(plan.get("request"), Mapping) else {}
    request_sequence = plan.get("request_sequence") if isinstance(plan.get("request_sequence"), Mapping) else {}
    flow_steps = plan.get("flow_steps") if isinstance(plan.get("flow_steps"), (list, tuple)) else []
    body_template = request.get("body_template_redacted") if isinstance(request.get("body_template_redacted"), Mapping) else {}
    blockers = plan.get("blockers") if isinstance(plan.get("blockers"), (list, tuple)) else []
    lines = [
        "# ACMEWEAR PP2 Pickup Completion API Request Plan",
        "",
        f"- Gate: `{plan.get('gate')}`",
        f"- Execute allowed now: `{plan.get('execute_allowed')}`",
        f"- Would mutate Kaspi if executed: `{plan.get('would_mutate_kaspi')}`",
        f"- Owner approval ref: `{plan.get('owner_approval_ref') or '-'}`",
        f"- Local ref: `{plan.get('local_ref')}`",
        f"- Order hash: `{plan.get('order_hash')}`",
        f"- Final size: `{plan.get('final_size') or '-'}`",
        f"- Pickup point: `{plan.get('pickup_point_id')}`",
        f"- Official doc: `{plan.get('official_doc_url')}`",
        "",
        "## Documented Two-Step Flow",
        "",
        "- Kaspi documents pickup completion as two POST requests: first with an empty `X-Security-Code` to send the customer code, then again with the customer-provided code.",
        "- This artifact is still review-only; both steps keep `execute_allowed=false`.",
        "",
        "| Step | Gate | Purpose | Blockers |",
        "|---|---|---|---|",
    ]
    for step in flow_steps:
        if not isinstance(step, Mapping):
            continue
        step_blockers = step.get("blockers") if isinstance(step.get("blockers"), (list, tuple)) else []
        lines.append(
            "| "
            + " | ".join(
                [
                    str(step.get("step", "")),
                    str(step.get("gate", "")),
                    str(step.get("purpose", "")),
                    ", ".join(str(blocker) for blocker in step_blockers) or "-",
                ]
            )
            + " |"
        )
    if not flow_steps:
        lines.append("| - | - | - | - |")
    lines.extend(
        [
            "",
            "## Redacted Request Sequence",
            "",
        ]
    )
    for sequence_key in ("send_customer_code", "complete_with_security_code"):
        sequence_request = request_sequence.get(sequence_key)
        if not isinstance(sequence_request, Mapping):
            continue
        sequence_body = sequence_request.get("body_template_redacted")
        if not isinstance(sequence_body, Mapping):
            sequence_body = {}
        lines.extend(
            [
                f"### {sequence_key}",
                "",
                f"- Method: `{sequence_request.get('method')}`",
                f"- URL: `{sequence_request.get('url')}`",
                f"- Headers: `{json.dumps(sequence_request.get('headers_redacted', {}), ensure_ascii=False)}`",
                f"- Body template: `{json.dumps(sequence_body, ensure_ascii=False)}`",
                "",
            ]
        )
    lines.extend(
        [
            "## Redacted Completion Request Shape",
            "",
            "Kept for backward compatibility with earlier review tooling; this is the second request in the documented sequence.",
            "",
        ]
    )
    lines.extend(
        [
            "",
            "## Redacted Request Shape",
        "",
        f"- Method: `{request.get('method')}`",
        f"- URL: `{request.get('url')}`",
        f"- Headers: `{json.dumps(request.get('headers_redacted', {}), ensure_ascii=False)}`",
        f"- Body template: `{json.dumps(body_template, ensure_ascii=False)}`",
        "",
        "## Env Gates",
        "",
        f"- Order id env `{plan.get('order_id_env')}` present: `{plan.get('order_id_env_present')}`",
        f"- Order code env `{plan.get('order_code_env')}` present: `{plan.get('order_code_env_present')}`",
        f"- Security code env `{plan.get('security_code_env')}` provided/hash present: `{plan.get('security_code_provided')}`",
        "",
        "## Blockers",
        "",
        *(f"- `{blocker}`" for blocker in blockers),
        *(["- None"] if not blockers else []),
        "",
        "## Guardrails",
        "",
        "- This is a review artifact only; it does not send the POST.",
        "- Do not persist raw security code, raw order id, raw order code, API token, customer phone, or address.",
        "- A future live executor must use a separate exact owner approval and then verify fresh Kaspi readback.",
        "",
        ]
    )
    return "\n".join(lines)


def _load_pickup_completion_api_plan(api_plan_path: Path) -> dict[str, Any]:
    if not api_plan_path.exists():
        raise ValueError(f"Pickup completion API plan not found: {api_plan_path}")
    plan = json.loads(api_plan_path.read_text(encoding="utf-8"))
    if not isinstance(plan, dict):
        raise ValueError("Pickup completion API plan must be a JSON object")
    if plan.get("plan_version") != PICKUP_COMPLETION_API_PLAN_VERSION:
        raise ValueError("Pickup completion API plan version mismatch")
    if plan.get("lane_id") != LANE_ID:
        raise ValueError("Pickup completion API plan lane mismatch")
    return plan


def _pickup_completion_flow_step_gate(plan: Mapping[str, Any], step: str) -> tuple[str, list[str]]:
    flow_steps = plan.get("flow_steps") if isinstance(plan.get("flow_steps"), (list, tuple)) else []
    for item in flow_steps:
        if not isinstance(item, Mapping):
            continue
        if _clean(item.get("step")) == step:
            blockers = item.get("blockers") if isinstance(item.get("blockers"), (list, tuple)) else []
            return _clean(item.get("gate")), [_clean(blocker) for blocker in blockers if _clean(blocker)]
    return "", ["missing_flow_step"]


def _pickup_completion_raw_request(
    *,
    step: str,
    token: str,
    order_id: str,
    order_code: str,
    security_code: str = "",
) -> urllib.request.Request:
    headers = {
        "Accept": "application/vnd.api+json",
        "Content-Type": "application/vnd.api+json",
        "User-Agent": "web-auto-acmewear-pickup-completion/1.0",
        "X-Auth-Token": token,
        "X-Send-Code": "true",
        "X-Security-Code": "" if step == "send_customer_code" else security_code,
    }
    body = {
        "data": {
            "type": "orders",
            "id": order_id,
            "attributes": {
                "code": order_code,
                "status": "COMPLETED",
            },
        }
    }
    return urllib.request.Request(
        DEFAULT_ORDER_API_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )


def _redacted_pickup_execution_request_shape(
    *,
    step: str,
    effective_token_env: str,
    order_id_env: str,
    order_code_env: str,
    security_code_env: str,
) -> dict[str, Any]:
    return {
        "method": "POST",
        "url": DEFAULT_ORDER_API_URL,
        "headers_redacted": {
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
            "X-Auth-Token": f"<raw token from ${effective_token_env}; never persist>",
            "X-Send-Code": "true",
            "X-Security-Code": (
                "<empty string; sends code to customer app>"
                if step == "send_customer_code"
                else f"<raw customer code from ${security_code_env}; never persist>"
            ),
        },
        "body_template_redacted": {
            "data": {
                "type": "orders",
                "id": f"<raw order id from ${order_id_env}; never persist>",
                "attributes": {
                    "code": f"<raw order code from ${order_code_env}; never persist>",
                    "status": "COMPLETED",
                },
            }
        },
    }


def _render_pickup_completion_api_execution_markdown(summary: Mapping[str, Any]) -> str:
    blockers = summary.get("blockers") if isinstance(summary.get("blockers"), (list, tuple)) else []
    warnings = summary.get("warnings") if isinstance(summary.get("warnings"), (list, tuple)) else []
    lines = [
        "# ACMEWEAR PP2 Pickup Completion API Execution Review",
        "",
        f"- Status: `{summary.get('status')}`",
        f"- Step: `{summary.get('step')}`",
        f"- Dry run: `{summary.get('dry_run')}`",
        f"- Execute requested: `{summary.get('execute_requested')}`",
        f"- External Kaspi mutation: `{summary.get('external_writes', {}).get('kaspi_order_mutation')}`",
        f"- HTTP status: `{summary.get('http_status') or '-'}`",
        f"- Local ref: `{summary.get('local_ref')}`",
        f"- Order hash: `{summary.get('order_hash')}`",
        f"- Owner approval ref: `{summary.get('owner_approval_ref') or '-'}`",
        f"- Effective token env: `{summary.get('effective_token_env') or '-'}`",
        f"- Plan path: `{summary.get('api_plan_path')}`",
        "",
        "## Request Shape",
        "",
        f"`{json.dumps(summary.get('request_shape_redacted', {}), ensure_ascii=False)}`",
        "",
        "## Blockers",
        "",
        *(f"- `{blocker}`" for blocker in blockers),
        *(["- None"] if not blockers else []),
        "",
        "## Warnings",
        "",
        *(f"- `{warning}`" for warning in warnings),
        *(["- None"] if not warnings else []),
        "",
        "## Guardrails",
        "",
        "- Dry-run mode never performs a network request.",
        "- Raw token, order id, order code, and customer security code are accepted only through environment variables.",
        "- Artifacts persist only env names, hashes already present in the sidecar/plan, status code, response length, and redacted request shape.",
        "- Use `send_customer_code` before `complete_with_security_code` unless the customer already has a fresh code.",
        "- After a live execution, perform a fresh Kaspi readback before marking the sidecar row closed.",
        "",
    ]
    return "\n".join(lines)


def run_pickup_completion_api_execute(
    *,
    api_plan_path: Path,
    run_dir: Path,
    step: str,
    owner_approval_ref: str = "",
    token_env: str = DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV,
    order_id_env: str = "ACMEWEAR_PICKUP_ORDER_ID",
    order_code_env: str = "ACMEWEAR_PICKUP_ORDER_CODE",
    security_code_env: str = "ACMEWEAR_PICKUP_SECURITY_CODE",
    execute: bool = False,
    timeout_seconds: int = 20,
    env: Mapping[str, str] | None = None,
    now_text: str | None = None,
    urlopen_func: Any = None,
) -> dict[str, Any]:
    """Execute or dry-run one owner-approved pickup-completion API step.

    The default mode is a dry run. Live execution is allowed only after all
    blockers are absent and --execute is explicitly supplied.
    """

    plan = _load_pickup_completion_api_plan(api_plan_path)
    step = _clean(step)
    if step not in {"send_customer_code", "complete_with_security_code"}:
        raise ValueError("--step must be send_customer_code or complete_with_security_code")
    owner_approval_ref = _clean(owner_approval_ref)
    expected_owner_ref = _clean(plan.get("owner_approval_ref"))
    now_text = now_text or datetime.now(ALMATY_TZ).isoformat(timespec="seconds")
    source_env = env if env is not None else {}
    effective_token_env = resolve_present_acmewear_kaspi_token_env(source_env, token_env)
    order_id_env = _clean(order_id_env) or "ACMEWEAR_PICKUP_ORDER_ID"
    order_code_env = _clean(order_code_env) or "ACMEWEAR_PICKUP_ORDER_CODE"
    security_code_env = _clean(security_code_env) or "ACMEWEAR_PICKUP_SECURITY_CODE"
    order_id = _clean(source_env.get(order_id_env))
    order_code = _clean(source_env.get(order_code_env))
    security_code = _clean(source_env.get(security_code_env))
    token = _clean(source_env.get(effective_token_env)) if effective_token_env else ""
    flow_gate, flow_blockers = _pickup_completion_flow_step_gate(plan, step)
    required_flow_gate = (
        "READY_FOR_OWNER_APPROVED_SECURITY_CODE_REQUEST"
        if step == "send_customer_code"
        else "READY_FOR_OWNER_APPROVED_API_PILOT"
    )

    blockers: list[str] = []
    warnings: list[str] = []
    if flow_gate != required_flow_gate:
        blockers.append(f"plan_step_gate_not_ready:{flow_gate or 'missing'}")
    blockers.extend(flow_blockers)
    if not owner_approval_ref:
        blockers.append("missing_owner_approval_ref")
    elif expected_owner_ref and owner_approval_ref != expected_owner_ref:
        blockers.append("owner_approval_ref_mismatch")
    if not effective_token_env:
        blockers.append(
            "missing_env_any:" + ",".join(acmewear_kaspi_token_env_candidates(token_env))
        )
    if not order_id:
        blockers.append(f"missing_env:{order_id_env}")
    if not order_code:
        blockers.append(f"missing_env:{order_code_env}")
    if step == "complete_with_security_code" and not security_code:
        blockers.append(f"missing_env:{security_code_env}")
    if execute and blockers:
        warnings.append("execute_requested_but_blocked")
    if not execute:
        warnings.append("dry_run_no_network_request")

    run_dir.mkdir(parents=True, exist_ok=True)
    request_shape = _redacted_pickup_execution_request_shape(
        step=step,
        effective_token_env=effective_token_env or _clean(token_env) or DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV,
        order_id_env=order_id_env,
        order_code_env=order_code_env,
        security_code_env=security_code_env,
    )
    status = "DRY_RUN_READY" if not execute and not blockers else "BLOCKED"
    http_status = ""
    response_bytes = ""
    network_attempted = False

    if execute and not blockers:
        request = _pickup_completion_raw_request(
            step=step,
            token=token,
            order_id=order_id,
            order_code=order_code,
            security_code=security_code,
        )
        opener = urlopen_func or urllib.request.urlopen
        network_attempted = True
        try:
            with opener(request, timeout=timeout_seconds) as response:
                payload = response.read()
                http_status = str(getattr(response, "status", getattr(response, "code", "")) or "")
                response_bytes = str(len(payload))
                status = "LIVE_EXECUTED_HTTP_OK"
        except urllib.error.HTTPError as exc:
            # Do not persist response bodies; they may contain order details.
            http_status = str(exc.code)
            response_bytes = ""
            status = "LIVE_EXECUTED_HTTP_ERROR"
        except urllib.error.URLError as exc:
            http_status = ""
            response_bytes = ""
            status = "LIVE_EXECUTION_URL_ERROR"
            warnings.append(f"url_error:{_clean(getattr(exc, 'reason', 'unknown'))}")
    elif execute:
        status = "BLOCKED"

    idempotency_key = _hash_text(
        "|".join(
            (
                LANE_ID,
                "pickup_completion_api_execution",
                _clean(plan.get("order_hash")),
                _clean(plan.get("local_ref")),
                step,
                owner_approval_ref,
                "execute" if execute else "dry_run",
            )
        )
    )
    ledger_row = {
        "execution_version": PICKUP_COMPLETION_API_EXECUTION_VERSION,
        "lane_id": LANE_ID,
        "idempotency_key": idempotency_key,
        "local_ref": _clean(plan.get("local_ref")),
        "order_hash": _clean(plan.get("order_hash")),
        "step": step,
        "dry_run": str(not execute).lower(),
        "execute_requested": str(bool(execute)).lower(),
        "execution_status": status,
        "http_status": http_status,
        "response_bytes": response_bytes,
        "api_plan_gate": _clean(plan.get("gate")),
        "api_plan_path": str(api_plan_path),
        "owner_approval_ref": owner_approval_ref,
        "effective_token_env": effective_token_env,
        "order_id_env": order_id_env,
        "order_id_env_present": str(bool(order_id)).lower(),
        "order_code_env": order_code_env,
        "order_code_env_present": str(bool(order_code)).lower(),
        "security_code_env": security_code_env,
        "security_code_env_present": str(bool(security_code)).lower(),
        "created_at": now_text,
        "blockers": ";".join(_dedupe_texts(blockers)),
        "warnings": ";".join(_dedupe_texts(warnings)),
        "notes": "no_raw_token_no_raw_order_id_no_raw_order_code_no_raw_security_code_persisted",
    }
    summary_path = run_dir / "pickup_completion_api_execution_summary.json"
    report_path = run_dir / "pickup_completion_api_execution_report.md"
    ledger_preview_path = run_dir / "pickup_completion_api_execution_ledger_preview.csv"
    request_shape_path = run_dir / "pickup_completion_api_request_shape_redacted.json"
    write_csv(ledger_preview_path, PICKUP_COMPLETION_API_EXECUTION_COLUMNS, [ledger_row])
    request_shape_path.write_text(json.dumps(request_shape, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "status": status,
        "lane_id": LANE_ID,
        "execution_version": PICKUP_COMPLETION_API_EXECUTION_VERSION,
        "run_dir": str(run_dir),
        "api_plan_path": str(api_plan_path),
        "step": step,
        "dry_run": not execute,
        "execute_requested": bool(execute),
        "network_attempted": network_attempted,
        "http_status": http_status,
        "response_bytes": response_bytes,
        "api_plan_gate": _clean(plan.get("gate")),
        "step_gate": flow_gate,
        "owner_approval_ref": owner_approval_ref,
        "effective_token_env": effective_token_env,
        "order_id_env": order_id_env,
        "order_id_env_present": bool(order_id),
        "order_code_env": order_code_env,
        "order_code_env_present": bool(order_code),
        "security_code_env": security_code_env,
        "security_code_env_present": bool(security_code),
        "local_ref": _clean(plan.get("local_ref")),
        "order_hash": _clean(plan.get("order_hash")),
        "blockers": _dedupe_texts(blockers),
        "warnings": _dedupe_texts(warnings),
        "request_shape_redacted": request_shape,
        "request_shape_redacted_path": str(request_shape_path),
        "ledger_preview_path": str(ledger_preview_path),
        "report_path": str(report_path),
        "external_writes": {
            "kaspi_order_mutation": bool(execute and network_attempted),
            "telegram_send": False,
            "print_job": False,
            "autonomous_business_write": False,
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(_render_pickup_completion_api_execution_markdown(summary), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def run_pickup_completion_review(
    *,
    sidecar_csv: Path,
    run_dir: Path,
    order_hash: str = "",
    local_ref: str = "",
    sidecar_idempotency_key: str = "",
    security_code: str = "",
    security_code_sha256: str = "",
    customer_arrived: bool = False,
    manual_handoff_confirmed: bool = False,
    ledger_csv: Path | None = None,
    write_ledger: bool = False,
    build_api_plan: bool = False,
    owner_approval_ref: str = "",
    order_id_env: str = "ACMEWEAR_PICKUP_ORDER_ID",
    order_code_env: str = "ACMEWEAR_PICKUP_ORDER_CODE",
    security_code_env: str = "ACMEWEAR_PICKUP_SECURITY_CODE",
    env: Mapping[str, str] | None = None,
    now_text: str | None = None,
) -> dict[str, Any]:
    if not sidecar_csv.exists():
        raise ValueError(f"Sidecar CSV not found: {sidecar_csv}")
    if write_ledger and ledger_csv is None:
        raise ValueError("--write-ledger requires --ledger-csv")
    rows = read_sidecar_csv(sidecar_csv)
    selected = _select_one_sidecar_row(
        rows,
        order_hash=order_hash,
        local_ref=local_ref,
        sidecar_idempotency_key=sidecar_idempotency_key,
    )
    review_row = build_pickup_completion_review_row(
        selected,
        security_code=security_code,
        security_code_sha256=security_code_sha256,
        customer_arrived=customer_arrived,
        manual_handoff_confirmed=manual_handoff_confirmed,
        now_text=now_text,
    )
    existing_ledger = read_pickup_completion_ledger(ledger_csv) if ledger_csv else []
    duplicate_rows = [
        row for row in existing_ledger if _clean(row.get("pickup_completion_key")) == review_row["pickup_completion_key"]
    ]

    run_dir.mkdir(parents=True, exist_ok=True)
    review_path = run_dir / "pickup_completion_review.csv"
    ledger_preview_path = run_dir / "pickup_completion_ledger_preview.csv"
    summary_path = run_dir / "pickup_completion_summary.json"
    report_path = run_dir / "pickup_completion_report.md"
    api_plan_path = run_dir / "pickup_completion_api_request_plan.json"
    api_plan_report_path = run_dir / "pickup_completion_api_request_plan.md"
    write_csv(review_path, PICKUP_COMPLETION_LEDGER_COLUMNS, [review_row])
    write_csv(ledger_preview_path, PICKUP_COMPLETION_LEDGER_COLUMNS, [review_row])

    persisted_ledger_path = ""
    if write_ledger and ledger_csv is not None:
        if not duplicate_rows:
            write_csv(ledger_csv, PICKUP_COMPLETION_LEDGER_COLUMNS, [*existing_ledger, review_row])
        else:
            write_csv(ledger_csv, PICKUP_COMPLETION_LEDGER_COLUMNS, existing_ledger)
        persisted_ledger_path = str(ledger_csv)

    api_plan_summary: dict[str, Any] = {}
    if build_api_plan:
        api_plan = build_pickup_completion_api_request_plan(
            review_row,
            owner_approval_ref=owner_approval_ref,
            order_id_env=order_id_env,
            order_code_env=order_code_env,
            security_code_env=security_code_env,
            env=env,
            now_text=review_row["created_at"],
        )
        api_plan_path.write_text(json.dumps(api_plan, ensure_ascii=False, indent=2), encoding="utf-8")
        api_plan_report_path.write_text(_render_pickup_completion_api_plan_markdown(api_plan), encoding="utf-8")
        api_plan_summary = {
            "api_plan_gate": api_plan["gate"],
            "api_plan_path": str(api_plan_path),
            "api_plan_report_path": str(api_plan_report_path),
            "api_plan_execute_allowed": api_plan["execute_allowed"],
            "api_plan_blockers": ";".join(api_plan["blockers"]),
        }

    summary = {
        "status": "success",
        "lane_id": LANE_ID,
        "ledger_version": PICKUP_COMPLETION_LEDGER_VERSION,
        "run_dir": str(run_dir),
        "sidecar_csv": str(sidecar_csv),
        "ledger_csv": str(ledger_csv) if ledger_csv else "",
        "persisted_ledger_path": persisted_ledger_path,
        "review_status": review_row["review_status"],
        "blockers": review_row["blockers"],
        "security_code_provided": review_row["security_code_provided"] == "true",
        "security_code_sha256": review_row["security_code_sha256"],
        "duplicate_ledger_rows": len(duplicate_rows),
        "review_path": str(review_path),
        "ledger_preview_path": str(ledger_preview_path),
        "report_path": str(report_path),
        "api_plan_requested": build_api_plan,
        "external_writes": {
            "kaspi_order_mutation": False,
            "telegram_send": False,
            "print_job": False,
            "autonomous_business_write": False,
        },
    }
    summary.update(api_plan_summary)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(_render_pickup_completion_markdown(summary, review_row), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def read_express_assembly_ledger(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return [{column: _clean(row.get(column)) for column in EXPRESS_ASSEMBLY_LEDGER_COLUMNS} for row in reader]


def build_express_assembly_key(
    *,
    order_hash: str,
    cropped_label_sha256: str = "",
    owner_assembly_approval_ref: str = "",
    event_type: str = "express_assembly_review",
) -> str:
    return _hash_text(
        "|".join(
            (
                LANE_ID,
                _clean(order_hash),
                "express_assembly",
                _clean(event_type) or "express_assembly_review",
                _clean(cropped_label_sha256) or "missing_label_hash",
                _clean(owner_assembly_approval_ref) or "missing_owner_approval_ref",
            )
        )
    )


def build_express_assembly_review_row(
    row: Mapping[str, str],
    *,
    label_printed: bool = False,
    operator_physically_ready: bool = False,
    owner_assembly_approval_ref: str = "",
    now_text: str | None = None,
) -> dict[str, str]:
    now_text = now_text or datetime.now(ALMATY_TZ).isoformat(timespec="seconds")
    delivery_kind = _clean(row.get("delivery_kind"))
    classification = _clean(row.get("classification"))
    pickup_point_id = _clean(row.get("pickup_point_id"))
    my_size = _clean(row.get("my_size"))
    owner_size_override = _clean(row.get("owner_size_override"))
    size_ready = bool(my_size or owner_size_override) or _clean(row.get("size_status")).startswith("SIZE_READY")
    waybill_status = _clean(row.get("waybill_status"))
    waybill_present = _clean(row.get("waybill_present"))
    cropped_label_sha256 = _clean(row.get("cropped_label_sha256"))
    telegram_label_status = _clean(row.get("telegram_label_status"))
    printed_status = _clean(row.get("printed_status"))
    source_blockers = _clean(row.get("blockers"))
    source_warnings = _clean(row.get("warnings"))
    owner_assembly_approval_ref = _clean(owner_assembly_approval_ref)

    blockers: list[str] = []
    warnings: list[str] = []
    if delivery_kind != "EXPRESS_DELIVERY":
        blockers.append("not_express_delivery")
    if classification not in {"KASPI_EXPRESS_DELIVERY_API_MATCH", "EXPRESS_DELIVERY_CANDIDATE"}:
        blockers.append("express_delivery_not_proven")
    if "ambiguous_delivery_mode" in source_blockers or _clean(row.get("ambiguous_delivery_mode_blocker")):
        blockers.append("ambiguous_delivery_mode")
    if pickup_point_id and pickup_point_id != PP2_PICKUP_POINT_ID:
        warnings.append(f"pickup_point_id_not_pp2={pickup_point_id}")
    if not size_ready:
        blockers.append("missing_size")
    if waybill_status != "WAYBILL_PRESENT_REDACTED" and waybill_present != "true":
        blockers.append("missing_waybill")
    if not cropped_label_sha256.startswith("sha256:"):
        blockers.append("missing_cropped_label_sha256")
    if telegram_label_status != "SENT":
        blockers.append("telegram_label_not_sent")
    if printed_status != "PRINTED" and not label_printed:
        blockers.append("label_not_printed")
    if not operator_physically_ready:
        blockers.append("operator_not_physically_ready_for_courier")
    if not owner_assembly_approval_ref:
        blockers.append("missing_owner_assembly_approval_ref")
    if _clean(row.get("close_status")) in {"CLOSED", "CANCELLED"}:
        blockers.append("row_already_closed_or_cancelled")

    carry_source_blockers = [
        item
        for item in source_blockers.split(";")
        if item and item not in {"unapproved_mutation", "missing_size", "missing_waybill"}
    ]
    if carry_source_blockers:
        warnings.append("source_sidecar_blockers=" + ";".join(carry_source_blockers))
    if source_warnings:
        warnings.append(f"source_sidecar_warnings={source_warnings}")

    review_status = "READY_FOR_MANUAL_EXPRESS_ASSEMBLY" if not blockers else "BLOCKED"
    kaspi_assembly_status = (
        "NOT_EXECUTED_REVIEW_ONLY_READY_FOR_MANUAL_OR_APPROVED_API"
        if not blockers
        else "NOT_EXECUTED_REVIEW_ONLY_BLOCKED"
    )
    return {
        "ledger_version": EXPRESS_ASSEMBLY_LEDGER_VERSION,
        "lane_id": LANE_ID,
        "sidecar_idempotency_key": _clean(row.get("idempotency_key")),
        "order_hash": _clean(row.get("order_hash")),
        "local_ref": _clean(row.get("local_ref")),
        "store_code": _clean(row.get("store_code")) or "ACMEWEAR",
        "merchant_id": _clean(row.get("merchant_id")) or ACMEWEAR_MERCHANT_ID,
        "event_type": "express_assembly_review",
        "express_assembly_key": build_express_assembly_key(
            order_hash=_clean(row.get("order_hash")),
            cropped_label_sha256=cropped_label_sha256,
            owner_assembly_approval_ref=owner_assembly_approval_ref,
        ),
        "review_status": review_status,
        "delivery_kind_at_review": delivery_kind,
        "classification_at_review": classification,
        "pickup_point_id": pickup_point_id,
        "size_status_at_review": _clean(row.get("size_status")),
        "my_size": my_size,
        "owner_size_override": owner_size_override,
        "waybill_status_at_review": waybill_status,
        "waybill_present": waybill_present,
        "cropped_label_sha256": cropped_label_sha256,
        "telegram_label_status": telegram_label_status,
        "printed_status": printed_status,
        "label_printed_confirmed": str(label_printed or printed_status == "PRINTED").lower(),
        "operator_physically_ready": str(operator_physically_ready).lower(),
        "owner_assembly_approval_ref": owner_assembly_approval_ref,
        "kaspi_assembly_status": kaspi_assembly_status,
        "blockers": ";".join(dict.fromkeys(blockers)),
        "warnings": ";".join(warnings),
        "created_at": now_text,
        "created_by": "web-auto acmewear-express-sidecar express-assembly-review",
        "notes": "review_only_no_kaspi_mutation_requires_size_label_print_and_operator_ready",
    }


def _render_express_assembly_markdown(summary: Mapping[str, Any], review_row: Mapping[str, str]) -> str:
    lines = [
        "# ACMEWEAR PP2 Express Assembly Review",
        "",
        f"- Status: `{summary.get('review_status')}`",
        f"- Run dir: `{summary.get('run_dir')}`",
        f"- Sidecar: `{summary.get('sidecar_csv')}`",
        f"- Local ref: `{review_row.get('local_ref')}`",
        f"- Order hash: `{review_row.get('order_hash')}`",
        f"- Product size: `{review_row.get('my_size') or review_row.get('owner_size_override') or '-'}`",
        f"- Waybill: `{review_row.get('waybill_status_at_review')}`",
        f"- Telegram label: `{review_row.get('telegram_label_status')}`",
        f"- Printed: `{review_row.get('printed_status')}`",
        f"- Operator physically ready: `{review_row.get('operator_physically_ready')}`",
        f"- Owner approval ref: `{review_row.get('owner_assembly_approval_ref') or '-'}`",
        f"- Blockers: `{review_row.get('blockers') or '-'}`",
        "",
        "## Guardrail",
        "",
        "- This command is review-only. It does not assemble the order in Kaspi.",
        "- `READY_FOR_MANUAL_EXPRESS_ASSEMBLY` means the operator can manually assemble only after separately deciding to perform that live Kaspi action.",
        "- Automated API/UI assembly remains a separate future write gate.",
        "",
    ]
    return "\n".join(lines)


def run_express_assembly_review(
    *,
    sidecar_csv: Path,
    run_dir: Path,
    order_hash: str = "",
    local_ref: str = "",
    sidecar_idempotency_key: str = "",
    label_printed: bool = False,
    operator_physically_ready: bool = False,
    owner_assembly_approval_ref: str = "",
    ledger_csv: Path | None = None,
    write_ledger: bool = False,
    now_text: str | None = None,
) -> dict[str, Any]:
    if not sidecar_csv.exists():
        raise ValueError(f"Sidecar CSV not found: {sidecar_csv}")
    if write_ledger and ledger_csv is None:
        raise ValueError("--write-ledger requires --ledger-csv")
    rows = read_sidecar_csv(sidecar_csv)
    selected = _select_one_sidecar_row(
        rows,
        order_hash=order_hash,
        local_ref=local_ref,
        sidecar_idempotency_key=sidecar_idempotency_key,
    )
    review_row = build_express_assembly_review_row(
        selected,
        label_printed=label_printed,
        operator_physically_ready=operator_physically_ready,
        owner_assembly_approval_ref=owner_assembly_approval_ref,
        now_text=now_text,
    )
    existing_ledger = read_express_assembly_ledger(ledger_csv) if ledger_csv else []
    duplicate_rows = [
        row for row in existing_ledger if _clean(row.get("express_assembly_key")) == review_row["express_assembly_key"]
    ]

    run_dir.mkdir(parents=True, exist_ok=True)
    review_path = run_dir / "express_assembly_review.csv"
    ledger_preview_path = run_dir / "express_assembly_ledger_preview.csv"
    summary_path = run_dir / "express_assembly_summary.json"
    report_path = run_dir / "express_assembly_report.md"
    write_csv(review_path, EXPRESS_ASSEMBLY_LEDGER_COLUMNS, [review_row])
    write_csv(ledger_preview_path, EXPRESS_ASSEMBLY_LEDGER_COLUMNS, [review_row])

    persisted_ledger_path = ""
    if write_ledger and ledger_csv is not None:
        if not duplicate_rows:
            write_csv(ledger_csv, EXPRESS_ASSEMBLY_LEDGER_COLUMNS, [*existing_ledger, review_row])
        else:
            write_csv(ledger_csv, EXPRESS_ASSEMBLY_LEDGER_COLUMNS, existing_ledger)
        persisted_ledger_path = str(ledger_csv)

    summary = {
        "status": "success",
        "lane_id": LANE_ID,
        "ledger_version": EXPRESS_ASSEMBLY_LEDGER_VERSION,
        "mode": "express_assembly_review_only",
        "run_dir": str(run_dir),
        "sidecar_csv": str(sidecar_csv),
        "ledger_csv": str(ledger_csv) if ledger_csv else "",
        "persisted_ledger_path": persisted_ledger_path,
        "review_status": review_row["review_status"],
        "blockers": review_row["blockers"],
        "duplicate_ledger_rows": len(duplicate_rows),
        "review_path": str(review_path),
        "ledger_preview_path": str(ledger_preview_path),
        "report_path": str(report_path),
        "external_writes": _external_writes_false(),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(_render_express_assembly_markdown(summary, review_row), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def run_sidecar_row_update(
    *,
    sidecar_csv: Path,
    run_dir: Path,
    order_hash: str = "",
    local_ref: str = "",
    sidecar_idempotency_key: str = "",
    my_size: str = "",
    owner_size_override: str = "",
    size_source: str = "",
    ordered_size: str = "",
    height_cm: str = "",
    weight_kg: str = "",
    cropped_label_path: str = "",
    cropped_label_sha256: str = "",
    telegram_alert_status: str = "",
    telegram_label_status: str = "",
    printed_status: str = "",
    close_status: str = "",
    build_shift_packet: bool = False,
    shift_run_root: Path = DEFAULT_RUN_ROOT,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Patch operator-known facts onto one existing sidecar row.

    Unlike manual intake, this preserves API-derived detection fields such as
    delivery mode, platform state/status, and classification.
    """

    if not sidecar_csv.exists():
        raise ValueError(f"Sidecar CSV not found: {sidecar_csv}")
    rows = read_sidecar_csv(sidecar_csv)
    selected = _select_one_sidecar_row(
        rows,
        order_hash=order_hash,
        local_ref=local_ref,
        sidecar_idempotency_key=sidecar_idempotency_key,
    )
    selected_key = selected["idempotency_key"]
    updated_rows: list[dict[str, str]] = []
    updated = dict(selected)

    patch_fields = {
        "my_size": _clean(my_size),
        "owner_size_override": _clean(owner_size_override),
        "size_source": _clean(size_source),
        "ordered_size": _clean(ordered_size),
        "height_cm": _clean(height_cm),
        "weight_kg": _clean(weight_kg),
        "cropped_label_path": _clean(cropped_label_path),
        "telegram_alert_status": _clean(telegram_alert_status),
        "telegram_label_status": _clean(telegram_label_status),
        "printed_status": _clean(printed_status),
        "close_status": _clean(close_status),
    }
    for field, value in patch_fields.items():
        if value:
            updated[field] = value
    if cropped_label_sha256:
        updated["cropped_label_sha256"] = _validate_hash_or_empty(
            cropped_label_sha256,
            field_name="cropped_label_sha256",
        )

    size_ready = bool(_clean(updated.get("my_size")) or _clean(updated.get("owner_size_override")))
    if size_ready:
        updated["sidecar_state"] = "SIZE_READY"
        updated["size_status"] = "SIZE_READY_OWNER_OVERRIDE" if _clean(updated.get("owner_size_override")) else "SIZE_READY_FROM_RULE"
        updated["missing_size_blocker"] = ""
        if _clean(updated.get("assemble_status")) == "BLOCKED_SIZE_MISSING":
            updated["assemble_status"] = "BLOCKED_MUTATION_UNAPPROVED"
            updated["unapproved_mutation_blocker"] = "unapproved_mutation"
    elif _clean(updated.get("sidecar_state")) == "SIZE_READY":
        updated["sidecar_state"] = "NEEDS_SIZE"
        updated["size_status"] = "NEEDS_SIZE"
        updated["missing_size_blocker"] = "missing_size"
        updated["assemble_status"] = "BLOCKED_SIZE_MISSING"

    if _clean(updated.get("waybill_present")) == "true":
        updated["missing_waybill_blocker"] = ""
    updated = _refresh_sidecar_blockers(updated)

    for row in rows:
        normalized = {column: _clean(row.get(column)) for column in SIDE_CAR_COLUMNS}
        if normalized["idempotency_key"] == selected_key:
            updated_rows.append(updated)
        else:
            updated_rows.append(normalized)

    alert_rows = build_alert_queue_rows([updated])
    operator_rows = build_operator_queue_rows([updated])

    run_dir.mkdir(parents=True, exist_ok=True)
    before_path = run_dir / "sidecar_row_before.csv"
    after_path = run_dir / "sidecar_row_after.csv"
    updated_path = run_dir / "sidecar_updated.csv"
    alert_queue_path = run_dir / "sidecar_update_telegram_alert_queue_review.csv"
    operator_queue_path = run_dir / "sidecar_update_operator_queue.csv"
    summary_path = run_dir / "sidecar_update_summary.json"

    write_csv(before_path, SIDE_CAR_COLUMNS, [selected])
    write_csv(after_path, SIDE_CAR_COLUMNS, [updated])
    write_csv(updated_path, SIDE_CAR_COLUMNS, updated_rows)
    write_csv(alert_queue_path, ALERT_QUEUE_COLUMNS, alert_rows)
    write_csv(operator_queue_path, OPERATOR_QUEUE_COLUMNS, operator_rows)
    write_csv(sidecar_csv, SIDE_CAR_COLUMNS, updated_rows)

    summary = {
        "status": "success",
        "mode": "repo_local_sidecar_row_update",
        "lane_id": LANE_ID,
        "schema_version": SCHEMA_VERSION,
        "run_dir": str(run_dir),
        "sidecar_csv": str(sidecar_csv),
        "local_ref": updated["local_ref"],
        "order_hash": updated["order_hash"],
        "idempotency_key": selected_key,
        "before_row_hash": selected["row_hash"],
        "after_row_hash": updated["row_hash"],
        "preserved_classification": updated["classification"],
        "preserved_delivery_mode": updated["delivery_mode"],
        "sidecar_state": updated["sidecar_state"],
        "size_status": updated["size_status"],
        "assemble_status": updated["assemble_status"],
        "blockers": updated["blockers"],
        "operator_queue_rows": len(operator_rows),
        "alert_queue_rows": len(alert_rows),
        "before_path": str(before_path),
        "after_path": str(after_path),
        "updated_sidecar_path": str(updated_path),
        "telegram_alert_queue_review_path": str(alert_queue_path),
        "operator_queue_path": str(operator_queue_path),
        "persisted_sidecar_path": str(sidecar_csv),
        "shift_packet_built": False,
        "shift_packet_run_dir": "",
        "shift_packet_summary_path": "",
        "shift_packet_report_path": "",
        "external_writes": _external_writes_false(),
    }
    if build_shift_packet:
        shift_summary = run_shift_packet_review(
            sidecar_csv=sidecar_csv,
            run_dir=run_dir / "shift_packet",
            run_root=shift_run_root,
            env=env or {},
        )
        summary["shift_packet_built"] = True
        summary["shift_packet_run_dir"] = _clean(shift_summary.get("run_dir"))
        summary["shift_packet_summary_path"] = _clean(shift_summary.get("shift_packet_summary_path"))
        summary["shift_packet_report_path"] = _clean(shift_summary.get("shift_packet_report_path"))
        summary["shift_operator_queue_path"] = _clean(shift_summary.get("shift_operator_queue_path"))
        summary["shift_readiness_matrix_path"] = _clean(shift_summary.get("shift_readiness_matrix_path"))
        summary["shift_operator_queue_rows"] = int(shift_summary.get("operator_queue_rows") or 0)
        summary["shift_gate"] = _clean(shift_summary.get("gate"))

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def _read_csv_header(path: Path) -> list[str]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            return next(reader)
        except StopIteration:
            return []


def _render_sidecar_init_markdown(summary: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "# ACMEWEAR PP2 Express / Self-Pickup Sidecar Init",
            "",
            f"- Status: `{summary.get('status')}`",
            f"- Mode: `{summary.get('mode')}`",
            f"- Sidecar CSV: `{summary.get('sidecar_csv')}`",
            f"- Schema CSV: `{summary.get('schema_csv')}`",
            f"- Existing before: `{summary.get('sidecar_existed_before')}`",
            f"- Rows before: `{summary.get('rows_before')}`",
            f"- Action: `{summary.get('action')}`",
            "",
            "## Guardrail",
            "",
            "- This command writes only a repo-local empty sidecar/header when missing.",
            "- It never overwrites existing sidecar rows.",
            "- It does not fetch Kaspi, send Telegram, print labels, mutate orders, or write to Autonomous_business.",
            "",
        ]
    )


def run_sidecar_init(
    *,
    sidecar_csv: Path,
    run_dir: Path,
    schema_csv: Path | None = None,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    existed_before = sidecar_csv.exists()
    header_before = _read_csv_header(sidecar_csv)
    if existed_before and header_before != list(SIDE_CAR_COLUMNS):
        raise ValueError(
            "existing sidecar CSV header does not match ACMEWEAR Express sidecar schema; "
            "do not overwrite without a separate backup/repair task"
        )
    rows_before = read_sidecar_csv(sidecar_csv)
    action = "unchanged_existing_sidecar"
    if not existed_before:
        write_csv(sidecar_csv, SIDE_CAR_COLUMNS, [])
        action = "created_empty_sidecar"

    schema_written_path = ""
    if schema_csv:
        write_csv(
            schema_csv,
            ("column", "type", "required", "allowed_values", "description", "sensitive_policy"),
            SIDE_CAR_SCHEMA_ROWS,
        )
        schema_written_path = str(schema_csv)

    summary_path = run_dir / "sidecar_init_summary.json"
    report_path = run_dir / "sidecar_init_report.md"
    summary = {
        "status": "success",
        "mode": "repo_local_sidecar_init",
        "lane_id": LANE_ID,
        "schema_version": SCHEMA_VERSION,
        "sidecar_csv": str(sidecar_csv),
        "schema_csv": schema_written_path,
        "sidecar_existed_before": existed_before,
        "rows_before": len(rows_before),
        "rows_after": len(read_sidecar_csv(sidecar_csv)),
        "action": action,
        "run_dir": str(run_dir),
        "summary_path": str(summary_path),
        "report_path": str(report_path),
        "external_writes": _external_writes_false(),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(_render_sidecar_init_markdown(summary), encoding="utf-8")
    return summary


def load_json_rows(path: Path) -> list[Mapping[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [row for row in data if isinstance(row, Mapping)]
    if isinstance(data, Mapping):
        api_data = data.get("data")
        if isinstance(api_data, list):
            return [row for row in api_data if isinstance(row, Mapping)]
        return [data]
    raise ValueError(f"Expected JSON object or list in {path}")


def load_json_or_jsonl_rows(path: Path) -> list[Mapping[str, Any]]:
    if path.suffix.lower() == ".jsonl":
        rows: list[Mapping[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            value = json.loads(text)
            if not isinstance(value, Mapping):
                raise ValueError(f"Expected JSON object per line in {path}")
            rows.append(value)
        return rows
    return load_json_rows(path)


def _epoch_ms(value: str) -> str:
    text = _clean(value)
    if not text:
        return ""
    if text.isdigit():
        return text
    normalized = text.replace("Z", "+00:00")
    try:
        if len(normalized) == 10 and normalized[4] == "-" and normalized[7] == "-":
            dt = datetime.fromisoformat(normalized).replace(tzinfo=ALMATY_TZ)
        else:
            dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise KaspiOrderFetchError(f"Invalid datetime for epoch-ms conversion: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ALMATY_TZ)
    return str(round(dt.timestamp() * 1000))


def _query_url(base_url: str, params: Mapping[str, Any]) -> str:
    clean_params = {key: value for key, value in params.items() if value not in (None, "")}
    return f"{base_url}?{urllib.parse.urlencode(clean_params)}"


def build_order_list_query_url(
    *,
    base_url: str = DEFAULT_ORDER_API_URL,
    page_number: int = 0,
    page_size: int = 100,
    state: str = "",
    status: str = "",
    delivery_type: str = "",
    created_from: str = "",
    created_to: str = "",
    signature_required: str = "",
) -> str:
    params: dict[str, Any] = {
        "page[number]": str(page_number),
        "page[size]": str(page_size),
        "filter[orders][state]": _clean(state),
        "filter[orders][status]": _clean(status),
        "filter[orders][signatureRequired]": _clean(signature_required),
    }
    if _clean(delivery_type) and _clean(state) != "PICKUP":
        params["filter[orders][deliveryType]"] = _clean(delivery_type)
    from_ms = _epoch_ms(created_from)
    to_ms = _epoch_ms(created_to)
    if from_ms:
        params["filter[orders][creationDate][$ge]"] = from_ms
    if to_ms:
        params["filter[orders][creationDate][$le]"] = to_ms
    return _query_url(base_url, params)


def build_order_code_query_url(
    *,
    order_code: str,
    base_url: str = DEFAULT_ORDER_API_URL,
    page_size: int = 10,
) -> str:
    params: dict[str, Any] = {
        "page[number]": "0",
        "page[size]": str(page_size),
        "filter[orders][code]": _clean(order_code),
    }
    return _query_url(base_url, params)


def _read_api_payload(
    *,
    url: str,
    token: str,
    timeout_seconds: int,
    urlopen_func: Any = None,
) -> Mapping[str, Any]:
    if not token:
        raise KaspiOrderFetchError("Missing Kaspi API token")
    opener = urlopen_func or urllib.request.urlopen
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
            "User-Agent": "web-auto-acmewear-express-sidecar/1.0",
            "X-Auth-Token": token,
        },
        method="GET",
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise KaspiOrderFetchError(f"Kaspi order-list fetch failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise KaspiOrderFetchError(f"Kaspi order-list fetch failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise KaspiOrderFetchError("Kaspi order-list response was not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise KaspiOrderFetchError("Kaspi order-list response was not a JSON object")
    return payload


def _download_waybill_pdf(
    *,
    waybill_url: str,
    token: str,
    output_path: Path,
    timeout_seconds: int = 45,
    urlopen_func: Any = None,
) -> dict[str, Any]:
    if not _clean(waybill_url):
        raise KaspiOrderFetchError("Missing existing waybill URL")
    if not token:
        raise KaspiOrderFetchError("Missing Kaspi API token")
    opener = urlopen_func or urllib.request.urlopen
    request = urllib.request.Request(
        waybill_url,
        headers={
            "Accept": "application/pdf,application/octet-stream,*/*",
            "User-Agent": "web-auto-acmewear-express-label/1.0",
            "X-Auth-Token": token,
        },
        method="GET",
    )
    try:
        with opener(request, timeout=timeout_seconds) as response:
            pdf_bytes = response.read()
    except urllib.error.HTTPError as exc:
        raise KaspiOrderFetchError(f"Kaspi waybill download failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise KaspiOrderFetchError("Kaspi waybill download failed") from exc
    if not pdf_bytes.startswith(b"%PDF"):
        raise KaspiOrderFetchError("Kaspi waybill download did not return PDF bytes")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(pdf_bytes)
    return {
        "pdf_sha256": _hash_bytes(pdf_bytes),
        "pdf_bytes": len(pdf_bytes),
        "platform_original_run_path": str(output_path),
    }


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _resolve_created_bounds_with_lookback(
    *,
    created_from: str,
    created_to: str,
    lookback_hours: int,
    now: datetime | None = None,
) -> tuple[str, str]:
    if lookback_hours < 1:
        raise ValueError("lookback_hours must be >= 1")
    if created_from and created_to:
        return created_from, created_to
    now_value = now or datetime.now(ALMATY_TZ)
    if now_value.tzinfo is None:
        now_value = now_value.replace(tzinfo=ALMATY_TZ)
    return (
        created_from or (now_value - timedelta(hours=lookback_hours)).isoformat(timespec="seconds"),
        created_to or now_value.isoformat(timespec="seconds"),
    )


def fetch_order_api_rows(
    *,
    token: str,
    states: Sequence[str] | None = None,
    statuses: Sequence[str] | None = None,
    delivery_types: Sequence[str] | None = None,
    created_from: str = "",
    created_to: str = "",
    page_size: int = 100,
    max_pages: int = 5,
    timeout_seconds: int = 20,
    base_url: str = DEFAULT_ORDER_API_URL,
    urlopen_func: Any = None,
) -> tuple[list[Mapping[str, Any]], list[dict[str, str]]]:
    if page_size < 1 or page_size > 100:
        raise KaspiOrderFetchError("page_size must be between 1 and 100")
    if max_pages < 1:
        raise KaspiOrderFetchError("max_pages must be >= 1")
    query_states = tuple(_clean(item) for item in (states or DEFAULT_FETCH_STATES) if _clean(item))
    query_statuses = tuple(_clean(item) for item in (statuses or ("",)))
    query_delivery_types = tuple(_clean(item) for item in (delivery_types or ("",)))
    rows: list[Mapping[str, Any]] = []
    query_log: list[dict[str, str]] = []

    for state in query_states:
        for status in query_statuses:
            for delivery_type in query_delivery_types:
                pages_seen = 0
                page_count_hint = ""
                for page_number in range(max_pages):
                    url = build_order_list_query_url(
                        base_url=base_url,
                        page_number=page_number,
                        page_size=page_size,
                        state=state,
                        status=status,
                        delivery_type=delivery_type,
                        created_from=created_from,
                        created_to=created_to,
                    )
                    payload = _read_api_payload(
                        url=url,
                        token=token,
                        timeout_seconds=timeout_seconds,
                        urlopen_func=urlopen_func,
                    )
                    page_rows = load_json_rows_from_api_payload(payload)
                    rows.extend(page_rows)
                    meta = payload.get("meta")
                    if isinstance(meta, Mapping):
                        page_count_hint = _clean(meta.get("pageCount"))
                    pages_seen += 1
                    query_log.append(
                        {
                            "state": state,
                            "status": status,
                            "delivery_type": delivery_type if state != "PICKUP" else "",
                            "page_number": str(page_number),
                            "page_rows": str(len(page_rows)),
                            "page_count_hint": page_count_hint,
                        }
                    )
                    if not page_rows:
                        break
                    if page_count_hint.isdigit() and page_number + 1 >= int(page_count_hint):
                        break
                if pages_seen >= max_pages:
                    continue
    return rows, query_log


def fetch_order_api_rows_by_code(
    *,
    token: str,
    order_code: str,
    timeout_seconds: int = 20,
    base_url: str = DEFAULT_ORDER_API_URL,
    urlopen_func: Any = None,
) -> tuple[list[Mapping[str, Any]], list[dict[str, str]]]:
    if not _clean(order_code):
        raise KaspiOrderFetchError("Missing order code")
    url = build_order_code_query_url(order_code=order_code, base_url=base_url)
    payload = _read_api_payload(
        url=url,
        token=token,
        timeout_seconds=timeout_seconds,
        urlopen_func=urlopen_func,
    )
    rows = load_json_rows_from_api_payload(payload)
    return rows, [{"query": "code_filter", "rows": str(len(rows))}]


def load_json_rows_from_api_payload(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = payload.get("data")
    if isinstance(data, list):
        return [row for row in data if isinstance(row, Mapping)]
    return []


def build_sidecar_rows_from_orders(
    rows: Iterable[Mapping[str, Any]],
    *,
    detected_at: str | None = None,
    local_ref_prefix: str = "order",
) -> list[dict[str, str]]:
    built = [
        build_sidecar_row(row, local_ref=f"{local_ref_prefix}_{index:03d}", detected_at=detected_at or None)
        for index, row in enumerate(rows, start=1)
    ]
    return dedupe_sidecar_rows(built)


def is_actionable_persist_sidecar_row(row: Mapping[str, str]) -> bool:
    """Return true for rows safe enough to persist into operational sidecar state."""
    return _clean(row.get("classification")) in ACTIONABLE_PERSIST_CLASSIFICATIONS


def _validate_hash_or_empty(value: str, *, field_name: str) -> str:
    text = _clean(value)
    if not text:
        return ""
    if not text.startswith("sha256:") or len(text) != len("sha256:") + 64:
        raise ValueError(f"{field_name} must be a sha256:... value")
    return text


def _manual_order_hash(
    *,
    order_hash: str = "",
    order_ref_text: str = "",
    local_ref: str,
) -> tuple[str, str]:
    validated = _validate_hash_or_empty(order_hash, field_name="order_hash")
    if validated:
        return validated, "provided_sha256"
    if order_ref_text:
        return _hash_text(order_ref_text), "hashed_from_env"
    if not _clean(local_ref):
        raise ValueError("manual intake requires --local-ref")
    return _hash_text(f"manual-local-ref:{_clean(local_ref)}"), "derived_from_local_ref"


def _manual_sidecar_source_row(
    *,
    delivery_kind: str,
    local_ref: str,
    order_hash: str,
    created_at: str = "",
    delivery_slot_label: str = "",
    courier_planning_at: str = "",
    sku_key: str = "",
    merchant_article: str = "",
    ordered_size: str = "",
    height_cm: str = "",
    weight_kg: str = "",
    my_size: str = "",
    owner_size_override: str = "",
    size_source: str = "",
    waybill_present: bool = False,
) -> dict[str, Any]:
    delivery_kind = _clean(delivery_kind).upper()
    base: dict[str, Any] = {
        "order_ref_hash": _validate_hash_or_empty(order_hash, field_name="order_hash"),
        "merchant_id": ACMEWEAR_MERCHANT_ID,
        "created_at": _clean(created_at),
        "state": "MANUAL_SIDE_CAR",
        "status": "MANUAL_INTAKE",
        "sku_key": _clean(sku_key),
        "merchant_article": _clean(merchant_article),
        "ordered_size": _clean(ordered_size),
        "height_cm": _clean(height_cm),
        "weight_kg": _clean(weight_kg),
        "my_size": _clean(my_size),
        "owner_size_override": _clean(owner_size_override),
        "size_source": _clean(size_source) or "manual_intake",
        "api_endpoint": "manual_intake",
    }
    if delivery_kind == "EXPRESS_DELIVERY":
        base.update(
            {
                "deliveryMode": "DELIVERY_LOCAL",
                "isKaspiDelivery": True,
                "pickupPointId": PP2_PICKUP_POINT_ID,
                "deliverySlot_safe": _clean(delivery_slot_label),
                "kaspiDelivery_safe": {
                    "express": True,
                    "waybill": "redacted_present" if waybill_present else "redacted_absent",
                    "courierTransmissionPlanningDate": _clean(courier_planning_at),
                },
            }
        )
        return base
    if delivery_kind == "SELLER_SELF_PICKUP":
        base.update(
            {
                "deliveryMode": "DELIVERY_PICKUP",
                "isKaspiDelivery": False,
                "pickupPointId": PP2_PICKUP_POINT_ID,
                "pickup_point_proof_status": "PP2_PROVEN",
            }
        )
        return base
    raise ValueError("delivery_kind must be EXPRESS_DELIVERY or SELLER_SELF_PICKUP")


def _refresh_sidecar_blockers(row: Mapping[str, str]) -> dict[str, str]:
    updated = {column: _clean(row.get(column)) for column in SIDE_CAR_COLUMNS}
    blockers = [
        _clean(updated.get("missing_size_blocker")),
        _clean(updated.get("missing_waybill_blocker")),
        _clean(updated.get("ambiguous_delivery_mode_blocker")),
        _clean(updated.get("unapproved_mutation_blocker")),
    ]
    updated["blockers"] = ";".join(item for item in blockers if item)
    updated["row_hash"] = _hash_text(
        json.dumps({k: updated[k] for k in SIDE_CAR_COLUMNS if k != "row_hash"}, sort_keys=True)
    )
    return updated


def run_manual_sidecar_intake(
    *,
    run_dir: Path,
    sidecar_csv: Path | None = None,
    update_sidecar: bool = False,
    delivery_kind: str,
    local_ref: str,
    order_hash: str = "",
    order_ref_text: str = "",
    detected_at: str | None = None,
    created_at: str = "",
    delivery_slot_label: str = "",
    courier_planning_at: str = "",
    sku_key: str = "",
    merchant_article: str = "",
    ordered_size: str = "",
    height_cm: str = "",
    weight_kg: str = "",
    my_size: str = "",
    owner_size_override: str = "",
    size_source: str = "",
    waybill_present: bool = False,
    cropped_label_path: str = "",
    cropped_label_sha256: str = "",
    telegram_alert_status: str = "",
    telegram_label_status: str = "",
    printed_status: str = "",
    close_status: str = "OPEN",
    build_shift_packet: bool = False,
    shift_run_root: Path = DEFAULT_RUN_ROOT,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    local_ref = _clean(local_ref)
    if not local_ref:
        raise ValueError("manual intake requires --local-ref")
    if update_sidecar and sidecar_csv is None:
        raise ValueError("--update-sidecar requires --sidecar-csv")
    if build_shift_packet and (not update_sidecar or sidecar_csv is None):
        raise ValueError("--build-shift-packet requires --sidecar-csv and --update-sidecar")
    normalized_order_hash, hash_source = _manual_order_hash(
        order_hash=order_hash,
        order_ref_text=order_ref_text,
        local_ref=local_ref,
    )
    source_row = _manual_sidecar_source_row(
        delivery_kind=delivery_kind,
        local_ref=local_ref,
        order_hash=normalized_order_hash,
        created_at=created_at,
        delivery_slot_label=delivery_slot_label,
        courier_planning_at=courier_planning_at,
        sku_key=sku_key,
        merchant_article=merchant_article,
        ordered_size=ordered_size,
        height_cm=height_cm,
        weight_kg=weight_kg,
        my_size=my_size,
        owner_size_override=owner_size_override,
        size_source=size_source,
        waybill_present=waybill_present,
    )
    sidecar_row = build_sidecar_row(source_row, local_ref=local_ref, detected_at=detected_at or None)
    if _clean(delivery_kind).upper() == "SELLER_SELF_PICKUP":
        sidecar_row["waybill_status"] = "WAYBILL_NOT_APPLICABLE"
        sidecar_row["waybill_present"] = "false"
        sidecar_row["missing_waybill_blocker"] = ""
    if cropped_label_path:
        sidecar_row["cropped_label_path"] = _clean(cropped_label_path)
    if cropped_label_sha256:
        sidecar_row["cropped_label_sha256"] = _validate_hash_or_empty(cropped_label_sha256, field_name="cropped_label_sha256")
    if telegram_alert_status:
        sidecar_row["telegram_alert_status"] = _clean(telegram_alert_status)
    if telegram_label_status:
        sidecar_row["telegram_label_status"] = _clean(telegram_label_status)
    if printed_status:
        sidecar_row["printed_status"] = _clean(printed_status)
    if close_status:
        sidecar_row["close_status"] = _clean(close_status)
    sidecar_row = _refresh_sidecar_blockers(sidecar_row)

    existing_rows = read_sidecar_csv(sidecar_csv) if sidecar_csv else []
    merged_rows, merge_counts = merge_sidecar_rows(existing_rows, [sidecar_row])
    alert_rows = build_alert_queue_rows([sidecar_row])
    operator_rows = build_operator_queue_rows([sidecar_row])

    run_dir.mkdir(parents=True, exist_ok=True)
    incoming_path = run_dir / "manual_intake_sidecar_row.csv"
    merged_preview_path = run_dir / "manual_intake_merged_preview.csv"
    alert_queue_path = run_dir / "manual_intake_telegram_alert_queue_review.csv"
    operator_queue_path = run_dir / "manual_intake_operator_queue.csv"
    summary_path = run_dir / "manual_intake_summary.json"

    write_csv(incoming_path, SIDE_CAR_COLUMNS, [sidecar_row])
    write_csv(merged_preview_path, SIDE_CAR_COLUMNS, merged_rows)
    write_csv(alert_queue_path, ALERT_QUEUE_COLUMNS, alert_rows)
    write_csv(operator_queue_path, OPERATOR_QUEUE_COLUMNS, operator_rows)
    persisted_sidecar_path = ""
    if sidecar_csv and update_sidecar:
        write_csv(sidecar_csv, SIDE_CAR_COLUMNS, merged_rows)
        persisted_sidecar_path = str(sidecar_csv)

    summary = {
        "status": "success",
        "mode": "repo_local_manual_sidecar_update" if update_sidecar else "manual_intake_review_only",
        "lane_id": LANE_ID,
        "schema_version": SCHEMA_VERSION,
        "manual_intake_version": "acmewear_express_selfpickup_manual_intake.v1",
        "run_dir": str(run_dir),
        "sidecar_csv": str(sidecar_csv) if sidecar_csv else "",
        "persisted_sidecar_path": persisted_sidecar_path,
        "local_ref": local_ref,
        "order_hash": normalized_order_hash,
        "order_hash_source": hash_source,
        "delivery_kind": sidecar_row["delivery_kind"],
        "classification": sidecar_row["classification"],
        "size_status": sidecar_row["size_status"],
        "sidecar_state": sidecar_row["sidecar_state"],
        "waybill_status": sidecar_row["waybill_status"],
        "incoming_sidecar_rows": 1,
        "existing_sidecar_rows": len(existing_rows),
        "merged_sidecar_rows": len(merged_rows),
        "alert_queue_rows": len(alert_rows),
        "operator_queue_rows": len(operator_rows),
        "merge_counts": merge_counts,
        "incoming_sidecar_path": str(incoming_path),
        "merged_preview_path": str(merged_preview_path),
        "telegram_alert_queue_review_path": str(alert_queue_path),
        "operator_queue_path": str(operator_queue_path),
        "summary_path": str(summary_path),
        "shift_packet_built": False,
        "shift_packet_run_dir": "",
        "shift_packet_summary_path": "",
        "shift_packet_report_path": "",
        "external_writes": _external_writes_false(),
    }
    if build_shift_packet:
        shift_summary = run_shift_packet_review(
            sidecar_csv=sidecar_csv,
            run_dir=run_dir / "shift_packet",
            run_root=shift_run_root,
            env=env or {},
            generated_at=detected_at or None,
        )
        summary["shift_packet_built"] = True
        summary["shift_packet_run_dir"] = _clean(shift_summary.get("run_dir"))
        summary["shift_packet_summary_path"] = _clean(shift_summary.get("shift_packet_summary_path"))
        summary["shift_packet_report_path"] = _clean(shift_summary.get("shift_packet_report_path"))
        summary["shift_operator_queue_path"] = _clean(shift_summary.get("shift_operator_queue_path"))
        summary["shift_readiness_matrix_path"] = _clean(shift_summary.get("shift_readiness_matrix_path"))
        summary["shift_operator_queue_rows"] = int(shift_summary.get("operator_queue_rows") or 0)
        summary["shift_gate"] = _clean(shift_summary.get("gate"))
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def run_sidecar_build_from_rows(
    *,
    source_rows: Sequence[Mapping[str, Any]],
    run_dir: Path,
    detected_at: str | None = None,
    sidecar_csv: Path | None = None,
    update_sidecar: bool = False,
    persist_actionable_only: bool = False,
    local_ref_prefix: str = "order",
    mode: str = "review_only",
    input_path: str = "",
    fetch_query_log: Sequence[Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    incoming_rows = build_sidecar_rows_from_orders(
        source_rows,
        detected_at=detected_at or None,
        local_ref_prefix=local_ref_prefix,
    )
    existing_rows = read_sidecar_csv(sidecar_csv) if sidecar_csv else []
    merged_rows, merge_counts = merge_sidecar_rows(existing_rows, incoming_rows)
    incoming_merge_keys = {
        _sidecar_merge_key(_normalize_sidecar_storage_row(row))
        for row in incoming_rows
    }
    alert_source_rows = [
        row
        for row in merged_rows
        if _sidecar_merge_key(row) in incoming_merge_keys
    ]
    persist_incoming_rows = (
        [row for row in incoming_rows if is_actionable_persist_sidecar_row(row)]
        if persist_actionable_only
        else list(incoming_rows)
    )
    persist_merged_rows, persist_merge_counts = merge_sidecar_rows(existing_rows, persist_incoming_rows)
    alert_rows = build_alert_queue_rows(alert_source_rows)

    run_dir.mkdir(parents=True, exist_ok=True)
    incoming_path = run_dir / "incoming_sidecar_rows.csv"
    merged_preview_path = run_dir / "sidecar_merged_preview.csv"
    alert_queue_path = run_dir / "telegram_alert_queue_review.csv"
    query_log_path = run_dir / "fetch_query_log.csv"
    summary_path = run_dir / "summary.json"

    write_csv(incoming_path, SIDE_CAR_COLUMNS, incoming_rows)
    write_csv(merged_preview_path, SIDE_CAR_COLUMNS, merged_rows)
    write_csv(alert_queue_path, ALERT_QUEUE_COLUMNS, alert_rows)
    if fetch_query_log is not None:
        write_csv(
            query_log_path,
            ("state", "status", "delivery_type", "page_number", "page_rows", "page_count_hint"),
            fetch_query_log,
        )

    persisted_sidecar_path = ""
    if sidecar_csv and update_sidecar:
        write_csv(sidecar_csv, SIDE_CAR_COLUMNS, persist_merged_rows)
        persisted_sidecar_path = str(sidecar_csv)

    summary = {
        "status": "success",
        "mode": (
            "repo_local_sidecar_update_actionable_only"
            if update_sidecar and persist_actionable_only
            else "repo_local_sidecar_update"
            if update_sidecar
            else mode
        ),
        "lane_id": LANE_ID,
        "schema_version": SCHEMA_VERSION,
        "input_path": input_path,
        "run_dir": str(run_dir),
        "source_rows": len(source_rows),
        "incoming_sidecar_rows": len(incoming_rows),
        "existing_sidecar_rows": len(existing_rows),
        "merged_sidecar_rows": len(merged_rows),
        "persist_actionable_only": bool(persist_actionable_only),
        "persistable_incoming_sidecar_rows": len(persist_incoming_rows),
        "skipped_persist_sidecar_rows": max(0, len(incoming_rows) - len(persist_incoming_rows)),
        "persisted_merged_sidecar_rows": len(persist_merged_rows) if update_sidecar else 0,
        "alert_queue_rows": len(alert_rows),
        "merge_counts": merge_counts,
        "persist_merge_counts": persist_merge_counts,
        "incoming_sidecar_path": str(incoming_path),
        "merged_preview_path": str(merged_preview_path),
        "telegram_alert_queue_review_path": str(alert_queue_path),
        "fetch_query_log_path": str(query_log_path) if fetch_query_log is not None else "",
        "persisted_sidecar_path": persisted_sidecar_path,
        "external_writes": {
            "kaspi_order_mutation": False,
            "telegram_send": False,
            "print_job": False,
            "autonomous_business_write": False,
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def run_sidecar_build_from_file(
    *,
    input_path: Path,
    run_dir: Path,
    detected_at: str | None = None,
    sidecar_csv: Path | None = None,
    update_sidecar: bool = False,
    persist_actionable_only: bool = False,
    local_ref_prefix: str = "order",
) -> dict[str, Any]:
    source_rows = load_json_or_jsonl_rows(input_path)
    return run_sidecar_build_from_rows(
        source_rows=source_rows,
        run_dir=run_dir,
        detected_at=detected_at or None,
        sidecar_csv=sidecar_csv,
        update_sidecar=update_sidecar,
        persist_actionable_only=persist_actionable_only,
        local_ref_prefix=local_ref_prefix,
        mode="review_only",
        input_path=str(input_path),
    )


def run_sidecar_fetch_once(
    *,
    token: str,
    run_dir: Path,
    detected_at: str | None = None,
    sidecar_csv: Path | None = None,
    update_sidecar: bool = False,
    persist_actionable_only: bool = False,
    local_ref_prefix: str = "order",
    states: Sequence[str] | None = None,
    statuses: Sequence[str] | None = None,
    delivery_types: Sequence[str] | None = None,
    created_from: str = "",
    created_to: str = "",
    page_size: int = 100,
    max_pages: int = 5,
    timeout_seconds: int = 20,
    base_url: str = DEFAULT_ORDER_API_URL,
    urlopen_func: Any = None,
) -> dict[str, Any]:
    source_rows, query_log = fetch_order_api_rows(
        token=token,
        states=states,
        statuses=statuses,
        delivery_types=delivery_types,
        created_from=created_from,
        created_to=created_to,
        page_size=page_size,
        max_pages=max_pages,
        timeout_seconds=timeout_seconds,
        base_url=base_url,
        urlopen_func=urlopen_func,
    )
    return run_sidecar_build_from_rows(
        source_rows=source_rows,
        run_dir=run_dir,
        detected_at=detected_at,
        sidecar_csv=sidecar_csv,
        update_sidecar=update_sidecar,
        persist_actionable_only=persist_actionable_only,
        local_ref_prefix=local_ref_prefix,
        mode="fetch_once_read_only",
        input_path="GET /shop/api/v2/orders",
        fetch_query_log=query_log,
    )


def _watch_now(now_func: Any = None) -> datetime:
    value = now_func() if now_func else datetime.now(ALMATY_TZ)
    if not isinstance(value, datetime):
        raise KaspiOrderFetchError("watch now_func must return datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=ALMATY_TZ)
    return value


def _iso_seconds(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _external_writes_false() -> dict[str, bool]:
    return {
        "kaspi_order_mutation": False,
        "telegram_send": False,
        "print_job": False,
        "autonomous_business_write": False,
    }


def _write_watch_heartbeat(
    *,
    heartbeat_path: Path,
    status: str,
    run_dir: Path,
    cycle_index: int,
    cycles_requested: int,
    last_cycle_summary: Mapping[str, Any],
    sidecar_csv: Path | None,
    updated_at: str,
    error: str = "",
) -> dict[str, Any]:
    heartbeat = {
        "schema_version": WATCH_HEARTBEAT_VERSION,
        "status": status,
        "updated_at": updated_at,
        "run_dir": str(run_dir),
        "cycle_index": cycle_index,
        "cycles_requested": cycles_requested,
        "last_cycle_status": _clean(last_cycle_summary.get("status")),
        "last_cycle_run_dir": _clean(last_cycle_summary.get("run_dir")),
        "last_source_rows": int(last_cycle_summary.get("source_rows") or 0),
        "last_incoming_sidecar_rows": int(last_cycle_summary.get("incoming_sidecar_rows") or 0),
        "last_persisted_merged_sidecar_rows": int(last_cycle_summary.get("persisted_merged_sidecar_rows") or 0),
        "last_skipped_persist_sidecar_rows": int(last_cycle_summary.get("skipped_persist_sidecar_rows") or 0),
        "last_alert_queue_rows": int(last_cycle_summary.get("alert_queue_rows") or 0),
        "sidecar_csv": str(sidecar_csv) if sidecar_csv else "",
        "error": error,
        "external_writes": _external_writes_false(),
    }
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_path.write_text(json.dumps(heartbeat, ensure_ascii=False, indent=2), encoding="utf-8")
    return heartbeat


def run_sidecar_watch_loop(
    *,
    token: str,
    run_dir: Path,
    detected_at: str | None = None,
    sidecar_csv: Path | None = None,
    update_sidecar: bool = False,
    persist_actionable_only: bool = False,
    local_ref_prefix: str = "watch",
    states: Sequence[str] | None = None,
    statuses: Sequence[str] | None = None,
    delivery_types: Sequence[str] | None = None,
    created_from: str = "",
    created_to: str = "",
    lookback_hours: int = 24,
    cycles: int = 1,
    interval_seconds: int = 0,
    page_size: int = 100,
    max_pages: int = 5,
    timeout_seconds: int = 20,
    base_url: str = DEFAULT_ORDER_API_URL,
    urlopen_func: Any = None,
    sleep_func: Any = None,
    now_func: Any = None,
) -> dict[str, Any]:
    if cycles < 1:
        raise KaspiOrderFetchError("cycles must be >= 1")
    if interval_seconds < 0:
        raise KaspiOrderFetchError("interval_seconds must be >= 0")
    if lookback_hours < 1:
        raise KaspiOrderFetchError("lookback_hours must be >= 1")

    run_dir.mkdir(parents=True, exist_ok=True)
    heartbeat_path = run_dir / "watch_heartbeat.json"
    watch_summary_path = run_dir / "watch_summary.json"
    cycle_summaries: list[dict[str, Any]] = []
    sleeper = sleep_func or time.sleep

    for cycle_index in range(1, cycles + 1):
        now_value = _watch_now(now_func)
        cycle_detected_at = detected_at or _iso_seconds(now_value)
        cycle_created_to = created_to or _iso_seconds(now_value)
        cycle_created_from = created_from or _iso_seconds(now_value - timedelta(hours=lookback_hours))
        cycle_dir = run_dir / f"cycle_{cycle_index:03d}"
        try:
            summary = run_sidecar_fetch_once(
                token=token,
                run_dir=cycle_dir,
                detected_at=cycle_detected_at,
                sidecar_csv=sidecar_csv,
                update_sidecar=update_sidecar,
                persist_actionable_only=persist_actionable_only,
                local_ref_prefix=f"{local_ref_prefix}_{cycle_index:03d}",
                states=states,
                statuses=statuses,
                delivery_types=delivery_types,
                created_from=cycle_created_from,
                created_to=cycle_created_to,
                page_size=page_size,
                max_pages=max_pages,
                timeout_seconds=timeout_seconds,
                base_url=base_url,
                urlopen_func=urlopen_func,
            )
        except KaspiOrderFetchError as exc:
            blocked_summary = {
                "status": "blocked",
                "run_dir": str(cycle_dir),
                "source_rows": 0,
                "incoming_sidecar_rows": 0,
                "alert_queue_rows": 0,
                "external_writes": _external_writes_false(),
            }
            _write_watch_heartbeat(
                heartbeat_path=heartbeat_path,
                status="blocked",
                run_dir=run_dir,
                cycle_index=cycle_index,
                cycles_requested=cycles,
                last_cycle_summary=blocked_summary,
                sidecar_csv=sidecar_csv,
                updated_at=_iso_seconds(_watch_now(now_func)),
                error=str(exc),
            )
            raise

        cycle_summaries.append(
            {
                "cycle_index": cycle_index,
                "status": summary["status"],
                "run_dir": summary["run_dir"],
                "source_rows": summary["source_rows"],
                "incoming_sidecar_rows": summary["incoming_sidecar_rows"],
                "persisted_merged_sidecar_rows": summary["persisted_merged_sidecar_rows"],
                "skipped_persist_sidecar_rows": summary["skipped_persist_sidecar_rows"],
                "alert_queue_rows": summary["alert_queue_rows"],
                "created_from": cycle_created_from,
                "created_to": cycle_created_to,
                "summary_path": summary["summary_path"],
            }
        )
        _write_watch_heartbeat(
            heartbeat_path=heartbeat_path,
            status="success",
            run_dir=run_dir,
            cycle_index=cycle_index,
            cycles_requested=cycles,
            last_cycle_summary=summary,
            sidecar_csv=sidecar_csv,
            updated_at=_iso_seconds(_watch_now(now_func)),
        )
        if cycle_index < cycles and interval_seconds:
            sleeper(interval_seconds)

    watch_summary = {
        "status": "success",
        "mode": "watch_loop_read_only",
        "run_dir": str(run_dir),
        "cycles_requested": cycles,
        "cycles_completed": len(cycle_summaries),
        "cycle_summaries": cycle_summaries,
        "heartbeat_path": str(heartbeat_path),
        "watch_summary_path": str(watch_summary_path),
        "sidecar_csv": str(sidecar_csv) if sidecar_csv else "",
        "update_sidecar": bool(update_sidecar),
        "persist_actionable_only": bool(persist_actionable_only),
        "external_writes": _external_writes_false(),
    }
    watch_summary_path.write_text(json.dumps(watch_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return watch_summary


def _hash_prefix(value: str) -> str:
    clean = _clean(value)
    if not clean:
        return ""
    if clean.startswith("sha256:"):
        return clean[:19]
    return _hash_text(clean)[:19]


def build_selfpickup_sample_scan_rows(rows: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    """Classify minimized sidecar rows for true PP2 seller self-pickup proof."""

    scan_rows: list[dict[str, str]] = []
    for row in rows:
        delivery_kind = _clean(row.get("delivery_kind"))
        classification = _clean(row.get("classification"))
        pickup_point_id = _clean(row.get("pickup_point_id"))
        is_kaspi_delivery = _parse_bool(row.get("is_kaspi_delivery"))
        pp2_seen = _is_pp2_pickup_point(pickup_point_id)
        pickup_proof = _clean(row.get("pickup_point_proof_status"))
        seller_proven = (
            delivery_kind == "SELLER_SELF_PICKUP"
            and classification == "PP2_SELF_SERVING_PICKUP_PROVEN"
            and pp2_seen
            and pickup_proof in {"PP2_PROVEN", "PP2_ADDRESS_PROVEN", "POINT_OF_SERVICE_PP2_PROVEN"}
            and is_kaspi_delivery is False
        )
        if seller_proven:
            reason = "true PP2 seller self-pickup proof exists; pickup-completion-review may be prepared"
        elif pp2_seen and is_kaspi_delivery is True:
            reason = "PP2 pickup point is visible, but is_kaspi_delivery=True; this is not seller self-pickup"
        elif delivery_kind == "SELLER_SELF_PICKUP":
            reason = "seller pickup shape exists, but PP2 proof or non-Kaspi-delivery proof is incomplete"
        else:
            reason = "row is outside true seller self-pickup completion scope"

        scan_rows.append(
            {
                "scan_version": SELF_PICKUP_SAMPLE_SCAN_VERSION,
                "lane_id": LANE_ID,
                "local_ref": _clean(row.get("local_ref")),
                "order_hash_prefix": _hash_prefix(_clean(row.get("order_hash"))),
                "delivery_kind": delivery_kind,
                "classification": classification,
                "classification_confidence": _clean(row.get("classification_confidence")),
                "pickup_point_id": pickup_point_id,
                "is_kaspi_delivery": "" if is_kaspi_delivery is None else str(is_kaspi_delivery),
                "pp2_pickup_point_seen": str(pp2_seen),
                "seller_self_pickup_proven": str(seller_proven),
                "unlock_pickup_completion_review": str(seller_proven),
                "reason": reason,
            }
        )
    return scan_rows


def _render_selfpickup_sample_scan_markdown(summary: Mapping[str, Any], rows: Sequence[Mapping[str, str]]) -> str:
    lines = [
        "# ACMEWEAR PP2 Seller Self-Pickup Sample Scan",
        "",
        f"- Schema: `{summary.get('schema_version')}`",
        f"- Gate: `{summary.get('gate')}`",
        f"- Decision: `{summary.get('decision')}`",
        f"- Generated at: `{summary.get('generated_at')}`",
        f"- Input CSV: `{summary.get('input_csv')}`",
        f"- Source rows: `{summary.get('source_rows')}`",
        f"- PP2 pickup-point rows: `{summary.get('pp2_pickup_point_rows')}`",
        f"- PP2 rows still marked Kaspi delivery: `{summary.get('pp2_kaspi_delivery_rows')}`",
        f"- Proven seller self-pickup rows: `{summary.get('proven_seller_selfpickup_rows')}`",
        "- External writes: none. This scan does not mutate Kaspi orders, Telegram, print jobs, or Autonomous_business.",
        "",
        "## Rule",
        "",
        "- `pickupPointId=30137883_PP2` alone is not enough.",
        "- Unlock requires `delivery_kind=SELLER_SELF_PICKUP`, `classification=PP2_SELF_SERVING_PICKUP_PROVEN`, PP2 proof, and `is_kaspi_delivery=False`.",
        "",
        "## Rows",
        "",
        "| Local ref | Delivery kind | Classification | PP2 seen | Kaspi delivery | Unlock | Reason |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| {local_ref} | {delivery_kind} | {classification} | {pp2} | {kaspi} | {unlock} | {reason} |".format(
                local_ref=_clean(row.get("local_ref")),
                delivery_kind=_clean(row.get("delivery_kind")),
                classification=_clean(row.get("classification")),
                pp2=_clean(row.get("pp2_pickup_point_seen")),
                kaspi=_clean(row.get("is_kaspi_delivery")),
                unlock=_clean(row.get("unlock_pickup_completion_review")),
                reason=_clean(row.get("reason")),
            )
        )
    lines.append("")
    return "\n".join(lines)


def run_selfpickup_sample_scan(
    *,
    input_csv: Path,
    run_dir: Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Write a read-only proof report for true PP2 seller self-pickup samples."""

    rows = read_sidecar_csv(input_csv)
    scan_rows = build_selfpickup_sample_scan_rows(rows)
    proven_rows = [row for row in scan_rows if _clean(row.get("seller_self_pickup_proven")) == "True"]
    pp2_rows = [row for row in scan_rows if _clean(row.get("pp2_pickup_point_seen")) == "True"]
    pp2_kaspi_rows = [
        row
        for row in pp2_rows
        if _parse_bool(row.get("is_kaspi_delivery")) is True
    ]
    run_dir.mkdir(parents=True, exist_ok=True)
    scan_csv_path = run_dir / "selfpickup_sample_scan.csv"
    summary_path = run_dir / "selfpickup_sample_scan_summary.json"
    report_path = run_dir / "selfpickup_sample_scan_report.md"
    generated_at_value = generated_at or datetime.now(ALMATY_TZ).isoformat(timespec="seconds")
    summary: dict[str, Any] = {
        "schema_version": SELF_PICKUP_SAMPLE_SCAN_VERSION,
        "status": "reviewed",
        "gate": "GREEN" if proven_rows else "YELLOW",
        "decision": "PICKUP_COMPLETION_REVIEW_READY" if proven_rows else "NO_GO_NO_PROVEN_SELF_PICKUP_SAMPLE",
        "generated_at": generated_at_value,
        "run_dir": str(run_dir),
        "input_csv": str(input_csv),
        "source_rows": len(rows),
        "scan_rows": len(scan_rows),
        "pp2_pickup_point_rows": len(pp2_rows),
        "pp2_kaspi_delivery_rows": len(pp2_kaspi_rows),
        "proven_seller_selfpickup_rows": len(proven_rows),
        "selfpickup_sample_scan_path": str(scan_csv_path),
        "selfpickup_sample_scan_report_path": str(report_path),
        "selfpickup_sample_scan_summary_path": str(summary_path),
        "external_writes": _external_writes_false(),
    }
    write_csv(scan_csv_path, SELF_PICKUP_SAMPLE_SCAN_COLUMNS, scan_rows)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(_render_selfpickup_sample_scan_markdown(summary, scan_rows), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def _latest_matching_file(root: Path, filename: str) -> Path | None:
    if not root.exists():
        return None
    matches = [path for path in root.rglob(filename) if path.is_file()]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def _latest_role_closeout(root: Path, role_keywords: Sequence[str], fallback_filename: str) -> Path | None:
    """Find the latest closeout for a role, not merely the latest agent number."""

    if not root.exists():
        return None
    normalized_keywords = tuple(_clean(keyword).lower() for keyword in role_keywords if _clean(keyword))
    matches: list[Path] = []
    for path in root.rglob("AGENT_*_CLOSEOUT.md"):
        if not path.is_file():
            continue
        haystack = str(path.parent).lower()
        if any(keyword in haystack for keyword in normalized_keywords):
            matches.append(path)
    if matches:
        return max(matches, key=lambda path: path.stat().st_mtime)
    return _latest_matching_file(root, fallback_filename)


def _closeout_gate(path: Path | None) -> tuple[str, str]:
    if path is None:
        return "MISSING", ""
    if not path.exists():
        return "MISSING", str(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "UNREADABLE", str(path)
    for line in text.splitlines():
        clean = _clean(line)
        if clean in {"Gate: GREEN", "Gate: YELLOW", "Gate: RED"}:
            return clean.split(": ", 1)[1], str(path)
        if clean in {"GREEN", "YELLOW", "RED"}:
            return clean, str(path)
    return "UNKNOWN", str(path)


def _readiness_gate_from_closeout(value: str) -> str:
    if value == "GREEN":
        return "GREEN"
    if value in {"YELLOW", "MISSING", "UNKNOWN", "UNREADABLE"}:
        return "YELLOW"
    return "RED"


def _env_has_value(env: Mapping[str, str], name: str) -> bool:
    return bool(_clean(env.get(name)))


def _dedupe_texts(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = _clean(value)
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


def _split_semicolon_text(value: str) -> list[str]:
    return [part for part in (_clean(item) for item in _clean(value).split(";")) if part]


def acmewear_kaspi_token_env_candidates(token_env: str = DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV) -> tuple[str, ...]:
    """Return preferred token env names without exposing token values."""

    requested = _clean(token_env) or DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV
    if requested in ACMEWEAR_KASPI_TOKEN_ENV_ALIASES:
        return tuple(_dedupe_texts([requested, *ACMEWEAR_KASPI_TOKEN_ENV_ALIASES]))
    return (requested,)


def _env_alias_candidates(requested_env: str, aliases: Sequence[str], default_env: str) -> tuple[str, ...]:
    requested = _clean(requested_env) or default_env
    if requested in aliases:
        return tuple(_dedupe_texts([requested, *aliases]))
    return (requested,)


def telegram_bot_token_env_candidates(
    token_env: str = DEFAULT_TELEGRAM_BOT_TOKEN_ENV,
) -> tuple[str, ...]:
    """Return Telegram bot-token env candidates without exposing values."""

    return _env_alias_candidates(token_env, TELEGRAM_BOT_TOKEN_ENV_ALIASES, DEFAULT_TELEGRAM_BOT_TOKEN_ENV)


def telegram_alert_chat_id_env_candidates(
    chat_id_env: str = DEFAULT_TELEGRAM_ALERT_CHAT_ID_ENV,
) -> tuple[str, ...]:
    """Return Telegram alert chat-id env candidates without exposing values."""

    return _env_alias_candidates(
        chat_id_env,
        TELEGRAM_ALERT_CHAT_ID_ENV_ALIASES,
        DEFAULT_TELEGRAM_ALERT_CHAT_ID_ENV,
    )


def telegram_print_chat_id_env_candidates(
    chat_id_env: str = DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV,
) -> tuple[str, ...]:
    """Return Telegram print chat-id env candidates without exposing values."""

    return _env_alias_candidates(
        chat_id_env,
        TELEGRAM_PRINT_CHAT_ID_ENV_ALIASES,
        DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV,
    )


def resolve_present_env_alias(
    env: Mapping[str, str],
    requested_env: str,
    aliases: Sequence[str],
    default_env: str,
) -> str:
    for candidate in _env_alias_candidates(requested_env, aliases, default_env):
        if _env_has_value(env, candidate):
            return candidate
    return ""


def resolve_present_acmewear_kaspi_token_env(
    env: Mapping[str, str],
    token_env: str = DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV,
) -> str:
    for candidate in acmewear_kaspi_token_env_candidates(token_env):
        if _env_has_value(env, candidate):
            return candidate
    return ""


def _sidecar_operational_summary(sidecar_csv: Path | None) -> dict[str, Any]:
    rows = read_sidecar_csv(sidecar_csv) if sidecar_csv else []
    express_rows = [row for row in rows if _clean(row.get("delivery_kind")) == "EXPRESS_DELIVERY"]
    self_pickup_rows = [row for row in rows if _clean(row.get("delivery_kind")) == "SELLER_SELF_PICKUP"]
    size_ready_rows = [
        row
        for row in rows
        if _clean(row.get("size_status")) in {"SIZE_READY_FROM_RULE", "SIZE_READY_OWNER_OVERRIDE"}
        or bool(_clean(row.get("my_size")) or _clean(row.get("owner_size_override")))
    ]
    assemble_candidate_rows = [
        row
        for row in express_rows
        if row in size_ready_rows
        and not _clean(row.get("ambiguous_delivery_mode_blocker"))
        and not _clean(row.get("missing_waybill_blocker"))
    ]
    pickup_review_candidate_rows = [
        row
        for row in self_pickup_rows
        if _clean(row.get("classification")) == "PP2_SELF_SERVING_PICKUP_PROVEN" and row in size_ready_rows
    ]
    return {
        "sidecar_path": str(sidecar_csv) if sidecar_csv else "",
        "sidecar_exists": bool(sidecar_csv and sidecar_csv.exists()),
        "rows": len(rows),
        "express_rows": len(express_rows),
        "seller_self_pickup_rows": len(self_pickup_rows),
        "size_ready_rows": len(size_ready_rows),
        "assemble_candidate_rows": len(assemble_candidate_rows),
        "pickup_review_candidate_rows": len(pickup_review_candidate_rows),
    }


def _render_readiness_markdown(summary: Mapping[str, Any], rows: Sequence[Mapping[str, str]]) -> str:
    lines = [
        "# ACMEWEAR PP2 Express / Self-Pickup Readiness",
        "",
        f"- Schema: `{summary.get('schema_version')}`",
        f"- Gate: `{summary.get('gate')}`",
        f"- Run dir: `{summary.get('run_dir')}`",
        f"- Generated at: `{summary.get('generated_at')}`",
        "- External writes: none. This review does not mutate Kaspi orders, Telegram, print jobs, or Autonomous_business.",
        "",
        "## Current Sidecar",
        "",
    ]
    sidecar = summary.get("sidecar_summary") if isinstance(summary.get("sidecar_summary"), Mapping) else {}
    for key in (
        "sidecar_path",
        "sidecar_exists",
        "rows",
        "express_rows",
        "seller_self_pickup_rows",
        "size_ready_rows",
        "assemble_candidate_rows",
        "pickup_review_candidate_rows",
    ):
        lines.append(f"- {key}: `{sidecar.get(key, '')}`")
    lines.extend(
        [
            "",
            "## Go / No-Go Matrix",
            "",
            "| Check | Gate | Decision | Blocker | Next step |",
            "|---|---|---|---|---|",
        ]
    )
    for row in rows:
        lines.append(
            "| {check_id} | {gate} | {decision} | {blocker} | {next_step} |".format(
                check_id=_clean(row.get("check_id")),
                gate=_clean(row.get("gate")),
                decision=_clean(row.get("decision")),
                blocker=_clean(row.get("blocker")),
                next_step=_clean(row.get("next_step")),
            )
        )
    lines.extend(
        [
            "",
            "## Operator Rule",
            "",
            "- Manual handling remains allowed: contact customer, resolve size, print/send the already-cropped label manually, and assemble only when the operator is ready for courier pickup.",
            "- Scheduled watch, live Telegram alerts, live label sends, Express assemble, and seller pickup completion remain gated until their rows are GREEN and separately owner-approved.",
            "",
        ]
    )
    return "\n".join(lines)


def build_operational_readiness_review(
    *,
    run_root: Path = DEFAULT_RUN_ROOT,
    sidecar_csv: Path | None = None,
    agent1_closeout: Path | None = None,
    agent2_closeout: Path | None = None,
    agent3_closeout: Path | None = None,
    agent4_closeout: Path | None = None,
    agent5_closeout: Path | None = None,
    token_env: str = DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV,
    telegram_bot_token_env: str = DEFAULT_TELEGRAM_BOT_TOKEN_ENV,
    telegram_alert_chat_id_env: str = DEFAULT_TELEGRAM_ALERT_CHAT_ID_ENV,
    telegram_print_chat_id_env: str = DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV,
    env: Mapping[str, str] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    env_map: Mapping[str, str] = env or {}
    token_env_candidates = acmewear_kaspi_token_env_candidates(token_env)
    resolved_token_env = resolve_present_acmewear_kaspi_token_env(env_map, token_env)
    telegram_bot_token_candidates = telegram_bot_token_env_candidates(telegram_bot_token_env)
    telegram_alert_chat_candidates = telegram_alert_chat_id_env_candidates(telegram_alert_chat_id_env)
    telegram_print_chat_candidates = telegram_print_chat_id_env_candidates(telegram_print_chat_id_env)
    resolved_telegram_bot_token_env = resolve_present_env_alias(
        env_map,
        telegram_bot_token_env,
        TELEGRAM_BOT_TOKEN_ENV_ALIASES,
        DEFAULT_TELEGRAM_BOT_TOKEN_ENV,
    )
    resolved_telegram_alert_chat_id_env = resolve_present_env_alias(
        env_map,
        telegram_alert_chat_id_env,
        TELEGRAM_ALERT_CHAT_ID_ENV_ALIASES,
        DEFAULT_TELEGRAM_ALERT_CHAT_ID_ENV,
    )
    resolved_telegram_print_chat_id_env = resolve_present_env_alias(
        env_map,
        telegram_print_chat_id_env,
        TELEGRAM_PRINT_CHAT_ID_ENV_ALIASES,
        DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV,
    )
    closeouts = {
        "agent1_live_fetch_watch": agent1_closeout
        or _latest_role_closeout(run_root, ("live_fetch_watch", "watch_ab_token"), "AGENT_1_CLOSEOUT.md"),
        "agent2_ab_google_handoff": agent2_closeout
        or _latest_role_closeout(run_root, ("ab_google_handoff",), "AGENT_2_CLOSEOUT.md"),
        "agent3_telegram_config": agent3_closeout
        or _latest_role_closeout(run_root, ("telegram",), "AGENT_3_CLOSEOUT.md"),
        "agent4_pickup_completion": agent4_closeout
        or _latest_role_closeout(run_root, ("pickup_completion", "self_pickup_completion"), "AGENT_4_CLOSEOUT.md"),
        "agent5_go_no_go": agent5_closeout
        or _latest_role_closeout(run_root, ("go_no_go",), "AGENT_5_CLOSEOUT.md"),
    }
    closeout_gates: dict[str, dict[str, str]] = {}
    for key, path in closeouts.items():
        gate, evidence_path = _closeout_gate(path)
        closeout_gates[key] = {"gate": gate, "path": evidence_path}

    token_ready = bool(resolved_token_env)
    telegram_alert_ready = bool(resolved_telegram_bot_token_env and resolved_telegram_alert_chat_id_env)
    telegram_label_ready = bool(resolved_telegram_bot_token_env and resolved_telegram_print_chat_id_env)
    sidecar_summary = _sidecar_operational_summary(sidecar_csv)
    live_fetch_gate = closeout_gates["agent1_live_fetch_watch"]["gate"]
    handoff_gate = closeout_gates["agent2_ab_google_handoff"]["gate"]
    telegram_config_gate = closeout_gates["agent3_telegram_config"]["gate"]
    pickup_review_gate = closeout_gates["agent4_pickup_completion"]["gate"]
    go_no_go_gate = closeout_gates["agent5_go_no_go"]["gate"]

    rows: list[dict[str, str]] = [
        {
            "check_id": "manual_operations",
            "domain": "operator",
            "gate": "GREEN",
            "decision": "GO_MANUAL_ONLY",
            "evidence_path": "",
            "blocker": "",
            "next_step": "Keep manual customer contact, size resolution, label print/send, and assembly handoff active.",
        },
        {
            "check_id": "kaspi_token_env",
            "domain": "api_read",
            "gate": "GREEN" if token_ready else "YELLOW",
            "decision": "GO_GET_ONLY_WHEN_PRESENT" if token_ready else "NO_GO_FETCH_WATCH",
            "evidence_path": ",".join(token_env_candidates),
            "blocker": "" if token_ready else f"missing_env_any:{','.join(token_env_candidates)}",
            "next_step": "Set token env in approved local env file and rerun GET-only fetch/watch proof.",
        },
        {
            "check_id": "live_fetch_watch_proof",
            "domain": "api_read",
            "gate": _readiness_gate_from_closeout(live_fetch_gate),
            "decision": "GO_SCHEDULER_REVIEW_INPUT" if live_fetch_gate == "GREEN" else "NO_GO_SCHEDULER",
            "evidence_path": closeout_gates["agent1_live_fetch_watch"]["path"],
            "blocker": "" if live_fetch_gate == "GREEN" else f"agent1_gate:{live_fetch_gate}",
            "next_step": "Run Agent 1 GET-only live proof after token config; scheduler install requires Gate: GREEN.",
        },
        {
            "check_id": "ab_google_handoff",
            "domain": "downstream_contract",
            "gate": _readiness_gate_from_closeout(handoff_gate),
            "decision": "GO_HANDOFF_PREP_ONLY" if handoff_gate == "GREEN" else "NO_GO_AB_GOOGLE_WRITE",
            "evidence_path": closeout_gates["agent2_ab_google_handoff"]["path"],
            "blocker": "" if handoff_gate == "GREEN" else f"ab_google_handoff_gate:{handoff_gate}",
            "next_step": "Use handoff for downstream AB/Google dry-run only; live AB/Google writes remain out of this repo.",
        },
        {
            "check_id": "telegram_alert_live_config",
            "domain": "telegram",
            "gate": "GREEN" if telegram_alert_ready else "YELLOW",
            "decision": "READY_FOR_OWNER_APPROVED_ALERT_PILOT" if telegram_alert_ready else "NO_GO_TELEGRAM_ALERT_SEND",
            "evidence_path": f"{','.join(telegram_bot_token_candidates)};{','.join(telegram_alert_chat_candidates)}",
            "blocker": "" if telegram_alert_ready else "missing_telegram_alert_env",
            "next_step": "Configure bot token and alert chat env, then run dry-run from current queue before any --send.",
        },
        {
            "check_id": "telegram_label_live_config",
            "domain": "telegram",
            "gate": "GREEN" if telegram_label_ready else "YELLOW",
            "decision": "READY_FOR_OWNER_APPROVED_LABEL_PILOT" if telegram_label_ready else "NO_GO_TELEGRAM_LABEL_SEND",
            "evidence_path": f"{','.join(telegram_bot_token_candidates)};{','.join(telegram_print_chat_candidates)}",
            "blocker": "" if telegram_label_ready else "missing_telegram_label_env",
            "next_step": "Configure bot token and print chat env, then run dry-run with current cropped PDF before any --send.",
        },
        {
            "check_id": "telegram_config_probe",
            "domain": "telegram",
            "gate": _readiness_gate_from_closeout(telegram_config_gate),
            "decision": "GO_DRY_RUN_REUSE" if telegram_config_gate == "GREEN" else "NEEDS_CURRENT_QUEUE_AND_CONFIG",
            "evidence_path": closeout_gates["agent3_telegram_config"]["path"],
            "blocker": "" if telegram_config_gate == "GREEN" else f"telegram_config_gate:{telegram_config_gate}",
            "next_step": "Re-run Telegram dry-run after current queue exists and env is configured.",
        },
        {
            "check_id": "sidecar_current_rows",
            "domain": "sidecar",
            "gate": "GREEN" if int(sidecar_summary["rows"]) > 0 else "YELLOW",
            "decision": "GO_ROW_REVIEW" if int(sidecar_summary["rows"]) > 0 else "NO_CURRENT_ROWS",
            "evidence_path": str(sidecar_csv) if sidecar_csv else "",
            "blocker": ""
            if int(sidecar_summary["rows"]) > 0
            else ("empty_sidecar" if sidecar_summary.get("sidecar_exists") else "missing_sidecar"),
            "next_step": "Populate sidecar through GET-only watch or reviewed redacted build before live pilots.",
        },
        {
            "check_id": "express_assemble_pilot",
            "domain": "kaspi_order_mutation",
            "gate": "YELLOW",
            "decision": "NO_GO_ASSEMBLE_AUTOMATION",
            "evidence_path": "",
            "blocker": "requires_exact_owner_approval_and_size_ready_express_row",
            "next_step": "Only after size is assigned and owner approves exact order-specific assemble pilot.",
        },
        {
            "check_id": "pickup_completion_review",
            "domain": "kaspi_order_mutation",
            "gate": _readiness_gate_from_closeout(pickup_review_gate),
            "decision": "GO_REVIEW_ONLY" if int(sidecar_summary["pickup_review_candidate_rows"]) > 0 else "NO_GO_PICKUP_COMPLETION",
            "evidence_path": closeout_gates["agent4_pickup_completion"]["path"],
            "blocker": "" if int(sidecar_summary["pickup_review_candidate_rows"]) > 0 else "no_ready_pp2_seller_selfpickup_row",
            "next_step": "Run pickup-completion-review only for proven PP2 seller self-pickup with arrival, handoff, and security-code proof.",
        },
        {
            "check_id": "final_go_no_go_review",
            "domain": "orchestration",
            "gate": _readiness_gate_from_closeout(go_no_go_gate),
            "decision": "GO_USE_MATRIX" if go_no_go_gate == "GREEN" else "USE_AS_YELLOW_STATUS",
            "evidence_path": closeout_gates["agent5_go_no_go"]["path"],
            "blocker": "" if go_no_go_gate == "GREEN" else f"go_no_go_gate:{go_no_go_gate}",
            "next_step": "Use this readiness command as the reusable follow-up matrix before every unlock.",
        },
    ]

    scheduler_ready = token_ready and live_fetch_gate == "GREEN"
    rows.append(
        {
            "check_id": "watch_scheduler_install",
            "domain": "scheduler",
            "gate": "GREEN" if scheduler_ready else "YELLOW",
            "decision": "READY_FOR_OWNER_APPROVED_INSTALL" if scheduler_ready else "NO_GO_SCHEDULER_INSTALL",
            "evidence_path": closeout_gates["agent1_live_fetch_watch"]["path"],
            "blocker": "" if scheduler_ready else "requires_token_env_and_agent1_green_live_proof",
            "next_step": "Install LaunchAgent only with ACMEWEAR_EXPRESS_WATCH_LIVE_PROOF_CLOSEOUT pointing to GREEN proof.",
        }
    )

    automation_rows = [row for row in rows if row["check_id"] != "manual_operations"]
    overall_gate = "GREEN" if all(row["gate"] == "GREEN" for row in automation_rows) else "YELLOW"
    if any(row["gate"] == "RED" for row in rows):
        overall_gate = "RED"
    summary = {
        "schema_version": READINESS_REVIEW_VERSION,
        "status": "reviewed",
        "gate": overall_gate,
        "mode": "readiness_review_only",
        "generated_at": generated_at or _iso_seconds(_watch_now()),
        "run_root": str(run_root),
        "sidecar_summary": sidecar_summary,
        "closeout_gates": closeout_gates,
        "env_checks": {
            "requested_kaspi_token_env": token_env,
            "effective_kaspi_token_env": resolved_token_env,
            **{candidate: _env_has_value(env_map, candidate) for candidate in token_env_candidates},
            "requested_telegram_bot_token_env": telegram_bot_token_env,
            "effective_telegram_bot_token_env": resolved_telegram_bot_token_env,
            **{candidate: _env_has_value(env_map, candidate) for candidate in telegram_bot_token_candidates},
            "requested_telegram_alert_chat_id_env": telegram_alert_chat_id_env,
            "effective_telegram_alert_chat_id_env": resolved_telegram_alert_chat_id_env,
            **{candidate: _env_has_value(env_map, candidate) for candidate in telegram_alert_chat_candidates},
            "requested_telegram_print_chat_id_env": telegram_print_chat_id_env,
            "effective_telegram_print_chat_id_env": resolved_telegram_print_chat_id_env,
            **{candidate: _env_has_value(env_map, candidate) for candidate in telegram_print_chat_candidates},
        },
        "matrix_rows": len(rows),
        "decisions": {
            "manual_operations": "GO",
            "scheduled_watch": "GO_WITH_OWNER_APPROVAL" if scheduler_ready else "NO_GO",
            "telegram_alert_live": "GO_WITH_OWNER_APPROVAL" if telegram_alert_ready else "NO_GO",
            "telegram_label_live": "GO_WITH_OWNER_APPROVAL" if telegram_label_ready else "NO_GO",
            "express_assemble": "NO_GO",
            "pickup_completion": "REVIEW_ONLY" if int(sidecar_summary["pickup_review_candidate_rows"]) > 0 else "NO_GO",
        },
        "external_writes": _external_writes_false(),
        "rows": rows,
    }
    return summary


def run_operational_readiness_review(
    *,
    run_dir: Path,
    run_root: Path = DEFAULT_RUN_ROOT,
    sidecar_csv: Path | None = None,
    agent1_closeout: Path | None = None,
    agent2_closeout: Path | None = None,
    agent3_closeout: Path | None = None,
    agent4_closeout: Path | None = None,
    agent5_closeout: Path | None = None,
    token_env: str = DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV,
    telegram_bot_token_env: str = DEFAULT_TELEGRAM_BOT_TOKEN_ENV,
    telegram_alert_chat_id_env: str = DEFAULT_TELEGRAM_ALERT_CHAT_ID_ENV,
    telegram_print_chat_id_env: str = DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV,
    env: Mapping[str, str] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    summary = build_operational_readiness_review(
        run_root=run_root,
        sidecar_csv=sidecar_csv,
        agent1_closeout=agent1_closeout,
        agent2_closeout=agent2_closeout,
        agent3_closeout=agent3_closeout,
        agent4_closeout=agent4_closeout,
        agent5_closeout=agent5_closeout,
        token_env=token_env,
        telegram_bot_token_env=telegram_bot_token_env,
        telegram_alert_chat_id_env=telegram_alert_chat_id_env,
        telegram_print_chat_id_env=telegram_print_chat_id_env,
        env=env,
        generated_at=generated_at,
    )
    rows = list(summary["rows"])
    matrix_path = run_dir / "readiness_matrix.csv"
    summary_path = run_dir / "readiness_summary.json"
    report_path = run_dir / "readiness_report.md"
    write_csv(matrix_path, READINESS_MATRIX_COLUMNS, rows)
    summary_for_json = dict(summary)
    summary_for_json["run_dir"] = str(run_dir)
    summary_for_json["readiness_matrix_path"] = str(matrix_path)
    summary_for_json["readiness_report_path"] = str(report_path)
    summary_for_json["readiness_summary_path"] = str(summary_path)
    summary_path.write_text(json.dumps(summary_for_json, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(_render_readiness_markdown(summary_for_json, rows), encoding="utf-8")
    summary_for_json["summary_path"] = str(summary_path)
    return summary_for_json


def _operator_action_for_row(row: Mapping[str, str]) -> tuple[str, str, str, str, str, str]:
    delivery_kind = _clean(row.get("delivery_kind"))
    classification = _clean(row.get("classification"))
    close_status = _clean(row.get("close_status"))
    blockers = _clean(row.get("blockers"))
    size_ready = _clean(row.get("size_status")) in {"SIZE_READY_FROM_RULE", "SIZE_READY_OWNER_OVERRIDE"} or bool(
        _clean(row.get("my_size")) or _clean(row.get("owner_size_override"))
    )
    waybill_ready = _clean(row.get("waybill_status")) == "WAYBILL_PRESENT_REDACTED" or _clean(row.get("waybill_present")) == "true"
    label_ready = bool(_clean(row.get("cropped_label_sha256"))) or _clean(row.get("telegram_label_status")) == "SENT"
    printed = _clean(row.get("printed_status")) == "PRINTED"

    if close_status in {"CLOSED", "CANCELLED"}:
        return (
            "LOW",
            "NO_ACTION_CLOSED",
            "Row is already closed or cancelled.",
            "NO_MUTATION",
            "NO_TELEGRAM",
            "NO_PRINT",
        )
    if delivery_kind not in {"EXPRESS_DELIVERY", "SELLER_SELF_PICKUP", "MANUAL_REVIEW"}:
        return (
            "LOW",
            "SKIP_NOT_PP2_SPECIAL_LANE",
            "Row is not an Express delivery or PP2 seller self-pickup candidate.",
            "NO_MUTATION",
            "NO_TELEGRAM",
            "NO_PRINT",
        )
    if "ambiguous_delivery_mode" in blockers:
        return (
            "HIGH",
            "MANUAL_REVIEW_DELIVERY_MODE",
            "Delivery mode is ambiguous; do not assemble or complete pickup until proof is resolved.",
            "BLOCKED_NEEDS_PROOF",
            "DRY_RUN_ONLY",
            "NO_PRINT",
        )
    if not size_ready:
        return (
            "CRITICAL" if delivery_kind == "EXPRESS_DELIVERY" else "HIGH",
            "CONTACT_CUSTOMER_FOR_SIZE",
            "Height/weight or owner size override is missing; assembly must wait.",
            "BLOCKED_SIZE_MISSING",
            "ALERT_REVIEW_READY",
            "NO_PRINT",
        )
    if delivery_kind == "EXPRESS_DELIVERY":
        if not waybill_ready:
            return (
                "CRITICAL",
                "SIZE_READY_WAIT_FOR_ASSEMBLE_APPROVAL_OR_WAYBILL",
                "Size is ready, but waybill is not proven present; assemble remains a separate live owner-approved gate.",
                "BLOCKED_OWNER_APPROVAL_REQUIRED",
                "DRY_RUN_ONLY",
                "NO_PRINT",
            )
        if not label_ready:
            return (
                "HIGH",
                "CROP_LABEL_AND_SEND_FOR_PRINT_REVIEW",
                "Existing waybill is present; crop the product label and run Telegram label dry-run before any live send.",
                "BLOCKED_OWNER_APPROVAL_REQUIRED",
                "DRY_RUN_FIRST",
                "PRINT_AFTER_LABEL_SEND",
            )
        if not printed:
            return (
                "HIGH",
                "PRINT_PRODUCT_LABEL",
                "Cropped label is ready/sent; operator should print or confirm thermal label attachment.",
                "BLOCKED_OWNER_APPROVAL_REQUIRED",
                "NO_TELEGRAM",
                "PRINT_READY",
            )
        return (
            "HIGH",
            "READY_FOR_MANUAL_ASSEMBLY_HANDOFF",
            "Size and label are ready; assemble only when operator is physically ready for courier pickup.",
            "BLOCKED_OWNER_APPROVAL_REQUIRED",
            "NO_TELEGRAM",
            "PRINTED",
        )
    if delivery_kind == "SELLER_SELF_PICKUP":
        if classification != "PP2_SELF_SERVING_PICKUP_PROVEN":
            return (
                "HIGH",
                "MANUAL_REVIEW_PICKUP_PROOF",
                "Seller self-pickup is not proven against PP2; do not complete pickup.",
                "BLOCKED_NEEDS_PROOF",
                "DRY_RUN_ONLY",
                "NO_PRINT",
            )
        return (
            "MEDIUM",
            "WAIT_FOR_CUSTOMER_ARRIVAL_AND_SECURITY_CODE",
            "Size is ready for PP2 seller self-pickup; complete only after customer arrival, handoff proof, and security-code review.",
            "BLOCKED_ARRIVAL_AND_CODE_REQUIRED",
            "NO_TELEGRAM",
            "NO_PRINT",
        )
    return (
        "HIGH",
        "OPERATOR_REVIEW",
        "Manual review row; do not mutate until a narrower proof exists.",
        "BLOCKED_NEEDS_PROOF",
        "DRY_RUN_ONLY",
        "NO_PRINT",
    )


def build_operator_queue_rows(rows: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    queue: list[dict[str, str]] = []
    for row in rows:
        priority, action, reason, mutation_gate, telegram_gate, print_gate = _operator_action_for_row(row)
        if action == "SKIP_NOT_PP2_SPECIAL_LANE":
            continue
        queue.append(
            {
                "queue_version": OPERATOR_QUEUE_VERSION,
                "lane_id": LANE_ID,
                "idempotency_key": _clean(row.get("idempotency_key")),
                "order_hash": _clean(row.get("order_hash")),
                "local_ref": _clean(row.get("local_ref")),
                "store_code": _clean(row.get("store_code")),
                "merchant_id": _clean(row.get("merchant_id")),
                "delivery_kind": _clean(row.get("delivery_kind")),
                "classification": _clean(row.get("classification")),
                "pickup_point_id": _clean(row.get("pickup_point_id")),
                "detected_at": _clean(row.get("detected_at")),
                "created_at": _clean(row.get("created_at")),
                "delivery_slot_label": _clean(row.get("delivery_slot_label")),
                "courier_planning_at": _clean(row.get("courier_planning_at")),
                "sku_key": _clean(row.get("sku_key")),
                "merchant_article": _clean(row.get("merchant_article")),
                "ordered_size": _clean(row.get("ordered_size")),
                "my_size": _clean(row.get("my_size")),
                "owner_size_override": _clean(row.get("owner_size_override")),
                "size_status": _clean(row.get("size_status")),
                "waybill_status": _clean(row.get("waybill_status")),
                "telegram_alert_status": _clean(row.get("telegram_alert_status")),
                "telegram_label_status": _clean(row.get("telegram_label_status")),
                "printed_status": _clean(row.get("printed_status")),
                "close_status": _clean(row.get("close_status")),
                "operator_priority": priority,
                "operator_next_action": action,
                "operator_reason": reason,
                "mutation_gate": mutation_gate,
                "telegram_gate": telegram_gate,
                "print_gate": print_gate,
                "blockers": _clean(row.get("blockers")),
                "warnings": _clean(row.get("warnings")),
            }
        )
    priority_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    queue.sort(key=lambda item: (priority_order.get(item["operator_priority"], 9), item["detected_at"], item["local_ref"]))
    return queue


def _render_operator_queue_markdown(summary: Mapping[str, Any], rows: Sequence[Mapping[str, str]]) -> str:
    lines = [
        "# ACMEWEAR PP2 Operator Queue",
        "",
        f"- Schema: `{summary.get('schema_version')}`",
        f"- Status: `{summary.get('status')}`",
        f"- Run dir: `{summary.get('run_dir')}`",
        f"- Source sidecar: `{summary.get('sidecar_csv')}`",
        f"- Queue rows: `{summary.get('operator_queue_rows')}`",
        "- External writes: none. This queue does not mutate Kaspi orders, Telegram, print jobs, Google, or Autonomous_business.",
        "",
        "| Priority | Action | Local ref | Delivery | Size | Waybill | Reason |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        size = _clean(row.get("my_size")) or _clean(row.get("owner_size_override")) or _clean(row.get("ordered_size")) or "-"
        lines.append(
            "| {priority} | {action} | {local_ref} | {delivery} | {size} | {waybill} | {reason} |".format(
                priority=_clean(row.get("operator_priority")),
                action=_clean(row.get("operator_next_action")),
                local_ref=_clean(row.get("local_ref")),
                delivery=_clean(row.get("delivery_kind")),
                size=size,
                waybill=_clean(row.get("waybill_status")) or "-",
                reason=_clean(row.get("operator_reason")),
            )
        )
    lines.extend(
        [
            "",
            "## Guardrail",
            "",
            "- `CONTACT_CUSTOMER_FOR_SIZE` comes before assembly.",
            "- `CROP_LABEL_AND_SEND_FOR_PRINT_REVIEW` is still dry-run/review until Telegram print-chat is configured and separately approved.",
            "- `READY_FOR_MANUAL_ASSEMBLY_HANDOFF` still does not authorize API/UI assembly; assembly remains a separate owner-approved live mutation.",
            "- Seller self-pickup completion requires customer arrival, handoff proof, and security-code review.",
            "",
        ]
    )
    return "\n".join(lines)


def run_operator_queue_review(
    *,
    sidecar_csv: Path,
    run_dir: Path,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    sidecar_rows = read_sidecar_csv(sidecar_csv)
    queue_rows = build_operator_queue_rows(sidecar_rows)
    queue_path = run_dir / "operator_queue.csv"
    summary_path = run_dir / "operator_queue_summary.json"
    report_path = run_dir / "operator_queue_report.md"
    write_csv(queue_path, OPERATOR_QUEUE_COLUMNS, queue_rows)
    action_counts: dict[str, int] = {}
    priority_counts: dict[str, int] = {}
    for row in queue_rows:
        action_counts[row["operator_next_action"]] = action_counts.get(row["operator_next_action"], 0) + 1
        priority_counts[row["operator_priority"]] = priority_counts.get(row["operator_priority"], 0) + 1
    summary = {
        "schema_version": OPERATOR_QUEUE_VERSION,
        "status": "success",
        "mode": "operator_queue_review_only",
        "run_dir": str(run_dir),
        "sidecar_csv": str(sidecar_csv),
        "sidecar_rows": len(sidecar_rows),
        "operator_queue_rows": len(queue_rows),
        "action_counts": action_counts,
        "priority_counts": priority_counts,
        "operator_queue_path": str(queue_path),
        "operator_queue_report_path": str(report_path),
        "operator_queue_summary_path": str(summary_path),
        "external_writes": _external_writes_false(),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(_render_operator_queue_markdown(summary, queue_rows), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def _count_by(rows: Iterable[Mapping[str, str]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = _clean(row.get(key)) or "blank"
        counts[value] = counts.get(value, 0) + 1
    return counts


def _render_shift_packet_markdown(
    summary: Mapping[str, Any],
    queue_rows: Sequence[Mapping[str, str]],
    readiness_rows: Sequence[Mapping[str, str]],
) -> str:
    lines = [
        "# ACMEWEAR PP2 Express / Self-Pickup Shift Packet",
        "",
        f"- Schema: `{summary.get('schema_version')}`",
        f"- Gate: `{summary.get('gate')}`",
        f"- Run dir: `{summary.get('run_dir')}`",
        f"- Sidecar: `{summary.get('sidecar_csv')}`",
        f"- Generated at: `{summary.get('generated_at')}`",
        "- External writes: none. This packet does not mutate Kaspi orders, Telegram, print jobs, Google, or Autonomous_business.",
        "",
        "## Current Action Queue",
        "",
        f"- Sidecar rows: `{summary.get('sidecar_rows')}`",
        f"- Operator queue rows: `{summary.get('operator_queue_rows')}`",
        f"- Actions: `{json.dumps(summary.get('action_counts', {}), ensure_ascii=False, sort_keys=True)}`",
        "",
    ]
    if not queue_rows:
        lines.extend(
            [
                "No current operator rows.",
                "",
                "Next safe move: populate the sidecar with `manual-intake` for a visible order, or configure `KASPI_TOKEN_ACMEWEAR` (legacy alias: `ACMEWEAR_KASPI_API_TOKEN`) and run the GET-only watch proof.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "| Priority | Action | Local ref | Delivery | Product | Size | Reason |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for row in queue_rows:
            size = _clean(row.get("my_size")) or _clean(row.get("owner_size_override")) or _clean(row.get("ordered_size")) or "-"
            product = _clean(row.get("sku_key")) or _clean(row.get("merchant_article")) or "-"
            lines.append(
                "| {priority} | {action} | {local_ref} | {delivery} | {product} | {size} | {reason} |".format(
                    priority=_clean(row.get("operator_priority")),
                    action=_clean(row.get("operator_next_action")),
                    local_ref=_clean(row.get("local_ref")),
                    delivery=_clean(row.get("delivery_kind")),
                    product=product,
                    size=size,
                    reason=_clean(row.get("operator_reason")),
                )
            )
        lines.append("")
    lines.extend(
        [
            "## Automation Readiness",
            "",
            f"- Readiness gate: `{summary.get('readiness_gate')}`",
            f"- Manual operations: `{summary.get('manual_operations_decision')}`",
            f"- Scheduled watch: `{summary.get('scheduled_watch_decision')}`",
            f"- Telegram alert live: `{summary.get('telegram_alert_decision')}`",
            f"- Telegram label live: `{summary.get('telegram_label_decision')}`",
            f"- Express assemble: `{summary.get('express_assemble_decision')}`",
            f"- Pickup completion: `{summary.get('pickup_completion_decision')}`",
            "",
            "| Check | Gate | Decision | Blocker | Next step |",
            "|---|---|---|---|---|",
        ]
    )
    for row in readiness_rows:
        lines.append(
            "| {check_id} | {gate} | {decision} | {blocker} | {next_step} |".format(
                check_id=_clean(row.get("check_id")),
                gate=_clean(row.get("gate")),
                decision=_clean(row.get("decision")),
                blocker=_clean(row.get("blocker")),
                next_step=_clean(row.get("next_step")),
            )
        )
    lines.extend(
        [
            "",
            "## Guardrail",
            "",
            "- Contact/size assignment comes before assembly.",
            "- Express assembly remains a separate live Kaspi mutation gate.",
            "- Seller self-pickup completion requires customer arrival, handoff proof, and security-code review.",
            "- Telegram alert/label sends require configured env values, dry-run review, and separate owner approval.",
            "",
        ]
    )
    return "\n".join(lines)


def run_shift_packet_review(
    *,
    sidecar_csv: Path,
    run_dir: Path,
    run_root: Path = DEFAULT_RUN_ROOT,
    env: Mapping[str, str] | None = None,
    token_env: str = DEFAULT_ACMEWEAR_KASPI_TOKEN_ENV,
    telegram_bot_token_env: str = DEFAULT_TELEGRAM_BOT_TOKEN_ENV,
    telegram_alert_chat_id_env: str = DEFAULT_TELEGRAM_ALERT_CHAT_ID_ENV,
    telegram_print_chat_id_env: str = DEFAULT_TELEGRAM_PRINT_CHAT_ID_ENV,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now(ALMATY_TZ).isoformat(timespec="seconds")
    run_dir.mkdir(parents=True, exist_ok=True)
    sidecar_rows = read_sidecar_csv(sidecar_csv)
    queue_rows = build_operator_queue_rows(sidecar_rows)
    readiness = build_operational_readiness_review(
        run_root=run_root,
        sidecar_csv=sidecar_csv,
        token_env=token_env,
        telegram_bot_token_env=telegram_bot_token_env,
        telegram_alert_chat_id_env=telegram_alert_chat_id_env,
        telegram_print_chat_id_env=telegram_print_chat_id_env,
        env=env or {},
        generated_at=generated_at,
    )
    readiness_rows = [
        {column: _clean(row.get(column)) for column in READINESS_MATRIX_COLUMNS}
        for row in readiness.get("rows", [])
        if isinstance(row, Mapping)
    ]
    decisions = readiness.get("decisions") if isinstance(readiness.get("decisions"), Mapping) else {}

    queue_path = run_dir / "shift_operator_queue.csv"
    matrix_path = run_dir / "shift_readiness_matrix.csv"
    summary_path = run_dir / "shift_packet_summary.json"
    report_path = run_dir / "shift_packet_report.md"
    write_csv(queue_path, OPERATOR_QUEUE_COLUMNS, queue_rows)
    write_csv(matrix_path, READINESS_MATRIX_COLUMNS, readiness_rows)

    gate = "GREEN" if readiness.get("gate") == "GREEN" and not queue_rows else "YELLOW"
    summary = {
        "schema_version": SHIFT_PACKET_VERSION,
        "status": "reviewed",
        "gate": gate,
        "mode": "shift_packet_review_only",
        "generated_at": generated_at,
        "run_dir": str(run_dir),
        "sidecar_csv": str(sidecar_csv),
        "sidecar_rows": len(sidecar_rows),
        "operator_queue_rows": len(queue_rows),
        "action_counts": _count_by(queue_rows, "operator_next_action"),
        "priority_counts": _count_by(queue_rows, "operator_priority"),
        "readiness_gate": _clean(readiness.get("gate")),
        "manual_operations_decision": _clean(decisions.get("manual_operations")),
        "scheduled_watch_decision": _clean(decisions.get("scheduled_watch")),
        "telegram_alert_decision": _clean(decisions.get("telegram_alert_live")),
        "telegram_label_decision": _clean(decisions.get("telegram_label_live")),
        "express_assemble_decision": _clean(decisions.get("express_assemble")),
        "pickup_completion_decision": _clean(decisions.get("pickup_completion")),
        "shift_operator_queue_path": str(queue_path),
        "shift_readiness_matrix_path": str(matrix_path),
        "shift_packet_report_path": str(report_path),
        "shift_packet_summary_path": str(summary_path),
        "external_writes": _external_writes_false(),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(_render_shift_packet_markdown(summary, queue_rows, readiness_rows), encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    return summary


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema-csv", type=Path, help="Write the sidecar schema CSV.")
    parser.add_argument("--sample-json", type=Path, help="Read redacted/minimized input rows from JSON.")
    parser.add_argument("--sample-csv", type=Path, help="Write minimized sample sidecar rows.")
    parser.add_argument("--run-dir", type=Path, help="Write a review run directory from --sample-json.")
    parser.add_argument("--sidecar-csv", type=Path, help="Existing repo-local sidecar CSV to merge with.")
    parser.add_argument("--update-sidecar", action="store_true", help="Persist merged rows into --sidecar-csv.")
    parser.add_argument("--local-ref-prefix", default="sample", help="Local ref prefix for generated rows.")
    parser.add_argument("--detected-at", default="", help="Fixed detection timestamp for reproducible artifacts.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.schema_csv:
        write_csv(args.schema_csv, ("column", "type", "required", "allowed_values", "description", "sensitive_policy"), SIDE_CAR_SCHEMA_ROWS)
    if args.sample_csv:
        if not args.sample_json:
            raise SystemExit("--sample-csv requires --sample-json")
        rows = build_sidecar_rows_from_orders(
            load_json_or_jsonl_rows(args.sample_json),
            detected_at=args.detected_at or None,
            local_ref_prefix=args.local_ref_prefix,
        )
        write_csv(args.sample_csv, SIDE_CAR_COLUMNS, dedupe_sidecar_rows(rows))
    if args.run_dir:
        if not args.sample_json:
            raise SystemExit("--run-dir requires --sample-json")
        summary = run_sidecar_build_from_file(
            input_path=args.sample_json,
            run_dir=args.run_dir,
            detected_at=args.detected_at or None,
            sidecar_csv=args.sidecar_csv,
            update_sidecar=args.update_sidecar,
            local_ref_prefix=args.local_ref_prefix,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
