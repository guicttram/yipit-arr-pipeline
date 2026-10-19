"""CLI wiring for the steps that do not need the embedding model."""

import duckdb

from yipit_pipeline.cli import main


def test_step_commands(settings):
    common = [
        "--data-dir",
        str(settings.data_dir),
        "--warehouse",
        str(settings.warehouse_path),
        "--outputs-dir",
        str(settings.outputs_dir),
        "--log-level",
        "WARNING",
    ]
    for command in ("ingest", "transform", "model"):
        assert main([*common, command]) == 0
    # No embeddings yet: the embedding coverage check is blocking, so dq exits with 1.
    assert main([*common, "dq"]) == 1

    con = duckdb.connect(str(settings.warehouse_path), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM fact_arr_observation").fetchone()[0] == 10
    finally:
        con.close()
