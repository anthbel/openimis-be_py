import importlib
import json
from unittest import mock, skipIf

from django.core import signals
from django.core.handlers.wsgi import WSGIHandler
from django.db import close_old_connections
from django.test import RequestFactory, TestCase

from openIMIS.settings import SITE_ROOT
from openIMIS.views import OpenIMISGraphQLView

try:
    import sentry_sdk
    from sentry_sdk.transport import Transport
except ImportError:  # sentry_sdk is optional (see sentry-requirements.txt)
    sentry_sdk = None
    Transport = object

MARKER = "SAMPLE9f3c"
QUERY = "query($x: String!) { __type(name: $x) { name } }"


class CapturingTransport(Transport):
    """Keeps every envelope the SDK would send; nothing leaves the process."""

    def __init__(self):
        super().__init__()
        self.envelopes = []

    def capture_envelope(self, envelope):
        self.envelopes.append(envelope)

    def items(self, *types):
        return [
            item.payload.json
            for envelope in self.envelopes
            for item in envelope.items
            if item.type in types
        ]


@skipIf(sentry_sdk is None, "sentry_sdk is not installed")
class SentryRequestContentTest(TestCase):
    """With a DSN, Sentry receives no request body, body span data or frame locals."""

    def setUp(self):
        sentry_settings = importlib.import_module("openIMIS.settings.sentry")
        self.transport = CapturingTransport()
        sentry_sdk.init(**{
            **sentry_settings.init_kwargs,
            "dsn": "https://public@sentry.invalid/1",
            "transport": self.transport,
            "traces_sample_rate": 1.0,
        })
        self.addCleanup(sentry_sdk.init)
        tracing = mock.patch("openIMIS.tracer.IS_SENTRY_ENABLED", True)
        tracing.start()
        self.addCleanup(tracing.stop)

    def _post(self, query, variables):
        """POST through the WSGI handler, where the Django integration traces requests."""
        environ = RequestFactory().post(
            f"/{SITE_ROOT()}graphql",
            data=json.dumps({"query": query, "variables": variables}),
            content_type="application/json",
            secure=True,
        ).environ
        status = []

        def start_response(status_line, headers, exc_info=None):
            status.append(int(status_line.split()[0]))

        # As the test client does: closing the connection would end the test transaction.
        signals.request_started.disconnect(close_old_connections)
        signals.request_finished.disconnect(close_old_connections)
        try:
            response = WSGIHandler()(environ, start_response)
            b"".join(response)
            response.close()
        finally:
            signals.request_started.connect(close_old_connections)
            signals.request_finished.connect(close_old_connections)
        return status[0]

    def test_unexpected_error_and_transaction_carry_no_request_content(self):
        with mock.patch.object(
            OpenIMISGraphQLView, "get_root_value", side_effect=RuntimeError("failure")
        ):
            status = self._post(QUERY, {"x": MARKER})
        self.assertEqual(status, 400)
        sentry_sdk.flush()

        errors = [e for e in self.transport.items("event") if e.get("exception")]
        transactions = self.transport.items("transaction")
        self.assertTrue(errors, "the unexpected error is reported")
        self.assertTrue(transactions, "the request is traced")

        for envelope in self.transport.envelopes:
            self.assertNotIn(MARKER.encode(), envelope.serialize())
        for event in errors + transactions:
            self.assertNotIn("data", event.get("request", {}))
        for event in errors:
            for exception in event["exception"]["values"]:
                for frame in (exception.get("stacktrace") or {}).get("frames", []):
                    self.assertNotIn("vars", frame)
        for transaction in transactions:
            for span in transaction.get("spans", []):
                self.assertNotIn("Body", span.get("data", {}))

    def test_well_formed_request_transaction_carries_no_request_content(self):
        status = self._post(QUERY, {"x": MARKER})
        self.assertEqual(status, 200)
        sentry_sdk.flush()

        transactions = self.transport.items("transaction")
        self.assertTrue(transactions, "the request is traced")
        for envelope in self.transport.envelopes:
            self.assertNotIn(MARKER.encode(), envelope.serialize())

    def test_init_options_exclude_request_content(self):
        options = importlib.import_module("openIMIS.settings.sentry").init_kwargs
        self.assertIs(options["send_default_pii"], False)
        self.assertEqual(options["max_request_body_size"], "never")
        self.assertIs(options["include_local_variables"], False)
        event = {"request": {"url": "/api/graphql", "data": {"variables": {"x": MARKER}}}}
        self.assertNotIn("data", options["before_send_transaction"](event, {})["request"])
