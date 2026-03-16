import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

async def init_db_pool():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL が設定されていません")

    return await asyncpg.create_pool(
        dsn=database_url,
        ssl="require"
    )