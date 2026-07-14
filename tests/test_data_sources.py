from __future__ import annotations

import json

import pytest

from backend import data_sources
from backend.data_sources import generate_synthetic_cases, load_live_cases, parse_live_content


def test_synthetic_dataset_is_reproducible_and_valid():
    first = generate_synthetic_cases(count=8, seed=7)
    second = generate_synthetic_cases(count=8, seed=7)
    assert first == second
    assert len(first) == 8
    assert all(case["data_source"] == "synthetic_demo" for case in first)
    assert all(case["case_id"] for case in first)


def test_live_csv_upload_is_normalized():
    csv_bytes = b"""request_id,payer_name,service,diagnosis,age,urgent,eligible,network_status,documents_received,documents_required,notes\nLIVE-1,Medicare,Advanced Imaging,Neurologic,66,true,yes,in network,5,5,De-identified record\n"""
    records, metadata = load_live_cases(file_bytes=csv_bytes, filename="cases.csv")
    assert metadata["transport"] == "upload"
    assert metadata["records"] == 1
    assert records[0]["case_id"] == "LIVE-1"
    assert records[0]["urgent"] is True
    assert records[0]["in_network"] is True
    assert records[0]["data_source"] == "live_external"


def test_live_json_upload_is_normalized():
    payload = {
        "cases": [
            {
                "authorization_id": "LIVE-JSON-1",
                "health_plan": "Commercial A",
                "request_type": "Surgery",
                "condition_group": "Orthopedic",
                "age_years": 52,
            }
        ]
    }
    records = parse_live_content(json.dumps(payload).encode(), filename="cases.json")
    assert records[0]["case_id"] == "LIVE-JSON-1"
    assert records[0]["payer"] == "Commercial A"
    assert records[0]["service_type"] == "Surgery"


def test_live_url_loader_uses_http_response(monkeypatch: pytest.MonkeyPatch):
    class FakeResponse:
        content = b"case_id,payer,service_type,diagnosis_group\nURL-1,Medicaid,DME,Other\n"
        headers = {"Content-Type": "text/csv"}

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(data_sources, "validate_live_url", lambda url: url)
    monkeypatch.setattr(data_sources.requests, "get", lambda *args, **kwargs: FakeResponse())
    records, metadata = load_live_cases(url="https://example.org/live.csv")
    assert metadata["transport"] == "url"
    assert records[0]["case_id"] == "URL-1"


def test_live_dataset_rejects_missing_required_schema():
    with pytest.raises(ValueError, match="schema validation"):
        parse_live_content(b"foo,bar\n1,2\n", filename="bad.csv")


def test_live_record_runs_through_pipeline(tmp_path):
    from backend.agents.committee import run_pipeline
    from backend.memory import JSONMemoryStore

    csv_bytes = b"""case_id,payer,service_type,diagnosis_group,age_years,evidence_count,required_document_count\nLIVE-PIPE-1,Commercial A,Advanced Imaging,Neurologic,54,5,5\n"""
    records, _ = load_live_cases(file_bytes=csv_bytes, filename="pipeline.csv")
    result = run_pipeline(records[0], persist=False, memory_store=JSONMemoryStore(tmp_path))
    assert result["data_source"] == "live_external"
    assert result["decision"]
