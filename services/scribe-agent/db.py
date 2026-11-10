"""Postgres storage for note drafts and the PHI access audit log.

Connection settings come from DB_* environment variables, which the Helm chart
fills from its database input values.
"""

import json
import os
import uuid

from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

SCHEMA = """
CREATE TABLE IF NOT EXISTS scribe_drafts (
    id UUID PRIMARY KEY,
    patient_id TEXT NOT NULL,
    transcript TEXT NOT NULL,
    note TEXT NOT NULL,
    tool_calls JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_by TEXT,
    approved_at TIMESTAMPTZ,
    fhir_reference TEXT
);
CREATE TABLE IF NOT EXISTS scribe_audit (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    patient_id TEXT,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS scribe_audit_patient_idx ON scribe_audit (patient_id, ts DESC);
"""


def conninfo() -> str:
    return make_conninfo(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ.get("DB_PASSWORD", ""),
        sslmode=os.environ.get("DB_SSLMODE", "prefer"),
    )


pool = AsyncConnectionPool(conninfo(), min_size=1, max_size=10, open=False, kwargs={"row_factory": dict_row})


async def init() -> None:
    await pool.open()
    async with pool.connection() as conn:
        await conn.execute(SCHEMA)


async def audit(actor: str, event: str, patient_id: str | None, detail: dict) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO scribe_audit (actor, event, patient_id, detail) VALUES (%s, %s, %s, %s)",
            (actor, event, patient_id, json.dumps(detail, default=str)),
        )


async def create_draft(patient_id: str, transcript: str, note: str, tool_calls: list) -> str:
    draft_id = str(uuid.uuid4())
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO scribe_drafts (id, patient_id, transcript, note, tool_calls) VALUES (%s, %s, %s, %s, %s)",
            (draft_id, patient_id, transcript, note, json.dumps(tool_calls, default=str)),
        )
    return draft_id


async def get_draft(draft_id: str) -> dict | None:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM scribe_drafts WHERE id = %s", (draft_id,))
        return await cur.fetchone()


async def approve_draft(draft_id: str, note: str, approved_by: str, fhir_reference: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE scribe_drafts SET note = %s, status = 'signed', approved_by = %s, "
            "approved_at = now(), fhir_reference = %s WHERE id = %s",
            (note, approved_by, fhir_reference, draft_id),
        )


async def list_audit(patient_id: str | None, limit: int = 100) -> list[dict]:
    async with pool.connection() as conn:
        if patient_id:
            cur = await conn.execute(
                "SELECT * FROM scribe_audit WHERE patient_id = %s ORDER BY ts DESC LIMIT %s", (patient_id, limit)
            )
        else:
            cur = await conn.execute("SELECT * FROM scribe_audit ORDER BY ts DESC LIMIT %s", (limit,))
        return await cur.fetchall()
