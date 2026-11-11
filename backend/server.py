from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, request

from backend.agents.committee import PIPELINE_STAGES, run_pipeline
from backend.llm_clients import default_provider
from backend.memory import JSONMemoryStore
from backend.model import DenialRiskModel
from backend.paypal import PayPalSandboxClient
from backend.rag import PolicyRAG


def create_app() -> Flask:
    app = Flask(__name__)
    store = JSONMemoryStore()

    @app.after_request
    def add_cors_headers(response: Any) -> Any:
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return response

    @app.get("/health")
    def health() -> Any:
        return jsonify({
            "status": "ok",
            "service": "AuthGuard AI API",
            "pipeline_stages": len(PIPELINE_STAGES),
            "time": datetime.now(timezone.utc).isoformat(),
        })

    @app.post("/api/process")
    def process_case() -> Any:
        payload = request.get_json(silent=True) or {}
        case = payload.get("case") or payload
        provider = payload.get("provider") or default_provider()
        provider_model = payload.get("provider_model") or payload.get("model")
        try:
            return jsonify(
                run_pipeline(
                    case,
                    provider=provider,
                    provider_model=provider_model,
                    memory_store=store,
                )
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": f"Pipeline failed: {exc}"}), 500

    @app.post("/api/review")
    def save_review() -> Any:
        payload = request.get_json(silent=True) or {}
        required = ["run_id", "case_id", "reviewer", "action"]
        missing = [field for field in required if not str(payload.get(field, "")).strip()]
        if missing:
            return jsonify({"error": "Missing fields: " + ", ".join(missing)}), 400
        record = {
            **payload,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
        store.append_audit(record)
        return jsonify({"status": "saved", "record": record}), 201

    @app.get("/api/cases")
    def list_cases() -> Any:
        return jsonify(store.list_cases(limit=int(request.args.get("limit", 50))))

    @app.get("/api/memory")
    def list_memory() -> Any:
        return jsonify(store.list_memory(limit=int(request.args.get("limit", 50))))

    @app.get("/api/audit")
    def list_audit() -> Any:
        return jsonify(store.list_audit(limit=int(request.args.get("limit", 50))))

    @app.get("/api/model/metrics")
    def model_metrics() -> Any:
        return jsonify(DenialRiskModel().metrics)

    @app.get("/api/knowledge")
    def knowledge() -> Any:
        rag = PolicyRAG()
        return jsonify(rag.documents)

    @app.post("/api/paypal/create-order")
    def create_paypal_order() -> Any:
        client = PayPalSandboxClient()
        if not client.is_configured():
            return jsonify({"error": "PayPal Sandbox configuration missing"}), 503

        payload = request.get_json(silent=True) or {}
        required = ["run_id", "case_id", "reviewer", "amount"]
        missing = [field for field in required if not str(payload.get(field, "")).strip()]
        if missing:
            return jsonify({"error": "Missing fields: " + ", ".join(missing)}), 400

        run_id = str(payload["run_id"]).strip()
        case_id = str(payload["case_id"]).strip()
        reviewer = str(payload["reviewer"]).strip()
        try:
            amount = float(payload["amount"])
            if amount <= 0:
                return jsonify({"error": "amount must be positive"}), 400
        except (ValueError, TypeError):
            return jsonify({"error": "amount must be numeric"}), 400

        audit_records = store.list_audit(limit=500)
        approval_found = False
        amount_cents = round(float(amount) * 100)
        for record in audit_records:
            if (
                record.get("run_id") == run_id
                and record.get("case_id") == case_id
                and record.get("action") == "PAYMENT_APPROVED"
            ):
                approved_cents = round(float(record.get("approved_amount", 0)) * 100)
                if approved_cents == amount_cents:
                    approval_found = True
                    break

        if not approval_found:
            return jsonify({"error": "No matching payment approval found for this amount"}), 400

        try:
            order_result = client.create_order(amount, case_id)
            order_id = order_result.get("order_id", "")

            store.append_audit({
                "run_id": run_id,
                "case_id": case_id,
                "reviewer": reviewer,
                "action": "PAYPAL_ORDER_CREATED",
                "order_id": order_id,
                "amount": amount,
                "currency": "USD",
                "order_status": order_result.get("status", ""),
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

            return jsonify({
                "status": "created",
                "order_id": order_id,
                "amount": amount,
                "currency": "USD",
                "paypal_status": order_result.get("status", ""),
                "approval_url": order_result.get("approval_url", ""),
            }), 201
        except RuntimeError as exc:
            store.append_audit({
                "run_id": run_id,
                "case_id": case_id,
                "reviewer": reviewer,
                "action": "PAYPAL_FAILED",
                "error": str(exc),
                "amount": amount,
                "failed_at": datetime.now(timezone.utc).isoformat(),
            })
            return jsonify({"error": f"PayPal order creation failed: {exc}"}), 500

    @app.post("/api/paypal/capture-order")
    def capture_paypal_order() -> Any:
        client = PayPalSandboxClient()
        if not client.is_configured():
            return jsonify({"error": "PayPal Sandbox configuration missing"}), 503

        payload = request.get_json(silent=True) or {}
        required = ["order_id", "run_id", "case_id", "reviewer"]
        missing = [field for field in required if not str(payload.get(field, "")).strip()]
        if missing:
            return jsonify({"error": "Missing fields: " + ", ".join(missing)}), 400

        order_id = str(payload["order_id"]).strip()
        run_id = str(payload["run_id"]).strip()
        case_id = str(payload["case_id"]).strip()
        reviewer = str(payload["reviewer"]).strip()

        audit_records = store.list_audit(limit=500)
        order_record = None
        for record in audit_records:
            if (
                record.get("order_id") == order_id
                and record.get("action") == "PAYPAL_ORDER_CREATED"
            ):
                order_record = record
                break

        if not order_record:
            return jsonify({"error": "Order not found in audit log"}), 400

        if order_record.get("run_id") != run_id:
            return jsonify({"error": "Order belongs to a different authorization run"}), 400

        if order_record.get("case_id") != case_id:
            return jsonify({"error": "Order belongs to a different case"}), 400

        for record in audit_records:
            if (
                record.get("order_id") == order_id
                and record.get("action") == "PAYPAL_CAPTURED"
            ):
                return jsonify({"error": "Order has already been captured"}), 400

        try:
            capture_result = client.capture_order(order_id)
            capture_id = capture_result.get("capture_id", "")
            capture_status = capture_result.get("capture_status", "")

            store.append_audit({
                "run_id": run_id,
                "case_id": case_id,
                "reviewer": reviewer,
                "action": "PAYPAL_CAPTURED",
                "order_id": order_id,
                "capture_id": capture_id,
                "capture_status": capture_status,
                "captured_at": datetime.now(timezone.utc).isoformat(),
            })

            return jsonify({
                "status": "captured",
                "order_id": order_id,
                "capture_id": capture_id,
                "capture_status": capture_status,
            }), 200
        except RuntimeError as exc:
            store.append_audit({
                "run_id": run_id,
                "case_id": case_id,
                "reviewer": reviewer,
                "action": "PAYPAL_FAILED",
                "order_id": order_id,
                "error": str(exc),
                "failed_at": datetime.now(timezone.utc).isoformat(),
            })
            return jsonify({"error": f"PayPal capture failed: {exc}"}), 500

    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.getenv("AUTHGUARD_API_PORT", "5008"))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
