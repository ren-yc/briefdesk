"""Typed Python client for weflow-server.

Two layers with different ownership:

- :mod:`weflow_sdk.generated` - request/response models produced from the
  server's OpenAPI description. Never edit by hand; rerun
  ``scripts/regen.py`` and commit the result (CI asserts no diff).
- :mod:`weflow_sdk.client` - the handwritten behavior layer (readiness
  polling, cursor draining, SSE watching, media retries). This is the API
  most downstreams should use.
"""

from .client import BadDate, Client, ClientError, NotReady, ShapeError, StatusError

__all__ = [
    "Client",
    "ClientError",
    "StatusError",
    "ShapeError",
    "NotReady",
    "BadDate",
]
