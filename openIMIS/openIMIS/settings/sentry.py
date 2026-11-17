import os
import logging

logger = logging.getLogger(__name__)

try:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import ignore_logger
except ModuleNotFoundError:
    sentry_sdk = None
    DjangoIntegration = None
    ignore_logger = None
    logger.error(
        "sentry_sdk has to be installed to use Sentry. Run `pip install --upgrade sentry_sdk` to install it."
    )

try:
    # scrubber is supportedb by version 1.29 or higher
    from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber
    HAS_SCRUBBER = True
except ModuleNotFoundError:
    DEFAULT_DENYLIST = []
    EventScrubber = None
    HAS_SCRUBBER = False
    logger.error(
        "You need to upgrade sentry_sdk to Version >= 1.29. Run `pip install --upgrade sentry_sdk` to upgrade it."
    )

SENTRY_DSN = os.environ.get("SENTRY_DSN", None)
SENTRY_SAMPLE_RATE = os.environ.get("SENTRY_SAMPLE_RATE", "0.2")
IS_SENTRY_ENABLED = False

SENSITIVE_KEYS = {
    "password", "passwd", "pwd",
    "token", "access_token", "refresh_token",
    "authorization", "auth",
    "api_key", "apikey",
    "secret",
    "host", "port", "user", "username",
    "dbname"
}

denylist = DEFAULT_DENYLIST + ["dsn", "conn_params"]
denylist += list(SENSITIVE_KEYS)

def sanitize(value):
    """Nettoyage récursif"""
    if isinstance(value, dict):
        clean = {}
        for k, v in value.items():
            if any(s in k.lower() for s in SENSITIVE_KEYS):
                clean[k] = "***"
            else:
                clean[k] = sanitize(v)
        return clean

    elif isinstance(value, list):
        return [sanitize(v) for v in value]

    return value


def _drop_request_body(event):
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("query_string", None)
    return event


def _scrub_messages(event):
    """Keep exception types and stack frames; drop every message and frame locals."""
    for exc in (event.get("exception") or {}).get("values") or []:
        exc.pop("value", None)
        for frame in (exc.get("stacktrace") or {}).get("frames") or []:
            frame.pop("vars", None)
    if "logentry" in event or "message" in event:
        event.pop("message", None)
        event["logentry"] = {"message": "Log record from %s" % event.get("logger", "an unnamed logger")}
    for breadcrumb in (event.get("breadcrumbs") or {}).get("values") or []:
        breadcrumb.pop("message", None)
        breadcrumb.pop("data", None)
    return event


def before_send(event, hint):
    """Send an event without request content or messages; on any failure, drop it."""
    try:
        return _scrub_messages(_drop_request_body(sanitize(event)))
    except Exception:
        return None


def before_send_transaction(event, hint):
    """Drop the request body from a transaction; on any failure, drop the transaction."""
    try:
        return _drop_request_body(event)
    except Exception:
        return None


# Request bodies and frame locals can hold person data and biometric samples:
# neither is collected, whatever the DSN.
init_kwargs = {
    "dsn": SENTRY_DSN,
    "integrations": [DjangoIntegration()] if DjangoIntegration else [],
    "traces_sample_rate": float(SENTRY_SAMPLE_RATE),
    "send_default_pii": False,
    "max_request_body_size": "never",
    "include_local_variables": False,
    "before_send": before_send,
    "before_send_transaction": before_send_transaction,
}

# graphql-core logs every resolver exception, client refusals included, with
# its message; openIMIS.views reports the unexpected ones itself.
if ignore_logger is not None:
    ignore_logger("graphql.execution.utils")
    ignore_logger("graphql.execution.executor")

if HAS_SCRUBBER:
    init_kwargs["event_scrubber"] = EventScrubber(
        denylist=denylist
    )

if SENTRY_DSN is not None:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.django import DjangoIntegration

        sentry_sdk.init(**init_kwargs)
        IS_SENTRY_ENABLED = True
    except ModuleNotFoundError:
        logger.error(
            "sentry_sdk has to be installed to use Sentry. Run `pip install --upgrade sentry_sdk` to install it."
        )
