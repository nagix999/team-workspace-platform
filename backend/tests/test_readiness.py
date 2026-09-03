from __future__ import annotations

from sqlalchemy import text


def test_readyz_requires_exact_database_revision(app_env):
    app, _hub, client = app_env

    missing = client.get("/readyz")
    assert missing.status_code == 503, missing.text
    assert missing.json()["error"]["code"] == "DATABASE_SCHEMA_NOT_READY"

    with app.state.engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('0003')")
        )
    outdated = client.get("/readyz")
    assert outdated.status_code == 503, outdated.text
    assert outdated.json()["error"]["code"] == "DATABASE_SCHEMA_NOT_READY"

    with app.state.engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = '0007'"))
    ready = client.get("/readyz")
    assert ready.status_code == 200, ready.text
    assert ready.json()["status"] == "ready"

    with app.state.engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            == "0007"
        )
