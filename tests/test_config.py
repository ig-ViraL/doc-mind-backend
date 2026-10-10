from config import Settings


def test_neon_url_is_normalised_for_asyncpg():
    url = "postgresql://u:p@ep-x-pooler.neon.tech/neondb?channel_binding=require&sslmode=require"
    assert Settings(database_url=url).database_url == "postgresql+asyncpg://u:p@ep-x-pooler.neon.tech/neondb?ssl=require"


def test_plain_url_untouched_apart_from_driver():
    assert Settings(database_url="postgres://u:p@h/db").database_url == "postgresql+asyncpg://u:p@h/db"
