from app.config import Settings


def test_hostport_wins_over_docker_database_url():
    cfg = Settings(
        database_url="postgresql+asyncpg://assistant:assistant@postgres:5432/assistant",
        database_hostport="checkme-postgres-abc:5432",
        postgres_user="assistant",
        postgres_password="secret",
        postgres_db="assistant",
    )
    assert "checkme-postgres-abc:5432" in cfg.async_database_url
    assert "@postgres:" not in cfg.async_database_url
    assert cfg.database_host == "checkme-postgres-abc:5432"


def test_hostport_is_used_even_without_password():
    cfg = Settings(
        database_url="postgresql://assistant:old@postgres:5432/assistant",
        database_hostport="srv-internal:5432",
        postgres_user="assistant",
        postgres_password="",
        postgres_db="assistant",
    )
    assert "srv-internal:5432" in cfg.async_database_url
