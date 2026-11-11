import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.memory import JSONMemoryStore
from backend.paypal import PayPalSandboxClient
from backend.server import create_app


@pytest.fixture
def app(tmp_path: Path):
    """Flask app with temp memory store."""
    from backend.server import create_app

    app = create_app()
    app.config["TESTING"] = True
    with app.app_context():
        app.memory_store = JSONMemoryStore(tmp_path)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


class TestPayPalSandboxClient:
    def test_not_configured_without_credentials(self):
        with patch.dict("os.environ", clear=True):
            paypal = PayPalSandboxClient()
            assert not paypal.is_configured()

    def test_configured_with_valid_env(self):
        with patch.dict("os.environ", {
            "PAYPAL_CLIENT_ID": "test_id",
            "PAYPAL_CLIENT_SECRET": "test_secret",
            "PAYPAL_MODE": "sandbox",
        }):
            paypal = PayPalSandboxClient()
            assert paypal.is_configured()

    def test_not_configured_wrong_mode(self):
        with patch.dict("os.environ", {
            "PAYPAL_CLIENT_ID": "test_id",
            "PAYPAL_CLIENT_SECRET": "test_secret",
            "PAYPAL_MODE": "live",
        }):
            paypal = PayPalSandboxClient()
            assert not paypal.is_configured()

    def test_create_order_requires_positive_amount(self):
        with patch.dict("os.environ", {
            "PAYPAL_CLIENT_ID": "test_id",
            "PAYPAL_CLIENT_SECRET": "test_secret",
            "PAYPAL_MODE": "sandbox",
        }):
            paypal = PayPalSandboxClient()
            with pytest.raises(ValueError, match="Amount must be positive"):
                paypal.create_order(0, "case-123")
            with pytest.raises(ValueError, match="Amount must be positive"):
                paypal.create_order(-10.00, "case-123")

    @patch("backend.paypal.requests.post")
    def test_create_order_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "7C3325AD-B0C0-4D40-995D-3B4E91738EFF",
            "status": "CREATED",
        }
        mock_post.return_value = mock_response

        with patch.dict("os.environ", {
            "PAYPAL_CLIENT_ID": "test_id",
            "PAYPAL_CLIENT_SECRET": "test_secret",
            "PAYPAL_MODE": "sandbox",
        }):
            paypal = PayPalSandboxClient()
            result = paypal.create_order(100.00, "case-123")

            assert result["order_id"] == "7C3325AD-B0C0-4D40-995D-3B4E91738EFF"
            assert result["status"] == "CREATED"
            assert result["amount"] == 100.00
            assert result["currency"] == "USD"

    def _order_payload(self, env: dict, amount: float = 640.00) -> tuple[dict, dict]:
        """Create an order against a mocked PayPal and return (json payload, request kwargs)."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "ORDER-TEST",
            "status": "CREATED",
            "access_token": "test_token_value",
        }
        base_env = {
            "PAYPAL_CLIENT_ID": "test_id",
            "PAYPAL_CLIENT_SECRET": "test_secret",
            "PAYPAL_MODE": "sandbox",
        }
        with patch("backend.paypal.requests.post", return_value=mock_response) as mock_post:
            with patch.dict("os.environ", {**base_env, **env}, clear=True):
                PayPalSandboxClient().create_order(amount, "case-123")
        order_calls = [c for c in mock_post.call_args_list if c.args[0].endswith("/v2/checkout/orders")]
        assert len(order_calls) == 1
        return order_calls[0].kwargs["json"], order_calls[0].kwargs

    def test_create_order_payload_has_approval_context(self):
        payload, _ = self._order_payload({
            "PAYPAL_RETURN_URL": "http://localhost:8501/return",
            "PAYPAL_CANCEL_URL": "http://localhost:8501/cancel",
        })
        context = payload["application_context"]
        assert context["return_url"] == "http://localhost:8501/return"
        assert context["cancel_url"] == "http://localhost:8501/cancel"
        assert context["user_action"] == "PAY_NOW"
        assert context["shipping_preference"] == "NO_SHIPPING"
        assert context["landing_page"] == "LOGIN"
        assert context["brand_name"] == "AuthGuard AI PayPal"

    def test_create_order_redirect_urls_default_to_local_dashboard(self):
        payload, _ = self._order_payload({})
        context = payload["application_context"]
        assert context["return_url"] == "http://localhost:8501"
        assert context["cancel_url"] == "http://localhost:8501"

    def test_create_order_ignores_non_http_redirect_urls(self):
        payload, _ = self._order_payload({
            "PAYPAL_RETURN_URL": "javascript:alert(1)",
            "PAYPAL_CANCEL_URL": "  ",
        })
        context = payload["application_context"]
        assert context["return_url"] == "http://localhost:8501"
        assert context["cancel_url"] == "http://localhost:8501"

    def test_create_order_sends_amount_with_two_decimals(self):
        payload, _ = self._order_payload({}, amount=640)
        assert payload["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "640.00"}
        payload, _ = self._order_payload({}, amount=5200.5)
        assert payload["purchase_units"][0]["amount"]["value"] == "5200.50"

    def test_create_order_payload_never_contains_credentials(self):
        payload, kwargs = self._order_payload({})
        body = json.dumps(payload)
        for secret in ("test_id", "test_secret", "test_token_value"):
            assert secret not in body
        assert kwargs["headers"]["Authorization"] == "Bearer test_token_value"

    @patch("backend.paypal.requests.post")
    def test_capture_order_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "7C3325AD-B0C0-4D40-995D-3B4E91738EFF",
            "status": "COMPLETED",
            "purchase_units": [
                {
                    "payments": {
                        "captures": [
                            {
                                "id": "3C679E50-ABC4-477B-84C6-D0F13A76A381",
                                "status": "COMPLETED",
                            }
                        ]
                    }
                }
            ],
        }
        mock_post.return_value = mock_response

        with patch.dict("os.environ", {
            "PAYPAL_CLIENT_ID": "test_id",
            "PAYPAL_CLIENT_SECRET": "test_secret",
            "PAYPAL_MODE": "sandbox",
        }):
            paypal = PayPalSandboxClient()
            result = paypal.capture_order("7C3325AD-B0C0-4D40-995D-3B4E91738EFF")

            assert result["order_id"] == "7C3325AD-B0C0-4D40-995D-3B4E91738EFF"
            assert result["status"] == "COMPLETED"
            assert result["capture_id"] == "3C679E50-ABC4-477B-84C6-D0F13A76A381"
            assert result["capture_status"] == "COMPLETED"


class TestPayPalFlaskEndpoints:
    def test_create_order_returns_503_without_config(self, client):
        with patch.dict("os.environ", clear=True):
            resp = client.post(
                "/api/paypal/create-order",
                json={
                    "run_id": "run-123",
                    "case_id": "case-123",
                    "reviewer": "test_reviewer",
                    "amount": 100.00,
                },
            )
            assert resp.status_code == 503
            assert "configuration missing" in resp.json.get("error", "").lower()

    def test_create_order_rejects_missing_fields(self, client):
        with patch.dict("os.environ", {
            "PAYPAL_CLIENT_ID": "test_id",
            "PAYPAL_CLIENT_SECRET": "test_secret",
            "PAYPAL_MODE": "sandbox",
        }):
            resp = client.post(
                "/api/paypal/create-order",
                json={"run_id": "run-123", "case_id": "case-123"},
            )
            assert resp.status_code == 400
            assert "Missing fields" in resp.json.get("error", "")

    def test_create_order_rejects_zero_amount(self, client):
        with patch.dict("os.environ", {
            "PAYPAL_CLIENT_ID": "test_id",
            "PAYPAL_CLIENT_SECRET": "test_secret",
            "PAYPAL_MODE": "sandbox",
        }):
            resp = client.post(
                "/api/paypal/create-order",
                json={
                    "run_id": "run-123",
                    "case_id": "case-123",
                    "reviewer": "test_reviewer",
                    "amount": 0,
                },
            )
            assert resp.status_code == 400
            assert "positive" in resp.json.get("error", "").lower()

    def test_create_order_requires_prior_approval(self, client, tmp_path: Path):
        with patch.dict("os.environ", {
            "PAYPAL_CLIENT_ID": "test_id",
            "PAYPAL_CLIENT_SECRET": "test_secret",
            "PAYPAL_MODE": "sandbox",
        }):
            store = JSONMemoryStore(tmp_path)
            with patch("backend.server.JSONMemoryStore", return_value=store):
                resp = client.post(
                    "/api/paypal/create-order",
                    json={
                        "run_id": "run-123",
                        "case_id": "case-123",
                        "reviewer": "test_reviewer",
                        "amount": 100.00,
                    },
                )
                assert resp.status_code == 400
                assert "approval" in resp.json.get("error", "").lower()

    @patch("backend.paypal.requests.post")
    def test_create_order_flows_through_mocked_paypal(self, mock_post):
        """Test that a proper request reaches the mocked PayPal API."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "ORDER-123",
            "status": "CREATED",
        }
        mock_post.return_value = mock_response

        with patch.dict("os.environ", {
            "PAYPAL_CLIENT_ID": "test_id",
            "PAYPAL_CLIENT_SECRET": "test_secret",
            "PAYPAL_MODE": "sandbox",
        }):
            paypal = PayPalSandboxClient()
            result = paypal.create_order(100.00, "case-123")
            assert result["order_id"] == "ORDER-123"
            assert result["status"] == "CREATED"
            assert result["amount"] == 100.00

    def test_capture_order_returns_503_without_config(self, client):
        with patch.dict("os.environ", clear=True):
            resp = client.post(
                "/api/paypal/capture-order",
                json={
                    "order_id": "ORDER-123",
                    "run_id": "run-123",
                    "case_id": "case-123",
                    "reviewer": "test_reviewer",
                },
            )
            assert resp.status_code == 503

    def test_capture_order_rejects_missing_fields(self, client):
        with patch.dict("os.environ", {
            "PAYPAL_CLIENT_ID": "test_id",
            "PAYPAL_CLIENT_SECRET": "test_secret",
            "PAYPAL_MODE": "sandbox",
        }):
            resp = client.post(
                "/api/paypal/capture-order",
                json={"order_id": "ORDER-123", "run_id": "run-123"},
            )
            assert resp.status_code == 400
            assert "Missing fields" in resp.json.get("error", "")

    def test_capture_order_requires_prior_order_creation(self, client):
        """Capture requires a matching PAYPAL_ORDER_CREATED audit record."""
        with patch.dict("os.environ", {
            "PAYPAL_CLIENT_ID": "test_id",
            "PAYPAL_CLIENT_SECRET": "test_secret",
            "PAYPAL_MODE": "sandbox",
        }):
            resp = client.post(
                "/api/paypal/capture-order",
                json={
                    "order_id": "UNKNOWN-ORDER",
                    "run_id": "run-123",
                    "case_id": "case-123",
                    "reviewer": "test_reviewer",
                },
            )
            assert resp.status_code == 400
            assert "not found" in resp.json.get("error", "").lower()

    def test_audit_records_payment_events(self, tmp_path: Path):
        store = JSONMemoryStore(tmp_path)
        store.append_audit({
            "run_id": "run-123",
            "case_id": "case-123",
            "action": "PAYMENT_APPROVED",
            "approved_amount": 100.00,
            "reviewer": "test_reviewer",
        })
        store.append_audit({
            "run_id": "run-123",
            "case_id": "case-123",
            "action": "PAYPAL_ORDER_CREATED",
            "order_id": "ORDER-123",
            "amount": 100.00,
            "currency": "USD",
            "order_status": "CREATED",
        })

        audit = store.list_audit(limit=10)
        assert len(audit) >= 2
        assert any(r.get("action") == "PAYMENT_APPROVED" for r in audit)
        assert any(r.get("action") == "PAYPAL_ORDER_CREATED" for r in audit)

    @patch("backend.paypal.requests.post")
    def test_create_order_returns_approval_url(self, mock_post):
        """create-order response includes safe approval_url from PayPal."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "ORDER-789",
            "status": "CREATED",
            "links": [
                {"rel": "self", "href": "https://api.sandbox.paypal.com/v2/checkout/orders/ORDER-789"},
                {"rel": "approve", "href": "https://sandbox.paypal.com/checkoutnow?token=EC-abc123xyz"},
            ],
        }
        mock_post.return_value = mock_response

        with patch.dict("os.environ", {
            "PAYPAL_CLIENT_ID": "test_id",
            "PAYPAL_CLIENT_SECRET": "test_secret",
            "PAYPAL_MODE": "sandbox",
        }):
            paypal = PayPalSandboxClient()
            result = paypal.create_order(100.00, "case-123")
            assert result["approval_url"] == "https://sandbox.paypal.com/checkoutnow?token=EC-abc123xyz"
            assert "client_id" not in result.get("approval_url", "")
            assert "secret" not in result.get("approval_url", "").lower()

    @patch("backend.paypal.requests.post")
    def test_create_order_handles_missing_approval_link(self, mock_post):
        """create-order returns empty approval_url if PayPal response lacks approve link."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "ORDER-999",
            "status": "CREATED",
            "links": [
                {"rel": "self", "href": "https://api.sandbox.paypal.com/v2/checkout/orders/ORDER-999"},
            ],
        }
        mock_post.return_value = mock_response

        with patch.dict("os.environ", {
            "PAYPAL_CLIENT_ID": "test_id",
            "PAYPAL_CLIENT_SECRET": "test_secret",
            "PAYPAL_MODE": "sandbox",
        }):
            paypal = PayPalSandboxClient()
            result = paypal.create_order(100.00, "case-123")
            assert result["approval_url"] == ""
            assert "order_id" in result
