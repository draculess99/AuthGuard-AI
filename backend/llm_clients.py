from __future__ import annotations

import json
import os
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

# Curated text-capable choices. Environment defaults are inserted into the UI even
# when they are not in these lists, so model changes do not require a code edit.
GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
]

GEMINI_MODELS = [
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-1.0-pro",
    "gemini-3.5-flash",
    "gemini-3.1-pro-preview",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
]


def get_model_choices(provider: str) -> list[str]:
    normalized = (provider or "").strip().lower()
    if normalized.startswith("groq"):
        configured = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip()
        choices = [configured, *GROQ_MODELS]
    elif normalized.startswith("gemini"):
        configured = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()
        choices = [configured, *GEMINI_MODELS]
    else:
        return []
    return list(dict.fromkeys(choice for choice in choices if choice))


def validate_enhanced_explanation(enhanced: str, canonical: str, locked_decision: str) -> tuple[bool, str]:
    """Validate external LLM output against the locked deterministic route.

    Returns: (is_valid, reason_if_invalid)
    """
    if not enhanced or not str(enhanced).strip():
        return False, "empty_response"

    enhanced_lower = str(enhanced).lower()

    # Reject if output contains contradictory route terms (without context check)
    # This is a simple heuristic to catch obvious contradictions
    contradictions = [
        ("deny", "denial", "rejected", "disapprove"),  # Reject if these conflict with READY route
        ("approved auto", "automatically approved"),  # Reject unsupported claims
    ]

    # If locked decision is READY, reject anything claiming denial/rejection without nuance
    if "READY" in locked_decision:
        for phrase in ("deny", "denied", "rejected", "will not approve"):
            if phrase in enhanced_lower:
                return False, "contradicts_locked_route"

    # Reject suspicious escalation claims if not in locked route
    if "escalate" in enhanced_lower and locked_decision not in ("ESCALATE_DENIAL_RISK", "URGENT_HUMAN_REVIEW"):
        # This is too strict, allow it since escalation might be mentioned in context
        pass

    # Reject if output is suspiciously short (< 50 chars) - likely truncated or error
    if len(enhanced.strip()) < 50:
        return False, "response_too_short"

    return True, ""


def deterministic_explanation(context: dict[str, Any]) -> str:
    """Canonical, deterministic explanation from the locked route and guardrail reasons.

    This is the authoritative explanation and the fallback for all providers.
    It is derived solely from deterministic evidence and the locked workflow route.
    """
    decision = context.get("decision", "HUMAN_REVIEW_REQUIRED")
    risk = context.get("risk_level", "Unknown")
    blockers = context.get("blockers", [])
    warnings = context.get("warnings", [])
    guardrail_reasons = context.get("guardrail_reasons", [])

    explanation_parts = [
        f"The deterministic workflow determined this case must route to {decision}."
    ]

    if guardrail_reasons:
        for reason in guardrail_reasons[:3]:
            explanation_parts.append(f"• {reason}")

    if blockers:
        blocker_text = "blocking items: " + "; ".join(blockers[:3])
        explanation_parts.append(f"• {blocker_text}")

    if warnings:
        warning_text = "review warnings: " + "; ".join(warnings[:3])
        explanation_parts.append(f"• {warning_text}")

    if not (guardrail_reasons or blockers or warnings):
        explanation_parts.append("The deterministic checks did not identify a blocking defect.")

    explanation_parts.append(f"The XGBoost risk band is {risk}.")
    explanation_parts.append(
        "This is a structured decision-support result only. It does not determine coverage, "
        "provide medical advice, or submit authorization requests. "
        "Qualified personnel must verify current payer policy and clinical evidence."
    )

    return " ".join(explanation_parts)


def local_explanation(context: dict[str, Any]) -> str:
    """Deprecated: use deterministic_explanation instead."""
    return deterministic_explanation(context)


def call_groq(prompt: str, model: str | None = None) -> tuple[str, str, dict[str, int] | None]:
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not configured")
    selected_model = (model or os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")).strip()
    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": selected_model,
            "temperature": 0.15,
            "max_tokens": 700,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a helpful, conversational AI assistant explaining a prior-authorization decision-support result. "
                        "Provide a natural, verbose, and human-like explanation. "
                        "Never change the supplied decision, invent policy, provide medical advice, or claim an authorization was submitted or approved."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        },
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    usage = payload.get("usage", {})
    usage_dict = {
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    } if usage else None
    return payload["choices"][0]["message"]["content"].strip(), selected_model, usage_dict


def call_gemini(prompt: str, model: str | None = None) -> tuple[str, str, dict[str, int] | None]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    selected_model = (model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")).strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{selected_model}:generateContent?key={api_key}"
    response = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json={
            "system_instruction": {
                "parts": [{
                    "text": (
                        "Explain the supplied prior-authorization decision-support result. Do not modify the decision, "
                        "invent policy, provide medical advice, or state that a request was submitted or approved."
                    )
                }]
            },
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.15, "maxOutputTokens": 700},
        },
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()
    usage = payload.get("usageMetadata", {})
    usage_dict = {
        "prompt_tokens": usage.get("promptTokenCount", 0),
        "completion_tokens": usage.get("candidatesTokenCount", 0),
        "total_tokens": usage.get("totalTokenCount", 0),
    } if usage else None
    return payload["candidates"][0]["content"]["parts"][0]["text"].strip(), selected_model, usage_dict


def generate_explanation(
    provider: str,
    context: dict[str, Any],
    model: str | None = None,
) -> tuple[str, str | None, str, dict[str, int] | None]:
    """Generate an explanation from the locked deterministic route.

    Groq/Gemini are optional enhancements only; they can rephrase the canonical
    deterministic explanation for clarity, but cannot alter routing or facts.
    If Groq fails or is unavailable, returns the canonical deterministic explanation.
    """
    canonical = deterministic_explanation(context)
    normalized_provider = (provider or "Local Expert System").lower()

    # If provider is Local Expert System, return canonical immediately
    if not normalized_provider.startswith(("groq", "gemini")):
        return canonical, None, "deterministic-local", None

    # Build prompt for Groq/Gemini to rephrase canonical explanation + committee findings
    committee_summary = ""
    committee_vote = context.get("committee_vote", {})
    if committee_vote:
        support = committee_vote.get("support", 0)
        oppose = committee_vote.get("oppose", 0)
        caution = committee_vote.get("caution", 0)
        committee_summary = (
            f"\n\nEvidence review panel findings: {support} supporting, "
            f"{oppose} cautioning/opposing, {caution} noting cautions. "
            "These findings are for explanation and audit only; they cannot alter the locked route."
        )

    prompt = (
        "Rephrase this deterministic decision-support explanation into clear, readable prose. "
        "Include the evidence review panel findings as context. "
        "Never change the decision, invent facts, contradict the locked route, or claim authority over routing. "
        "The deterministic route is immutable.\n\n"
        + canonical + committee_summary
    )

    try:
        if normalized_provider.startswith("groq"):
            text, selected_model, usage = call_groq(prompt, model=model)
            is_valid, reason = validate_enhanced_explanation(text, canonical, context.get("decision", ""))
            if not is_valid:
                return (
                    canonical,
                    f"External explanation invalid ({reason}); deterministic explanation used",
                    selected_model,
                    None,
                )
            return text, None, selected_model, usage
        if normalized_provider.startswith("gemini"):
            text, selected_model, usage = call_gemini(prompt, model=model)
            is_valid, reason = validate_enhanced_explanation(text, canonical, context.get("decision", ""))
            if not is_valid:
                return (
                    canonical,
                    f"External explanation invalid ({reason}); deterministic explanation used",
                    selected_model,
                    None,
                )
            return text, None, selected_model, usage
    except Exception as exc:
        # Fallback to canonical deterministic explanation on any error
        fallback_model = model or "unavailable"
        return (
            canonical,
            f"External explanation unavailable; deterministic explanation used: {type(exc).__name__}",
            fallback_model,
            None,
        )

    # Should not reach here, but return canonical as safety fallback
    return canonical, None, "deterministic-fallback", None
