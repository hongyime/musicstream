import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine


@pytest.mark.parametrize("query", [
    "SELECT count(*) FROM download_attempts "
    "WHERE success IS TRUE AND attempted_at > '2026-09-01'",
    "SELECT max(attempted_at) FROM download_attempts WHERE success IS TRUE",
])
def test_success_history_queries_use_time_index(engine: Engine, query: str) -> None:
    with engine.connect() as connection:
        plan = connection.execute(text(f"EXPLAIN QUERY PLAN {query}")).all()
    assert any("idx_download_attempts_success_at" in row[3] for row in plan), (
        "Health success-history queries must avoid scanning the full attempt log"
    )
