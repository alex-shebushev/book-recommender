"""Ship application logs to AWS CloudWatch Logs (optional).

Enabled when ``CLOUDWATCH_LOG_GROUP`` is set *and* AWS credentials are available
to boto3 — in the container those come from the injected ``AWS_*`` env vars. When
it is unset (local dev), logging stays on stdout only, which ``docker logs`` and
local runs already capture. Any failure to reach CloudWatch is swallowed so it
can never take the app down. Safe to call more than once.

Env:
  CLOUDWATCH_LOG_GROUP   e.g. "/bookrec/app"  (required to enable)
  CLOUDWATCH_LOG_STREAM  stream name          (default: the hostname)
  AWS_REGION             region               (default: us-east-1)
  LOG_LEVEL              root log level       (default: INFO)
"""
from __future__ import annotations

import logging
import os
import socket

_configured = False

# uvicorn's own loggers, so HTTP request logs reach CloudWatch too.
_LOGGERS = ("", "uvicorn", "uvicorn.access", "uvicorn.error")


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(level=level)  # keep stdout logging as well
    log = logging.getLogger(__name__)

    log_group = os.getenv("CLOUDWATCH_LOG_GROUP")
    if not log_group:
        log.info("CloudWatch logging disabled (CLOUDWATCH_LOG_GROUP unset)")
        return

    try:
        import boto3
        import watchtower
    except ModuleNotFoundError:
        log.warning("watchtower/boto3 not installed; CloudWatch logging off")
        return

    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
    stream = os.getenv("CLOUDWATCH_LOG_STREAM") or socket.gethostname()

    try:
        client = boto3.client("logs", region_name=region)
        handler = watchtower.CloudWatchLogHandler(
            log_group_name=log_group,
            log_stream_name=stream,
            boto3_client=client,
            create_log_group=False,  # the group is pre-created by ops
        )
    except Exception as e:  # noqa: BLE001 — never let logging setup crash the app
        log.warning("CloudWatch logging unavailable: %s", e)
        return

    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    for name in _LOGGERS:
        logging.getLogger(name).addHandler(handler)

    log.info("CloudWatch logging enabled -> group=%s stream=%s region=%s",
             log_group, stream, region)
