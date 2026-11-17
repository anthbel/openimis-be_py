import json
import logging
from unittest import mock, skipIf, skipUnless

from django.apps import apps
from django.test import TestCase

from openIMIS.settings import SITE_ROOT
from openIMIS.tests.log_capture import capture_handled_records, formatted

try:
    import sentry_sdk
    from sentry_sdk.transport import Transport
except ImportError:  # sentry_sdk is optional (see sentry-requirements.txt)
    sentry_sdk = None
    Transport = object

MARKER = "SAMPLE7d2e"

# Resolver refusals that quote the request: the individual resolvers raise a
# ValidationError naming the value they could not use.
REFUSALS = {
    "group_id": 'query { individual(groupId: "%s") { totalCount } }' % MARKER,
    "benefit_plan_id": 'query { individual(benefitPlanId: "%s") { totalCount } }' % MARKER,
    "custom_filters": 'query { individual(customFilters: ["%s"]) { totalCount } }' % MARKER,
}
UNEXPECTED = "query { individual { totalCount } }"


def _failing_filter(**kwargs):
    raise ValueError("cannot read %s" % MARKER)


class CapturingTransport(Transport):
    """Keeps every envelope the SDK would send; nothing leaves the process."""

    def __init__(self):
        super().__init__()
        self.envelopes = []

    def capture_envelope(self, envelope):
        self.envelopes.append(envelope)

    def events(self):
        return [
            item.payload.json
            for envelope in self.envelopes
            for item in envelope.items
            if item.type == "event"
        ]


@skipUnless(apps.is_installed("individual"), "the individual module is not installed")
class ResolverErrorTestMixin:

    @classmethod
    def setUpTestData(cls):
        from core.models.openimis_graphql_test_case import BaseTestContext
        from core.test_helpers import create_admin_role, create_test_interactive_user

        admin = create_test_interactive_user(
            username="assemblyErrorsAdmin", roles=[create_admin_role().id]
        )
        cls.token = BaseTestContext(user=admin).get_jwt()

    def _post(self, query):
        return self.client.post(
            f"/{SITE_ROOT()}graphql",
            data=json.dumps({"query": query}),
            content_type="application/json",
            secure=True,
            HTTP_AUTHORIZATION=f"Bearer {self.token}",
        )

    def _post_unexpected(self):
        with mock.patch("individual.schema.append_validity_filter", side_effect=_failing_filter):
            return self._post(UNEXPECTED)


class ResolverErrorLoggingTest(ResolverErrorTestMixin, TestCase):
    """Each error is logged once, by the view, under the id the response carries."""

    def _view_records(self, records):
        self.assertFalse(
            [formatted(r) for r in records.values() if r.name.startswith("graphql")]
        )
        return [r for r in records.values() if r.name == "openIMIS.views"]

    def test_refusals_answer_their_message_and_log_no_request_content(self):
        for case, query in REFUSALS.items():
            with self.subTest(case), capture_handled_records() as records:
                response = self._post(query)

            for record in records.values():
                self.assertNotIn(MARKER, formatted(record), record.name)
            view_records = self._view_records(records)

            error = response.json()["errors"][0]
            self.assertIn(MARKER, error["message"])
            self.assertEqual(error["extensions"]["code"], "BAD_REQUEST")
            error_id = error["extensions"]["errorId"]
            self.assertEqual(len(view_records), 1, [formatted(r) for r in view_records])
            self.assertEqual(view_records[0].levelno, logging.INFO)
            self.assertEqual(
                view_records[0].getMessage(),
                f"GraphQL client error {error_id}: ValidationError at 1:9, path individual",
            )

    def test_unexpected_error_is_logged_once_with_its_traceback(self):
        with capture_handled_records() as records:
            response = self._post_unexpected()

        view_records = self._view_records(records)

        error = response.json()["errors"][0]
        self.assertNotIn(MARKER, response.content.decode())
        self.assertEqual(error["extensions"]["code"], "INTERNAL_ERROR")
        error_id = error["extensions"]["errorId"]
        self.assertEqual(len(view_records), 1, [formatted(r) for r in view_records])
        record = view_records[0]
        self.assertEqual(record.levelno, logging.ERROR)
        self.assertEqual(
            record.getMessage(),
            f"Unexpected GraphQL error {error_id}: ValueError at 1:9, path individual",
        )
        self.assertIs(record.exc_info[0], ValueError)


@skipIf(sentry_sdk is None, "sentry_sdk is not installed")
class ResolverErrorSentryTest(ResolverErrorTestMixin, TestCase):
    """Sentry gets unexpected errors only, without their message or frame locals."""

    def setUp(self):
        import importlib

        sentry_settings = importlib.import_module("openIMIS.settings.sentry")
        self.transport = CapturingTransport()
        sentry_sdk.init(**{
            **sentry_settings.init_kwargs,
            "dsn": "https://public@sentry.invalid/1",
            "transport": self.transport,
        })
        self.addCleanup(sentry_sdk.init)

    def _assert_nothing_carries_the_marker(self):
        for envelope in self.transport.envelopes:
            self.assertNotIn(MARKER.encode(), envelope.serialize())

    def test_refusals_send_nothing(self):
        for case, query in REFUSALS.items():
            with self.subTest(case):
                self._post(query)
        sentry_sdk.flush()
        self.assertEqual(self.transport.events(), [])

    def test_unexpected_error_is_sent_once_without_message_or_locals(self):
        self._post_unexpected()
        sentry_sdk.flush()

        events = self.transport.events()
        self.assertEqual(len(events), 1, events)
        exception = events[0]["exception"]["values"][-1]
        self.assertEqual(exception["type"], "ValueError")
        self.assertNotIn("value", exception)
        frames = exception["stacktrace"]["frames"]
        self.assertTrue(frames)
        for frame in frames:
            self.assertNotIn("vars", frame)
        self._assert_nothing_carries_the_marker()
