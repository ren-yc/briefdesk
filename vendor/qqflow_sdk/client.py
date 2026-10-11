"""Handwritten behavior layer over :mod:`qqflow_sdk.generated`.

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

One deliberate divergence from the weflow sibling: the legacy SSE face's
``message.new`` / ``message.revoke`` payloads are the server's push events
serialized verbatim and are *not* part of the description's schemas (the
generated ``NotificationFrame`` is meta-only by design), so those frames
are decoded by the behavior layer's own ``MessageEvent`` model;
``sync`` frames use the generated ``SyncFrame`` (which carries ``generation``).
The watermarks in a ``sync`` frame are SQLite row numbers *per table*,
not per session: after a ``generation`` jump the catch-up is a full
re-drain of each watched session, not a cursor resume.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import urllib.parse
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from .generated.qqflow_sdk import models as gen

log = logging.getLogger(__name__)

# Published connect budget (seconds) for every request the SDK makes: a server
# that is not there must fail fast rather than hang a caller.
CONNECT_TIMEOUT = 5.0

# Published read budget for ordinary JSON requests, charged per read operation.
READ_TIMEOUT = 30.0

# Stall budget for the requests whose cost is set by the data or by server-side
# work (a full-roster message count, a media body, an export trigger, a sync
# pass, the SSE stream). httpx charges it per read operation and resets it on
# progress, so a 500 MB download or a minutes-long first pull never trips it *while
# advancing* - but a connection that goes silent (peer killed without a FIN, a NAT
# that dropped the mapping, a laptop resuming from sleep) surfaces instead of
# hanging the caller forever. 90s is about four times the server keep-alive
# interval (25s): one missed ping plus a scheduling hiccup stays healthy, a black
# hole does not.
STALL_TIMEOUT = 90.0

# 200-with-refusal states the registration endpoint answers with: a
# conflict leaves the incumbent bound, the other three reject the request
# before any state change. Treating these 200s as "accepted" makes the
# readiness poll time out and hide the real cause, so ensure_ready maps
# them to StatusError; wait-only callers classify the body themselves.
_REFUSAL_STATES = frozenset({
    "account_conflict",
    "invalid_key",
    "invalid_db_path",
    "unknown_qq",
})

# SSE framing is byte-level: U+0085/U+2028/U+2029 are legal *inside* JSON
# bodies, and str.splitlines (what ``aiter_lines`` uses) would split the
# body there and corrupt the frame. Only LF terminates a line; a lone CR is
# stripped for CRLF servers. The cap bounds BOTH the unconsumed
# accumulation of a malformed stream that never emits a blank line AND a
# single complete frame: without the accumulation bound one such stream
# grows without limit, and without the frame-bound check an over-cap frame
# would be delivered before any check could stop it - the cap would have
# no defined semantics for frames.
_SSE_BUFFER_CAP = 1 << 20


class ClientError(Exception):
    """Base error for the behavior layer."""


class TransportError(ClientError):
    """The request never produced an HTTP response (connect, timeout, reset),
    or was never sent at all because httpx could not parse the URL.

    Why this exists: ``httpx`` raises its own exception family, so a caller doing
    ``except ClientError`` used to miss **every** network failure while catching
    every server refusal. Wrapping puts both under one root.
    """


class StatusError(ClientError):
    """The server answered, but not with a usable body."""

    def __init__(self, status: int, url: str, detail: str | None = None) -> None:
        note = f" ({detail})" if detail else ""
        super().__init__(f"HTTP {status} on {url}{note}")
        self.status = status
        self.url = url
        #: Why this status matters (e.g. the refusal state a 200 carried).
        self.detail = detail


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
    """A time bound was neither ``YYYYMMDD`` nor unix seconds."""


@dataclass(frozen=True)
class RegisterOutcome:
    """Raw ``POST /api/v1/accounts`` outcome.

    HTTP 200 covers several business states with **different shapes**
    (``accepted`` / ``in_progress`` / ``already_ready`` carry a status; a
    conflict carries who holds the binding; the invalid-* refusals carry
    neither), so only the two fields every state has are typed here and the
    decoded body stays available for the caller's own classification.

    A refusal is a **value**, not an error: what to do about it is the
    caller's decision - the caller is the one holding the configuration that
    identity is compared against.
    """

    state: str
    status: str | None
    body: dict[str, Any]


def _encode_path_segment(s: str) -> str:
    """Percent-encode one path segment (unreserved set plus ``@``, which ids use).

    Why: an id containing ``#`` or ``?`` interpolated straight into a URL string is
    read as a fragment or query, so the request silently targets a different path.
    The Rust client encodes too - the same input must not mean two things.
    """
    return urllib.parse.quote(s, safe="-._~@")


def _validate_time_bound(field: str, value: str) -> None:
    """Accept a bare ``YYYYMMDD`` date or unix seconds - the server parses
    both, so rejecting either here would block a legal request."""
    # `str.isdigit()` also accepts non-ASCII digits (full-width, superscript),
    # which the server rejects with a 400. The Rust client refuses them locally,
    # so the same input must yield the same error class on both sides.
    if not value.isascii() or not value.isdigit():
        raise BadDate(
            f"invalid time bound {field}={value!r}: expected YYYYMMDD or unix seconds"
        )


def _message_query_params(
    talker: str,
    keyword: str | None,
    start: str | None,
    end: str | None,
    limit: int | None,
    offset: int | None,
    media: bool,
) -> dict[str, str]:
    """Query for either messages face.

    The native face (``/api/v1/messages``) and the ChatLab face
    (``/chatlab/messages``) take the **same** query surface - only the
    envelope differs. Building it in two places is how the two faces end up
    answering the same request with different pages.
    """
    if not talker:
        raise ShapeError("talker must not be empty")
    query: dict[str, str] = {"talker": talker}
    if keyword is not None:
        query["keyword"] = keyword
    for field, value in (("start", start), ("end", end)):
        if value is not None:
            _validate_time_bound(field, value)
            query[field] = value
    if limit is not None:
        query["limit"] = str(limit)
    if offset is not None:
        query["offset"] = str(offset)
    if media:
        query["media"] = "1"
    return query


class MessageEvent(BaseModel):
    """The legacy SSE face's ``message.new`` / ``message.revoke`` payload.

    The server serializes its push events verbatim (camelCase); these are
    not part of the description's schemas, so the behavior layer owns this
    decoding type - reusing the generated ``NotificationFrame`` would drop
    the content (that frame is meta-only by design). Optional keys are the
    ones the server omits when unknown. Shape mirrors the Rust client's
    ``MessageEvent`` field for field, including its
    ``#[serde(rename_all = "camelCase")]``: the aliases below are what the
    wire actually carries, and ``populate_by_name`` keeps the snake_case
    attribute names usable from Python.

    ``event`` carries the event name (``message.new`` vs ``message.revoke``):
    without it the two kinds decode into indistinguishable objects and a
    downstream listener cannot tell a new message from a revoke. The
    decoder fills it from the frame body, falling back to the SSE
    ``event:`` header when the body omits it.
    """

    model_config = ConfigDict(populate_by_name=True)

    event: str
    session_id: str = Field(validation_alias=AliasChoices("sessionId", "session_id"))
    session_type: str = Field(validation_alias=AliasChoices("sessionType", "session_type"))
    group_name: str | None = Field(default=None, validation_alias=AliasChoices("groupName", "group_name"))
    rawid: str
    avatar_url: str | None = Field(default=None, validation_alias=AliasChoices("avatarUrl", "avatar_url"))
    source_name: str | None = Field(default=None, validation_alias=AliasChoices("sourceName", "source_name"))
    content: str
    timestamp: int
    media: dict | None = None
    media_id: str | None = Field(default=None, validation_alias=AliasChoices("mediaId", "media_id"))


@dataclass(frozen=True)
class _SseFrame:
    event: str | None
    data: str | None
    id_: str | None


def _parse_sse_block(block: bytes) -> _SseFrame | None:
    event = id_ = None
    data_lines: list[bytes] = []
    for raw in block.split(b"\n"):
        raw = raw.rstrip(b"\r")
        if raw.startswith(b":"):
            continue  # heartbeat comment
        if raw.startswith(b"id:"):
            id_ = raw[3:].strip().decode("utf-8", "replace")
        elif raw.startswith(b"event:"):
            event = raw[6:].strip().decode("utf-8", "replace")
        elif raw.startswith(b"data:"):
            data_lines.append(raw[5:])
    if not data_lines:
        return None
    # SSE spec: several data lines in one frame join with LF into the
    # dispatched payload. The old per-line overwrite kept only the last
    # line - a today-unreachable shape (the server emits exactly one data
    # line per frame) that would silently drop a multi-line payload. The
    # single-line path strips exactly like before, so committed frames
    # decode byte-identically.
    data = b"\n".join(data_lines).strip().decode("utf-8", "replace")
    return _SseFrame(event, data, id_)


class Client:
    """Async client for one qqflow-server instance.

    ``token`` is the API token the server printed on first start (or
    ``--show-token``). Auth goes in the ``Authorization`` header only -
    never in the URL.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = READ_TIMEOUT,
        stall_timeout: float = STALL_TIMEOUT,
    ) -> None:
        """Point a client at a server.

        ``timeout`` bounds one read of an ordinary JSON response; ``stall_timeout``
        bounds one read of a request whose size or server-side work is unbounded
        (roster counts, media bytes, ``sync_now``, the SSE stream). Both are charged
        per read operation rather than as a total deadline, so a slow answer that
        keeps arriving is never cut off while one that goes silent still fails.
        Pass a large ``stall_timeout`` to opt a stream out of stall detection.
        """
        self._timeout = httpx.Timeout(timeout, connect=CONNECT_TIMEOUT)
        self._long_timeout = httpx.Timeout(stall_timeout, connect=CONNECT_TIMEOUT)
        self._http = httpx.AsyncClient(timeout=self._timeout)
        self._base = base_url.rstrip("/")
        self._token = token

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ---- plumbing -------------------------------------------------------

    def _url(self, path: str) -> str:
        return self._base + path

    async def _http_get(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET that maps `httpx` transport failures into :class:`TransportError`."""
        try:
            return await self._http.get(url, **kwargs)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise TransportError(f"{type(exc).__name__} on {url}: {exc}") from exc

    async def _http_post(self, url: str, **kwargs: Any) -> httpx.Response:
        """POST with the same transport-failure mapping as :meth:`_http_get`.

        Every public request entry must go through one of these two: an entry
        that calls ``self._http`` directly lets a raw `httpx.ConnectError`
        escape the ClientError tree, and callers that classify
        ``except ClientError`` then miss transport failures for that route
        alone (regression: test_transport_failures_are_client_errors walks one
        representative per transport method)."""
        try:
            return await self._http.post(url, **kwargs)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise TransportError(f"{type(exc).__name__} on {url}: {exc}") from exc

    async def _get_json(
        self, path: str, query: dict[str, str], *, long: bool = False
    ):
        url = self._url(path)
        resp = await self._http_get(
            url,
            headers={"Authorization": f"Bearer {self._token}"},
            params=query,
            timeout=self._long_timeout if long else self._timeout,
        )
        return await self._decode(resp, url)

    @staticmethod
    async def _decode(resp: httpx.Response, url: str):
        # Non-2xx, not just 4xx/5xx: the Rust client treats anything outside
        # 200..300 as an error, and a surfaced 3xx would otherwise fall into
        # `resp.json()` and come out as a shape error instead of a status error.
        if not 200 <= resp.status_code < 300:
            raise StatusError(resp.status_code, url)
        try:
            return resp.json()
        except _json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise ShapeError(str(exc)) from exc
        # json.loads raises UnicodeDecodeError (a ValueError subclass but NOT a
        # JSONDecodeError) on bytes with invalid UTF-8 sequences; leaving it
        # uncaught gives shape corruption a hole to escape ClientError through.
        except UnicodeDecodeError as exc:  # pragma: no cover - defensive
            raise ShapeError(str(exc)) from exc

    @staticmethod
    def _validate(model: Any, payload: Any) -> Any:
        """`model_validate` with shape corruption kept inside ShapeError.

        A 200 with a body of the wrong *type* (a list where the route
        returns an object, a string where a number lives) raises pydantic's
        own ValidationError, which is not a ClientError. Callers that
        classify ``except ClientError`` would see it escape for that route
        alone (regression: test_health_shape_corruption_is_a_shape_error
        feeds a 200 whose `account` field is a number)."""
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            raise ShapeError(str(exc)) from exc

    # ---- readiness ------------------------------------------------------

    async def wait_ready(self, account: str, timeout: float = 120.0) -> None:
        """Poll the account listing until `account` reports ready.

        Wait-only: performs **no** registration action and sends no body -
        the caller owns registration and the classification of business
        rejections (200 responses whose JSON `state` names a conflict or a
        refusal). Errors are reserved for the deadline or the account
        landing in ``error``; intermediate states are waiting, not errors.
        The listing is matched by ``AccountView.qq``.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        last_state = "not-registered"
        dropped = 0
        while True:
            # A dropped round is waiting, not failure: this poll exists to ride out
            # a transient connect blip or a momentary 5xx while the index is still
            # coming up. A 4xx is a misconfiguration, and a listing that no longer
            # matches the declared shape is a broken contract - both propagate
            # instead of being waited out.
            try:
                listing = self._validate(
                    gen.AccountsList, await self._get_json("/api/v1/accounts", {})
                )
            except TransportError as exc:
                listing = None
                dropped += 1
                last_state = f"unreachable ({exc})"
            except StatusError as exc:
                if 500 <= exc.status < 600:
                    listing = None
                    dropped += 1
                    last_state = f"server error {exc.status} on the listing"
                else:
                    raise
            mine = (
                next((a for a in listing.accounts if a.qq == account), None)
                if listing is not None
                else None
            )
            if mine is not None:
                if mine.state is gen.AccountStatus.READY:
                    return
                if mine.state is gen.AccountStatus.ERROR:
                    raise NotReady(timeout, f"error: {mine.error or ''}")
                last_state = str(mine.state.value)
            elif listing is not None:
                last_state = "not-registered"
            if asyncio.get_running_loop().time() >= deadline:
                # Naming the dropped rounds is the difference between "indexing, keep
                # waiting" and "this server never answered once" - both end at the
                # same deadline, and only one of them is a caller-side problem.
                raise NotReady(
                    timeout,
                    last_state
                    if not dropped
                    else f"{last_state}; {dropped} poll(s) never got through",
                )
            await asyncio.sleep(0.25)

    async def ensure_ready(self, account: str, body: dict, timeout: float = 120.0) -> None:
        """Register (idempotently) and poll until the account is ready.

        The HTTP POST failing (>=400) is the only transport-level error the
        registration step raises. A **200 whose JSON body carries a
        `state` naming a refusal** (`account_conflict` / `invalid_key` /
        `invalid_db_path` / `unknown_qq`) raises :class:`StatusError`
        instead of entering the wait: treating those rejections as accepted
        makes the readiness poll time out and hide the real cause. Callers
        that need to distinguish refusal states themselves should POST and
        use :meth:`wait_ready` directly.

        The registration payload carries ``qq`` / ``key`` / ``db_path``.
        Intermediate states (``indexing``) are waiting, not errors; errors
        are reserved for the deadline or the account landing in ``error``.
        """
        outcome = await self.register(body)
        if outcome.state in _REFUSAL_STATES:
            url = self._url("/api/v1/accounts")
            raise StatusError(200, url, f"state={outcome.state}")
        await self.wait_ready(account, timeout)

    # ---- health / accounts / register -----------------------------------

    async def health(self) -> gen.Health:
        """``GET /health`` - liveness and account phase. **Unauthenticated.**

        Deliberately carries no account identity: confirming *which* account
        is bound requires :meth:`accounts`. The SDK sends no credentials
        here, and a test pins that.
        """
        url = self._url("/health")
        resp = await self._http_get(url)
        return self._validate(gen.Health, await self._decode(resp, url))

    async def accounts(self) -> list[gen.AccountView]:
        """``GET /api/v1/accounts`` - one entry per bound account.

        The failure reason (``error``) and the message count live only here;
        ``/health`` collapses everything to a scalar phase.
        """
        listing = self._validate(gen.AccountsList, 
            await self._get_json("/api/v1/accounts", {})
        )
        return listing.accounts

    async def register(self, body: dict) -> RegisterOutcome:
        """``POST /api/v1/accounts`` - register, returning the raw outcome
        **without waiting**.

        The waiting half is :meth:`wait_ready`; :meth:`ensure_ready` is the
        two composed with the refusal vocabulary applied. Callers that must
        classify the answer themselves (a refusal is a business state, not an
        HTTP error) use this pair instead.
        """
        url = self._url("/api/v1/accounts")
        resp = await self._http_post(
            url,
            headers={"Authorization": f"Bearer {self._token}"},
            json=body,
        )
        payload = await self._decode(resp, url)
        if not isinstance(payload, dict) or not isinstance(payload.get("state"), str):
            raise ShapeError(f"{url}: no string `state` in the body: {payload!r}")
        status = payload.get("status")
        return RegisterOutcome(
            state=payload["state"],
            status=status if isinstance(status, str) else None,
            body=payload,
        )

    async def sync_now(self) -> gen.SyncResult:
        """``POST /api/v1/sync`` - run one manual incremental sync.

        The watcher runs the same sync on file changes, so this exists for a
        caller that must have a fresh read *now* rather than eventually. It is
        a write against the server's state - it advances watermarks and may
        export media - so it is deliberately absent from every polling path:
        only a caller that asked for it triggers it.
        """
        url = self._url("/api/v1/sync")
        # A sync pass indexes and may export media: bounded only by the size of
        # the library, never by a fixed budget.
        resp = await self._http_post(
            url,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=self._long_timeout,
        )
        return self._validate(gen.SyncResult, await self._decode(resp, url))

    # ---- pull_page / drain_session --------------------------------------

    async def pull_page(
        self,
        talker: str,
        since: int | None,
        offset: int = 0,
        limit: int | None = None,
    ) -> gen.PullEnvelope:
        """Fetch **one page** of the ChatLab Pull cursor loop.

        :meth:`drain_session` is this in a loop; the single-page entry exists
        for callers that must bound a single request. ``since`` is
        **exclusive** and ``offset`` advances within one timestamp group.
        Both cursors come back in ``sync`` and must be echoed verbatim: the
        server pages by (timestamp group, offset), and a client-derived
        cursor is how pages get silently skipped or replayed.

        ``limit`` is a per-page cap (the server caps it at 5000). ``None``
        means the server default, which is also 5000.
        """
        query: dict[str, str] = {}
        if since is not None:
            query["since"] = str(since)
        if offset:
            query["offset"] = str(offset)
        if limit is not None:
            query["limit"] = str(limit)
        return self._validate(gen.PullEnvelope, 
            await self._get_json(
            f"/api/v1/sessions/{_encode_path_segment(talker)}/messages", query
        )
        )

    async def drain_session(
        self,
        talker: str,
        since: int | None,
        on_page: Callable[[list[gen.PullMessage]], None],
    ) -> int:
        """Drain one session through the Pull cursor loop.

        Cursors are echoed verbatim (``next_since`` / ``next_offset`` come
        back exactly as the server sent them): the server pages by
        (timestamp group, offset), and a client-derived cursor is how pages
        get silently skipped or replayed.
        """
        next_since = since
        next_offset = 0
        total = 0
        while True:
            page = await self.pull_page(talker, next_since, next_offset)
            total += len(page.messages)
            on_page(page.messages)
            if not page.sync.has_more:
                return total
            next_since = page.sync.next_since
            next_offset = page.sync.next_offset

    # ---- list_all_sessions ----------------------------------------------

    async def list_all_sessions(
        self,
        *,
        page_size: int | None = None,
        keyword: str | None = None,
    ) -> list[gen.SessionNative]:
        """Fetch the complete session list via offset paging.

        The list is a live view; duplicates across pages are collapsed with a
        warning (losing data silently is the failure mode this loop must not
        have).

        ``page_size`` is the server-side page size (the server caps it at
        10000). Leave it ``None`` for the server default; a polling consumer
        that re-reads the list every cycle should ask for the maximum instead,
        because the request count is ``sessions / page_size`` and the server's
        default page is two orders of magnitude smaller than the cap.
        ``keyword`` filters server-side, so pagination walks the filtered list.
        """
        out: list[gen.SessionNative] = []
        seen: set[str] = set()
        offset = 0
        while True:
            query: dict[str, str] = {"offset": str(offset)}
            if page_size is not None:
                query["limit"] = str(page_size)
            if keyword is not None:
                query["keyword"] = keyword
            page = self._validate(gen.SessionsNative, 
                await self._get_json("/api/v1/sessions", query)
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

    # ---- media_bytes ----------------------------------------------------

    async def media_bytes(self, message: gen.ChatlabMessage, talker: str) -> bytes:
        """Fetch media bytes for a uniquely-named handle.

        ``talker`` is the **session id** the export side door needs; it is NOT
        ``message.account_name`` (that is the sender's display name, which only
        coincides with the session id for 1:1 chats where the display name was
        never customized - real data breaks the coincidence, so the caller owns
        the value).

        One automatic retry after a 404: the caller may have serialized the
        handle before the export finished, and the ``media=1`` re-export is
        what mints the file. A second 404 means the handle was never
        exportable and the error propagates.
        """
        if message.media is None:
            raise ShapeError("(no media on message): nothing to fetch")
        if not talker.strip():
            # Whitespace included, matching the Rust client: this is the media family,
            # where an empty handle addresses a different path than the caller means.
            raise ShapeError("talker must not be empty")
        name = message.media.file_name
        if not name.strip():
            # Fail-fast, whitespace included (the Rust client trims too): an empty
            # handle addresses a different path than the caller means, and the
            # server cannot tell "no file name" from "some file I never heard of".
            raise ShapeError("media file_name must not be empty")
        url = self._url(f"/api/v1/media/{_encode_path_segment(name)}")
        auth = {"Authorization": f"Bearer {self._token}"}
        # Media size is whatever the sender uploaded; the export trigger behind a
        # 404 copies whole files. Neither has a meaningful read budget.
        resp = await self._http_get(url, headers=auth, timeout=self._long_timeout)
        if resp.status_code == 404:
            await self._get_json(
                "/chatlab/messages",
                {"talker": talker, "media": "1"},
                long=True,
            )
            resp = await self._http_get(url, headers=auth, timeout=self._long_timeout)
        # Not-2xx (not just 4xx/5xx): a 3xx falling through to `return` hands
        # the caller an empty redirect body as media bytes with no error signal
        # - same rule as _decode / media_bytes_by_id.
        # (regression: test_media_bytes_rejects_redirect_like_statuses)
        if not 200 <= resp.status_code < 300:
            raise StatusError(resp.status_code, url)
        return resp.content

    # ---- list_messages / contacts / media_bytes_by_id ---------------------

    async def list_messages(
        self,
        talker: str,
        *,
        keyword: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        media: bool = False,
    ) -> gen.MessagesNative:
        """``GET /api/v1/messages`` - the **native** messages face.

        Descending by time, offset-paged: advance ``offset`` by the page size
        until ``has_more`` is false. This is the face that carries what the
        ChatLab shape drops - ``raw_content``, ``is_send``, ``local_type`` - and
        the only one that can export media (``media=True``).

        Prefer :meth:`drain_session` when both faces would do: the Pull cursor
        is stable across a live database, while offset paging over a growing
        table can shift.
        """
        query = _message_query_params(
            talker, keyword, start, end, limit, offset, media
        )
        return self._validate(gen.MessagesNative, 
            await self._get_json("/api/v1/messages", query)
        )

    async def chatlab_messages(
        self,
        talker: str,
        *,
        keyword: str | None = None,
        start: str | None = None,
        end: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        media: bool = False,
    ) -> gen.ChatlabMessages:
        """``GET /chatlab/messages`` - the **ChatLab-shaped** messages face.

        Same query surface as :meth:`list_messages`, different envelope:
        ascending by time, ChatLab type codes, ``media`` on the message,
        ``count``/``page`` for paging and **no** ``success`` key. Project from
        this face when the caller wants ChatLab field names; the native face
        is the only one carrying ``raw_content``/``is_send``.

        ``offset`` is this face's paging cursor: the wire also accepts
        ``cursor``, but the two are the same integer, so advancing by the page
        size and passing ``page.next_cursor`` are equivalent - and it keeps
        the two faces from growing different paging parameters for one list.
        """
        query = _message_query_params(
            talker, keyword, start, end, limit, offset, media
        )
        return self._validate(gen.ChatlabMessages, 
            await self._get_json("/chatlab/messages", query)
        )

    async def contacts(
        self,
        *,
        limit: int | None = None,
        offset: int | None = None,
        keyword: str | None = None,
    ) -> gen.Contacts:
        """``GET /api/v1/contacts`` - one page of the contact list.

        Contact detail is not part of the ChatLab shape at all; this is the
        only source for display names, remarks and aliases.
        """
        query: dict[str, str] = {}
        if limit is not None:
            query["limit"] = str(limit)
        if offset is not None:
            query["offset"] = str(offset)
        if keyword is not None:
            query["keyword"] = keyword
        return self._validate(gen.Contacts, 
            await self._get_json("/api/v1/contacts", query)
        )

    async def group_members(
        self,
        chatroom: str,
        *,
        include_message_counts: bool = False,
    ) -> gen.GroupMembers:
        """``GET /api/v1/group-members`` - group members (**roster union speakers**).

        Silent members (in the roster, never spoke) appear too, with
        ``message_count`` 0 - listing only speakers would make "who is in this
        group" depend on who talked last. The server counts real numbers only
        when ``include_message_counts`` is true: the count is a whole-conversation
        scan, not free.
        """
        if not chatroom:
            # Same fail-fast as `talker` everywhere else: an empty key asks the
            # server a different question, and a 200 with an empty roster would
            # read as "this group has no members".
            raise ShapeError("chatroom must not be empty")
        query: dict[str, str] = {"chatroomId": chatroom}
        if include_message_counts:
            query["includeMessageCounts"] = "1"
        # Counting messages scans every session behind each roster entry: that is
        # exactly the request the published read budget must not apply to.
        return self._validate(gen.GroupMembers, 
            await self._get_json(
                "/api/v1/group-members", query, long=include_message_counts
            )
        )
    async def media_bytes_by_id(self, media_id: str) -> bytes:
        """``GET /api/v1/media/{id}`` - bytes for a handle the server advertised.

        ``media_id`` is a **single path segment**: the native face's
        ``media_id``, or the last segment of ``media_url``. Prefer
        :meth:`media_bytes` when a ChatLab message is at hand - that one also
        triggers an export and retries once on a 404.
        """
        if not media_id.strip():
            # Whitespace counts as empty, matching the Rust client: an empty handle
            # addresses a *different* path, and the
            # server answering "no such file" would dress up a caller mistake as an
            # unexportable handle. (The check sits after the docstring: a statement
            # before it turns the docstring into a discarded expression, so help()
            # and API docs generators would lose this method entirely.)
            raise ShapeError("media_id must not be empty")
        url = self._url(f"/api/v1/media/{_encode_path_segment(media_id)}")
        resp = await self._http_get(
            url,
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=self._long_timeout,
        )
        if not 200 <= resp.status_code < 300:
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
        cap that bounds both the unconsumed accumulation and any single
        complete frame: an over-cap frame (well-formed or not) is not
        delivered and the stream ends for a backing-off reconnect.

        Decoding failures on a single frame (bad JSON, shape mismatch,
        non-object payload) are logged and skipped, never fatal: one bad
        frame must not kill the stream. `StatusError` (HTTP >= 400) and
        network errors keep their documented semantics - the former
        propagates to the caller, the latter reconnects with backoff.
        Cancelling the iteration closes the stream.

        ``sync`` frames carry the server's `generation`; a jump means the
        replay buffer cannot fill the gap and - the watermarks being
        per-table SQLite row numbers - the caller should re-drain each
        watched session *from scratch* (a full re-drain, not a cursor
        resume).
        """
        last_event_id: str | None = None
        backoff = 0.5
        while True:
            # Only a *clean* stream end (EOF, no overflow, no transport
            # error) resets backoff - see the flag below.
            clean_exit = False
            try:
                headers = {
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "text/event-stream",
                }
                if last_event_id is not None:
                    headers["Last-Event-ID"] = last_event_id
                overflow = False
                async with self._http.stream(
                    "GET",
                    self._url("/api/v1/push/messages"),
                    headers=headers,
                    timeout=self._long_timeout,
                ) as resp:
                    # 3xx is also a failure (same not-2xx rule as _decode /
                    # media_bytes): a 302 decoded as a readable stream ends in
                    # an empty-body EOF, resetting the backoff and looping
                    # forever at 0.5s with no error signal to the caller.
                    if not 200 <= resp.status_code < 300:
                        raise StatusError(resp.status_code, self._url("/api/v1/push/messages"))
                    # The old shape reset backoff right here, on every
                    # 200: a server that connects fine and then emits
                    # over-cap frames reconnected forever at a flat 0.5s
                    # (measured: 6 connects in 3s) - which is not a backoff
                    # at all. Only the clean-exit path below may reset it.
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
                            if pending_bytes > _SSE_BUFFER_CAP:
                                log.warning(
                                    "SSE frame over %d bytes (malformed stream?), "
                                    "ending this stream for reconnect",
                                    _SSE_BUFFER_CAP,
                                )
                                overflow = True
                                break
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
                    if not overflow:
                        residual = bytes(buffer).rstrip(b"\r")
                        buffer.clear()
                        if residual:
                            pending.append(residual)
                        # (No separate EOF frame-cap check: the accumulation
                        # check above already fires once pending_bytes passes
                        # the cap, so an over-cap unterminated frame never
                        # reaches this flush - a branch here would be dead.
                        # Pinned by
                        # test_watch_drops_oversized_final_frame_at_eof_without_blank.)
                        if pending:
                            # EOF: fold the unterminated final line (still in
                            # the buffer) into the pending frame - a server
                            # may close without the trailing blank line, and
                            # the last event would silently vanish with it.
                            frame = _parse_sse_block(b"\n".join(pending))
                            if frame is not None:
                                if frame.id_ is not None and frame.id_.isdigit():
                                    last_event_id = frame.id_
                                try:
                                    decoded = self._decode_event(frame)
                                except (ShapeError, ValidationError, AttributeError) as exc:
                                    log.warning("undecodable final SSE frame: %s", exc)
                                else:
                                    if decoded is not None:
                                        yield decoded
                        if not overflow:
                            # Every guard passed: the next reconnect starts
                            # from the floor instead of carrying a stale
                            # doubled delay over.
                            clean_exit = True
                    # Overflow: skip the EOF flush entirely (an un-trusted
                    # stream must not deliver its tail) and keep escalating.
            except httpx.InvalidURL as exc:
                # Not a stream hiccup: this client cannot parse its own URL, so every
                # reconnect fails identically. Swallowing it would turn a
                # configuration mistake into a stream that never yields and never
                # errors - the two other entry points raise, so raise here too.
                raise TransportError(f"unusable base_url for SSE: {exc}") from exc
            except httpx.HTTPError as exc:
                log.warning("SSE stream error, reconnecting: %s", exc)
            if clean_exit:
                backoff = 0.5
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    @staticmethod
    def _decode_event(frame: _SseFrame) -> Any | None:
        assert frame.data is not None
        try:
            payload = _json.loads(frame.data)
        except _json.JSONDecodeError as exc:
            raise ShapeError(str(exc)) from exc
        kind = payload.get("event", frame.event or "")
        if kind in ("message.new", "message.revoke"):
            # The event name lives in the frame; some legacy payloads omit
            # it from the body, and MessageEvent must carry it either way -
            # without it new and revoke decode into indistinguishable
            # objects and a listener cannot tell them apart.
            if isinstance(payload, dict):
                payload.setdefault("event", kind)
            return Client._validate(MessageEvent, payload)
        if kind == "sync":
            return Client._validate(gen.SyncFrame, payload)
        # Notification face / unknown kinds: meta-only pass-through so a new
        # server event kind degrades instead of killing the stream.
        log.debug("ignoring unknown SSE event kind: %s", kind)
        return None
