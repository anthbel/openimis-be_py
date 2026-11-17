import json
import logging
from unittest import mock

from django.test import TestCase

from openIMIS.settings import SITE_ROOT

# The marker identifies request content in responses and log records; the rest
# of the canary carries the delimiters the redaction must not depend on.
MARKER = "SAMPLE9f3c"
CANARY = MARKER + ', found ".x'
TASK_GROUP_ID = "8f2b6a2e-3c1d-4c55-9a4e-0d6f1b2c3d4e"

USERS_BY_FIRST = "query($x: Int) { users(first: $x) { totalCount } }"
CREATE_TASK_FLOW = (
    "mutation($input: CreateTaskFlowMutationInput!) "
    "{ createTaskFlow(input: $input) { clientMutationId } }"
)


def _graphql_string(value):
    return json.dumps(value)


class GraphQLRequestContentTest(TestCase):
    """Client errors are answered and logged without the request's values."""

    def _post(self, query, variables=None):
        records = []
        call_handlers = logging.Logger.callHandlers

        def capture(logger, record):
            records.append(record)
            return call_handlers(logger, record)

        body = {"query": query}
        if variables is not None:
            body["variables"] = variables
        with mock.patch.object(logging.Logger, "callHandlers", capture), \
                mock.patch.object(logging.Logger, "isEnabledFor", lambda logger, level: True), \
                mock.patch("openIMIS.views.capture_exception") as capture_exception:
            response = self.client.post(
                f"/{SITE_ROOT()}graphql",
                data=json.dumps(body),
                content_type="application/json",
                secure=True,
            )
        return response, records, capture_exception

    def _assert_client_error(self, query, variables, expected_message, expected_location):
        response, records, capture_exception = self._post(query, variables)

        content = response.content.decode()
        self.assertNotIn(MARKER, content)
        self.assertEqual(response.status_code, 400)
        error = response.json()["errors"][0]
        self.assertEqual(error["message"], expected_message)
        self.assertEqual(error["extensions"]["code"], "BAD_REQUEST")

        formatter = logging.Formatter("%(levelname)s %(name)s: %(message)s")
        for record in records:
            self.assertNotIn(MARKER, formatter.format(record), record.name)
        self.assertFalse(
            [r for r in records if r.name == "openIMIS.views" and r.levelno >= logging.ERROR]
        )
        capture_exception.assert_not_called()

        client_errors = [
            r.getMessage() for r in records
            if r.name == "openIMIS.views" and r.getMessage().startswith("GraphQL client error")
        ]
        self.assertEqual(len(client_errors), 1, client_errors)
        self.assertIn(f"at {expected_location}", client_errors[0])
        self.assertIn(error["extensions"]["errorId"], client_errors[0])
        return client_errors[0], error["extensions"]["errorId"]

    def test_syntax_error_omits_source_line_and_token_value(self):
        query = "query { __typename " + _graphql_string(CANARY) + " }"
        logged, error_id = self._assert_client_error(
            query, None, "Syntax Error GraphQL (1:20) Expected Name, found String", "1:20"
        )
        self.assertEqual(
            logged, f"GraphQL client error {error_id}: GraphQLSyntaxError at 1:20, path -"
        )

    def test_invalid_argument_literal_omits_value(self):
        query = "query { users(first: " + _graphql_string(CANARY) + ") { totalCount } }"
        self._assert_client_error(
            query, None,
            'Argument "first" has invalid value.\nExpected type "Int".',
            "1:22",
        )

    def test_invalid_variable_default_omits_value(self):
        query = (
            "query($x: Int = " + _graphql_string(CANARY) + ") "
            "{ users(first: $x) { totalCount } }"
        )
        self._assert_client_error(
            query, None,
            'Variable "$x" of type "Int" has invalid default value.\nExpected type "Int".',
            "1:17",
        )

    def test_scalar_variable_that_fails_to_parse_is_a_client_error(self):
        self._assert_client_error(
            USERS_BY_FIRST, {"x": CANARY}, 'Variable "$x" got invalid value.', "1:7"
        )

    def test_out_of_range_variable_is_a_client_error(self):
        response, records, _ = self._post(USERS_BY_FIRST, {"x": 99999999999})
        self.assertEqual(response.status_code, 400)
        error = response.json()["errors"][0]
        self.assertEqual(error["message"], 'Variable "$x" got invalid value.')
        self.assertEqual(error["extensions"]["code"], "BAD_REQUEST")
        formatter = logging.Formatter("%(message)s")
        for record in records:
            self.assertNotIn("99999999999", formatter.format(record), record.name)

    def test_input_object_with_unknown_field_omits_the_input(self):
        variables = {"input": {
            "code": CANARY,
            "steps": [{"taskGroupId": TASK_GROUP_ID}],
            "note": 1,
        }}
        self._assert_client_error(
            CREATE_TASK_FLOW, variables,
            'Variable "$input" got invalid value.\nIn field "note": Unknown field.',
            "1:10",
        )

    def test_nested_enum_value_is_omitted(self):
        variables = {"input": {
            "code": "flow",
            "steps": [{"taskGroupId": TASK_GROUP_ID, "completionPolicy": CANARY}],
        }}
        self._assert_client_error(
            CREATE_TASK_FLOW, variables,
            'Variable "$input" got invalid value.\n'
            'In field "steps": In element #0: In field "completionPolicy": '
            'Expected type "StepCompletionPolicyEnum".',
            "1:10",
        )

    def test_nested_custom_scalar_that_fails_to_parse_is_a_client_error(self):
        variables = {"input": {"code": "flow", "steps": [{"taskGroupId": CANARY}]}}
        self._assert_client_error(
            CREATE_TASK_FLOW, variables, 'Variable "$input" got invalid value.', "1:10"
        )
