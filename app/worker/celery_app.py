"""
Celery application instance — the entry point a worker process
(`celery -A app.worker.celery_app worker`) loads, and the object used
elsewhere to define/enqueue tasks (see app/worker/tasks.py).

Points at the same Valkey instance for both broker and result backend
(app/config.py's celery_broker_url/celery_result_backend) -- Valkey is
protocol-compatible with Redis, so Celery's redis:// support works against
it with no changes needed.
"""

from __future__ import annotations

from celery import Celery

from app.config import settings

celery_app = Celery(
    "tripweaver",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Ack AFTER a task finishes, not when it's received (Celery's default
    # with some brokers) -- if a worker process crashes mid-task, an
    # un-acked task goes back on the queue for another worker to pick up,
    # instead of silently vanishing. Matters a lot here specifically: a
    # task that's mid-way through a multi-minute Flow run (real Duffel
    # calls, real DB writes) disappearing silently on a worker crash would
    # be a much worse failure than occasionally re-running one.
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)
