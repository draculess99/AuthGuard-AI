from backend.expert_system import evaluate_rules


def test_missing_documents_create_blocker():
    result = evaluate_rules({
        "required_document_count": 5,
        "evidence_count": 2,
        "member_eligible": True,
        "in_network": True,
        "guideline_min_weeks": 6,
        "conservative_therapy_weeks": 1,
        "failed_conservative_therapy": False,
        "specialist_order": False,
        "estimated_cost": 1000,
        "previous_denials": 0,
    })
    assert result["missing_document_count"] == 3
    assert len(result["blockers"]) >= 2
