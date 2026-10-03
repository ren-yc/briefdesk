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
    BadDate,
    Client,
    ClientError,
    MessageEvent,
    NotReady,
    ShapeError,
    StatusError,
)

__all__ = [
    "BadDate",
    "Client",
    "ClientError",
    "MessageEvent",
    "NotReady",
    "ShapeError",
    "StatusError",
]
