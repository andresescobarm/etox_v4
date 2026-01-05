#!/bin/bash
cd /Users/upsomedia/Desktop/etox_v2/backend
source .env
source venv/bin/activate
PYTHONPATH=/Users/upsomedia/Desktop/etox_v2/backend celery -A celery_app worker --loglevel=info