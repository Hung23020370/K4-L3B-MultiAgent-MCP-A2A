# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ input/candidate resolution đến MCP investigation, specialist agents, conflict resolver, verifier, output và trace.

```text
Input → Entity Resolver → Coordinator → Specialists → Conflict Resolver → Verifier → Output
            │                              │                  │             │
            └──────────────────────────── MCP ────────────────┴──────────── Trace
```

## 2. Agent ownership

### Model and coordinator constraint

Mặc dù repo starter kit không buộc runtime model nào, nhưng theo yêu cầu lab/thiết kế hợp lệ, hệ thống nên dùng một coordinator model kích thước tối ưu, ưu tiên dưới 10 tỷ tham số, để giữ latency thấp và dễ kiểm soát tài nguyên. Vai trò coordinator là quyết định đường đi xử lý, tổng hợp memory, phân công nhiệm vụ và bắt buộc phải kiểm tra evidence trước khi đưa ra quyết định final.

- Reference model for the lab requirement: DeepSeek-7B
- Coordinator: model nhỏ, nhiệm vụ chính là lên kế hoạch, quyết định handoff, tổng hợp memory, kiểm tra conflict, gọi verifier trước khi finalize.
- Specialist agents: chỉ xử lý ngữ cảnh chuyên biệt như customer/order/shipment/payment/policy, không tự quyết định cuối cùng.
- Memory: lưu case_id, candidate list, resolved order, evidence_refs, open questions, prior decisions, và các conflict đã được xử lý.
- Thinking policy: chỉ suy luận trên evidence, không suy đoán missing fact, luôn xác định mức độ không chắc chắn và báo ít nhất một evidence_ref cho mọi claim quan trọng.
- Handoff pattern: Coordinator -> Entity resolver -> Specialists -> Conflict review -> Verifier -> Coordinator final.

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | case + customer_unique_id_hint + candidate ids | resolve đúng customer/order identity, reject mismatches | read-only customer/order tools | resolved_order_ids + rejected_candidates + confidence |
| Coordinator | task state + memory + open questions | triage, assign tasks, merge partial findings, verify finality | discovery + orchestration metadata only | task plan + handoff + final decision |
| Order/product | order candidate set | xác minh item/seller context và scope | order/item/product evidence | concrete order facts + gaps |
| Shipment | order + delivery timeline | xác định on_time/seller_delay/logistics_delay/lost/returned | shipment tools only | shipment verdict + evidence |
| Payment/refund | order + payment refs | reconcile capture/refund/refundable totals | payment/refund tools only | payment verdict + financial resolution |
| Policy | policy version + claim type | xác định điều kiện hoàn tiền và policy alignment | policy tools only | policy-grounded recommendation |
| Conflict resolver | multiple sources / conflicting claims | chọn source ưu tiên, ghi conflict và resolution code | read-only evidence comparison | data_conflicts |
| Verifier | draft output + evidence refs + trace | validate schema, consistency, evidence ownership, confidence bounds | no external tool required | pass/fail before finalize |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

### Clean system prompt example

```text
You are the coordinator for a compact multi-agent ecommerce complaint investigation system.

Mission:
- Resolve the correct order using candidate IDs and evidence.
- Investigate customer history, shipment, payment, and policy only for the current case.
- Use a small, efficient reasoning model with explicit memory and structured handoff.
- Never invent missing facts; if evidence is weak, return insufficient_evidence.
- Produce a final decision grounded in evidence references and verification.

Constraints:
1. Keep case_id on every action.
2. Use memory to track resolved_order_ids, rejected_candidates, evidence_refs, and open questions.
3. Prefer exact evidence over speculation.
4. After specialists finish, verify schema consistency and confidence before finalization.
5. Do not expose hidden chain-of-thought or internal reasoning.
6. Keep system prompt concise, operational, and policy-compliant.

Workflow:
Coordinator -> Entity Resolver -> Specialists -> Conflict Resolver -> Verifier -> Final output
```

This prompt is intentionally compact and avoids hidden reasoning traces; it describes the operational contract rather than private chain-of-thought.

## 3. Entity resolution và A2A protocol

Mô tả cách xếp hạng/reject candidate, confidence threshold, message envelope, correlation theo `case_id`, điều kiện handoff, timeout và cách tránh vòng lặp. Không trace nội dung suy luận riêng.

## 4. Evidence và conflict lifecycle

Mô tả cách validate MCP response, lưu `evidence_ref`, chọn source theo policy, biểu diễn unresolved conflict, map evidence vào claim/output và emit `tool_result_consumed`. Evidence không được tái sử dụng giữa các case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | TODO | TODO | TODO |
| Entity not found/ambiguous | TODO | TODO | TODO |
| Source conflict | TODO | TODO | TODO |
| Invalid specialist result | TODO | TODO | TODO |

Nêu query budget/cache strategy để tránh gọi lặp và quét rộng. Retry phải có giới hạn, idempotent và không biến missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Liệt kê kiểm tra trước finalize: schema, entity scope, rejected candidates, evidence ownership, claim linkage, timeline, payment/refund totals, source precedence, responsibility/action consistency và confidence bounds.

## 7. Reproducibility

Ghi model/config, dependency pinning, concurrency limit, random seed (nếu có), lệnh chạy và giới hạn tài nguyên. Không ghi API key.
