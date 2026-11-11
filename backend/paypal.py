from __future__ import annotations

import math
import os
from typing import Any

import requests


def safe_patient_pay_amount(value: Any) -> float:
    """Return a finite, non-negative synthetic amount rounded to cents, else 0.0."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(amount) or amount < 0:
        return 0.0
    return round(amount, 2)


DEFAULT_REDIRECT_URL = "http://localhost:8501"


class PayPalSandboxClient:
    """PayPal Orders v2 REST API client for Sandbox environment only."""

    BASE_URL = "https://api-m.sandbox.paypal.com"
    TIMEOUT = 8

    def __init__(self) -> None:
        self.client_id = os.getenv("PAYPAL_CLIENT_ID", "").strip()
        self.client_secret = os.getenv("PAYPAL_CLIENT_SECRET", "").strip()
        self.mode = os.getenv("PAYPAL_MODE", "sandbox").strip().lower()
        self.access_token: str | None = None
        self.return_url = self._redirect_url("PAYPAL_RETURN_URL")
        self.cancel_url = self._redirect_url("PAYPAL_CANCEL_URL")

    @staticmethod
    def _redirect_url(env_name: str) -> str:
        """Read a buyer redirect URL from the environment; only http(s) values are accepted."""
        value = os.getenv(env_name, "").strip()
        if value.lower().startswith(("http://", "https://")):
            return value
        return DEFAULT_REDIRECT_URL

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.mode == "sandbox")

    def _get_access_token(self) -> str:
        """Obtain OAuth access token. Raises if credentials missing or request fails."""
        if not self.client_id or not self.client_secret:
            raise RuntimeError("PayPal credentials not configured (PAYPAL_CLIENT_ID or PAYPAL_CLIENT_SECRET missing)")

        url = f"{self.BASE_URL}/v1/oauth2/token"
        try:
            resp = requests.post(
                url,
                auth=(self.client_id, self.client_secret),
                data={"grant_type": "client_credentials"},
                timeout=self.TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("access_token", "")
        except requests.RequestException:
            raise RuntimeError("PayPal OAuth token request failed")

    def create_order(self, amount_usd: float, case_id: str) -> dict[str, Any]:
        """
        Create a PayPal Sandbox order.

        Returns: {
            "order_id": str,
            "status": str ("CREATED"),
            "amount": float,
            "currency": "USD",
            "case_id": str,
            "approval_url": str (buyer approval link from PayPal, or empty string if not available),
        }
        """
        if not self.is_configured():
            raise RuntimeError("PayPal Sandbox not configured")

        if amount_usd <= 0:
            raise ValueError(f"Amount must be positive (got {amount_usd})")

        token = self._get_access_token()
        url = f"{self.BASE_URL}/v2/checkout/orders"

        payload = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": case_id,
                    "amount": {
                        "currency_code": "USD",
                        "value": f"{round(amount_usd, 2):.2f}",
                    },
                }
            ],
            "application_context": {
                "brand_name": "AuthGuard AI PayPal",
                "landing_page": "LOGIN",
                "shipping_preference": "NO_SHIPPING",
                "user_action": "PAY_NOW",
                "return_url": self.return_url,
                "cancel_url": self.cancel_url,
            },
        }

        try:
            resp = requests.post(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=self.TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            order_id = data.get("id", "")
            status = data.get("status", "")

            approval_url = ""
            links = data.get("links", [])
            for link in links:
                if link.get("rel") == "approve":
                    approval_url = link.get("href", "")
                    break

            return {
                "order_id": order_id,
                "status": status,
                "amount": amount_usd,
                "currency": "USD",
                "case_id": case_id,
                "approval_url": approval_url,
            }
        except requests.RequestException:
            raise RuntimeError("PayPal create order failed")

    def capture_order(self, order_id: str) -> dict[str, Any]:
        """
        Capture a PayPal Sandbox order.

        Returns: {
            "order_id": str,
            "status": str ("COMPLETED"),
            "capture_id": str,
            "capture_status": str ("COMPLETED"),
        }
        """
        if not self.is_configured():
            raise RuntimeError("PayPal Sandbox not configured")

        if not order_id:
            raise ValueError("order_id is required")

        token = self._get_access_token()
        url = f"{self.BASE_URL}/v2/checkout/orders/{order_id}/capture"

        try:
            resp = requests.post(
                url,
                json={},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=self.TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            order_id_resp = data.get("id", "")
            status = data.get("status", "")

            capture_id = ""
            capture_status = ""
            if data.get("purchase_units"):
                captures = data["purchase_units"][0].get("payments", {}).get("captures", [])
                if captures:
                    capture_id = captures[0].get("id", "")
                    capture_status = captures[0].get("status", "")

            return {
                "order_id": order_id_resp,
                "status": status,
                "capture_id": capture_id,
                "capture_status": capture_status,
            }
        except requests.RequestException:
            raise RuntimeError("PayPal capture order failed")
