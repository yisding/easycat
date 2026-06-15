"""Server metrics SKELETON — names defined as constants, NOTHING emitted (M5).

M5 ships ONLY the metric-name and label-key constants below. It does NOT:

* import :mod:`easycat._observability`,
* call ``_record_metric`` / ``increment_counter`` / ``observe_gauge`` /
  ``sanitize_attributes``,
* mutate ``METRIC_DEFINITIONS`` or ``LOW_CARDINALITY_ATTRIBUTE_KEYS``.

Registration AND emission are strictly M8 ("Server Metrics + Endpoints"), in
the SAME PR, because the observability layer is a hard allow-list:
``_record_metric`` and ``sanitize_attributes`` RAISE ``ValueError`` on any
unregistered metric name or attribute key. Emitting any of these names before
M8 registers them would crash. The M4 boundary test asserts none of these
names/labels are registered after import — keep it green by never touching the
allow-lists from here.

``easycat.transport`` is already registered (do NOT re-add it). The three NEW
labels below must be added to ``LOW_CARDINALITY_ATTRIBUTE_KEYS`` in M8.

``easycat.route`` is a PII/cardinality hazard: the key-name allow-list cannot
distinguish a route TEMPLATE from a raw path. M8 must assert the value is in the
enumerated :data:`easycat.server.routes.ROUTE_TEMPLATES` set BEFORE recording —
never a resolved/raw path. :data:`SERVER_ROUTE_TEMPLATES` re-exports that single
source of truth so the metric layer and the route layer cannot drift.
"""

from __future__ import annotations

from easycat.server.routes import ROUTE_TEMPLATES

# ── Future metric names (registered + emitted in M8) ─────────────────
SERVER_REQUESTS_TOTAL = "easycat.server.requests.total"
SERVER_REQUEST_DURATION = "easycat.server.request.duration"
SERVER_SESSIONS_REJECTED_TOTAL = "easycat.server.sessions.rejected.total"
SERVER_CONNECTIONS_ACTIVE = "easycat.server.connections.active"
SERVER_DRAINING = "easycat.server.draining"

#: All ``easycat.server.*`` metric names this layer will emit (M8 registers them).
SERVER_METRIC_NAMES: frozenset[str] = frozenset(
    {
        SERVER_REQUESTS_TOTAL,
        SERVER_REQUEST_DURATION,
        SERVER_SESSIONS_REJECTED_TOTAL,
        SERVER_CONNECTIONS_ACTIVE,
        SERVER_DRAINING,
    }
)

# ── Future label keys (NEW ones registered in M8) ────────────────────
# ``easycat.route`` / ``easycat.server_state`` / ``easycat.auth_result`` are
# NEW — M8 adds them to ``LOW_CARDINALITY_ATTRIBUTE_KEYS``. ``easycat.transport``
# is already registered and is NOT re-added.
LABEL_ROUTE = "easycat.route"
LABEL_SERVER_STATE = "easycat.server_state"
LABEL_AUTH_RESULT = "easycat.auth_result"

#: The NEW label keys M8 must add to the observability allow-list.
NEW_SERVER_LABEL_KEYS: frozenset[str] = frozenset(
    {
        LABEL_ROUTE,
        LABEL_SERVER_STATE,
        LABEL_AUTH_RESULT,
    }
)

#: Enumerated route templates ``easycat.route`` may carry (single source of
#: truth re-exported from :mod:`easycat.server.routes`). M8 asserts a value is
#: in this set before recording the label.
SERVER_ROUTE_TEMPLATES: frozenset[str] = ROUTE_TEMPLATES
