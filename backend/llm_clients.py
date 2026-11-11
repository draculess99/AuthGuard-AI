from __future__ import annotations

import json
import os
import re
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

# Curated text-capable choices. Environment defaults are inserted into the UI even
# when they are not in these lists, so model changes do not require a code edit.
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_MODELS = [
    DEFAULT_GROQ_MODEL,
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
]
LOCAL_PROVIDER = "Local Expert System"
LOCAL_MODEL = "deterministic-local"
_TRUTHY = {"1", "true", "yes", "on"}


def groq_enabled() -> bool:
    """Groq is opt-in: only AUTHGUARD_ENABLE_GROQ=1/true/yes/on (any case) enables it."""
    return os.getenv("AUTHGUARD_ENABLE_GROQ", "").strip().lower() in _TRUTHY


def groq_key_configured() -> bool:
    return bool(os.getenv("GROQ_API_KEY", "").strip())


def groq_model() -> str:
    return os.getenv("GROQ_MODEL", "").strip() or DEFAULT_GROQ_MODEL


def groq_status() -> str:
    """Return 'disabled', 'missing_key' or 'active'. Never exposes the key itself."""
    if not groq_enabled():
        return "disabled"
    return "active" if groq_key_configured() else "missing_key"


def default_provider() -> str:
    """Environment-controlled default explanation provider."""
    return "Groq" if groq_enabled() else LOCAL_PROVIDER

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
        configured = groq_model()
        choices = [configured, *GROQ_MODELS]
    elif normalized.startswith("gemini"):
        configured = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()
        choices = [configured, *GEMINI_MODELS]
    else:
        return []
    return list(dict.fromkeys(choice for choice in choices if choice))


# Denial risk is an authorization-denial score, never a clinical or complication risk.
_RISK_MISDESCRIPTION = re.compile(
    r"\bcomplications?\b"
    r"|\b(clinical|medical|patient|health|disease)[\s-]+(risk|severity)\b"
    r"|\brisk\s+(of|to|for)\s+(the\s+)?(patient|a\s+problem|harm)\b"
    r"|\blikelihood\s+of\s+(a|any|the)?\s*(problem|medical|clinical|adverse)"
    r"|\b(problem|problems)\s+(is|are)\s+(considered\s+)?(low|high|likely|unlikely)\b"
)

# A prohibited phrase is tolerated only when it is directly denied: a negator followed, with
# nothing but determiners, verbs of measurement and risk-category words in between, by the phrase
# ("is not a clinical or patient-risk assessment"). "not only", "not merely", intervening
# affirmative words, and later clauses never count as a denial.
_DENIAL_NEGATOR = re.compile(r"\b(?:not|no|never|cannot|nor|neither)\b|n't\b")
_DENIAL_SCOPE_WORDS = frozenset({
    "a", "an", "the", "any", "this", "that", "these", "those", "of", "to", "for", "as", "about", "or", "nor",
    "predict", "predicts", "indicate", "indicates", "imply", "implies", "reflect", "reflects", "represent",
    "represents", "measure", "measures", "estimate", "estimates", "assess", "assesses", "forecast", "forecasts",
    "signal", "signals", "describe", "describes", "clinical", "medical", "patient", "health", "disease",
    "complication", "complications", "risk", "risks", "severity", "assessment", "prediction", "indicator",
    "outcome", "outcomes",
})
_CLAUSE_BREAK = re.compile(
    r"[;:–—]|\s-\s|\b(?:but|however|whereas|while|although|though|yet|rather|instead)\b"
)


def _is_directly_denied(clause: str, start: int, scope: frozenset[str] | None = None) -> bool:
    scope = _DENIAL_SCOPE_WORDS if scope is None else scope
    prefix = clause[:start]
    for negator in reversed(list(_DENIAL_NEGATOR.finditer(prefix))):
        between = prefix[negator.end():]
        if not re.fullmatch(r"[a-z\s,'-]*", between):
            continue
        words = re.findall(r"[a-z]+", between)
        if len(words) <= 6 and all(word in scope for word in words):
            return True
    return False


def _misdescribes_denial_risk(lowered: str) -> bool:
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", lowered):
        for clause in _CLAUSE_BREAK.split(sentence):
            for match in _RISK_MISDESCRIPTION.finditer(clause):
                if not _is_directly_denied(clause, match.start()):
                    return True
    return False


# Workflow routes the guardrails can lock, as they appear in prose.
_ROUTE_PHRASES = {
    "READY_FOR_SUBMISSION_REVIEW": "ready for submission",
    "HUMAN_REVIEW_REQUIRED": "human review required",
    "URGENT_HUMAN_REVIEW": "urgent human review",
    "ESCALATE_DENIAL_RISK": "escalate denial risk",
    "HOLD_ELIGIBILITY": "hold eligibility",
    "HOLD_FOR_DOCUMENTATION": "hold for documentation",
}

_XGB_VERBS = r"(?:determin\w*|generat\w*|decid\w*|produc\w*|recommend\w*|select\w*|chos\w+|choos\w*|dictat\w*|pick\w*)"
_PANEL_VERBS = (
    r"(?:determin\w*|select\w*|decid\w*|chos\w+|choos\w*|lock\w*|set|sets|alter\w*|chang\w*|overrid\w*|"
    r"overrul\w*|establish\w*|dictat\w*)"
)
_NO_SCORE_WORDS = r"(?:(?!\b(?:score|probability|estimate|band|risk)\b)[^.]){0,40}?"

# (reason, pattern, negatable). Negatable claims are tolerated only when directly denied; the
# group "v" marks the claim word that a negator must directly precede. Patterns are matched
# clause by clause, never against a whole sentence.
_CLAIMS = (
    ("xgboost_overclaim", re.compile(
        rf"\bxgboost\b[^.]{{0,60}}?\b(?P<v>{_XGB_VERBS})\b{_NO_SCORE_WORDS}"
        r"\b(?:recommendation|route|routing|decision|outcome|result|workflow|it|them)\b"
    ), True),
    ("xgboost_overclaim", re.compile(rf"\b(?P<v>{_XGB_VERBS})\b[^.]{{0,40}}?\bby\s+xgboost\b"), True),
    ("xgboost_overclaim", re.compile(
        r"\b(?P<v>solely|alone)\b[^.]{0,30}\bxgboost\b|\bxgboost\b[^.]{0,30}\b(?P<w>alone|solely)\b"
    ), True),
    ("panel_not_internal", re.compile(
        r"\b(?P<v>independent|external|outside|third[- ]party)\W+(?:[\w-]+\W+){0,3}panel\b"
        r"|\bpanel\b[^.]{0,30}\b(?P<w>independent|external|third[- ]party|independently|externally)\b"
    ), True),
    ("panel_not_internal", re.compile(
        r"\bpanel\b[^.]{0,30}\b(?:not|isn't|aren't|non)[\s-]+(?:an?\s+)?internal\b"
        r"|\bpanel\b[^.]{0,30}\bnot\s+(?:merely|only|just|simply|purely|solely)\s+advisory\b"
    ), False),
    ("panel_overclaim", re.compile(
        rf"\bpanel\b[^.]{{0,60}}?\b(?P<v>{_PANEL_VERBS})\b{_NO_SCORE_WORDS}"
        r"\b(?:route|routing|decision|outcome|result|recommendation|it)\b"
    ), True),
    ("unsupported_approval_or_submission_claim", re.compile(r"\b(?P<v>approved|granted|submitted)\b"), True),
    ("unsupported_approval_or_submission_claim", re.compile(
        r"\b(?:will|shall|going to)\s+(?:\w+\s+){0,2}?(?P<v>submit|submits|submission|approve|approves|grant|grants)\b"
        r"|\bsubmission\s+(?P<w>will)\s+(?!not\b)\w+"
        r"|\bsubmission\s+is\s+(?P<x>guaranteed|certain|assured|inevitable|automatic|mandatory)\b"
        r"|\b(?P<y>definitely|certainly|guaranteed)\b[^.]{0,30}\bsubmit\w*"
    ), True),
    ("unsupported_approval_or_submission_claim", re.compile(r"\bsubmission\s+is\s+not\s+optional\b"), False),
)

# Words allowed between a negator and the claim word it denies: auxiliaries, articles, list
# connectives, and the claim verbs themselves ("did not approve, grant, or submit").
_NEGATION_BRIDGE = frozenset({
    "did", "does", "do", "has", "have", "had", "was", "were", "is", "are", "be", "been", "being", "ever",
    "yet", "itself", "directly", "independently", "actually", "automatically", "used", "to", "a", "an", "the",
    "this", "that", "or", "nor", "for", "as", "routed", "route", "sent", "assigned", "placed", "independent",
    "external", "determine", "select", "decide", "choose", "generate", "produce", "recommend", "establish",
    "lock", "set", "alter", "change", "override", "approve", "grant", "submit", "approved", "granted",
    "submitted",
})
_MODAL_BEFORE = re.compile(r"\b(?:may|might|can|could|should|would|to)\s+(?:be\s+)?$")
_CLAIM_BREAK = re.compile(
    r"[;:–—]|\s-\s|\b(?:but|however|whereas|while|although|though|rather|instead|because|since|"
    r"therefore|thus|hence|and)\b"
)
# A clause with no subject of its own continues the previous clause's subject
# ("XGBoost did not only score; it determined the route").
_SUBJECTLESS = re.compile(
    r"\s*(?:it|they|which|also|instead|then|did|does|do|has|have|had|was|were|is|are|chose|made|set|"
    r"independent|external|outside|third\b|\w+ed)\b"
)


def _claim_clauses(lowered: str):
    """Yield claim-evaluation clauses, carrying a dropped subject (XGBoost or the panel) forward."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", lowered):
        subject = None
        for clause in _CLAIM_BREAK.split(sentence):
            evaluated = clause
            if subject and _SUBJECTLESS.match(clause):
                evaluated = f"{subject} {clause}"
            yield evaluated
            if "xgboost" in clause:
                subject = "xgboost"
            elif "panel" in clause:
                subject = "panel"
            else:
                subject = None


def _claim_is_denied(clause: str, start: int, bridge: frozenset[str] = _NEGATION_BRIDGE) -> bool:
    return _is_directly_denied(clause, start, bridge)


def _violates_claims(lowered: str, locked_decision: str) -> str | None:
    other_routes = [
        phrase for route, phrase in _ROUTE_PHRASES.items() if route != locked_decision
    ]
    for clause in _claim_clauses(lowered):
        for reason, pattern, negatable in _CLAIMS:
            for match in pattern.finditer(clause):
                if not negatable:
                    return reason
                starts = [match.start(name) for name in match.re.groupindex if match.group(name)]
                start = min(starts) if starts else match.start()
                if reason == "unsupported_approval_or_submission_claim":
                    if _MODAL_BEFORE.search(clause[:start]):
                        continue
                if not _claim_is_denied(clause, start):
                    return reason
        normalized = clause.replace("_", " ")
        for phrase in other_routes:
            position = normalized.find(phrase)
            if position != -1 and not _claim_is_denied(normalized, position):
                return "contradicts_locked_route"
    return None


def validate_enhanced_explanation(enhanced: str, canonical: str, locked_decision: str) -> tuple[bool, str]:
    """Validate external LLM output against the locked deterministic route.

    Returns: (is_valid, reason_if_invalid)
    """
    if not enhanced or not str(enhanced).strip():
        return False, "empty_response"

    text = str(enhanced)
    lowered = text.lower()

    if len(text.strip()) < 50:
        return False, "response_too_short"

    if _misdescribes_denial_risk(lowered):
        return False, "misdescribes_denial_risk"

    violation = _violates_claims(lowered, locked_decision)
    if violation:
        return False, violation

    # If locked decision is READY, reject anything claiming denial/rejection
    if "READY" in locked_decision:
        for phrase in ("deny", "denied", "rejected", "will not approve"):
            if phrase in lowered:
                return False, "contradicts_locked_route"

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

    explanation_parts.append(f"The XGBoost authorization denial-risk band is {risk}.")
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
    selected_model = (model or groq_model()).strip()
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


def _build_prompt(canonical: str, context: dict[str, Any]) -> str:
    """Prompt contains only the locked route/deterministic findings and panel tallies."""
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
    rules = (
        "Rephrase this deterministic decision-support explanation into clear, readable prose. "
        "Follow these rules exactly:\n"
        "- Use the exact phrase \"authorization denial risk\" for the risk level. It is the only kind of risk "
        "you may mention. Do not discuss, contrast, or negate any other kind of risk, and do not name other "
        "risk categories even as a disclaimer.\n"
        "- State that expert rules, configured thresholds, and guardrails locked the route before this "
        "explanation was written.\n"
        "- State that XGBoost contributed a denial-risk score only; it did not generate or determine the route.\n"
        "- Describe the Evidence Review Panel as an internal advisory panel. Its findings and your prose "
        "cannot change the locked route.\n"
        "- This application does not submit an authorization. You may say a qualified reviewer can review "
        "the package and decide whether to submit it; never state that submission will definitely happen, "
        "and never say anything was approved, granted, or submitted.\n"
        "- Never change the decision, invent facts, or contradict the locked route.\n\n"
    )
    return rules + canonical + committee_summary


def generate_explanation_detail(
    provider: str,
    context: dict[str, Any],
    model: str | None = None,
) -> dict[str, Any]:
    """Generate an explanation from the locked deterministic route.

    Groq/Gemini are optional, explanation-only enhancements. Groq is called only when
    AUTHGUARD_ENABLE_GROQ is true (the environment flag is authoritative, even if a
    caller requests Groq). Any failure returns the canonical deterministic explanation.

    Returns a dict: text, warning, model, usage, provider_used (the provider that
    actually produced the text), attempted (an external call was attempted) and
    fallback (an attempted external call fell back to the deterministic text).
    """
    canonical = deterministic_explanation(context)
    requested = (provider or LOCAL_PROVIDER).strip().lower()

    local = {
        "text": canonical, "warning": None, "model": LOCAL_MODEL, "usage": None,
        "provider_used": LOCAL_PROVIDER, "attempted": False, "fallback": False,
    }

    if requested.startswith("groq"):
        if not groq_enabled():
            return local  # Disabled: Groq is never called, whatever the caller requested.
        call, label = call_groq, "Groq"
        if not groq_key_configured():
            return {**local, "attempted": True, "fallback": True,
                    "warning": "Groq unavailable (GROQ_API_KEY is missing); deterministic explanation used"}
    elif requested.startswith("gemini"):
        call, label = call_gemini, "Gemini"
    else:
        return local

    def fallback(reason: str) -> dict[str, Any]:
        return {**local, "attempted": True, "fallback": True, "warning": reason}

    try:
        text, selected_model, usage = call(_build_prompt(canonical, context), model=model)
        is_valid, reason = validate_enhanced_explanation(text, canonical, context.get("decision", ""))
        if not is_valid:
            return fallback(f"External explanation invalid ({reason}); deterministic explanation used")
        return {
            "text": text.strip(), "warning": None, "model": selected_model, "usage": usage,
            "provider_used": label, "attempted": True, "fallback": False,
        }
    except Exception as exc:
        # Type name only: never include exception text, which could echo request details.
        return fallback(
            f"External explanation unavailable; deterministic explanation used: {type(exc).__name__}"
        )


def generate_explanation(
    provider: str,
    context: dict[str, Any],
    model: str | None = None,
) -> tuple[str, str | None, str, dict[str, int] | None]:
    """Tuple wrapper around generate_explanation_detail: (text, warning, model, usage)."""
    detail = generate_explanation_detail(provider, context, model=model)
    return detail["text"], detail["warning"], detail["model"], detail["usage"]
