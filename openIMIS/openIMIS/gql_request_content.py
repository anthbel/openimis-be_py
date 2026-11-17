"""GraphQL client errors stated without the request's content.

graphql-core quotes the request in some client errors: a syntax error carries
the offending token's text and source line, an invalid argument, variable or
default value carries the value. Those messages are rebuilt from anchored parts
holding only names, types and positions; a part of no known shape is dropped.
"""
import re

from graphql.error import GraphQLError, GraphQLSyntaxError
from graphql.execution.values import get_variable_values
from graphql.utils.get_operation_ast import get_operation_ast

_NAME = r"[_A-Za-z][_0-9A-Za-z]*"
_TYPE = r"[_A-Za-z\[\]!][_0-9A-Za-z\[\]!]*"

_VALUE_HEADS = (
    (re.compile(rf'Argument "(?P<name>{_NAME})" has invalid value '),
     'Argument "{name}" has invalid value.'),
    (re.compile(rf'Variable "\$(?P<name>{_NAME})" got invalid value '),
     'Variable "${name}" got invalid value.'),
    (re.compile(rf'Variable "\$(?P<name>{_NAME})" of type "(?P<type>{_TYPE})" has invalid default value: '),
     'Variable "${name}" of type "{type}" has invalid default value.'),
)

_DETAIL = re.compile(
    rf'(?P<prefix>(?:In field "{_NAME}": |In element #\d+: )*)'
    rf'(?:Expected type "(?P<type>{_TYPE})", found .*\.'
    rf'|(?P<kept>Expected "{_TYPE}", found (?:null|not an object)\.|Unknown field\.))'
)

_SYNTAX_DESCRIPTION = re.compile(
    r"(?P<lexer>Unexpected character|Invalid character within String"
    r"|Invalid character escape sequence|Invalid character"
    r"|Invalid number, unexpected digit after 0|Invalid number, expected digit but got"
    r"|Unterminated string)"
    r'|(?P<parser>(?:Expected (?:"[_A-Za-z]+"|[^\s,"]+), found|Unexpected) [^\s"]+)'
)


def _detail(line):
    match = _DETAIL.fullmatch(line)
    if not match:
        return None
    if match.group("type"):
        return '{}Expected type "{}".'.format(match.group("prefix"), match.group("type"))
    return match.group("prefix") + match.group("kept")


def _syntax_message(error):
    """``Syntax Error <source> (line:column) <description>`` without token text or source lines."""
    locations = error.locations or []
    if not locations:
        return "Syntax Error"
    marker = "({}:{}) ".format(locations[0].line, locations[0].column)
    head, _, rest = error.message.partition(marker)
    if not rest:
        return "Syntax Error {}".format(marker.strip())
    match = _SYNTAX_DESCRIPTION.match(rest.split("\n", 1)[0])
    description = match.group(0) if match else ""
    return (head + marker + description).strip()


def _value_free_message(error):
    if isinstance(error, GraphQLSyntaxError):
        return _syntax_message(error)
    message = error.message or ""
    for head, template in _VALUE_HEADS:
        match = head.match(message)
        if match:
            # The first line holds the value; the following ones explain it.
            details = message[match.end():].split("\n")[1:]
            lines = [template.format(**match.groupdict())]
            lines += [d for d in map(_detail, details) if d]
            return "\n".join(lines)
    return None


def without_request_content(error):
    """Rewrite, in place, the message of an error that quotes the request."""
    if isinstance(error, GraphQLError):
        message = _value_free_message(error)
        if message is not None:
            error.message = message
            error.args = (message,)
    return error


def invalid_variable_error(document, variables, operation_name):
    """The client error for the first variable whose value cannot be coerced, if any.

    A scalar's parser can raise a plain exception (``int("abc")``) that quotes
    the value; coercing each variable again tells such a refusal apart from a
    server failure without reading the exception.
    """
    operation = get_operation_ast(document.document_ast, operation_name)
    for definition in (operation.variable_definitions if operation else None) or []:
        try:
            get_variable_values(document.schema, [definition], variables)
        except Exception:
            return GraphQLError(
                'Variable "${}" got invalid value.'.format(definition.variable.name.value),
                [definition],
            )
    return None
