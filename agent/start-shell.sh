#!/bin/sh
# The wrong way, on purpose: without exec, this shell stays PID 1 and uvicorn is its
# child. PID 1 ignores SIGTERM, so the server never hears it, keeps taking turns, and
# is killed mid-answer when the grace period runs out.
uvicorn app:app --host 0.0.0.0 --port 8080 --no-access-log
echo "server exited"
