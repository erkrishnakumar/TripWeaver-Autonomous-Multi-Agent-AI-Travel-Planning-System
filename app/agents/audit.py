"""
Audit-log helpers for TripPlanningFlow's Celery-driven, resumable stages
(research, plan). app/db/models/audit_log.py's AuditLog table already
exists with exactly the right shape (trip_id, event_type, payload: JSONB)
for this -- these functions are the ONE place that reads/writes it for
this purpose, so the event_type naming convention and payload shape can't
drift between call sites.

Event type convention: "{stage}_started" / "{stage}_completed" /
"{stage}_failed", e.g. "research_started", "research_completed",
"research_failed". A "_completed" entry's payload is that stage's actual
output (e.g. a ResearchOutput's model_dump()) -- that's what makes
resuming possible: see get_last_completed_payload() below.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog


async def log_stage_event(
    session: AsyncSession,
    trip_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Writes one AuditLog row and flushes -- does not commit; the caller
    controls the transaction boundary, same convention as create_trip().

    sequence is computed here in plain Python arithmetic, not left to a
    DB/clock-based default -- verified live that BOTH created_at
    (wall-clock) and time.monotonic_ns() returned IDENTICAL values for two
    back-to-back writes on this machine (Windows clock resolution is
    coarser than either name implies). A per-trip MAX(sequence)+1 sidesteps
    OS clock resolution entirely. Relies on the same invariant
    get_last_completed_payload() documents: one trip's audit events are
    always written sequentially by whichever single worker is currently
    processing that trip, never concurrently by two workers for the same
    trip_id -- if that's ever violated, this needs a real row lock
    (SELECT ... FOR UPDATE), not a plain MAX query."""
    result = await session.execute(
        select(func.max(AuditLog.sequence)).where(AuditLog.trip_id == uuid.UUID(trip_id))
    )
    next_sequence = (result.scalar_one_or_none() or 0) + 1
    session.add(
        AuditLog(
            trip_id=uuid.UUID(trip_id),
            event_type=event_type,
            payload=payload or {},
            sequence=next_sequence,
        )
    )
    await session.flush()


async def get_last_completed_payload(
    session: AsyncSession, trip_id: str, stage: str
) -> dict[str, Any] | None:
    """Returns the payload of the most recent "{stage}_completed" AuditLog
    entry for this trip, or None if that stage hasn't completed yet. This
    IS the resumability check: a non-None result means the caller should
    skip re-running that stage and use this payload instead."""
    result = await session.execute(
        select(AuditLog)
        .where(
            AuditLog.trip_id == uuid.UUID(trip_id),
            AuditLog.event_type == f"{stage}_completed",
        )
        .order_by(AuditLog.sequence.desc())
        .limit(1)
    )
    entry = result.scalar_one_or_none()
    return entry.payload if entry is not None else None
