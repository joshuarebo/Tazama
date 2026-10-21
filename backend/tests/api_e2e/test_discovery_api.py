"""Data Explorer and catalog endpoints: authorisation, refusals, masking, audit.

These are the endpoints that accept SQL, so the tests here are as much about what is
*refused* as about what works.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tazama.infrastructure.db.models import Cell, Complaint, Site

T0 = datetime(2026, 3, 17, 6, 0, tzinfo=UTC)


@pytest.fixture
def dataset(db_session):
    session = db_session
    session.add(
        Site(
            id="SITE-NRB-001",
            name="Westlands Macro",
            region="Nairobi",
            county="Nairobi",
            lat=-1.2653,
            lon=36.8039,
            site_type="macro",
        ),
    )
    session.flush()
    session.add(
        Cell(
            id="CELL-NRB-001",
            site_id="SITE-NRB-001",
            name="Westlands 1",
            tech="4G",
            band="L1800",
            region="Nairobi",
            lat=-1.2653,
            lon=36.8039,
        ),
    )
    for index in range(4):
        session.add(
            Complaint(
                id=f"CMP-{index:04d}",
                received_at=T0 + timedelta(hours=index),
                channel="call_centre",
                language="en",
                raw_text="no service in Westlands",
                subscriber_id="SUB-AAAAAAAAAAAAAAAA",
                region="Nairobi",
                cell_id="CELL-NRB-001" if index < 2 else None,
                category="no_service",
            ),
        )
    session.commit()
    return session


# -------------------------------------------------------------- catalog


def test_catalog_requires_a_capability(client, dataset) -> None:
    assert client.get("/api/v1/discovery/catalog").status_code == 401


def test_care_agent_cannot_read_the_catalog(client, dataset, auth_headers) -> None:
    response = client.get("/api/v1/discovery/catalog", headers=auth_headers("care_agent"))
    assert response.status_code == 403
    assert response.json()["type"].endswith("/forbidden")


def test_analyst_reads_the_catalog(client, dataset, auth_headers) -> None:
    response = client.get("/api/v1/discovery/catalog", headers=auth_headers("analyst"))
    assert response.status_code == 200
    body = response.json()
    assert body["app_mode"] == "demo"
    assert body["db_backend"] == "sqlite"
    complaint = next(t for t in body["tables"] if t["name"] == "complaint")
    assert complaint["row_count"] == 4
    assert "raw_text" in complaint["pii_columns"]
    assert complaint["app_owned"] is False  # it has a source mapping
    assert body["coverage"]["complaint_to_cell_pct"] == 50.0


def test_profile_one_table(client, dataset, auth_headers) -> None:
    response = client.get("/api/v1/discovery/catalog/cell", headers=auth_headers("analyst"))
    assert response.status_code == 200
    assert response.json()["row_count"] == 1


def test_profile_unknown_table_is_404(client, dataset, auth_headers) -> None:
    response = client.get("/api/v1/discovery/catalog/wat", headers=auth_headers("analyst"))
    assert response.status_code == 404


def test_join_coverage_endpoint(client, dataset, auth_headers) -> None:
    response = client.get(
        "/api/v1/discovery/join-coverage",
        headers=auth_headers("noc_engineer"),
    )
    body = response.json()
    row = next(r for r in body["rows"] if r["question"] == "complaint -> cell")
    assert (row["resolved"], row["total"]) == (2, 4)


def test_mapping_validation_endpoint_is_clean(client, dataset, auth_headers) -> None:
    body = client.get("/api/v1/discovery/mappings", headers=auth_headers("analyst")).json()
    assert body["ok"] is True
    assert body["mode"] == "demo"
    assert [i for i in body["issues"] if i["severity"] == "error"] == []


def test_tables_endpoint_lists_the_schema(client, dataset, auth_headers) -> None:
    tables = client.get("/api/v1/discovery/tables", headers=auth_headers("analyst")).json()
    assert "complaint" in tables and "kpi_cell" in tables


# ------------------------------------------------------------- explorer


def test_analyst_runs_a_safe_query(client, dataset, auth_headers) -> None:
    response = client.post(
        "/api/v1/discovery/query",
        json={"sql": "select id, region from complaint order by id"},
        headers=auth_headers("analyst"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["row_count"] == 4
    assert [c["name"] for c in body["columns"]] == ["id", "region"]
    assert body["truncated"] is False
    assert "limit" in body["executed_sql"].lower()


@pytest.mark.parametrize(
    "sql",
    [
        "delete from complaint",
        "update complaint set region = 'x'",
        "drop table complaint",
        "select id from complaint; drop table complaint",
        "select id from complaint -- comment",
        "select * from sqlite_master",
        "select id from audit_log",  # not in the explorer allowlist
    ],
)
def test_dangerous_queries_are_refused(client, dataset, auth_headers, sql: str) -> None:
    response = client.post(
        "/api/v1/discovery/query",
        json={"sql": sql},
        headers=auth_headers("analyst"),
    )
    assert response.status_code == 400, sql
    problem = response.json()
    assert problem["type"].endswith("/query-rejected")
    assert problem["detail"]  # the analyst is told which rule fired


def test_select_star_on_a_pii_table_is_refused(client, dataset, auth_headers) -> None:
    response = client.post(
        "/api/v1/discovery/query",
        json={"sql": "select * from complaint"},
        headers=auth_headers("analyst"),
    )
    assert response.status_code == 400


def test_pii_columns_are_masked(client, dataset, auth_headers) -> None:
    response = client.post(
        "/api/v1/discovery/query",
        json={"sql": "select id, raw_text from complaint order by id"},
        headers=auth_headers("analyst"),
    )
    assert response.status_code == 200
    body = response.json()
    masked = [c for c in body["columns"] if c["masked"]]
    assert [c["name"] for c in masked] == ["raw_text"]
    for row in body["rows"]:
        assert row[1] != "no service in Westlands"


def test_row_cap_is_enforced(client, dataset, auth_headers) -> None:
    response = client.post(
        "/api/v1/discovery/query",
        json={"sql": "select id from complaint order by id", "max_rows": 2},
        headers=auth_headers("analyst"),
    )
    body = response.json()
    assert body["row_count"] == 2
    assert body["truncated"] is True
    assert any("truncated" in w.lower() for w in body["warnings"])


def test_explain_does_not_execute(client, dataset, auth_headers) -> None:
    response = client.post(
        "/api/v1/discovery/query/explain",
        json={"sql": "select id from kpi_cell"},
        headers=auth_headers("analyst"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["tables"] == ["kpi_cell"]
    assert body["estimated_full_scan"] is True
    assert body["warnings"]


def test_noc_engineer_cannot_query(client, dataset, auth_headers) -> None:
    response = client.post(
        "/api/v1/discovery/query",
        json={"sql": "select id from complaint"},
        headers=auth_headers("noc_engineer"),
    )
    assert response.status_code == 403


def test_csv_export_is_masked_and_safely_named(client, dataset, auth_headers) -> None:
    response = client.post(
        "/api/v1/discovery/query/export",
        json={"sql": "select id, raw_text from complaint order by id"},
        headers=auth_headers("analyst"),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="tazama-export-')
    assert ".." not in disposition and "/" not in disposition.split("filename=")[1]
    assert "no service in Westlands" not in response.text
    assert response.text.splitlines()[0] == "id,raw_text"


def test_refused_and_accepted_queries_are_both_audited(client, dataset, auth_headers) -> None:
    headers = auth_headers("analyst", subject="amina")
    client.post(
        "/api/v1/discovery/query",
        json={"sql": "select id from complaint"},
        headers=headers,
    )
    client.post(
        "/api/v1/discovery/query",
        json={"sql": "drop table complaint"},
        headers=headers,
    )
    history = client.get("/api/v1/discovery/query/history", headers=headers).json()
    assert len(history) == 2
    accepted = [h for h in history if h["accepted"]]
    refused = [h for h in history if not h["accepted"]]
    assert len(accepted) == 1
    assert len(refused) == 1
    assert refused[0]["reject_reason"]


def test_history_is_scoped_to_the_caller(client, dataset, auth_headers) -> None:
    client.post(
        "/api/v1/discovery/query",
        json={"sql": "select id from complaint"},
        headers=auth_headers("analyst", subject="amina"),
    )
    other = client.get(
        "/api/v1/discovery/query/history",
        headers=auth_headers("analyst", subject="brian"),
    ).json()
    assert other == []


# -------------------------------------------------------- saved queries


def test_save_and_fetch_a_query(client, dataset, auth_headers) -> None:
    headers = auth_headers("analyst", subject="amina")
    created = client.post(
        "/api/v1/discovery/saved-queries",
        json={"name": "complaints by region", "sql": "select region from complaint"},
        headers=headers,
    )
    assert created.status_code == 201
    query_id = created.json()["id"]

    fetched = client.get(f"/api/v1/discovery/saved-queries/{query_id}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "complaints by region"


def test_saving_an_unsafe_query_is_refused(client, dataset, auth_headers) -> None:
    response = client.post(
        "/api/v1/discovery/saved-queries",
        json={"name": "sneaky", "sql": "drop table complaint"},
        headers=auth_headers("analyst"),
    )
    assert response.status_code == 400


def test_another_analysts_private_query_is_not_readable(client, dataset, auth_headers) -> None:
    created = client.post(
        "/api/v1/discovery/saved-queries",
        json={"name": "mine", "sql": "select id from complaint"},
        headers=auth_headers("analyst", subject="amina"),
    )
    query_id = created.json()["id"]

    # Object-level authorisation: a known id must still be refused (no IDOR).
    response = client.get(
        f"/api/v1/discovery/saved-queries/{query_id}",
        headers=auth_headers("analyst", subject="brian"),
    )
    assert response.status_code == 404


def test_shared_query_is_visible_to_colleagues(client, dataset, auth_headers) -> None:
    created = client.post(
        "/api/v1/discovery/saved-queries",
        json={"name": "team view", "sql": "select id from complaint", "shared": True},
        headers=auth_headers("analyst", subject="amina"),
    )
    query_id = created.json()["id"]
    response = client.get(
        f"/api/v1/discovery/saved-queries/{query_id}",
        headers=auth_headers("analyst", subject="brian"),
    )
    assert response.status_code == 200


def test_delete_only_your_own_query(client, dataset, auth_headers) -> None:
    created = client.post(
        "/api/v1/discovery/saved-queries",
        json={"name": "mine", "sql": "select id from complaint"},
        headers=auth_headers("analyst", subject="amina"),
    )
    query_id = created.json()["id"]

    assert (
        client.delete(
            f"/api/v1/discovery/saved-queries/{query_id}",
            headers=auth_headers("analyst", subject="brian"),
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/api/v1/discovery/saved-queries/{query_id}",
            headers=auth_headers("analyst", subject="amina"),
        ).status_code
        == 204
    )


def test_oversized_sql_is_rejected_by_validation(client, dataset, auth_headers) -> None:
    response = client.post(
        "/api/v1/discovery/query",
        json={"sql": "select id from complaint where id = '" + "x" * 25_000 + "'"},
        headers=auth_headers("analyst"),
    )
    # Pydantic bounds reject it before the guard even sees it.
    assert response.status_code == 422
