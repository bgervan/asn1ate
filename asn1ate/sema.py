# Copyright (c) 2013-2017, Schneider Electric Buildings AB
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of Schneider Electric Buildings AB nor the
#       names of contributors may be used to endorse or promote products
#       derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from asn1ate import parser


def build_semantic_model(parse_result):
    """ Build a semantic model of the ASN.1 definition
    from a syntax tree generated by asn1ate.parser.
    """
    root = []
    for token in parse_result:
        _assert_annotated_token(token)
        root.append(_create_sema_node(token))

    # Head back through the model to act on any automatic tagging
    # Objects in root must be Modules by definition of the parser
    for module in root:
        if module.tag_default == TagImplicitness.AUTOMATIC:
            # Automatic tagging is on - wrap the members of constructed types
            for descendant in module.descendants():
                if isinstance(descendant, ConstructedType):
                    descendant.auto_tag()

    return root


def topological_sort(assignments):
    """ Algorithm adapted from:
    http://en.wikipedia.org/wiki/Topological_sorting.

    Use this in code generators to sort assignments in dependency order, IFF
    there are no circular dependencies.

    Assumes assignments is an iterable of items with two methods:
    - reference_name() -- returns the reference name of the assignment
    - references() -- returns an iterable of reference names
    upon which the assignment depends.
    """
    graph = dict((a.reference_name(), a.references()) for a in assignments)

    def has_predecessor(node):
        for predecessors in graph.values():
            if node in predecessors:
                return True

        return False

    # Build a topological order of reference names
    topological_order = []
    roots = [name for name in graph.keys()
             if not has_predecessor(name)]

    while roots:
        root = roots.pop()

        # Remove the current node from the graph
        # and collect all new roots (the nodes that
        # were previously only referenced from n)
        successors = graph.pop(root, set())
        roots.extend(successor for successor in successors
                     if not has_predecessor(successor))

        topological_order.insert(0, root)

    if graph:
        raise Exception('Can\'t sort cyclic references: %s' % graph)

    # Sort the actual assignments based on the topological order
    return sorted(assignments,
                  key=lambda a: topological_order.index(a.reference_name()))


def dependency_sort(assignments):
    """ We define a dependency sort as a Tarjan strongly-connected
    components resolution. Tarjan's algorithm happens to topologically
    sort as a by-product of finding strongly-connected components.

    Use this in code generators to sort assignments in dependency order, if
    there are circular dependencies. It is slower than ``topological_sort``.

    In the sema model, each node depends on types mentioned in its
    ``descendants``. The model is nominally a tree, except ``descendants``
    can contain node references forming a cycle.

    Returns a list of tuples, where each item represents a component
    in the graph. Ideally they're one-tuples, but if the graph has cycles
    items can have any number of elements constituting a cycle.

    This is nice, because the output is in perfect dependency order,
    except for the cycle components, where there is no order. They
    can be detected on the basis of their plurality and handled
    separately.
    """
    # Build reverse-lookup table from name -> node.
    assignments_by_name = {a.reference_name(): a for a in assignments}

    # Build the dependency graph.
    graph = {}
    for assignment in assignments:
        references = assignment.references()
        graph[assignment] = [assignments_by_name[r] for r in references
                             if r in assignments_by_name]

    # Now let Tarjan do its work! Adapted from here:
    # http://www.logarithmic.net/pfh-files/blog/01208083168/tarjan.py
    index_counter = [0]
    stack = []
    lowlinks = {}
    index = {}
    result = []

    def strongconnect(node):
        # Set the depth index for this node to the smallest unused index
        index[node] = index_counter[0]
        lowlinks[node] = index_counter[0]
        index_counter[0] += 1
        stack.append(node)

        # Consider successors of `node`
        successors = graph.get(node, [])
        for successor in successors:
            if successor not in lowlinks:
                # Successor has not yet been visited; recurse on it
                strongconnect(successor)
                lowlinks[node] = min(lowlinks[node], lowlinks[successor])
            elif successor in stack:
                # the successor is in the stack and hence in the current
                # strongly connected component (SCC)
                lowlinks[node] = min(lowlinks[node], index[successor])

        # If `node` is a root node, pop the stack and generate an SCC
        if lowlinks[node] == index[node]:
            connected_component = []

            while True:
                successor = stack.pop()
                connected_component.append(successor)
                if successor == node:
                    break

            component = tuple(connected_component)
            result.append(component)

    for node in sorted(graph.keys(), key=lambda a: a.reference_name()):
        if node not in lowlinks:
            strongconnect(node)

    return result


# Registered object identifier names
REGISTERED_OID_NAMES = {
    'ccitt': 0,
    'iso': 1,
    'joint-iso-ccitt': 2,
    # ccitt
    'recommendation': 0,
    'question': 1,
    'administration': 2,
    'network-operator': 3,
    # iso
    'standard': 0,
    'registration-authority': 1,
    'member-body': 2,
    'identified-organization': 3,
    # joint-iso-ccitt
    'country': 16,
    'registration-procedures': 17
}


class TagImplicitness(object):
    """ Tag implicit/explicit enumeration """
    IMPLICIT = 0
    EXPLICIT = 1
    AUTOMATIC = 2


"""
Sema nodes

Concepts in the ASN.1 specification are mirrored here as a simple object model.

Class and member names typically follow the ASN.1 terminology, but there are
some concepts captured which are not expressed in the spec.

Most notably, we build a dependency graph of all types and values in a module,
to allow code generators to build code in dependency order.

All nodes that somehow denote a referenced type or value, either definitions
(type and value assignments) or references (referenced types, referenced values,
etc) must have a method called ``reference_name``.
"""


class SemaNode(object):
    """ Base class for all sema nodes. """

    def children(self):
        """ Return a list of all contained sema nodes.

        This implementation finds all member variables of type
        SemaNode. It also expands list members, to transparently
        handle the case where a node holds a list of other
        sema nodes.
        """
        # Collect all SemaNode members.
        members = list(vars(self).values())
        children = [m for m in members if isinstance(m, SemaNode)]

        # Expand SemaNodes out of list members, but do not recurse
        # through lists of lists.
        list_members = [m for m in members if isinstance(m, list)]
        for m in list_members:
            children.extend(n for n in m if isinstance(n, SemaNode))

        return children

    def descendants(self):
        """ Return a list of all recursively contained sema nodes.
        """
        descendants = []
        for child in self.children():
            descendants.append(child)
            descendants.extend(child.descendants())

        return descendants


class Module(SemaNode):
    def __init__(self, elements):
        self._user_types = {}

        module_reference, definitive_identifier, tag_default, extension_default, module_body = elements

        self.name = module_reference.elements[0]

        if tag_default == 'IMPLICIT TAGS':
            self.tag_default = TagImplicitness.IMPLICIT
        elif tag_default == 'EXPLICIT TAGS':
            self.tag_default = TagImplicitness.EXPLICIT
        elif tag_default == 'AUTOMATIC TAGS':
            self.tag_default = TagImplicitness.AUTOMATIC
        else:
            if tag_default is not None:
                raise Exception('Unexpected tag default: %s' % tag_default)
            # Tag default was not specified, default to explicit
            self.tag_default = TagImplicitness.EXPLICIT

        exports, imports, assignments = module_body.elements
        self.exports = _maybe_create_sema_node(exports)
        self.imports = _maybe_create_sema_node(imports)
        self.assignments = [_create_sema_node(token) for token in assignments.elements]

    def user_types(self):
        if not self._user_types:
            # Index all type assignments by name
            type_assignments = [a for a in self.assignments if isinstance(a, TypeAssignment)]
            for user_defined in type_assignments:
                self._user_types[user_defined.type_name] = user_defined.type_decl

        return self._user_types

    def resolve_type_decl(self, type_decl, referenced_modules):
        """ Recursively resolve user-defined types to their built-in
        declaration.
        """
        if isinstance(type_decl, ReferencedType):
            module = None
            if not type_decl.module_name or type_decl.module_name == self.name:
                module = self
            else:
                # Find the referenced module
                for ref_mod in referenced_modules:
                    if ref_mod.name == type_decl.module_name:
                        module = ref_mod
                        break
            if not module:
                raise Exception('Unrecognized referenced module %s in %s.' % (type_decl.module_name,
                                                                              [module.name for module in
                                                                               referenced_modules]))
            return module.resolve_type_decl(module.user_types()[type_decl.type_name], referenced_modules)
        else:
            return type_decl

    def get_type_decl(self, type_name):
        user_types = self.user_types()
        return user_types[type_name]

    def resolve_selection_type(self, selection_type_decl):
        if not isinstance(selection_type_decl, SelectionType):
            raise Exception("Expected SelectionType, was %s" % selection_type_decl.__class__.__name__)

        choice_type = self.get_type_decl(selection_type_decl.type_decl.type_name)
        for named_type in choice_type.components:
            if named_type.identifier == selection_type_decl.identifier:
                return named_type.type_decl

        return None

    def resolve_tag_implicitness(self, tag_implicitness, tagged_type_decl):
        """ The implicitness for a tag depends on three things:
        * Any written implicitness on the tag decl itself (``tag_implicitness``)
        * The module's tag default (kept in ``self.tag_default``)
        * Details of the tagged type according to X.680, 30.6c (``tagged_type_decl``)
        """
        if tag_implicitness is not None:
            return tag_implicitness

        # Tagged CHOICEs must always be explicit if the default is implicit, automatic or empty
        # See X.680, 30.6c
        if isinstance(tagged_type_decl, ChoiceType):
            return TagImplicitness.EXPLICIT

        # No tag implicitness specified, use module-default
        if self.tag_default is None:
            # Explicit is default if nothing
            return TagImplicitness.EXPLICIT
        elif self.tag_default == TagImplicitness.AUTOMATIC:
            # TODO: Expand according to rules for automatic tagging.
            return TagImplicitness.IMPLICIT

        return self.tag_default

    def __str__(self):
        lines = []
        lines += ['%s DEFINITIONS ::=' % self.name]
        lines += ['BEGIN']

        if self.exports:
            lines += [str(self.exports)]
            lines += ['']

        if self.imports:
            lines += [str(self.imports)]
            lines += ['']

        lines += [str(a) for a in self.assignments]
        lines += ['END']

        return '\n'.join(lines)

    __repr__ = __str__


class Exports(SemaNode):
    def __init__(self, elements):
        self.symbols = [s for s in elements]

    def __str__(self):
        return 'EXPORTS %s;' % ', '.join(self.symbols)

    __repr__ = __str__


class Imports(SemaNode):
    def __init__(self, elements):
        self.imports = {}
        for symbols, module, oid in elements:
            module = GlobalModuleReference(module, oid)
            self.imports.setdefault(module, []).extend(symbols)

    def __str__(self):
        lines = ['IMPORTS']
        for module, symbols in sorted(self.imports.items()):
            lines += ['  %s FROM %s' % (', '.join(symbols), module)]
        return '\n'.join(lines) + ';'

    __repr__ = __str__


class GlobalModuleReference(SemaNode):
    def __init__(self, module, oid):
        self.module = module.elements[0]
        self.oid = _maybe_create_sema_node(oid)

    def __str__(self):
        moduleref = self.module
        if self.oid:
            moduleref += ' ' + str(self.oid)
        return moduleref

    __repr__ = __str__


class Assignment(SemaNode):
    def references(self):
        """ Return a set of all reference names (both values and types) that
        this assignment depends on.

        This happens to coincide with all contained SemaNodes as exposed by
        ``descendants`` with a ``reference_name`` method.
        """
        return set(d.reference_name() for d in self.descendants()
                   if hasattr(d, 'reference_name'))


class TypeAssignment(Assignment):
    def __init__(self, elements):
        if len(elements) != 3:
            raise Exception('Malformed type assignment')
        type_name, _, type_decl = elements
        self.type_name = type_name
        self.type_decl = _create_sema_node(type_decl)

    def reference_name(self):
        return self.type_name

    def __str__(self):
        return '%s ::= %s' % (self.type_name, self.type_decl)

    __repr__ = __str__


class ValueAssignment(Assignment):
    def __init__(self, elements):
        value_name, type_name, _, value = elements
        self.value_name = value_name
        self.type_decl = _create_sema_node(type_name)
        self.value = _maybe_create_sema_node(value)

    def reference_name(self):
        return self.value_name

    def __str__(self):
        return '%s %s ::= %s' % (self.value_name, self.type_decl, self.value)

    __repr__ = __str__


class ConstructedType(SemaNode):
    """ Base type for SEQUENCE, SET and CHOICE. """

    def __init__(self, elements):
        type_name, component_tokens = elements
        self.type_name = type_name
        self.components = [_create_sema_node(token)
                           for token in component_tokens]

    def auto_tag(self):
        # Constructed types can have ExtensionMarkers as components, ignore them
        component_types = [c.type_decl for c in self.components
                           if hasattr(c, 'type_decl')]
        already_tagged = any(isinstance(c, TaggedType) for c in component_types)
        if not already_tagged:
            # Wrap components in TaggedTypes
            for tag_number, child in enumerate([c for c in self.children()
                                                if hasattr(c, 'type_decl')]):
                element = child.type_decl
                tagged_type = TaggedType((None, str(tag_number), None, element))
                child.type_decl = tagged_type

    def __str__(self):
        component_type_list = ', '.join(map(str, self.components))
        return '%s { %s }' % (self.type_name, component_type_list)

    __repr__ = __str__


class ChoiceType(ConstructedType):
    def __init__(self, elements):
        super(ChoiceType, self).__init__(elements)


class SequenceType(ConstructedType):
    def __init__(self, elements):
        super(SequenceType, self).__init__(elements)


class SetType(ConstructedType):
    def __init__(self, elements):
        super(SetType, self).__init__(elements)


class CollectionType(SemaNode):
    """ Base type for SET OF and SEQUENCE OF. """

    def __init__(self, kind, elements):
        self.kind = kind
        self.type_name = self.kind + ' OF'
        self.size_constraint = _maybe_create_sema_node(elements[0])
        self.type_decl = _create_sema_node(elements[1])

    def __str__(self):
        if self.size_constraint:
            return '%s %s OF %s' % (self.kind, self.size_constraint, self.type_decl)
        else:
            return '%s OF %s' % (self.kind, self.type_decl)

    __repr__ = __str__


class SequenceOfType(CollectionType):
    def __init__(self, elements):
        super(SequenceOfType, self).__init__('SEQUENCE', elements)


class SetOfType(CollectionType):
    def __init__(self, elements):
        super(SetOfType, self).__init__('SET', elements)


class TaggedType(SemaNode):
    def __init__(self, elements):
        self.class_name = None
        self.class_number = None
        if len(elements) == 3:
            tag_token, implicitness, type_token = elements
            for tag_element in tag_token.elements:
                if tag_element.ty == 'TagClassNumber':
                    self.class_number = tag_element.elements[0]
                elif tag_element.ty == 'TagClass':
                    self.class_name = tag_element.elements[0]
                else:
                    raise Exception('Unknown tag element: %s' % tag_element)
            self.type_decl = _create_sema_node(type_token)
        elif len(elements) == 4:
            self.class_name, self.class_number, implicitness, self.type_decl = elements
        else:
            raise Exception('Incorrect number of elements passed to TaggedType')

        if implicitness == 'IMPLICIT':
            self.implicitness = TagImplicitness.IMPLICIT
        elif implicitness == 'EXPLICIT':
            self.implicitness = TagImplicitness.EXPLICIT
        elif implicitness is None:
            self.implicitness = None  # Module-default or automatic

    @property
    def type_name(self):
        return self.type_decl.type_name

    def __str__(self):
        class_spec = []
        if self.class_name:
            class_spec.append(self.class_name)
        class_spec.append(self.class_number)

        result = '[%s] ' % ' '.join(class_spec)
        if self.implicitness == TagImplicitness.IMPLICIT:
            result += 'IMPLICIT '
        elif self.implicitness == TagImplicitness.EXPLICIT:
            result += 'EXPLICIT '
        else:
            pass  # module-default

        result += str(self.type_decl)

        return result

    __repr__ = __str__


class SimpleType(SemaNode):
    def __init__(self, elements):
        self.constraint = None
        self.type_name = elements[0]
        if len(elements) > 1:
            _assert_annotated_token(elements[1])
            self.constraint = _create_sema_node(elements[1])

    def __str__(self):
        if self.constraint is None:
            return self.type_name

        return '%s %s' % (self.type_name, self.constraint)

    __repr__ = __str__


class ReferencedType(SemaNode):
    pass


class DefinedType(ReferencedType):
    def __init__(self, elements):
        self.constraint = None
        self.module_name = None

        module_ref, type_ref, size_constraint = elements
        if module_ref:
            self.module_name = module_ref.elements[0]
        self.type_name = type_ref
        if size_constraint:
            self.constraint = _create_sema_node(size_constraint)

    def reference_name(self):
        return self.type_name

    def __str__(self):
        if self.module_name:
            type_name = self.module_name + '.' + self.type_name
        else:
            type_name = self.type_name

        if self.constraint is None:
            return type_name

        return '%s %s' % (type_name, self.constraint)

    __repr__ = __str__


class SelectionType(ReferencedType):
    def __init__(self, elements):
        self.identifier = elements[0].elements[0]
        self.type_decl = _create_sema_node(elements[1])

    @property
    def type_name(self):
        return self.type_decl.type_name

    def reference_name(self):
        return self.type_name

    def __str__(self):
        return '%s < %s' % (self.identifier, self.type_name)

    __repr__ = __str__


class ReferencedValue(SemaNode):
    def __init__(self, elements):
        if len(elements) > 1 and elements[0].ty == 'ModuleReference':
            self.module_reference = elements[0].elements[0]
            self.name = elements[1]
        else:
            self.module_reference = None
            self.name = elements[0]

    def reference_name(self):
        return self.name

    def __str__(self):
        if not self.module_reference:
            return self.name
        return '%s.%s' % (self.module_reference, self.name)

    __repr__ = __str__


class SingleValueConstraint(SemaNode):
    def __init__(self, elements):
        self.value = _maybe_create_sema_node(elements[0])

    def __str__(self):
        return '(%s)' % self.value

    __repr__ = __str__


class ValueRangeConstraint(SemaNode):
    def __init__(self, elements):
        self.min_value = _maybe_create_sema_node(elements[0])
        self.max_value = _maybe_create_sema_node(elements[1])

    def __str__(self):
        return '(%s..%s)' % (self.min_value, self.max_value)

    __repr__ = __str__


class SizeConstraint(SemaNode):
    """ Size constraints nest single-value or range constraints to denote valid sizes. """

    def __init__(self, elements):
        self.nested = _create_sema_node(elements[0])
        if not isinstance(self.nested, (ValueRangeConstraint, SingleValueConstraint)):
            raise Exception('Unexpected size constraint type %s' % self.nested.__class__.__name__)

    def __str__(self):
        return 'SIZE%s' % self.nested

    __repr__ = __str__


class ComponentType(SemaNode):
    def __init__(self, elements):
        self.identifier = None
        self.type_decl = None
        self.default_value = None
        self.optional = False
        self.components_of_type = None

        def crack_named_type(token):
            named_type = NamedType(token)
            self.identifier = named_type.identifier
            self.type_decl = named_type.type_decl

        first_token = elements[0]
        if first_token.ty == 'NamedType':
            crack_named_type(first_token.elements)
        elif first_token.ty == 'ComponentTypeOptional':
            crack_named_type(first_token.elements[0].elements)
            self.optional = True
        elif first_token.ty == 'ComponentTypeDefault':
            crack_named_type(first_token.elements[0].elements)
            self.default_value = _maybe_create_sema_node(first_token.elements[1])
        elif first_token.ty == 'ComponentTypeComponentsOf':
            self.components_of_type = _create_sema_node(first_token.elements[0])
        else:
            raise Exception('Unknown component type %s' % first_token)

    def __str__(self):
        if self.components_of_type:
            return 'COMPONENTS OF %s' % self.components_of_type

        result = '%s %s' % (self.identifier, self.type_decl)
        if self.optional:
            result += ' OPTIONAL'
        elif self.default_value is not None:
            result += ' DEFAULT %s' % self.default_value

        return result

    __repr__ = __str__


class NamedType(SemaNode):
    def __init__(self, elements):
        first_token = elements[0]
        if first_token.ty == 'Type':
            # EXT: unnamed member
            type_token = first_token
            self.identifier = _get_next_unnamed()
        elif first_token.ty == 'Identifier':
            # an identifier
            self.identifier = first_token.elements[0]
            type_token = elements[1]
        else:
            raise Exception('Unexpected token %s' % first_token.ty)

        self.type_decl = _create_sema_node(type_token)

    def __str__(self):
        return '%s %s' % (self.identifier, self.type_decl)

    __repr__ = __str__


class ValueListType(SemaNode):
    def __init__(self, elements):
        self.constraint = None
        self.type_name = elements[0]

        self.named_values = [_create_sema_node(token) for token in elements[1]]
        for idx, n in enumerate(self.named_values):
            if isinstance(n, NamedValue) and n.value is None:
                if idx == 0:
                    n.value = str(0)
                else:
                    n.value = str(int(self.named_values[idx - 1].value) + 1)

        if len(elements) > 2:
            self.constraint = _maybe_create_sema_node(elements[2])

    def __str__(self):
        named_value_list = ''
        constraint = ''

        if self.named_values:
            named_value_list = ' { %s }' % ', '.join(map(str, self.named_values))

        if self.constraint:
            constraint = ' %s' % self.constraint

        return '%s%s%s' % (self.type_name, named_value_list, constraint)

    __repr__ = __str__


class BitStringType(SemaNode):
    def __init__(self, elements):
        self.type_name = elements[0]
        self.named_bits = [_create_sema_node(token) for token in elements[1]]
        self.constraint = _maybe_create_sema_node(elements[2])

    def __str__(self):
        named_bit_list = ''
        constraint = ''

        if self.named_bits:
            named_bit_list = ' { %s }' % ', '.join(map(str, self.named_bits))

        if self.constraint:
            constraint = ' %s' % self.constraint

        return '%s%s%s' % (self.type_name, named_bit_list, constraint)

    __repr__ = __str__


class NamedValue(SemaNode):
    def __init__(self, elements):
        if len(elements) == 1:
            identifier_token = elements[0]
            self.identifier = identifier_token
            self.value = None
        else:
            identifier_token, value_token = elements
            self.identifier = identifier_token.elements[0]
            self.value = value_token.elements[0]

    def __str__(self):
        return '%s (%s)' % (self.identifier, self.value)

    __repr__ = __str__


class ExtensionMarker(SemaNode):
    def __init__(self, elements):
        pass

    def __str__(self):
        return '...'

    __repr__ = __str__


class NameForm(SemaNode):
    def __init__(self, elements):
        self.name = elements[0]

    def reference_name(self):
        return self.name

    def __str__(self):
        return self.name

    __repr__ = __str__


class NumberForm(SemaNode):
    def __init__(self, elements):
        self.value = elements[0]

    def __str__(self):
        return str(self.value)

    __repr__ = __str__


class NameAndNumberForm(SemaNode):
    def __init__(self, elements):
        self.name = _create_sema_node(elements[0])
        self.number = _create_sema_node(elements[1])

    def __str__(self):
        return '%s(%s)' % (self.name, self.number)

    __repr__ = __str__


class ObjectIdentifierValue(SemaNode):
    def __init__(self, elements):
        self.components = [_create_sema_node(c) for c in elements]

    def __str__(self):
        return '{' + ' '.join(str(x) for x in self.components) + '}'

    __repr__ = __str__


class BinaryStringValue(SemaNode):
    def __init__(self, elements):
        self.value = elements[0]

    def __str__(self):
        return '\'%s\'B' % self.value

    __repr__ = __str__


class HexStringValue(SemaNode):
    def __init__(self, elements):
        self.value = elements[0]

    def __str__(self):
        return '\'%s\'H' % self.value

    __repr__ = __str__


def _maybe_create_sema_node(token):
    if isinstance(token, parser.AnnotatedToken):
        return _create_sema_node(token)
    else:
        return token


def _create_sema_node(token):
    _assert_annotated_token(token)

    if token.ty == 'ModuleDefinition':
        return Module(token.elements)
    elif token.ty == 'Exports':
        return Exports(token.elements)
    elif token.ty == 'Imports':
        return Imports(token.elements)
    elif token.ty == 'TypeAssignment':
        return TypeAssignment(token.elements)
    elif token.ty == 'ValueAssignment':
        return ValueAssignment(token.elements)
    elif token.ty == 'ComponentType':
        return ComponentType(token.elements)
    elif token.ty == 'NamedType':
        return NamedType(token.elements)
    elif token.ty == 'ValueListType':
        return ValueListType(token.elements)
    elif token.ty == 'BitStringType':
        return BitStringType(token.elements)
    elif token.ty == 'NamedValue':
        return NamedValue(token.elements)
    elif token.ty == 'Type':
        # Type tokens have a more specific type category
        # embedded as their first element
        return _create_sema_node(token.elements[0])
    elif token.ty == 'SimpleType':
        return SimpleType(token.elements)
    elif token.ty == 'DefinedType':
        return DefinedType(token.elements)
    elif token.ty == 'SelectionType':
        return SelectionType(token.elements)
    elif token.ty == 'ReferencedValue':
        return ReferencedValue(token.elements)
    elif token.ty == 'TaggedType':
        return TaggedType(token.elements)
    elif token.ty == 'SequenceType':
        return SequenceType(token.elements)
    elif token.ty == 'ChoiceType':
        return ChoiceType(token.elements)
    elif token.ty == 'SetType':
        return SetType(token.elements)
    elif token.ty == 'SequenceOfType':
        return SequenceOfType(token.elements)
    elif token.ty == 'SetOfType':
        return SetOfType(token.elements)
    elif token.ty == 'ExtensionMarker':
        return ExtensionMarker(token.elements)
    elif token.ty == 'SingleValueConstraint':
        return SingleValueConstraint(token.elements)
    elif token.ty == 'SizeConstraint':
        return SizeConstraint(token.elements)
    elif token.ty == 'ValueRangeConstraint':
        return ValueRangeConstraint(token.elements)
    elif token.ty == 'ObjectIdentifierValue':
        return ObjectIdentifierValue(token.elements)
    elif token.ty == 'NameForm':
        return NameForm(token.elements)
    elif token.ty == 'NumberForm':
        return NumberForm(token.elements)
    elif token.ty == 'NameAndNumberForm':
        return NameAndNumberForm(token.elements)
    elif token.ty == 'BinaryStringValue':
        return BinaryStringValue(token.elements)
    elif token.ty == 'HexStringValue':
        return HexStringValue(token.elements)

    raise Exception('Unknown token type: %s' % token.ty)


def _assert_annotated_token(obj):
    if type(obj) is not parser.AnnotatedToken:
        raise Exception('Object %r is not an annotated token' % obj)


# HACK: Generate unique names for unnamed members
_unnamed_counter = 0


def _get_next_unnamed():
    global _unnamed_counter
    _unnamed_counter += 1
    return 'unnamed%d' % _unnamed_counter
