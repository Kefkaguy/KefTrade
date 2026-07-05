from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row

from app.settings import settings


def get_connection() -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.database_url, row_factory=dict_row, connect_timeout=3) as conn:
        yield conn


def connect() -> psycopg.Connection:
    return psycopg.connect(settings.database_url, row_factory=dict_row, connect_timeout=3)
