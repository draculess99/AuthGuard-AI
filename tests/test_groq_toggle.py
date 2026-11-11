import ast
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.agents.committee import run_pipeline
from backend.llm_clients import (
    DEFAULT_GROQ_MODEL,
    default_provider,
    _build_prompt,
    generate_explanation_detail,
    get_model_choices,
    groq_enabled,
    groq_status,
    validate_enhanced_explanation,
)
from backend.memory import JSONMemoryStore
from backend.server import create_app
from test_pipeline import base_case

VALID_TEXT = (
    "AuthGuard routed this case for review by the deterministic workflow, and the evidence "
    "panel notes are included here only as supporting context for the reviewer."
)
FAKE_KEY = "test-key-not-real"


def run(tmp_path: Path, provider: str | None = "Groq", **kwargs):
    return run_pipeline(
        base_case(), provider=provider, persist=False, memory_store=JSONMemoryStore(tmp_path), **kwargs
    )


def enable(monkeypatch: pytest.MonkeyPatch, key: str | None = FAKE_KEY):
    monkeypatch.setenv("AUTHGUARD_ENABLE_GROQ", "true")
    if key:
        monkeypatch.setenv("GROQ_API_KEY", key)


def test_groq_disabled_by_default():
    assert groq_enabled() is False
    assert groq_status() == "disabled"
    assert default_provider() == "Local Expert System"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " On "])
def test_env_flag_selects_groq_when_truthy(monkeypatch, value):
    monkeypatch.setenv("AUTHGUARD_ENABLE_GROQ", value)
    assert groq_enabled() is True
    assert default_provider() == "Groq"


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "enabled"])
def test_env_flag_other_values_stay_disabled(monkeypatch, value):
    monkeypatch.setenv("AUTHGUARD_ENABLE_GROQ", value)
    assert groq_enabled() is False


def test_status_reports_missing_key_and_active(monkeypatch):
    enable(monkeypatch, key=None)
    assert groq_status() == "missing_key"
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    assert groq_status() == "active"


def test_disabled_groq_never_calls_provider_even_when_requested(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)  # key present, flag absent
    with patch("backend.llm_clients.call_groq") as mock_call:
        result = run(tmp_path, provider="Groq")
    mock_call.assert_not_called()
    assert result["explanation_provider"] == "Local Expert System"
    assert result["explanation_fallback"] is False
    assert result["decision"] in result["narrative"]


def test_disabled_groq_via_api_payload_never_calls_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("GROQ_API_KEY", FAKE_KEY)
    monkeypatch.setenv("AUTHGUARD_DATA_DIR", str(tmp_path))
    client = create_app().test_client()
    with patch("backend.llm_clients.call_groq") as mock_call:
        response = client.post("/api/process", json={**base_case(), "provider": "Groq"})
    mock_call.assert_not_called()
    assert response.status_code == 200
    assert response.get_json()["explanation_provider"] == "Local Expert System"


def test_api_default_provider_follows_environment(monkeypatch, tmp_path):
    enable(monkeypatch)
    monkeypatch.setenv("AUTHGUARD_DATA_DIR", str(tmp_path))
    seen = {}

    def fake_run(case, provider=None, **kwargs):
        seen["provider"] = provider
        return {}

    client = create_app().test_client()
    with patch("backend.server.run_pipeline", side_effect=fake_run):
        client.post("/api/process", json=base_case())
    assert seen["provider"] == "Groq"


def test_enabled_valid_groq_output_is_used_and_labeled(tmp_path, monkeypatch):
    enable(monkeypatch)
    usage = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
    with patch("backend.llm_clients.call_groq", return_value=(VALID_TEXT, DEFAULT_GROQ_MODEL, usage)) as mock_call:
        result = run(tmp_path)
    mock_call.assert_called_once()
    assert result["narrative"] == VALID_TEXT
    assert result["explanation_provider"] == "Groq"
    assert result["explanation_model"] == DEFAULT_GROQ_MODEL
    assert result["explanation_fallback"] is False
    assert result["llm_warning"] is None


def test_groq_prompt_contains_only_locked_route_and_panel_findings(monkeypatch):
    enable(monkeypatch)
    context = {
        "decision": "HUMAN_REVIEW_REQUIRED", "risk_level": "High", "blockers": [], "warnings": [],
        "guardrail_reasons": ["High denial risk"], "committee_vote": {"support": 3, "oppose": 2, "caution": 1},
    }
    with patch("backend.llm_clients.call_groq", return_value=(VALID_TEXT, DEFAULT_GROQ_MODEL, None)) as mock_call:
        generate_explanation_detail("Groq", context)
    prompt = mock_call.call_args.args[0]
    assert "HUMAN_REVIEW_REQUIRED" in prompt
    assert "3 supporting" in prompt
    assert FAKE_KEY not in prompt


def test_missing_key_falls_back_without_calling_provider(tmp_path, monkeypatch):
    enable(monkeypatch, key=None)
    with patch("backend.llm_clients.call_groq") as mock_call:
        result = run(tmp_path)
    mock_call.assert_not_called()
    assert result["explanation_provider"] == "Local Expert System"
    assert result["explanation_fallback"] is True
    assert "GROQ_API_KEY" in result["llm_warning"]


def test_provider_exception_falls_back_and_hides_message(tmp_path, monkeypatch):
    enable(monkeypatch)
    with patch("backend.llm_clients.call_groq", side_effect=RuntimeError(f"boom {FAKE_KEY}")):
        result = run(tmp_path)
    assert result["explanation_provider"] == "Local Expert System"
    assert result["explanation_fallback"] is True
    assert result["decision"] in result["narrative"]
    assert FAKE_KEY not in result["llm_warning"]
    assert "RuntimeError" in result["llm_warning"]


@pytest.mark.parametrize("bad_text", [
    "",
    "   ",
    "Too short.",
    "This case will be denied due to high cost and insufficient evidence in the submitted record.",
    "The request has been approved and was submitted to the payer for final authorization today.",
])
def test_empty_or_contradictory_output_falls_back(tmp_path, monkeypatch, bad_text):
    enable(monkeypatch)
    with patch("backend.llm_clients.call_groq", return_value=(bad_text, DEFAULT_GROQ_MODEL, None)):
        result = run(tmp_path)
    assert result["explanation_provider"] == "Local Expert System"
    assert result["explanation_fallback"] is True
    assert result["narrative"] != bad_text
    assert result["decision"] in result["narrative"]


def test_locked_decision_and_human_gate_unchanged_by_groq(tmp_path, monkeypatch):
    case = base_case()
    case["estimated_cost"] = 250000
    case["previous_denials"] = 3
    baseline = run_pipeline(case, provider="Local Expert System", persist=False,
                            memory_store=JSONMemoryStore(tmp_path))
    enable(monkeypatch)
    hostile = "Approved: this case is ready, no human review is needed, and the payment amount is zero."
    with patch("backend.llm_clients.call_groq", return_value=(hostile, DEFAULT_GROQ_MODEL, None)):
        with_groq = run_pipeline(case, provider="Groq", persist=False, memory_store=JSONMemoryStore(tmp_path))
    for key in ("decision", "human_review_required", "review_status", "guardrail_reasons"):
        assert with_groq[key] == baseline[key]
    assert with_groq["model"]["denial_probability"] == baseline["model"]["denial_probability"]
    assert with_groq["case"]["patient_pay_amount"] == baseline["case"]["patient_pay_amount"]
    assert with_groq["human_review_required"] is True


def test_model_defaults_and_retired_models_removed(monkeypatch):
    assert DEFAULT_GROQ_MODEL == "openai/gpt-oss-120b"
    choices = get_model_choices("Groq")
    assert choices[0] == "openai/gpt-oss-120b"
    assert "qwen/qwen3.8-27b" in choices
    assert "llama-3.3-70b-versatile" not in choices
    assert "llama-3.1-8b-instant" not in choices
    monkeypatch.setenv("GROQ_MODEL", "qwen/qwen3.8-27b")
    assert get_model_choices("Groq")[0] == "qwen/qwen3.8-27b"


def test_dashboard_contains_all_status_labels_and_never_the_key():
    source = (Path(__file__).resolve().parents[1] / "frontend" / "dashboard.py").read_text(encoding="utf-8")
    for label in (
        "Groq enabled — optional explanation enhancement is active. Any Groq failure automatically uses the deterministic explanation.",
        "Groq enabled, but GROQ_API_KEY is missing — deterministic fallback is active.",
        "Groq disabled — AuthGuard is using the deterministic explanation.",
        "Groq fallback active — deterministic explanation used.",
        "Groq explanation enhancement used; the route remains deterministic.",
        "Routing, risk scoring, guardrails, and PayPal actions always remain deterministic.",
    ):
        assert label in source
    assert 'getenv("GROQ_API_KEY"' not in source


def test_conftest_blocks_real_network():
    import requests

    with pytest.raises(AssertionError, match="Real network request"):
        requests.post("https://api.groq.com/openai/v1/chat/completions", json={})


LIVE_PROBLEM_PHRASES = [
    "The likelihood of a problem or complication is considered low.",
    "The XGBoost model generated the recommendation.",
    "An independent evidence-review panel examined the case.",
]
ACCEPTABLE_TEXT = (
    "The XGBoost model estimated a low authorization denial risk. Expert rules and guardrails had already "
    "locked the route before this explanation was generated. The internal advisory Evidence Review Panel "
    "recorded four supporting findings and two cautions."
)


@pytest.mark.parametrize("phrase", LIVE_PROBLEM_PHRASES + [
    "Denial risk is a low clinical risk for this member, so the review should be quick and simple.",
    "There is a low medical risk and a low patient risk here, which keeps the case straightforward.",
    "XGBoost alone determined the route for this case after scoring the available information.",
    "An external panel of reviewers examined the case and agreed with the recommendation shown.",
    "The authorization was granted after the review and has been submitted to the payer already.",
])
def test_semantically_wrong_groq_output_is_rejected(phrase):
    padded = phrase + " The rest of this sentence only pads the response beyond the minimum length."
    valid, reason = validate_enhanced_explanation(padded, "", "READY_FOR_SUBMISSION_REVIEW")
    assert valid is False
    assert reason


@pytest.mark.parametrize("phrase", LIVE_PROBLEM_PHRASES)
def test_live_problem_wording_falls_back_deterministically(tmp_path, monkeypatch, phrase):
    enable(monkeypatch)
    text = phrase + " The deterministic workflow routed the case and the reviewer can proceed as appropriate."
    with patch("backend.llm_clients.call_groq", return_value=(text, DEFAULT_GROQ_MODEL, None)):
        result = run(tmp_path)
    assert result["explanation_provider"] == "Local Expert System"
    assert result["explanation_model"] == "deterministic-local"
    assert result["explanation_fallback"] is True
    assert result["narrative"] != text
    assert result["decision"] in result["narrative"]
    assert FAKE_KEY not in str(result["llm_warning"])


def test_accurate_groq_wording_is_accepted(tmp_path, monkeypatch):
    assert validate_enhanced_explanation(ACCEPTABLE_TEXT, "", "READY_FOR_SUBMISSION_REVIEW") == (True, "")
    enable(monkeypatch)
    with patch("backend.llm_clients.call_groq", return_value=(ACCEPTABLE_TEXT, DEFAULT_GROQ_MODEL, None)):
        result = run(tmp_path)
    assert result["narrative"] == ACCEPTABLE_TEXT
    assert result["explanation_provider"] == "Groq"
    assert result["explanation_fallback"] is False


def test_negated_claims_are_not_rejected():
    text = (
        "XGBoost does not determine the route. This application has not submitted anything, and the "
        "Evidence Review Panel is not an independent panel; it is an internal advisory step."
    )
    assert validate_enhanced_explanation(text, "", "READY_FOR_SUBMISSION_REVIEW") == (True, "")


def test_ready_claim_rejected_when_route_is_not_ready():
    text = "This case is ready for submission review and the reviewer can proceed straight away today."
    valid, reason = validate_enhanced_explanation(text, "", "HUMAN_REVIEW_REQUIRED")
    assert (valid, reason) == (False, "contradicts_locked_route")


def test_prompt_states_denial_risk_and_authority_semantics():
    prompt = _build_prompt("The deterministic workflow determined this case must route to X.", {
        "committee_vote": {"support": 4, "oppose": 1, "caution": 1},
    })
    lowered = prompt.lower()
    for expected in (
        'exact phrase "authorization denial risk"',
        "do not discuss, contrast, or negate any other kind of risk",
        "do not name other risk categories even as a disclaimer",
        "expert rules, configured thresholds, and guardrails locked the route",
        "xgboost contributed a denial-risk score only",
        "internal advisory panel",
        "cannot change the locked route",
        "does not submit an authorization",
        "a qualified reviewer can review the package and decide whether to submit it",
        "never state that submission will definitely happen",
    ):
        assert expected in lowered
    for category in ("clinical", "medical", "patient", "complication", "disease"):
        assert category not in lowered, "prompt must not prime the model with prohibited risk categories"


def test_dashboard_separates_route_from_explanation_and_never_injects_narrative_into_html():
    source = (Path(__file__).resolve().parents[1] / "frontend" / "dashboard.py").read_text(encoding="utf-8")
    for text in (
        "DETERMINISTIC LOCKED ROUTE",
        "GROQ EXPLANATION SUMMARY",
        "DETERMINISTIC EXPLANATION SUMMARY",
        'st.text(result["narrative"])',
    ):
        assert text in source

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and any(
            kw.arg == "unsafe_allow_html" and getattr(kw.value, "value", False) is True for kw in node.keywords
        ):
            segment = ast.get_source_segment(source, node) or ""
            for forbidden in ("narrative", "llm_warning", "explanation_provider", "explanation_model"):
                assert forbidden not in segment, f"{forbidden} rendered inside unsafe HTML"


PAD = " The deterministic workflow routed this case for the reviewer to consider."
ACCURATE_NEGATED = [
    "The XGBoost model estimated a low authorization denial risk.",
    "The authorization denial-risk score is not a clinical or patient-risk assessment.",
    "This score does not predict medical complications.",
    "Expert rules, configured thresholds, and guardrails had already locked the route.",
    "The score isn't a measure of medical complications.",
]
MISLEADING = [
    "The clinical risk is low.",
    "The patient risk is low.",
    "The likelihood of a problem or complication is considered low.",
    "The XGBoost score means complications are unlikely.",
    "Not only is the clinical risk low, the complication risk is also low.",
    "This is not merely clinical risk; it represents overall patient risk.",
    "This is not just a billing score but also a clinical-risk estimate.",
    "The score is not high and the clinical risk is low.",
    "This does not indicate risk and complications are unlikely.",
    "The score is not a clinical risk assessment, and complications are unlikely.",
    "The score is not a concern; the patient-risk level is low.",
]


@pytest.mark.parametrize("sentence", ACCURATE_NEGATED)
def test_accurate_and_directly_negated_statements_are_accepted(sentence):
    assert validate_enhanced_explanation(sentence + PAD, "", "READY_FOR_SUBMISSION_REVIEW") == (True, "")


@pytest.mark.parametrize("sentence", MISLEADING)
def test_misleading_risk_statements_are_rejected(sentence):
    valid, reason = validate_enhanced_explanation(sentence + PAD, "", "READY_FOR_SUBMISSION_REVIEW")
    assert (valid, reason) == (False, "misdescribes_denial_risk")


@pytest.mark.parametrize("opener", ["Not only", "not merely", "Not just", "Not necessarily"])
def test_not_only_and_not_merely_cannot_bypass_validation(opener):
    text = f"{opener} is the clinical risk low, the patient risk is low too." + PAD
    assert validate_enhanced_explanation(text, "", "READY_FOR_SUBMISSION_REVIEW")[0] is False


@pytest.mark.parametrize("sentence", MISLEADING)
def test_misleading_risk_output_falls_back_without_leaking(tmp_path, monkeypatch, sentence):
    enable(monkeypatch)
    text = sentence + PAD
    with patch("backend.llm_clients.call_groq", return_value=(text, DEFAULT_GROQ_MODEL, None)):
        result = run(tmp_path)
    assert result["explanation_provider"] == "Local Expert System"
    assert result["explanation_model"] == "deterministic-local"
    assert result["explanation_fallback"] is True
    assert result["narrative"] != text
    warning = result["llm_warning"]
    assert warning == "External explanation invalid (misdescribes_denial_risk); deterministic explanation used"
    assert FAKE_KEY not in str(result)
    assert sentence not in str(result["llm_warning"]) and sentence not in result["narrative"]


READY = "READY_FOR_SUBMISSION_REVIEW"
HUMAN = "HUMAN_REVIEW_REQUIRED"
HOLD = "HOLD_FOR_DOCUMENTATION"

CLAUSE_ACCEPTED = {
    "xgboost": [
        "XGBoost did not determine the route.",
        "XGBoost supplied a denial-risk score only and did not select the workflow decision.",
        "XGBoost supplied a denial-risk score only, but expert rules and guardrails determined the route.",
        "XGBoost supplied a score and expert rules determined the route.",
    ],
    "panel": [
        "The internal advisory panel does not alter the locked route.",
        "The panel is not independent or external.",
        "The panel's findings are for audit only; they cannot alter the locked route.",
    ],
    "approval": [
        "The application did not approve, grant, or submit the authorization.",
        "A qualified reviewer may submit the package if appropriate.",
        "Submission is not guaranteed.",
        "The authorization has not yet been approved, granted, or submitted.",
    ],
}
CLAUSE_REJECTED = {
    "xgboost": [
        "XGBoost determined the route.",
        "XGBoost generated the recommendation.",
        "XGBoost did not merely generate the recommendation; it determined the route.",
        "XGBoost did not only score denial risk but selected the workflow decision.",
        "The route was not chosen by rules because XGBoost determined it.",
    ],
    "panel": [
        "An independent panel reviewed the case.",
        "The panel was not merely advisory; it independently determined the result.",
        "The panel is not internal but external.",
        "The panel did not only review the evidence; it selected the route.",
    ],
    "approval": [
        "The authorization was approved.",
        "The request has been submitted.",
        "The application did not merely review the request; it approved it.",
        "The request was not only approved but submitted.",
        "Submission is not optional because the system will submit it.",
        "The request will be submitted to the payer next.",
        "Submission will occur after review.",
    ],
}
ACCEPTED_FLAT = [text for group in CLAUSE_ACCEPTED.values() for text in group]
REJECTED_FLAT = [text for group in CLAUSE_REJECTED.values() for text in group]


@pytest.mark.parametrize("sentence", ACCEPTED_FLAT)
def test_direct_negation_of_authority_claims_is_accepted(sentence):
    assert validate_enhanced_explanation(sentence + PAD, "", READY) == (True, "")


@pytest.mark.parametrize("sentence", REJECTED_FLAT)
def test_blanket_negation_cannot_hide_authority_claims(sentence):
    valid, reason = validate_enhanced_explanation(sentence + PAD, "", READY)
    assert valid is False
    assert reason in {
        "xgboost_overclaim", "panel_not_internal", "panel_overclaim", "unsupported_approval_or_submission_claim",
    }


@pytest.mark.parametrize("sentence", REJECTED_FLAT)
def test_rejected_authority_claims_fall_back_with_fixed_warning(tmp_path, monkeypatch, sentence):
    enable(monkeypatch)
    text = sentence + PAD
    with patch("backend.llm_clients.call_groq", return_value=(text, DEFAULT_GROQ_MODEL, None)):
        result = run(tmp_path)
    assert result["explanation_provider"] == "Local Expert System"
    assert result["explanation_model"] == "deterministic-local"
    assert result["explanation_fallback"] is True
    assert result["narrative"] != text
    assert result["llm_warning"].startswith("External explanation invalid (")
    assert result["llm_warning"].endswith("); deterministic explanation used")
    assert sentence not in str(result)
    assert FAKE_KEY not in str(result)


def test_a_negation_elsewhere_in_the_sentence_does_not_exempt_a_claim():
    text = "Rules are not optional, and XGBoost determined the route for this case." + PAD
    assert validate_enhanced_explanation(text, "", READY)[0] is False
    text = "There is no doubt here: the request has been submitted to the payer today." + PAD
    assert validate_enhanced_explanation(text, "", READY)[0] is False


def test_contradictory_route_claims_are_rejected_even_with_a_negation_elsewhere():
    denied = "The case was denied after review, so the reviewer should not be surprised." + PAD
    assert validate_enhanced_explanation(denied, "", READY) == (False, "contradicts_locked_route")

    ready_claim = "The case is ready for submission review and nothing else remains." + PAD
    assert validate_enhanced_explanation(ready_claim, "", HUMAN) == (False, "contradicts_locked_route")

    mixed_clause = "The explanation cannot change the route, and the case is ready for submission review." + PAD
    assert validate_enhanced_explanation(mixed_clause, "", HUMAN) == (False, "contradicts_locked_route")

    mixed_contrast = "The explanation cannot change the locked route; the case is ready for submission review." + PAD
    assert validate_enhanced_explanation(mixed_contrast, "", HUMAN) == (False, "contradicts_locked_route")

    other_route = "This case is routed to HOLD_FOR_DOCUMENTATION for the reviewer." + PAD
    assert validate_enhanced_explanation(other_route, "", HUMAN) == (False, "contradicts_locked_route")
    assert validate_enhanced_explanation(other_route, "", HOLD) == (True, "")


def test_direct_route_statements_are_accepted():
    cannot_change = "The explanation cannot change the locked route." + PAD
    assert validate_enhanced_explanation(cannot_change, "", HUMAN) == (True, "")
    assert validate_enhanced_explanation(cannot_change, "", READY) == (True, "")
    not_ready = "The case is not ready for submission review yet." + PAD
    assert validate_enhanced_explanation(not_ready, "", HUMAN) == (True, "")


def test_natural_groq_style_wording_is_still_accepted():
    for text in (
        "Expert rules, configured thresholds, and guardrails locked the route before this explanation was "
        "written, and XGBoost contributed a denial-risk score only.",
        "The Evidence Review Panel is an internal advisory panel, and its findings cannot change the locked route.",
        "A qualified reviewer can review the package and decide whether to submit it if appropriate.",
    ):
        assert validate_enhanced_explanation(text + PAD, "", READY) == (True, "")
