#!/bin/bash
# Lambda entry point behind the Lambda Web Adapter. Access log off so no header is ever logged.
PATH=/var/runtime:$PATH
export PYTHONPATH=/var/task:$PYTHONPATH
exec python -m uvicorn gateway.app:app --host 127.0.0.1 --port "${PORT:-8080}" --no-access-log --log-level warning
