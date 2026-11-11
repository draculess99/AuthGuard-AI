import ast
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1] / "frontend" / "dashboard.py"
SOURCE = DASHBOARD.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _calls(attr: str):
    return [
        node for node in ast.walk(TREE)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr
    ]


def _keyword(call: ast.Call, name: str):
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def test_review_workspace_is_a_persistent_keyed_radio_not_st_tabs():
    assert not _calls("tabs"), "st.tabs() resets to the first tab on every rerun"
    (radio,) = [
        call for call in _calls("radio")
        if _keyword(call, "key") is not None and ast.literal_eval(_keyword(call, "key")) == "active_review_tab"
    ]
    assert ast.literal_eval(_keyword(radio, "horizontal")) is True


def test_review_workspace_keeps_the_same_six_labels_in_order():
    labels = next(
        ast.literal_eval(node.value) for node in TREE.body
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "REVIEW_TABS" for t in node.targets)
    )
    assert labels == [
        "🗣️ Evidence Review Panel", "🧠 Evidence & RAG", "📈 XGBoost", "🛡️ Guardrails",
        "👤 Human Review", "🧾 Memory & Audit",
    ]


def test_selection_resets_only_for_a_new_run():
    assert 'st.session_state.get("active_review_run_id") != result["run_id"]' in SOURCE
    reset = SOURCE.split('st.session_state.get("active_review_run_id") != result["run_id"]:', 1)[1][:200]
    assert 'st.session_state["active_review_tab"] = REVIEW_TABS[0]' in reset


def test_stay_on_human_review_callback_selects_human_review():
    (func,) = [n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == "stay_on_human_review"]
    assert 'st.session_state["active_review_tab"] = HUMAN_REVIEW_TAB' in ast.unparse(func).replace("'", '"')
    assert "HUMAN_REVIEW_TAB = REVIEW_TABS[4]" in SOURCE


def test_every_human_review_and_payment_action_button_keeps_human_review():
    wanted = [
        "Approve package", "Request evidence", "Reject / reroute",
        "Record Synthetic Payment Approval", "Create Sandbox Order", "Capture Sandbox Payment",
    ]
    buttons = {}
    for call in _calls("button"):
        label = ast.literal_eval(call.args[0])
        for name in wanted:
            if name in label:
                buttons[name] = _keyword(call, "on_click")
    assert sorted(buttons) == sorted(wanted)
    assert all(isinstance(v, ast.Name) and v.id == "stay_on_human_review" for v in buttons.values())


def test_human_review_inputs_survive_switching_workspaces():
    assert 'key="review_reviewer"' in SOURCE and 'key="review_rationale"' in SOURCE
    assert "payment_approval_confirm_{result['run_id']}" in SOURCE and "buyer_approved_{result['run_id']}" in SOURCE


def test_order_and_payment_session_keys_are_unchanged():
    for key in (
        "payment_approval_recorded_{result['run_id']}",
        "paypal_order_id_{result['run_id']}",
        "paypal_approval_url_{result['run_id']}",
    ):
        assert key in SOURCE
