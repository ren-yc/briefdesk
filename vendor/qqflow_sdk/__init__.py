"""Typed Python client for qqflow-server.

Two layers with different ownership:

- :mod:`qqflow_sdk.generated` - request/response models produced from the
  server's OpenAPI description. Never edit by hand; rerun
  ``scripts/regen.py`` and commit the result (CI asserts no diff).
- :mod:`qqflow_sdk.client` - the handwritten behavior layer (readiness
  polling, cursor draining, SSE watching, media retries). This is the API
  most downstreams should use.
"""

from .client import (
    CONNECT_TIMEOUT,
    READ_TIMEOUT,
    STALL_TIMEOUT,
    BadDate,
    Client,
    ClientError,
    MessageEvent,
    NotReady,
    ShapeError,
    StatusError,
    TransportError,
)

__all__ = [
    "CONNECT_TIMEOUT",
    "READ_TIMEOUT",
    "STALL_TIMEOUT",
    "BadDate",
    "Client",
    "ClientError",
    "MessageEvent",
    "NotReady",
    "ShapeError",
    "StatusError",
    "TransportError",
]
