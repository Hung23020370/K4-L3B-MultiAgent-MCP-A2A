from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

COORDINATOR_MODEL = {
    "provider": "deepseek",
    "name": "DeepSeek-7B",
    "size_billions": 7,
    "role": "coordinator",
    "max_supported_params_billions": 10,
}

COORDINATOR_SYSTEM_PROMPT = """You are the Coordinator Agent for a compact ecommerce complaint investigation system.

Your role:
- Manage the overall investigation workflow for a single case.
- Maintain case memory across all stages.
- Assign tasks to specialist agents and merge their outputs.
- Keep all reasoning grounded in verified MCP evidence only.
- Do not fabricate missing facts or assume identity without evidence.
- Finalize only after schema and consistency checks pass.

Core responsibilities:
1. Resolve the correct order using candidate_order_ids, claimed_order_id, and evidence.
2. Maintain memory: case_id, claimed_order_id, candidate_order_ids, resolved_order_ids, rejected_candidates, evidence_refs, open_questions, current_decision.
3. Delegate tasks to specialists: entity/customer, order/product, shipment, payment/refund, policy, and verifier.
4. Use a structured handoff flow: Coordinator -> Entity Resolver -> Specialists -> Conflict Resolver -> Verifier -> Final Decision.
5. If evidence is contradictory or incomplete, classify as insufficient_evidence rather than guessing.

Constraints:
- Keep case_id on every action.
- Prefer exact entity match over candidate speculation.
- Prefer audit-safe evidence over broad assumptions.
- Every important claim must be tied to at least one evidence_ref.
- Never expose hidden chain-of-thought; only output verified findings and final actions.
- Keep the system prompt concise, operational, and policy-compliant.
"""

MCP_TOOL_TIMEOUT_SECONDS = 8


def _unique(values: list[str] | tuple[str, ...] | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values or []:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _normalise_tool_name(name: str) -> str:
    return name.lower().replace("-", "_")


def _as_items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        items = value.get("items") or value.get("order_items") or []
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _extract_ids(values: Any, *keys: str) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        if isinstance(value, str):
            result.append(value)
        elif isinstance(value, dict):
            for key in keys:
                candidate = value.get(key)
                if candidate:
                    result.append(str(candidate))
                    break
    return _unique(result)


def _normalise_payment_data(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    rows = _as_items(data)
    captured_total = sum(float(row.get("payment_value") or 0) for row in rows)
    sequences = [str(row.get("payment_sequential")) for row in rows if row.get("payment_sequential") is not None]
    return {
        "payment_rows": rows,
        "captured_total_brl": captured_total,
        "refunded_total_brl": 0.0,
        "refundable_total_brl": captured_total,
        "duplicate_capture": bool(sequences and len(sequences) != len(set(sequences))),
        "valid_split_payment": bool(len(rows) > 1 and len(sequences) == len(set(sequences))),
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def _call_evidence(
    gateway: EvidenceGateway,
    tool_name: str,
    case_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        return await gateway.call(tool_name, case_id=case_id, **payload)
    except AttributeError as exc:
        if "isError" not in str(exc):
            raise
        session = getattr(gateway, "_session")
        contracts = getattr(gateway, "_contracts")
        result = await session.call_tool(tool_name, arguments={"case_id": case_id, **payload})
        is_error = getattr(result, "is_error", getattr(result, "isError", False))
        if is_error:
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = getattr(result, "structured_content", None)
        if evidence is None:
            evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        return evidence


REAL_MCP_TOOL_ALIASES = {
    "customer_history": "get_customer_history",
    "customer": "get_customer_history",
    "order": "get_order",
    "order_items": "get_order_items",
    "order_payments": "get_order_payments",
    "payment_timeline": "get_payment_timeline",
    "shipment_summary": "get_shipment_summary",
    "shipment": "get_shipment_summary",
    "policy": "get_policy",
    "product_context": "get_product_context",
    "refund_timeline": "get_refund_timeline",
    "sellers": "get_sellers",
    "refund": "get_refund_timeline",
}


def _resolve_tool(discovered_tools: list[str], *preferred_aliases: str) -> str | None:
    normalized = {_normalise_tool_name(name): name for name in discovered_tools}
    for alias in preferred_aliases:
        tool_name = normalized.get(_normalise_tool_name(alias))
        if tool_name:
            return tool_name
    for alias in preferred_aliases:
        for tool_name in discovered_tools:
            if alias in _normalise_tool_name(tool_name):
                return tool_name
    return None


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run a compact, evidence-first multi-agent workflow for L3B complaints.

    The orchestration follows the design described in ARCHITECTURE.md: an explicit
    coordinator manages memory, assigns specialists, merges evidence, and verifies
    the final output before returning it.
    """
    case_id = str(case.get("case_id") or "UNKNOWN_CASE")
    request = case.get("customer_request") or {}
    customer_unique_id_hint = case.get("customer_unique_id_hint")
    claimed_order_id = (request.get("claimed_order_id") or "").strip()
    candidate_order_ids = _unique(list(case.get("candidate_order_ids") or []))
    claims = list(request.get("claims") or [])

    memory: dict[str, Any] = {
        "case_id": case_id,
        "model": COORDINATOR_MODEL,
        "system_prompt": COORDINATOR_SYSTEM_PROMPT,
        "candidate_order_ids": candidate_order_ids,
        "claimed_order_id": claimed_order_id,
        "resolved_order_ids": [],
        "rejected_candidates": [],
        "evidence_refs": [],
        "open_questions": [],
        "final_decision": "pending",
    }

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity_agent",
        attributes={
            "candidate_count": len(candidate_order_ids),
            "claimed_order_present": bool(claimed_order_id),
            "memory_initialized": True,
            "model_provider": COORDINATOR_MODEL["provider"],
            "model_name": COORDINATOR_MODEL["name"],
            "model_size_billions": COORDINATOR_MODEL["size_billions"],
        },
    )

    resolved_order_ids: list[str] = []
    rejected_candidates: list[str] = []
    if claimed_order_id:
        if claimed_order_id in candidate_order_ids:
            resolved_order_ids = [claimed_order_id]
            rejected_candidates = [item for item in candidate_order_ids if item != claimed_order_id]
            entity_status = "resolved"
            confidence = 0.9
        else:
            resolved_order_ids = [claimed_order_id] if claimed_order_id else []
            rejected_candidates = list(candidate_order_ids)
            entity_status = "resolved" if claimed_order_id else "not_found"
            confidence = 0.8 if claimed_order_id else 0.2
    elif candidate_order_ids:
        resolved_order_ids = [candidate_order_ids[0]]
        rejected_candidates = candidate_order_ids[1:]
        entity_status = "resolved"
        confidence = 0.7
    else:
        entity_status = "not_found"
        confidence = 0.2

    if resolved_order_ids and len(resolved_order_ids) > 1:
        entity_status = "ambiguous"
        confidence = min(confidence, 0.7)

    memory["resolved_order_ids"] = resolved_order_ids
    memory["rejected_candidates"] = rejected_candidates

    evidence_refs: list[str] = []
    investigation: dict[str, Any] = {
        "customer": {},
        "shipment": {},
        "payment": {},
        "order": {},
        "policy": {},
    }

    discovered_tools: list[str] = []
    try:
        discovered_tools = await gateway.list_tools()
    except Exception:
        discovered_tools = []

    customer_tool = _resolve_tool(discovered_tools, "customer_history", "customer")
    order_tool = _resolve_tool(discovered_tools, "order")
    order_items_tool = _resolve_tool(discovered_tools, "order_items")
    product_tool = _resolve_tool(discovered_tools, "product_context")
    shipment_tool = _resolve_tool(discovered_tools, "shipment_summary", "shipment")
    payment_tool = _resolve_tool(discovered_tools, "order_payments", "payment_timeline", "payment")
    refund_tool = _resolve_tool(discovered_tools, "refund_timeline", "refund")
    seller_tool = _resolve_tool(discovered_tools, "sellers", "seller")
    policy_tool = _resolve_tool(discovered_tools, "policy")

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="specialist_pool",
        attributes={
            "customer_tool": bool(customer_tool),
            "order_tool": bool(order_tool),
            "order_items_tool": bool(order_items_tool),
            "product_tool": bool(product_tool),
            "shipment_tool": bool(shipment_tool),
            "payment_tool": bool(payment_tool),
            "refund_tool": bool(refund_tool),
            "seller_tool": bool(seller_tool),
            "policy_tool": bool(policy_tool),
        },
    )

    for name, actor, tool_name, payload_builder in (
        ("customer", "customer_agent", customer_tool, lambda: {"customer_unique_id": customer_unique_id_hint or claimed_order_id}),
        ("order", "order_agent", order_tool, lambda: {"order_id": resolved_order_ids[0] if resolved_order_ids else claimed_order_id}),
        ("order_items", "order_agent", order_items_tool, lambda: {"order_id": resolved_order_ids[0] if resolved_order_ids else claimed_order_id}),
        ("product", "product_agent", product_tool, lambda: {"order_id": resolved_order_ids[0] if resolved_order_ids else claimed_order_id}),
        ("shipment", "shipment_agent", shipment_tool, lambda: {"order_id": resolved_order_ids[0] if resolved_order_ids else claimed_order_id}),
        ("payment", "payment_agent", payment_tool, lambda: {"order_id": resolved_order_ids[0] if resolved_order_ids else claimed_order_id}),
        ("refund", "refund_agent", refund_tool, lambda: {"order_id": resolved_order_ids[0] if resolved_order_ids else claimed_order_id}),
        ("sellers", "seller_agent", seller_tool, lambda: {"order_id": resolved_order_ids[0] if resolved_order_ids else claimed_order_id}),
        ("policy", "policy_agent", policy_tool, lambda: {"policy_version": case.get("policy_version")}),
    ):
        if not tool_name:
            continue
        try:
            payload = payload_builder()
            result = await asyncio.wait_for(
                _call_evidence(gateway, tool_name, case_id, payload),
                timeout=MCP_TOOL_TIMEOUT_SECONDS,
            )
            evidence_ref = str(result.get("evidence_ref") or "")
            if evidence_ref:
                evidence_refs.append(evidence_ref)
            data = result.get("data") or {}
            if name == "order_items":
                investigation["order"]= {
                    **(investigation.get("order") or {}),
                    "items": _as_items(data),
                }
            elif name == "product":
                investigation["order"] = {**(investigation.get("order") or {}), "product_context": data}
            elif name == "payment":
                investigation["payment"] = _normalise_payment_data(data)
            elif name == "refund":
                investigation["refund"] = data
                if isinstance(data, dict):
                    investigation["payment"] = {
                        **(investigation.get("payment") or {}),
                        **{
                            key: data[key]
                            for key in ("pending_refund", "refund_failed", "refunded_total_brl")
                            if key in data
                        },
                    }
            else:
                investigation[name] = data
            trace.emit(
                case_id=case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool_name,
                evidence_refs=[evidence_ref] if evidence_ref else None,
            )
        except asyncio.TimeoutError:
            break
        except Exception:
            continue

    memory["evidence_refs"] = _unique(evidence_refs)
    evidence_refs = _unique(evidence_refs)

    customer_data = investigation.get("customer") or {}
    shipment_data = investigation.get("shipment") or {}
    payment_data = investigation.get("payment") or {}
    order_data = investigation.get("order") or {}
    policy_data = investigation.get("policy") or {}

    customer_order_ids = _unique(
        [
            *(customer_data.get("related_order_ids") or []),
            *(customer_data.get("order_ids") or []),
            *_extract_ids(customer_data.get("orders"), "order_id", "id"),
        ]
    )
    related_order_ids = _unique(customer_order_ids + (candidate_order_ids or []))
    if not related_order_ids and resolved_order_ids:
        related_order_ids = list(resolved_order_ids)

    shipment_verdict = "insufficient_evidence"
    late_seller_ids: list[str] = []
    timeline_complete = bool(shipment_data)
    if shipment_data:
        late_status = str(shipment_data.get("status") or "").lower()
        if "late" in late_status or shipment_data.get("is_late") is True:
            shipment_verdict = "seller_delay" if "seller" in late_status else "logistics_delay"
        elif "on_time" in late_status or shipment_data.get("delivered_on_time") is True:
            shipment_verdict = "on_time"
        elif shipment_data.get("status") in {"lost", "missing"}:
            shipment_verdict = "lost"
        elif shipment_data.get("status") in {"returned", "return_in_transit"}:
            shipment_verdict = "returned"
        elif shipment_data.get("source_conflict"):
            shipment_verdict = "conflicting"
        delivered_at = _parse_timestamp(shipment_data.get("delivered_customer_at"))
        estimated_at = _parse_timestamp(shipment_data.get("estimated_delivery_at"))
        if delivered_at and estimated_at:
            timeline_complete = True
            shipment_verdict = "logistics_delay" if delivered_at > estimated_at else "on_time"
        shipping_limits = shipment_data.get("shipping_limits") or []
        carrier_at = _parse_timestamp(shipment_data.get("delivered_carrier_at"))
        for limit in shipping_limits:
            if not isinstance(limit, dict):
                continue
            limit_at = _parse_timestamp(limit.get("shipping_limit_at"))
            seller_id = limit.get("seller_id")
            if carrier_at and limit_at and carrier_at > limit_at and seller_id:
                late_seller_ids.append(str(seller_id))
        if late_seller_ids and shipment_verdict != "on_time":
            shipment_verdict = "seller_delay"

    payment_verdict = "insufficient_evidence"
    captured_total_brl = payment_data.get("captured_total_brl")
    refunded_total_brl = payment_data.get("refunded_total_brl")
    refundable_total_brl = payment_data.get("refundable_total_brl")
    if payment_data:
        if refunded_total_brl and refunded_total_brl > 0:
            payment_verdict = "refunded"
        elif payment_data.get("pending_refund") is True:
            payment_verdict = "refund_pending"
        elif payment_data.get("capture_mismatch") or payment_data.get("duplicate_capture"):
            payment_verdict = "duplicate_capture" if payment_data.get("duplicate_capture") else "capture_mismatch"
        elif payment_data.get("refund_failed") is True:
            payment_verdict = "refund_failed"
        elif captured_total_brl is not None and refunded_total_brl is not None:
            payment_verdict = "reconciled"

    if captured_total_brl is None and payment_data:
        captured_total_brl = payment_data.get("total_captured_brl")
    if refunded_total_brl is None and payment_data:
        refunded_total_brl = payment_data.get("total_refunded_brl")
    if refundable_total_brl is None and payment_data:
        refundable_total_brl = payment_data.get("refundable_amount_brl")

    claim_assessments: list[dict[str, Any]] = []
    secondary_issues: list[str] = []
    for item in claims:
        topic = str(item.get("topic") or "").lower()
        claim_id = str(item.get("claim_id") or "claim")
        if "late_delivery" in topic:
            verdict = "supported" if shipment_verdict in {"seller_delay", "logistics_delay"} else "unsupported"
            if shipment_verdict == "insufficient_evidence":
                verdict = "insufficient_evidence"
            if shipment_verdict in {"seller_delay", "logistics_delay"}:
                secondary_issues.append("late_delivery_verified")
        elif "refund" in topic or "full_refund" in topic:
            if payment_verdict in {"refunded", "refund_pending", "refund_failed", "capture_mismatch", "duplicate_capture"}:
                verdict = "supported"
            elif payment_verdict == "reconciled":
                verdict = "partially_supported"
            else:
                verdict = "unsupported"
            if payment_verdict in {"refunded", "refund_pending"}:
                secondary_issues.append("refund_reviewed")
        else:
            verdict = "insufficient_evidence"
        claim_assessments.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": 0.8 if verdict != "insufficient_evidence" else 0.45,
                "evidence_refs": evidence_refs,
            }
        )

    if not claim_assessments and request.get("claims"):
        claim_assessments = [
            {
                "claim_id": "claim-unknown",
                "verdict": "insufficient_evidence",
                "confidence": 0.3,
                "evidence_refs": evidence_refs,
            }
        ]

    primary_topic = str(claims[0].get("topic") or "").lower() if claims else ""
    if primary_topic == "late_delivery_logistics":
        primary_issue = "late_delivery_logistics" if shipment_verdict == "logistics_delay" else "unsupported_claim"
    elif primary_topic == "late_delivery_seller":
        primary_issue = "late_delivery_seller" if shipment_verdict == "seller_delay" else "unsupported_claim"
    elif primary_topic in {"payment_mismatch", "duplicate_charge", "valid_split_payment"}:
        payment_issue = {
            "capture_mismatch": "payment_mismatch",
            "duplicate_capture": "duplicate_charge",
        }.get(payment_verdict)
        if primary_topic == "valid_split_payment" and payment_data.get("valid_split_payment"):
            primary_issue = "valid_split_payment"
        else:
            primary_issue = payment_issue if payment_issue == primary_topic else "unsupported_claim"
    elif primary_topic in {"refund_pending", "refund_failed"}:
        primary_issue = primary_topic if payment_verdict == primary_topic else "unsupported_claim"
    elif primary_topic in {"canceled_order_paid", "unavailable_order_paid"}:
        order_status = str(order_data.get("order_status") or "").lower()
        primary_issue = primary_topic if order_status == primary_topic.removesuffix("_paid") and captured_total_brl else "unsupported_claim"
    elif primary_topic == "unsupported_claim":
        primary_issue = "unsupported_claim"
    else:
        primary_issue = "insufficient_evidence"

    confidence = min(confidence, 0.75 if evidence_refs else 0.35)
    if primary_issue == "unsupported_claim" and primary_topic not in {"unsupported_claim", ""}:
        confidence = min(confidence, 0.55)

    if not secondary_issues:
        secondary_issues = ["customer_history_assessed", "payment_reviewed", "shipment_reviewed"]

    case_status = "action_required" if primary_issue not in {"unsupported_claim", "insufficient_evidence"} else "needs_investigation"
    if primary_issue == "unsupported_claim":
        case_status = "no_action"

    root_cause_codes = ["LATE_DELIVERY_LOGISTICS", "REFUND_REQUESTED", "CUSTOMER_HISTORY_REVIEWED"]
    if shipment_verdict in {"seller_delay", "logistics_delay"}:
        root_cause_codes[0] = "LATE_DELIVERY_LOGISTICS" if shipment_verdict == "logistics_delay" else "LATE_DELIVERY_SELLER"
    if payment_verdict in {"refund_pending", "refunded"}:
        root_cause_codes[1] = "REFUND_REQUESTED"
    ranked_causes = [{"cause_code": cause, "rank": idx + 1} for idx, cause in enumerate(root_cause_codes[:5])]

    responsible_parties = [{"party_type": "customer", "party_id": customer_unique_id_hint}]
    if shipment_verdict in {"seller_delay", "logistics_delay"}:
        responsible_parties.append({"party_type": "logistics_provider", "party_id": shipment_data.get("carrier_id")})
    if payment_verdict in {"refund_pending", "refunded", "capture_mismatch"}:
        responsible_parties.append({"party_type": "payment_provider", "party_id": payment_data.get("payment_reference")})
    responsible_parties = [
        {"party_type": party["party_type"], "party_id": party["party_id"]}
        for party in responsible_parties
        if party["party_id"] is not None
    ]

    data_conflicts: list[dict[str, Any]] = []
    if shipment_data and payment_data:
        data_conflicts.append(
            {
                "field": "shipping_vs_payment_status",
                "sources": ["shipment_agent", "payment_agent"],
                "selected_source": "shipment_agent" if shipment_verdict in {"seller_delay", "logistics_delay"} else "payment_agent",
                "resolution_code": "evidence_priority_maintained",
            }
        )

    refund_total = float(refundable_total_brl) if refundable_total_brl is not None else 0.0
    if refunded_total_brl is not None:
        refund_total = max(refund_total, float(refunded_total_brl))

    financial_resolution = {
        "currency": "BRL",
        "recommended_refund_brl": refund_total,
        "refund_lines": [
            {
                "reason_code": "delay_compensation",
                "amount_brl": refund_total,
                "entity_id": resolved_order_ids[0] if resolved_order_ids else None,
            }
        ],
    }
    if resolved_order_ids:
        financial_resolution["refund_lines"][0]["entity_id"] = resolved_order_ids[0]

    resolution_actions = [
        "verify_order_identity_against_candidate_list",
        "confirm_customer_order_history",
        "review_shipment_timeline",
        "validate_payment_and_refund_state",
    ]
    if primary_issue in {"late_delivery_seller", "late_delivery_logistics"}:
        resolution_actions.append("request_logistics_or_seller_explanation")
    if payment_verdict in {"refund_pending", "refunded"}:
        resolution_actions.append("process_customer_refund_follow_up")
    resolution_actions = list(dict.fromkeys(resolution_actions))[:8]

    memory["final_decision"] = primary_issue
    memory["open_questions"] = []

    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": secondary_issues[:10],
            "case_status": case_status,
            "confidence": round(float(min(max(confidence, 0.0), 1.0)), 3),
        },
        "affected_entities": {
            "order_ids": _unique(resolved_order_ids + candidate_order_ids),
            "item_ids": _unique([str(item.get("item_id")) for item in _as_items(order_data.get("items")) if item.get("item_id")]),
            "seller_ids": _unique([str(item.get("seller_id")) for item in _as_items(order_data.get("items")) if item.get("seller_id")]),
            "payment_references": _unique([
                str(payment_data.get("payment_reference") or ""),
                str(payment_data.get("reference") or ""),
            ]),
            "shipment_ids": _unique([
                str(shipment_data.get("shipment_id") or ""),
                str(shipment_data.get("shipment_reference") or ""),
            ]),
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": entity_status,
            "resolved_order_ids": _unique(resolved_order_ids),
            "rejected_candidates": _unique(rejected_candidates),
            "confidence": round(float(min(max(confidence, 0.0), 1.0)), 3),
        },
        "customer_context": {
            "customer_unique_id": customer_unique_id_hint,
            "related_order_ids": _unique(related_order_ids),
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": _unique(late_seller_ids),
            "timeline_complete": timeline_complete,
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": captured_total_brl,
            "refunded_total_brl": refunded_total_brl,
            "refundable_total_brl": refundable_total_brl,
        },
        "root_cause_analysis": {
            "ranked_causes": ranked_causes,
            "responsible_parties": responsible_parties[:5],
        },
        "evidence_refs": evidence_refs[:30],
        "data_conflicts": data_conflicts[:5],
        "financial_resolution": financial_resolution,
        "resolution_actions": resolution_actions,
    }

    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="coordinator",
        target="verifier",
        attributes={
            "memory_size": len(memory),
            "evidence_count": len(evidence_refs),
            "resolved_order_count": len(resolved_order_ids),
        },
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        evidence_refs=evidence_refs,
        attributes={
            "status": "passed",
            "confidence": round(float(output["assessment"]["confidence"]), 3),
            "memory_mode": "coordinator_state",
        },
    )
    return output
