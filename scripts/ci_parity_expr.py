"""Bounded GitHub expression grammar. No dynamic Python execution."""

from __future__ import annotations

import json
import math
import re
import string


class Unmapped(ValueError):
    """An input needs explicit planner/evaluator support before execution."""


CONTEXTS = {'github', 'needs', 'steps', 'matrix', 'job', 'runner', 'env', 'inputs'}
FUNCTIONS = {
    'always': (0, 0), 'success': (0, 0), 'failure': (0, 0), 'cancelled': (0, 0),
    'contains': (2, 2), 'startswith': (2, 2), 'endswith': (2, 2),
    'format': (1, 100), 'fromjson': (1, 1), 'join': (1, 2),
}
STATUS_FUNCTIONS = {'always', 'success', 'failure', 'cancelled'}
CONTEXT_FIELDS = {
    'github': {'event', 'event_name', 'sha', 'ref', 'head_ref', 'base_ref',
               'run_id', 'run_attempt', 'workflow', 'workspace'},
    'runner': {'temp', 'os', 'arch'},
    'job': {'status', 'services'},
}
TOKEN = re.compile(
    r"\s*(?:(?P<string>'(?:[^']|'')*')|"
    r'(?P<number>-?(?:0[xX][0-9a-fA-F]+|(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?))|'
    r'(?P<name>[A-Za-z_][A-Za-z_0-9-]*)|(?P<op>\&\&|\|\||==|!=|<=|>=|[!<>().,\[\]*]))'
)
PRECEDENCE = {'||': 1, '&&': 2, '==': 3, '!=': 3, '<': 4, '<=': 4, '>': 4, '>=': 4}


def truth(value):
    return not (value is None or value is False or value == '' or
                (isinstance(value, (int, float)) and (value == 0 or math.isnan(value))))


def number(value):
    if value is None or value == '':
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value.strip()) if value.strip() else 0
            if type(parsed) in (int, float):
                return parsed
        except (ValueError, TypeError):
            pass
    return math.nan


def equal(left, right):
    if isinstance(left, (dict, list)) or isinstance(right, (dict, list)):
        return left is right
    if type(left) is not type(right):
        return number(left) == number(right)
    if isinstance(left, str):
        return left.casefold() == right.casefold()
    return left == right


def text(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list)):
        raise Unmapped('object/array string conversion; add support in ci_parity_expr')
    return str(value)


class Parser:
    def __init__(self, source):
        self.tokens = []
        position = 0
        while position < len(source.rstrip()):
            match = TOKEN.match(source, position)
            if not match:
                raise Unmapped(f'expression syntax near {source[position:position + 40]!r}; use ci_parity_expr grammar')
            self.tokens.append((match.lastgroup, match.group(match.lastgroup)))
            position = match.end()
        self.tokens.append(('end', ''))
        self.index = 0

    def peek(self):
        return self.tokens[self.index][1]

    def take(self, wanted=None):
        token = self.tokens[self.index]
        if wanted is not None and token[1] != wanted:
            raise Unmapped(f'expression expected {wanted!r}, found {token[1]!r}; extend ci_parity_expr')
        self.index += 1
        return token

    def expression(self, minimum=1):
        if self.peek() == '!':
            self.take()
            left = ('not', self.expression(5))
        elif self.peek() == '(':
            self.take()
            left = self.expression()
            self.take(')')
        else:
            kind, value = self.take()
            if kind == 'string':
                left = ('literal', value[1:-1].replace("''", "'"))
            elif kind == 'number':
                left = ('literal', int(value, 16) if 'x' in value.lower() else json.loads(value))
            elif kind == 'name':
                lowered = value.lower()
                if self.peek() == '(':
                    if lowered not in FUNCTIONS:
                        raise Unmapped(f'function {value}; add support in ci_parity_expr')
                    self.take()
                    args = []
                    if self.peek() != ')':
                        args.append(self.expression())
                        while self.peek() == ',':
                            self.take()
                            args.append(self.expression())
                    self.take(')')
                    low, high = FUNCTIONS[lowered]
                    if not low <= len(args) <= high:
                        raise Unmapped(f'function {value} arity; see ci_parity_expr')
                    if lowered == 'format' and args[0][0] == 'literal':
                        format_text(text(args[0][1]), [''] * (len(args) - 1))
                    left = ('call', lowered, args)
                elif lowered in ('true', 'false', 'null'):
                    left = ('literal', {'true': True, 'false': False, 'null': None}[lowered])
                else:
                    if value not in CONTEXTS:
                        raise Unmapped(f'context {value}; add support in ci_parity_expr')
                    left = ('context', value)
            else:
                raise Unmapped(f'expression token {value!r}; use ci_parity_expr grammar')
        while self.peek() in ('.', '['):
            if self.take()[1] == '.':
                kind, field = self.take()
                if kind != 'name' and field != '*':
                    raise Unmapped('property name required; see ci_parity_expr')
                left = ('get', left, ('literal', field))
            else:
                key = self.expression()
                self.take(']')
                left = ('get', left, key)
        while PRECEDENCE.get(self.peek(), 0) >= minimum:
            operation = self.take()[1]
            left = ('binary', operation, left, self.expression(PRECEDENCE[operation] + 1))
        return left


def parse(source):
    if isinstance(source, bool):
        return ('literal', source)
    if not isinstance(source, str):
        raise Unmapped('expression must be a string or boolean; see ci_parity_expr')
    source = source.strip()
    if source.startswith('${{') and source.endswith('}}'):
        source = source[3:-2].strip()
    parser = Parser(source)
    try:
        node = parser.expression()
        if parser.peek():
            raise Unmapped(f'expression trailing token {parser.peek()!r}; see ci_parity_expr')
        for item in walk(node):
            if item[0] == 'get' and item[1][0] == 'context' and item[1][1] in CONTEXT_FIELDS:
                if item[2][0] != 'literal' or item[2][1] not in CONTEXT_FIELDS[item[1][1]]:
                    raise Unmapped(f'context field outside supported {item[1][1]} fields; extend ci_parity_expr and runner contexts')
        return node
    except (IndexError, RecursionError, ValueError) as error:
        if isinstance(error, Unmapped):
            raise
        raise Unmapped(f'expression outside ci_parity_expr grammar: {source!r}') from error


class Projection(list):
    """A wildcard projection, distinct from an ordinary array property lookup."""


def property_value(value, key):
    if key == '*':
        if isinstance(value, dict):
            return Projection(value.values())
        return Projection(value) if isinstance(value, list) else Projection()
    if isinstance(value, Projection):
        return Projection(property_value(item, key) for item in value)
    if isinstance(value, dict):
        return value.get(key, '')
    if isinstance(value, list) and type(key) is int:
        return value[key] if 0 <= key < len(value) else ''
    return ''


def format_text(template, args):
    result = []
    try:
        for literal, field, spec, conversion in string.Formatter().parse(template):
            result.append(literal)
            if field is not None:
                if not field.isdigit() or spec or conversion:
                    raise Unmapped('format supports numbered placeholders only; see ci_parity_expr')
                result.append(text(args[int(field)]))
    except (ValueError, IndexError) as error:
        raise Unmapped('invalid format placeholder; see ci_parity_expr') from error
    return ''.join(result)


def execute(node, context, status='success', ancestor_failure=False):
    kind = node[0]
    if kind == 'literal':
        return node[1]
    if kind == 'context':
        return context.get(node[1], {})
    if kind == 'get':
        return property_value(execute(node[1], context, status, ancestor_failure),
                              execute(node[2], context, status, ancestor_failure))
    if kind == 'not':
        return not truth(execute(node[1], context, status, ancestor_failure))
    if kind == 'binary':
        op = node[1]
        left = execute(node[2], context, status, ancestor_failure)
        if op == '&&' and not truth(left) or op == '||' and truth(left):
            return left
        right = execute(node[3], context, status, ancestor_failure)
        if op in ('&&', '||'):
            return right
        if op in ('==', '!='):
            return equal(left, right) if op == '==' else not equal(left, right)
        if isinstance(left, str) and isinstance(right, str):
            left, right = left.casefold(), right.casefold()
        else:
            left, right = number(left), number(right)
        if op == '<':
            return left < right
        if op == '<=':
            return left <= right
        if op == '>':
            return left > right
        return left >= right
    name = node[1]
    args = [execute(arg, context, status, ancestor_failure) for arg in node[2]]
    if name in STATUS_FUNCTIONS:
        return {'always': True, 'success': status == 'success',
                'failure': status == 'failure' or ancestor_failure,
                'cancelled': status == 'cancelled'}[name]
    if name == 'contains':
        if isinstance(args[0], list):
            return any(equal(item, args[1]) for item in args[0])
        return text(args[1]).casefold() in text(args[0]).casefold()
    if name == 'startswith':
        return text(args[0]).casefold().startswith(text(args[1]).casefold())
    if name == 'endswith':
        return text(args[0]).casefold().endswith(text(args[1]).casefold())
    if name == 'format':
        return format_text(text(args[0]), args[1:])
    if name == 'fromjson':
        try:
            return json.loads(text(args[0]))
        except ValueError as error:
            raise Unmapped('invalid fromJSON value; see ci_parity_expr') from error
    if name == 'join':
        return text(args[1] if len(args) == 2 else ',').join(map(text, args[0])) if isinstance(args[0], list) else text(args[0])
    raise Unmapped(f'function {name}; add support in ci_parity_expr')


def walk(node):
    yield node
    if node[0] in ('get', 'not'):
        for child in node[1:]:
            yield from walk(child)
    elif node[0] == 'binary':
        yield from walk(node[2])
        yield from walk(node[3])
    elif node[0] == 'call':
        for child in node[2]:
            yield from walk(child)


def evaluate(source, context, status='success', ancestor_failure=False):
    return execute(parse(source), context, status, ancestor_failure)


def condition(source, context, status='success', ancestor_failure=False):
    node = parse(source if source is not None else 'success()')
    explicit = any(item[0] == 'call' and item[1] in STATUS_FUNCTIONS for item in walk(node))
    return (explicit or status == 'success') and truth(execute(node, context, status, ancestor_failure))


def segments(value):
    """Split interpolation, respecting single quotes that can contain closing braces."""
    position = 0
    while position < len(value):
        start = value.find('${{', position)
        if start == -1:
            yield False, value[position:]
            return
        yield False, value[position:start]
        index, quoted = start + 3, False
        while index < len(value):
            if value[index] == "'":
                if quoted and value[index:index + 2] == "''":
                    index += 2
                    continue
                quoted = not quoted
            if not quoted and value[index:index + 2] == '}}':
                yield True, value[start + 3:index]
                position = index + 2
                break
            index += 1
        else:
            raise Unmapped('unterminated interpolation; use ci_parity_expr grammar')


def render(value, context, status='success'):
    if not isinstance(value, str):
        return text(value)
    return ''.join(text(evaluate(part, context, status)) if expression else part
                   for expression, part in segments(value))
