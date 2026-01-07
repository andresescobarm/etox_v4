#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Celery app for background translation jobs.
Handles heavy workloads asynchronously. 
"""

import os
from urllib.parse import urlparse
from celery import Celery


def _redis_db_url(redis_url: str, db_num: int) -> str:
    """Build a Redis URL with the provided DB, preserving auth/host/port."""
    parsed = urlparse(redis_url)
    netloc = parsed.netloc
    if not netloc and parsed.path:
        # Handle redis://host:port/db without netloc parsing edge cases
        netloc = parsed.path
    return parsed._replace(path=f"/{db_num}", netloc=netloc).geturl()

redis_url = os.getenv("REDIS_URL")
default_broker = "redis://localhost:6379/1"
default_backend = "redis://localhost:6379/2"

if redis_url:
    default_broker = _redis_db_url(redis_url, 1)
    default_backend = _redis_db_url(redis_url, 2)

# Create Celery app
celery_app = Celery(
    "etos_backend",
    broker=os.getenv("CELERY_BROKER_URL", default_broker),
    backend=os.getenv("CELERY_RESULT_BACKEND", default_backend),
)

# Configuration
celery_app.conf. update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,
    task_soft_time_limit=240,
    worker_prefetch_multiplier=1,
    worker_max_tasks_per_child=50,
)

# Auto-discover tasks from the backend package
celery_app.autodiscover_tasks(['backend.celery_tasks'], force=True)

print("✅ Celery app configured")
