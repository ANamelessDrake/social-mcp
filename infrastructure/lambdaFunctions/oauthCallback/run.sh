#!/bin/bash
# Started by Lambda Web Adapter (AWS_LAMBDA_EXEC_WRAPPER=/opt/bootstrap).
exec python -m uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"
