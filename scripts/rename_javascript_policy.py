"""Bounded JavaScript construction syntax; no runtime or type inference."""
from __future__ import annotations

import hashlib
from importlib.metadata import version

MAX_SOURCE_BYTES = 2_000_000
MAX_NODES = 100_000
# This is a closed syntax vocabulary, not a list discovered from a new parser.
SUPPORTED_NODES = frozenset('''
program hash_bang_line comment identifier property_identifier number string
string_fragment escape_sequence regex regex_pattern regex_flags true false null
undefined lexical_declaration variable_declaration variable_declarator
expression_statement statement_block empty_statement parenthesized_expression
function_declaration generator_function_declaration function_expression
generator_function arrow_function formal_parameters required_parameter
optional_parameter rest_pattern assignment_pattern object_assignment_pattern
return_statement throw_statement if_statement else_clause switch_statement
switch_body switch_case switch_default break_statement continue_statement
while_statement do_statement for_statement for_in_statement try_statement
catch_clause finally_clause debugger_statement labeled_statement with_statement
await_expression yield_expression unary_expression update_expression
binary_expression assignment_expression augmented_assignment_expression
ternary_expression sequence_expression call_expression new_expression arguments
member_expression subscript_expression optional_chain template_string
template_substitution array object pair spread_element shorthand_property_identifier
array_pattern object_pattern pair_pattern shorthand_property_identifier_pattern
method_definition computed_property_name class class_declaration class_body
class_heritage field_definition static_block this super import import_statement
import_clause namespace_import named_imports import_specifier export_statement
export_clause export_specifier
'''.split())


def inspect_tree(root, source: bytes):
    """Return exact construction sites and unwaivable syntax/resource failures."""
    pending = [root]
    remaining = MAX_NODES
    found = []
    while pending:
        remaining -= 1
        if remaining < 0:
            return [*found, (1, 'unsupported JavaScript analysis budget exhausted')]
        node = pending.pop()
        line, column = node.start_point
        if node.is_error or node.is_missing or node.type not in SUPPORTED_NODES:
            found.append((line + 1, f'unsupported JavaScript syntax node ({node.type}); use the rename-compat contract helper'))
        kind = None
        if node.type in {'binary_expression', 'augmented_assignment_expression'}:
            operator = node.child_by_field_name('operator')
            if operator is not None and operator.text in {b'+', b'+='}:
                kind = operator.text.decode('ascii')
        elif node.type == 'template_string' and any(child.type == 'template_substitution' for child in node.named_children):
            kind = 'interpolated template'
        elif node.type == 'call_expression':
            function = node.child_by_field_name('function')
            while function is not None and function.type == 'parenthesized_expression' and len(function.named_children) == 1:
                function = function.named_children[0]
            if function is not None and function.type in {'member_expression', 'subscript_expression'}:
                member = function.child_by_field_name('property') or function.child_by_field_name('index')
                if function.type == 'subscript_expression':
                    # Computed method selection cannot prove which constructor runs.
                    kind = 'computed member call'
                elif member is not None and member.text in {b'join', b'concat'}:
                    kind = '.' + member.text.decode('ascii') + '()'
        if kind:
            end_line, end_column = node.end_point
            digest = hashlib.sha256(source[node.start_byte:node.end_byte]).hexdigest()
            found.append((line + 1, f'unsupported JavaScript construction ({kind}) at {column + 1}-{end_line + 1}:{end_column + 1} sha256={digest}; use the rename-compat contract helper'))
        pending.extend(reversed(node.named_children))
    return found


def constructions(text: str):
    # Bound allocation and parsing before entering the native parser.
    if len(text) > MAX_SOURCE_BYTES:
        return [(1, 'unsupported JavaScript source size')]
    try:
        source = text.encode('utf-8')
    except UnicodeEncodeError:
        return [(1, 'unsupported ambiguous JavaScript encoding')]
    if len(source) > MAX_SOURCE_BYTES:
        return [(1, 'unsupported JavaScript source size')]
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_javascript
        if version('tree-sitter') != '0.25.2' or version('tree-sitter-javascript') != '0.25.0':
            return [(1, 'unsupported JavaScript parser version; install the pinned development dependencies')]
    except ImportError:
        return [(1, 'unsupported JavaScript parser unavailable; install the pinned development dependencies')]
    parser = Parser(Language(tree_sitter_javascript.language()))
    parser.timeout_micros = 1_000_000
    try:
        tree = parser.parse(source)
    except ValueError:
        return [(1, 'unsupported JavaScript parse timeout')]
    if tree is None:
        return [(1, 'unsupported JavaScript parse timeout')]
    if tree.root_node.has_error:
        return [(1, 'unsupported ambiguous JavaScript syntax')]
    return inspect_tree(tree.root_node, source)
