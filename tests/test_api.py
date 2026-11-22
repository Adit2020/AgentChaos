"""Offline HTTP tests for the read-only artifact API."""

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentchaos import api
from agentchaos.artifacts import export_experiment, export_multi_run, load_export
from agentchaos.multi_run import summarize_repetitions
from tests.test_artifacts import sample_experiment, sample_outcome


def multi_export(root: Path) -> Path:
    report = summarize_repetitions(
        (sample_outcome(1), sample_outcome(2)),
        requested_repetitions=4,
        incomplete_pairs=1,
        stop_reason="error",
    )
    return export_multi_run(report, experiment=sample_experiment(), root=root)


def change_json(path: Path, change: dict[str, object]) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(change)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_health_and_openapi_docs(tmp_path: Path) -> None:
    client = TestClient(api.create_app(tmp_path))

    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/docs").status_code == 200
    assert "/api/exports/{export_id}" in client.get("/openapi.json").json()["paths"]


def test_empty_artifact_directory(tmp_path: Path) -> None:
    client = TestClient(api.create_app(tmp_path / "missing"))

    assert client.get("/api/exports").json() == {"exports": []}
    response = client.get("/api/exports/unknown")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "export_not_found"


def test_factory_uses_configured_artifact_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = export_experiment(sample_outcome(), root=tmp_path)
    monkeypatch.setenv("AGENTCHAOS_ARTIFACT_ROOT", str(tmp_path))
    client = TestClient(api.create_app())

    assert client.get("/api/exports").json()["exports"][0]["export_id"] == (
        directory.name
    )


def test_listing_single_and_multi_exports_is_ordered_and_filterable(
    tmp_path: Path,
) -> None:
    single = export_experiment(sample_outcome(), root=tmp_path)
    multi = multi_export(tmp_path)
    client = TestClient(api.create_app(tmp_path))

    response = client.get("/api/exports")
    assert response.status_code == 200
    exports = response.json()["exports"]
    assert {item["export_id"] for item in exports} == {single.name, multi.name}
    assert {item["artifact_type"] for item in exports} == {"experiment", "multi_run"}
    assert all("manifest" in item for item in exports)
    assert [item["exported_at"] for item in exports] == sorted(
        [item["exported_at"] for item in exports], reverse=True
    )
    assert [
        item["export_id"]
        for item in client.get(
            "/api/exports", params={"artifact_type": "experiment"}
        ).json()["exports"]
    ] == [single.name]
    assert [
        item["export_id"]
        for item in client.get(
            "/api/exports", params={"artifact_type": "multi_run"}
        ).json()["exports"]
    ] == [multi.name]


def test_listing_does_not_load_execution_traces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_experiment(sample_outcome(), root=tmp_path)

    def should_not_load(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("listing loaded a trace")

    monkeypatch.setattr(api, "load_export", should_not_load)
    monkeypatch.setattr(api, "load_multi_run_export", should_not_load)
    client = TestClient(api.create_app(tmp_path))

    assert len(client.get("/api/exports").json()["exports"]) == 1


def test_detail_loads_single_run_and_preserves_unknowns(tmp_path: Path) -> None:
    directory = export_experiment(sample_outcome(), root=tmp_path)
    client = TestClient(api.create_app(tmp_path))

    response = client.get(f"/api/exports/{directory.name}")

    assert response.status_code == 200
    body = response.json()
    assert body["artifact_type"] == "experiment"
    assert body["export_id"] == directory.name
    assert body["artifact"]["manifest"]["artifact_type"] == "experiment"
    assert body["artifact"]["baseline"]["run_id"] == "baseline-1"
    assert body["artifact"]["comparison"]["recovered"] is None
    assert body["artifact"]["comparison"]["faulted_task_success"] is None


def test_detail_loads_multi_run_with_partial_states(tmp_path: Path) -> None:
    directory = multi_export(tmp_path)
    client = TestClient(api.create_app(tmp_path))

    response = client.get(f"/api/exports/{directory.name}")

    assert response.status_code == 200
    body = response.json()
    assert body["artifact_type"] == "multi_run"
    assert body["artifact"]["summary"]["completed_repetitions"] == 2
    repetitions = body["artifact"]["repetitions"]
    assert [item["state"] for item in repetitions] == [
        "recorded",
        "recorded",
        "incomplete",
        "not_started",
    ]
    assert repetitions[0]["outcome"]["comparison"]["recovered"] is None
    assert repetitions[2]["outcome"] is None
    assert repetitions[3]["outcome"] is None


def test_invalid_ids_and_query_parameters_have_sanitized_errors(tmp_path: Path) -> None:
    client = TestClient(api.create_app(tmp_path))

    invalid = client.get("/api/exports/..%5Coutside")
    assert invalid.status_code == 400
    assert invalid.json() == {
        "error": {
            "code": "unsafe_artifact_path",
            "message": "Invalid or unsafe artifact path.",
        }
    }
    assert (
        client.get("/api/exports", params={"artifact_type": "unknown"}).json()["error"][
            "code"
        ]
        == "invalid_request"
    )


def test_malformed_and_unsupported_artifacts_do_not_leak_paths(
    tmp_path: Path,
) -> None:
    directory = export_experiment(sample_outcome(), root=tmp_path)
    client = TestClient(api.create_app(tmp_path))
    (directory / "comparison.json").write_text("{bad", encoding="utf-8")

    malformed = client.get(f"/api/exports/{directory.name}")
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "invalid_artifact"
    assert str(tmp_path) not in malformed.text

    change_json(directory / "manifest.json", {"schema_version": 2})
    unsupported = client.get(f"/api/exports/{directory.name}")
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["code"] == "unsupported_artifact"
    assert str(tmp_path) not in unsupported.text


def test_missing_required_document_has_sanitized_404(tmp_path: Path) -> None:
    directory = export_experiment(sample_outcome(), root=tmp_path)
    (directory / "faulted.json").unlink()
    client = TestClient(api.create_app(tmp_path))

    response = client.get(f"/api/exports/{directory.name}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "export_not_found"
    assert str(tmp_path) not in response.text


def test_export_symlink_escape_is_rejected_without_path_leak(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    directory = export_experiment(sample_outcome(), root=outside)
    (root / directory.name).symlink_to(directory, target_is_directory=True)
    client = TestClient(api.create_app(root))

    response = client.get(f"/api/exports/{directory.name}")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsafe_artifact_path"
    assert str(tmp_path) not in response.text


def test_repetition_file_symlink_escape_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    directory = multi_export(root)
    repetition = directory / "repetitions" / "001"
    outside = tmp_path / "outside.json"
    shutil.copyfile(repetition / "baseline.json", outside)
    (repetition / "baseline.json").unlink()
    (repetition / "baseline.json").symlink_to(outside)
    client = TestClient(api.create_app(root))

    response = client.get(f"/api/exports/{directory.name}")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsafe_artifact_path"
    assert str(tmp_path) not in response.text


def test_ambiguous_export_ids_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = export_experiment(sample_outcome(), root=tmp_path)
    artifact = load_export(directory.name, root=tmp_path)
    monkeypatch.setattr(api, "load_export", lambda *_args, **_kwargs: artifact)
    monkeypatch.setattr(
        api, "load_multi_run_export", lambda *_args, **_kwargs: artifact
    )
    client = TestClient(api.create_app(tmp_path))

    response = client.get(f"/api/exports/{directory.name}")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ambiguous_export"
