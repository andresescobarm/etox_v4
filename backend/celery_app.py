#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Celery app for background translation jobs.
Handles heavy workloads asynchronously. 
"""

import os
from celery import Celery

# Create Celery app
celery_app = Celery(
    "etos_backend",
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1"),
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")
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

# Auto-discover tasks from celery_tasks module
celery_app.autodiscover_tasks(['celery_tasks'], force=True)

print("✅ Celery app configured")