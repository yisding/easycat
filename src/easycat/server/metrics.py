"""Server metric emission for :class:`VoiceServer` (registered + emitted in M8).

The five ``easycat.server.*`` metric names and the three new label keys
(``easycat.route`` / ``easycat.server_state`` / ``easycat.auth_result``) are
registered in :mod:`easycat._observability` (``METRIC_DEFINITIONS`` /
``LOW_CARDINALITY_ATTRIBUTE_KEYS``) in the SAME change that this module first
emits them — the observability layer is a hard allow-list: ``_record_metric``
and ``sanitize_attributes`` RAISE ``ValueError`` on any unregistered metric name
or attribute key.

Every emission routes ONLY through :func:`easycat._observability.increment_counter`
/ :func:`record_histogram` / :func:`observe_gauge`, which call
``sanitize_attributes`` internally — there is no direct ``_record_metric`` call
and no hand-built bypass. When no OTel SDK is configured those helpers are a
no-op (the meter is ``None``), but ``sanitize_attributes`` still runs, which is
exactly why the names/keys must be registered first.

``easycat.route`` is a PII/cardinality hazard: the key-name allow-list cannot
distinguish a route TEMPLATE from a raw path. :func:`_route_attrs` ASSERTS the
value is in :data:`SERVER_ROUTE_TEMPLATES` BEFORE it is placed on the attribute
dict — a raw path with user content (e.g. ``?token=...``) is rejected with
``ValueError`` and can never reach a label. ``easycat.transport`` is already
registered (do NOT re-add it).

``easycat._observability`` is imported LAZILY inside each emission function (not
at module top) so ``import easycat.server.metrics`` stays light and matches the
package's import-weight discipline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from easycat.server.routes import ROUTE_TEMPLATES

if TYPE_CHECKING:
    from easycat.server.auth import AuthReason
    from easycat.server.health import ServerState

# ── Metric names (registered + emitted in M8) ────────────────────────
SERVER_REQUESTS_TOTAL = "easycat.server.requests.total"
SERVER_REQUEST_DURATION = "easycat.server.request.duration"
SERVER_SESSIONS_REJECTED_TOTAL = "easycat.server.sessions.rejected.total"
SERVER_CONNECTIONS_ACTIVE = "easycat.server.connections.active"
SERVER_DRAINING = "easycat.server.draining"

#: All ``easycat.server.*`` metric names this layer emits (registered in M8).
SERVER_METRIC_NAMES: frozenset[str] = frozenset(
    {
        SERVER_REQUESTS_TOTAL,
        SERVER_REQUEST_DURATION,
        SERVER_SESSIONS_REJECTED_TOTAL,
        SERVER_CONNECTIONS_ACTIVE,
        SERVER_DRAINING,
    }
)

# ── Label keys (the three NEW ones registered in M8) ─────────────────
# ``easycat.route`` / ``easycat.server_state`` / ``easycat.auth_result`` are
# NEW — M8 adds them to ``LOW_CARDINALITY_ATTRIBUTE_KEYS``. ``easycat.transport``
# is already registered and is NOT re-added.
LABEL_ROUTE = "easycat.route"
LABEL_SERVER_STATE = "easycat.server_state"
LABEL_AUTH_RESULT = "easycat.auth_result"

#: The NEW label keys M8 added to the observability allow-list.
NEW_SERVER_LABEL_KEYS: frozenset[str] = frozenset(
    {
        LABEL_ROUTE,
        LABEL_SERVER_STATE,
        LABEL_AUTH_RESULT,
    }
)

#: Enumerated route templates ``easycat.route`` may carry (single source of
#: truth re-exported from :mod:`easycat.server.routes`). Asserted before
#: recording the label so a raw path can never be emitted.
SERVER_ROUTE_TEMPLATES: frozenset[str] = ROUTE_TEMPLATES


def _route_attrs(route: str) -> dict[str, str]:
    """Return ``{easycat.route: route}`` after asserting ``route`` is a template.

    This is the PII/cardinality firewall the plan demands: the value MUST be a
    route TEMPLATE from :data:`SERVER_ROUTE_TEMPLATES`, never a resolved/raw path
    (which can carry ``?token=...`` or user content). An off-template value
    RAISES :class:`ValueError` BEFORE the value is placed on any attribute dict.
    """
    if route not in SERVER_ROUTE_TEMPLATES:
        raise ValueError(
            f"easycat.route must be an enumerated route template, not {route!r}; "
            f"never record a raw path"
        )
    return {LABEL_ROUTE: route}


def record_request(route: str, *, duration_s: float, server_state: ServerState) -> None:
    """Record one handled read-only request (count + duration).

    ``route`` MUST be a route TEMPLATE (asserted via :func:`_route_attrs`);
    ``server_state`` is the ``ServerState`` Literal (``serving``/``draining``).
    Increments :data:`SERVER_REQUESTS_TOTAL` and records the latency on
    :data:`SERVER_REQUEST_DURATION`. Routes through ``sanitize_attributes``.
    """
    from easycat import _observability as observability

    route_attrs = _route_attrs(route)
    observability.increment_counter(
        SERVER_REQUESTS_TOTAL,
        attributes={**route_attrs, LABEL_SERVER_STATE: server_state},
    )
    observability.record_histogram(
        SERVER_REQUEST_DURATION,
        duration_s,
        attributes=route_attrs,
    )


def record_session_rejected(
    *,
    server_state: ServerState,
    auth_result: AuthReason | None = None,
) -> None:
    """Record one rejected session (draining / at-capacity / unauthorized).

    ``server_state`` is always carried (the rejection happened in a given
    serving/draining state); ``auth_result`` is the ``AuthReason`` Literal
    (``missing``/``invalid``) when the rejection is an auth failure, ``None``
    otherwise. Increments :data:`SERVER_SESSIONS_REJECTED_TOTAL` through
    ``sanitize_attributes``.
    """
    from easycat import _observability as observability

    attributes: dict[str, str] = {LABEL_SERVER_STATE: server_state}
    if auth_result is not None:
        attributes[LABEL_AUTH_RESULT] = auth_result
    observability.increment_counter(
        SERVER_SESSIONS_REJECTED_TOTAL,
        attributes=attributes,
    )


def observe_connections_active(count: int, *, server_state: ServerState) -> None:
    """Observe the live active-connection count (point-in-time gauge).

    ``count`` is the shared gate's reservation count; ``server_state`` is the
    ``ServerState`` Literal. Updates :data:`SERVER_CONNECTIONS_ACTIVE` through
    ``sanitize_attributes``.
    """
    from easycat import _observability as observability

    observability.observe_gauge(
        SERVER_CONNECTIONS_ACTIVE,
        count,
        attributes={LABEL_SERVER_STATE: server_state},
    )


def observe_draining(is_draining: bool) -> None:
    """Observe the draining flag as a 0/1 gauge (:data:`SERVER_DRAINING`)."""
    from easycat import _observability as observability

    observability.observe_gauge(SERVER_DRAINING, 1 if is_draining else 0)
