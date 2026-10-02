"""Handwritten behavior layer over :mod:`weflow_sdk.generated`.

The generated face covers shapes: request URLs, response models, error
decoding. It cannot cover behavior - pagination loops, reconnection,
readiness polling - and its operations carry no query parameters (the
description currently declares only responses), so every request here
builds its own query string and reuses the generated models purely for
deserialization. The transport is ``httpx.AsyncClient``; the generated
``api_client`` stays in the tree as the typed request face but the
behavior layer does not route through it.

Mirrors ``clients/rust/src/client.rs`` method by method, including the
failure semantics: 503 while indexing is *waiting*, not an error.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Optional

import httpx
from pydantic import ValidationError

from .generated.weflow_sdk import models as gen

log = logging.getLogger(__name__)

# 200-with-refusal vocabulary across the two servers: the weflow server
# currently emits only account_conflict, the qqflow sibling adds
# invalid_key / invalid_db_path / unknown_qq. One shared set keeps the two
# SDKs symmetric against future server-side additions.
_REFUSAL_STATES = frozenset({
    "account_conflict",
    "invalid_key",
    "invalid_db_path",
    "unknown_qq",
})

# SSE framing is byte-level: U+0085/U+2028/U+2029 are legal *inside* JSON
# bodies, and str.splitlines (what ``aiter_lines`` uses) would split the
# body there and corrupt the frame. Only LF terminates a line; a lone CR is
# stripped for CRLF servers. The cap bounds a malformed stream that never
# emits a blank line - without it, one such stream grows the buffer without
# bound.
_SSE_BUFFER_CAP = 1 << 20


class ClientError(Exception):
    """Base error for the behavior layer."""


class StatusError(ClientError):
    """The server answered, but not with a usable body."""

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status} on {url}")
        self.status = status
        self.url = url


class ShapeError(ClientError):
    """The body did not match the documented shape."""


class NotReady(ClientError):
    """``wait_ready``/``ensure_ready`` hit its deadline before the account reached ready."""

    def __init__(self, timeout: float, last_state: str) -> None:
        super().__init__(
            f"account did not become ready within {timeout}s (last state: {last_state})"
        )
        self.timeout = timeout
        self.last_state = last_state


class BadDate(ClientError):
    """``search`` was given a malformed YYYYMMDD bound."""


@dataclass(frozen=True)
class _SseFrame:
    event: Optional[str]
    data: Optional[str]
    id_: Optional[str]


def _parse_sse_block(block: bytes) -> Optional[_SseFrame]:
    event = data = id_ = None
    for raw in block.split(b"\n"):
        raw = raw.rstrip(b"\r")
        if raw.startswith(b":"):
            continue  # heartbeat comment
        if raw.startswith(b"id:"):
            id_ = raw[3:].strip().decode("utf-8", "replace")
        elif raw.startswith(b"event:"):
            event = raw[6:].strip().decode("utf-8", "replace")
        elif raw.startswith(b"data:"):
            data = raw[5:].strip().decode("utf-8", "replace")
    if data is None:
        return None
    return _SseFrame(event, data, id_)


class Client:
    """Async client for one weflow-server instance.

    ``token`` is the API token the server printed on first start (or
    ``--show-token``). Auth goes in the ``Authorization`` header only -
    never in the URL.
    """

    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self._http = httpx.AsyncClient(timeout=timeout)
        self._base = base_url.rstrip("/")
        self._token = token

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "Client":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ---- plumbing -------------------------------------------------------

    def _url(self, path: str) -> str:
        return self._base + path

    async def _get_json(self, path: str, query: dict[str, str]):
        url = self._url(path)
        resp = await self._http.get(
            url,
            headers={"Authorization": f"Bearer {self._token}"},
            params=query,
        )
        return await self._decode(resp, url)

    @staticmethod
    async def _decode(resp: httpx.Response, url: str):
        if resp.status_code >= 400:
            raise StatusError(resp.status_code, url)
        try:
            return resp.json()
        except _json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise ShapeError(str(exc)) from exc

    # ---- readiness ------------------------------------------------------

    async def wait_ready(self, account: str, timeout: float = 120.0) -> None:
        """Poll the account listing until `account` reports ready.

        Wait-only: performs **no** registration action and sends no body -
        the caller owns registration and the classification of business
        rejections (200 responses whose JSON `state` names a conflict or a
        refusal). Errors are reserved for the deadline or the account
        landing in ``error``; intermediate states are waiting, not errors.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        last_state = "not-registered"
        while True:
            listing = gen.AccountsList.model_validate(
                await self._get_json("/api/v1/accounts", {})
            )
            mine = next((a for a in listing.accounts if a.wxid == account), None)
            if mine is not None:
                if mine.state is gen.AccountStatus.READY:
                    return
                if mine.state is gen.AccountStatus.ERROR:
                    raise NotReady(timeout, f"error: {mine.error or ''}")
                last_state = str(mine.state.value)
            else:
                last_state = "not-registered"
            if asyncio.get_running_loop().time() >= deadline:
                raise NotReady(timeout, last_state)
            await asyncio.sleep(0.25)

    async def ensure_ready(self, account: str, body: dict, timeout: float = 120.0) -> None:
        """Register (idempotently) and poll until the account is ready.

        The HTTP POST failing (>=400) is the only transport-level error the
        registration step raises. A **200 whose JSON body carries a
        `state` naming a refusal** (e.g. `account_conflict`) raises
        :class:`StatusError` instead of entering the wait: treating those
        rejections as accepted makes the readiness poll time out and hide
        the real cause. Callers that need to distinguish refusal states
        themselves should POST and use :meth:`wait_ready` directly.
        """
        url = self._url("/api/v1/accounts")
        resp = await self._http.post(
            url,
            headers={"Authorization": f"Bearer {self._token}"},
            json=body,
        )
        if resp.status_code >= 400:
            raise StatusError(resp.status_code, url)
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        state = payload.get("state") if isinstance(payload, dict) else None
        if isinstance(state, str) and state in _REFUSAL_STATES:
            raise StatusError(200, f"{url} (state={state})")
        await self.wait_ready(account, timeout)

    # ---- drain_session --------------------------------------------------

    async def drain_session(
        self,
        talker: str,
        since: Optional[int],
        on_page: Callable[[list[gen.PullMessage]], None],
    ) -> int:
        """Drain one session through the Pull cursor loop.

        Cursors are echoed verbatim (``next_since`` / ``next_offset`` come
        back exactly as the server sent them): the server pages by
        (timestamp group, offset), and a client-derived cursor is how pages
        get silently skipped or replayed.
        """
        next_since = since
        next_offset: Optional[int] = None
        total = 0
        while True:
            query: dict[str, str] = {}
            if next_since is not None:
                query["since"] = str(next_since)
            if next_offset is not None:
                query["offset"] = str(next_offset)
            page = gen.PullEnvelope.model_validate(
                await self._get_json(f"/api/v1/sessions/{talker}/messages", query)
            )
            total += len(page.messages)
            on_page(page.messages)
            if not page.sync.has_more:
                return total
            next_since = page.sync.next_since
            next_offset = page.sync.next_offset

    # ---- list_all_sessions ----------------------------------------------

    async def list_all_sessions(self) -> list[gen.SessionNative]:
        """Fetch the complete session list via offset paging.

        The list is a live view; duplicates across pages are collapsed with a
        warning (losing data silently is the failure mode this loop must not
        have).
        """
        out: list[gen.SessionNative] = []
        seen: set[str] = set()
        offset = 0
        while True:
            page = gen.SessionsNative.model_validate(
                await self._get_json("/api/v1/sessions", {"offset": str(offset)})
            )
            count = len(page.sessions)
            for session in page.sessions:
                if session.username not in seen:
                    seen.add(session.username)
                    out.append(session)
                else:
                    log.warning(
                        "session list shifted during pagination: duplicate %s collapsed",
                        session.username,
                    )
            if count == 0:
                return out
            offset += count

    # ---- search ---------------------------------------------------------

    async def search(
        self,
        talker: str,
        keyword: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> gen.MessagesNative:
        """Keyword + time-window search; YYYYMMDD validated client-side.

        ``end`` covers the whole day, same as the server.
        """
        for field, value in (("start", start), ("end", end)):
            if value is not None and not re.fullmatch(r"\d{8}", value):
                raise BadDate(f"invalid date bound {field}={value!r}: expected YYYYMMDD")
        query = {"talker": talker, "keyword": keyword}
        if start is not None:
            query["start"] = start
        if end is not None:
            query["end"] = end
        return gen.MessagesNative.model_validate(
            await self._get_json("/api/v1/messages", query)
        )

    # ---- media_bytes ----------------------------------------------------

    async def media_bytes(self, message: gen.ChatlabMessage) -> bytes:
        """Fetch media bytes for a uniquely-named handle.

        One automatic retry after a 404: the caller may have serialized the
        handle before the export finished, and the ``media=1`` re-export is
        what mints the file. A second 404 means the handle was never
        exportable and the error propagates.
        """
        if message.media is None:
            raise StatusError(404, "(no media on message)")
        name = message.media.file_name
        url = self._url(f"/api/v1/media/{name}")
        resp = await self._http.get(url, headers={"Authorization": f"Bearer {self._token}"})
        if resp.status_code == 404:
            await self._get_json(
                "/chatlab/messages",
                {"talker": message.account_name, "media": "1"},
            )
            resp = await self._http.get(url, headers={"Authorization": f"Bearer {self._token}"})
        if resp.status_code >= 400:
            raise StatusError(resp.status_code, url)
        return resp.content

    # ---- watch ------------------------------------------------------------

    async def watch(self) -> AsyncIterator[Any]:
        """Yield live server events over one long-lived SSE connection.

        One connection produces many events: the read loop yields each
        decoded frame and keeps reading until the stream itself ends or
        errors - only then does it reconnect, carrying `Last-Event-ID` so
        the server's replay window fills the gap. A per-frame disconnect
        would turn every frame (including idle `sync` heartbeats) into a
        reconnect cycle and make the server re-send its baseline each time.

        Framing is byte-level LF - never ``aiter_lines``, whose splitlines
        semantics split JSON bodies at U+0085/U+2028/U+2029 - with a 1 MiB
        cap on unconsumed bytes: a malformed stream that never emits a
        blank line grows a naive buffer without bound.

        Decoding failures on a single frame (bad JSON, shape mismatch,
        non-object payload) are logged and skipped, never fatal: one bad
        frame must not kill the stream. `StatusError` (HTTP >= 400) and
        network errors keep their documented semantics - the former
        propagates to the caller, the latter reconnects with backoff.
        Cancelling the iteration closes the stream.
        """
        last_event_id: Optional[str] = None
        backoff = 0.5
        while True:
            try:
                headers = {
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "text/event-stream",
                }
                if last_event_id is not None:
                    headers["Last-Event-ID"] = last_event_id
                overflow = False
                async with self._http.stream(
                    "GET", self._url("/api/v1/push/messages"), headers=headers
                ) as resp:
                    if resp.status_code >= 400:
                        raise StatusError(resp.status_code, self._url("/api/v1/push/messages"))
                    backoff = 0.5
                    buffer = bytearray()
                    pending: list[bytes] = []
                    pending_bytes = 0
                    async for chunk in resp.aiter_bytes():
                        buffer.extend(chunk)
                        while True:
                            nl = buffer.find(b"\n")
                            if nl < 0:
                                break
                            line = bytes(buffer[:nl]).rstrip(b"\r")
                            del buffer[: nl + 1]
                            if line:
                                pending.append(line)
                                pending_bytes += len(line) + 1
                                continue
                            # blank line = end of frame
                            frame = _parse_sse_block(b"\n".join(pending))
                            pending = []
                            pending_bytes = 0
                            if frame is None:
                                continue
                            if frame.id_ is not None and frame.id_.isdigit():
                                last_event_id = frame.id_
                            try:
                                decoded = self._decode_event(frame)
                            except (ShapeError, ValidationError, AttributeError) as exc:
                                log.warning("undecodable SSE frame skipped: %s", exc)
                                continue
                            if decoded is not None:
                                yield decoded
                        if pending_bytes + len(buffer) > _SSE_BUFFER_CAP:
                            log.warning(
                                "SSE buffer over %d bytes without a frame boundary "
                                "(malformed stream?), ending this stream for reconnect",
                                _SSE_BUFFER_CAP,
                            )
                            overflow = True
                            break
                    residual = bytes(buffer).rstrip(b"\r")
                    buffer.clear()
                    if residual:
                        pending.append(residual)
                    if not overflow and pending:
                        # EOF: fold the unterminated final line (still in the buffer) into the
                        # pending frame - a server may close without
                        # the trailing blank line, and the last event would
                        # silently vanish with the buffer.
                        frame = _parse_sse_block(b"\n".join(pending))
                        if frame is not None:
                            if frame.id_ is not None and frame.id_.isdigit():
                                last_event_id = frame.id_
                            try:
                                decoded = self._decode_event(frame)
                            except Exception as exc:
                                log.warning("undecodable final SSE frame: %s", exc)
                            else:
                                if decoded is not None:
                                    yield decoded
            except httpx.HTTPError as exc:
                log.warning("SSE stream error, reconnecting: %s", exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    @staticmethod
    def _decode_event(frame: _SseFrame) -> Optional[Any]:
        assert frame.data is not None
        try:
            payload = _json.loads(frame.data)
        except _json.JSONDecodeError as exc:
            raise ShapeError(str(exc)) from exc
        kind = payload.get("event", frame.event or "")
        if kind == "message.new":
            return gen.EventNew.model_validate(payload)
        if kind == "message.revoke":
            return gen.EventRevoke.model_validate(payload)
        if kind == "sync":
            return gen.EventSync.model_validate(payload)
        # Notification face / unknown kinds: meta-only pass-through so a new
        # server event kind degrades instead of killing the stream.
        log.debug("ignoring unknown SSE event kind: %s", kind)
        return None
