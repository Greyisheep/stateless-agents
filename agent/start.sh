#!/bin/sh
# The right way: exec makes uvicorn PID 1, so it hears SIGTERM, stops taking new turns
# and finishes the ones in flight before it exits.
exec uvicorn app:app --host 0.0.0.0 --port 8080 --timeout-graceful-shutdown 25 --no-access-log
