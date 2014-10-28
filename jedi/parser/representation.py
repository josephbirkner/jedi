"""
If you know what an abstract syntax tree (ast) is, you'll see that this module
is pretty much that. The classes represent syntax elements: ``Import``,
``Function``.

A very central class is ``Scope``. It is not used directly by the parser, but
inherited. It's used by ``Function``, ``Class``, ``Flow``, etc. A ``Scope`` may
have ``subscopes``, ``imports`` and ``statements``. The entire parser is based
on scopes, because they also stand for indentation.

One special thing:

``Array`` values are statements. But if you think about it, this makes sense.
``[1, 2+33]`` for example would be an Array with two ``Statement`` inside. This
is the easiest way to write a parser. The same behaviour applies to ``Param``,
which is being used in a function definition.

The easiest way to play with this module is to use :class:`parsing.Parser`.
:attr:`parsing.Parser.module` holds an instance of :class:`SubModule`:

>>> from jedi._compatibility import u
>>> from jedi.parser import Parser
>>> parser = Parser(u('import os'), 'example.py')
>>> submodule = parser.module
>>> submodule
<SubModule: example.py@1-1>

Any subclasses of :class:`Scope`, including :class:`SubModule` has
attribute :attr:`imports <Scope.imports>`.  This attribute has import
statements in this scope.  Check this out:

>>> submodule.imports
[<Import: import os @1,0>]

See also :attr:`Scope.subscopes` and :attr:`Scope.statements`.


# TODO New docstring

"""
import os
import re
from inspect import cleandoc
from collections import defaultdict
from itertools import chain

from jedi._compatibility import (next, Python3Method, encoding, unicode,
                                 is_py3, u, literal_eval, use_metaclass)
from jedi import common
from jedi import debug
from jedi import cache
from jedi.parser import tokenize
from jedi.parser.pytree import python_symbols, type_repr


SCOPE_CONTENTS = 'asserts', 'subscopes', 'imports', 'statements', 'returns'


def is_node(node, *symbol_names):
    if isinstance(node, Node):
        for symbol_name in symbol_names:
            if getattr(python_symbols, symbol_name) == node.type:
                return True
    return False


def filter_after_position(names, position):
    """
    Removes all names after a certain position. If position is None, just
    returns the names list.
    """
    if position is None:
        return names

    names_new = []
    for n in names:
        if n.start_pos[0] is not None and n.start_pos < position:
            names_new.append(n)
    return names_new


class GetCodeState(object):
    """A helper class for passing the state of get_code in a thread-safe
    manner."""
    __slots__ = ("last_pos",)

    def __init__(self):
        self.last_pos = (0, 0)


class DocstringMixin(object):
    __slots__ = ()

    def add_docstr(self, token):
        """ Clean up a docstring """
        self._doc_token = token

    @property
    def raw_doc(self):
        """ Returns a cleaned version of the docstring token. """
        try:
            # Returns a literal cleaned version of the ``Token``.
            cleaned = cleandoc(literal_eval(self._doc_token.string))
            # Since we want the docstr output to be always unicode, just force
            # it.
            if is_py3 or isinstance(cleaned, unicode):
                return cleaned
            else:
                return unicode(cleaned, 'UTF-8', 'replace')
        except AttributeError:
            return u('')


class Base(object):
    """
    This is just here to have an isinstance check, which is also used on
    evaluate classes. But since they have sometimes a special type of
    delegation, it is important for those classes to override this method.

    I know that there is a chance to do such things with __instancecheck__, but
    since Python 2.5 doesn't support it, I decided to do it this way.
    """
    __slots__ = ()

    def isinstance(self, *cls):
        return isinstance(self, cls)

    @property
    def newline(self):
        """Returns the newline type for the current code."""
        # TODO: we need newline detection
        return "\n"

    @property
    def whitespace(self):
        """Returns the whitespace type for the current code: tab or space."""
        # TODO: we need tab detection
        return " "

    @Python3Method
    def get_parent_until(self, classes=(), reverse=False,
                         include_current=True):
        """
        Searches the parent "chain" until the object is an instance of
        classes. If classes is empty return the last parent in the chain
        (is without a parent).
        """
        if type(classes) not in (tuple, list):
            classes = (classes,)
        scope = self if include_current else self.parent
        while scope.parent is not None:
            # TODO why if classes?
            if classes and reverse != scope.isinstance(*classes):
                break
            scope = scope.parent
        return scope

    def get_parent_scope(self):
        """
        Returns the underlying scope.
        """
        scope = self.parent
        while scope.parent is not None:
            if scope.is_scope():
                break
            scope = scope.parent
        return scope

    def space(self, from_pos, to_pos):
        """Return the space between two tokens"""
        linecount = to_pos[0] - from_pos[0]
        if linecount == 0:
            return self.whitespace * (to_pos[1] - from_pos[1])
        else:
            return "%s%s" % (
                self.newline * linecount,
                self.whitespace * to_pos[1],
            )

    def is_scope(self):
        # Default is not being a scope. Just inherit from Scope.
        return False


class _Leaf(Base):
    __slots__ = ('value', 'parent', 'start_pos', 'prefix')

    def __init__(self, value, start_pos, prefix=''):
        self.value = value
        self.start_pos = start_pos
        self.prefix = prefix
        self.parent = None

    @property
    def end_pos(self):
        return self.start_pos[0], self.start_pos[1] + len(self.value)

    def get_code(self):
        return self.prefix + self.value

    def next_sibling(self):
        """
        The node immediately following the invocant in their parent's children
        list. If the invocant does not have a next sibling, it is None
        """
        # Can't use index(); we need to test by identity
        for i, child in enumerate(self.parent.children):
            if child is self:
                try:
                    return self.parent.children[i + 1]
                except IndexError:
                    return None

    def prev_sibling(self):
        """
        The node immediately preceding the invocant in their parent's children
        list. If the invocant does not have a previous sibling, it is None.
        """
        # Can't use index(); we need to test by identity
        for i, child in enumerate(self.parent.children):
            if child is self:
                if i == 0:
                    return None
                return self.parent.children[i - 1]

    def __repr__(self):
        return "<%s: %s>" % (type(self).__name__, repr(self.value))


class Whitespace(_Leaf):
    """Contains NEWLINE and ENDMARKER tokens."""


class Name(_Leaf):
    """
    A string. Sometimes it is important to know if the string belongs to a name
    or not.
    """
    # Unfortunately there's no way to use slots for str (non-zero __itemsize__)
    # -> http://utcc.utoronto.ca/~cks/space/blog/python/IntSlotsPython3k
    # Therefore don't subclass `str`.

    def __str__(self):
        return self.value

    def __unicode__(self):
        return self.value

    def __repr__(self):
        return "<%s: %s@%s,%s>" % (type(self).__name__, self.value,
                                   self.start_pos[0], self.start_pos[1])

    def get_definition(self):
        return self.parent.get_parent_until((ArrayStmt, StatementElement, Node), reverse=True)

    def is_definition(self):
        stmt = self.get_definition()
        return isinstance(stmt, (ExprStmt, Import)) \
            and self in stmt.get_defined_names()

    def assignment_indexes(self):
        """
        Returns an array of ints of the indexes that are used in tuple
        assignments.

        For example if the name is ``y`` in the following code::

            x, (y, z) = 2, ''

        would result in ``[1, 0]``.
        """
        indexes = []
        node = self.parent
        compare = self
        while node is not None:
            if is_node(node, 'testlist_comp') or is_node(node, 'testlist_star_expr'):
                for i, child in enumerate(node.children):
                    if child == compare:
                        indexes.insert(0, int(i / 2))
                        break
                else:
                    raise LookupError("Couldn't find the assignment.")

            compare = node
            node = node.parent
        return indexes


class Literal(_Leaf):
    def eval(self):
        return literal_eval(self.value)

    def __repr__(self):
        # TODO remove?
        """
        if is_py3:
            s = self.literal
        else:
            s = self.literal.encode('ascii', 'replace')
        """
        return "<%s: %s>" % (type(self).__name__, self.value)


class Operator(_Leaf):
    def __str__(self):
        return self.value

    def __eq__(self, other):
        """
        Make comparisons with strings easy.
        Improves the readability of the parser.
        """
        if isinstance(other, Operator):
            return self is other
        else:
            return self.value == other

    def __ne__(self, other):
        """Python 2 compatibility."""
        return self.value != other

    def __hash__(self):
        return hash(self.value)


class Keyword(_Leaf):
    def __eq__(self, other):
        """
        Make comparisons with strings easy.
        Improves the readability of the parser.
        """
        return self.value == other

    def __ne__(self, other):
        """Python 2 compatibility."""
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.value)


class Simple(Base):
    """
    The super class for Scope, Import, Name and Statement. Every object in
    the parser tree inherits from this class.
    """
    __slots__ = ('children', 'parent')

    def __init__(self, children):
        """
        Initialize :class:`Simple`.

        :param children: The module in which this Python object locates.
        """
        for c in children:
            c.parent = self
        self.children = children
        self.parent = None

    def move(self, line_offset, column_offset):
        """
        Move the Node's start_pos.
        """
        for c in self.children:
            if isinstance(c, _Leaf):
                c.start_pos = (c.start_pos[0] + line_offset,
                               c.start_pos[1] + column_offset)
            else:
                c.move(line_offset, column_offset)

    @property
    def start_pos(self):
        return self.children[0].start_pos

    @property
    def _sub_module(self):
        return self.get_parent_until()

    @property
    def end_pos(self):
        return self.children[-1].end_pos

    def get_code(self):
        return "".join(c.get_code() for c in self.children)

    def __repr__(self):
        code = self.get_code().replace('\n', ' ')
        if not is_py3:
            code = code.encode(encoding, 'replace')
        return "<%s: %s@%s,%s>" % \
            (type(self).__name__, code, self.start_pos[0], self.start_pos[1])


class Node(Simple):
    """Concrete implementation for interior nodes."""

    def __init__(self, type, children):
        """
        Initializer.

        Takes a type constant (a symbol number >= 256), a sequence of
        child nodes, and an optional context keyword argument.

        As a side effect, the parent pointers of the children are updated.
        """
        super(Node, self).__init__(children)
        self.type = type

    def __repr__(self):
        """Return a canonical string representation."""
        return "%s(%s, %r)" % (self.__class__.__name__,
                               type_repr(self.type),
                               self.children)


class IsScopeMeta(type):
    def __instancecheck__(self, other):
        return other.is_scope()


class IsScope(use_metaclass(IsScopeMeta)):
    pass


def _return_empty_list():
    """
    Necessary for pickling. It needs to be reachable for pickle, cannot
    be a lambda or a closure.
    """
    return []


class Scope(Simple, DocstringMixin):
    """
    Super class for the parser tree, which represents the state of a python
    text file.
    A Scope manages and owns its subscopes, which are classes and functions, as
    well as variables and imports. It is used to access the structure of python
    files.

    :param start_pos: The position (line and column) of the scope.
    :type start_pos: tuple(int, int)
    """
    __slots__ = ('imports', '_doc_token', 'asserts', 'names_dict',
                 'is_generator')

    def __init__(self, children):
        super(Scope, self).__init__(children)
        self.imports = []
        self._doc_token = None
        self.asserts = []
        self.is_generator = False

    @property
    def returns(self):
        # Needed here for fast_parser, because the fast_parser splits and
        # returns will be in "normal" modules.
        return self._search_in_scope(ReturnStmt)

    @property
    def subscopes(self):
        return self._search_in_scope(Scope)

    def _search_in_scope(self, typ):
        def scan(children):
            elements = []
            for element in children:
                if isinstance(element, typ):
                    elements.append(element)
                elif is_node(element, 'suite') or is_node(element, 'simple_stmt'):
                    elements += scan(element.children)
            return elements

        return scan(self.children)

    @property
    def statements(self):
        return [s for c in self.children if is_node(c, 'simple_stmt')
                for s in c.children if isinstance(s, (ExprStmt, Import,
                                                      KeywordStatement))]

    def is_scope(self):
        return True

    def add_scope(self, sub, decorators):
        sub.parent = self.use_as_parent
        sub.decorators = decorators
        for d in decorators:
            # the parent is the same, because the decorator has not the scope
            # of the function
            d.parent = self.use_as_parent
        self.subscopes.append(sub)
        return sub

    def add_statement(self, stmt):
        """
        Used to add a Statement or a Scope.
        A statement would be a normal command (Statement) or a Scope (Flow).
        """
        stmt.parent = self.use_as_parent
        self.statements.append(stmt)
        return stmt

    def add_import(self, imp):
        self.imports.append(imp)
        imp.parent = self.use_as_parent

    def get_imports(self):
        """ Gets also the imports within flow statements """
        i = [] + self.imports
        for s in self.statements:
            if isinstance(s, Scope):
                i += s.get_imports()
        return i

    @Python3Method
    def get_defined_names(self):
        """
        Get all defined names in this scope.

        >>> from jedi._compatibility import u
        >>> from jedi.parser import Parser
        >>> parser = Parser(u('''
        ... a = x
        ... b = y
        ... b.c = z
        ... '''))
        >>> parser.module.get_defined_names()
        [<Name: a@2,0>, <Name: b@3,0>, <Name: b.c@4,0>]
        """
        names = []
        children = self.children
        if is_node(children[-1], 'suite'):
            children = children[-1].children
        for c in children:
            if is_node(c, 'simple_stmt'):
                names += chain.from_iterable(
                    [s.get_defined_names() for s in c.children
                     if isinstance(s, (ExprStmt, Import, KeywordStatement))])
            elif isinstance(c, (Function, Class)):
                names.append(c.name)
        return names

    @Python3Method
    def get_statement_for_position(self, pos, include_imports=False):
        checks = self.statements + self.asserts
        if include_imports:
            checks += self.imports
        if self.isinstance(Function):
            checks += self.decorators
            checks += [r for r in self.returns if r is not None]
        if self.isinstance(Flow):
            checks += self.inputs
        if self.isinstance(ForFlow) and self.set_stmt is not None:
            checks.append(self.set_stmt)

        for s in checks:
            if isinstance(s, Flow):
                p = s.get_statement_for_position(pos, include_imports)
                while s.next and not p:
                    s = s.next
                    p = s.get_statement_for_position(pos, include_imports)
                if p:
                    return p
            elif s.start_pos <= pos <= s.end_pos:
                return s

        for s in self.subscopes:
            if s.start_pos <= pos <= s.end_pos:
                p = s.get_statement_for_position(pos, include_imports)
                if p:
                    return p

    def __repr__(self):
        try:
            name = self.path
        except AttributeError:
            try:
                name = self.name
            except AttributeError:
                name = self.command

        return "<%s: %s@%s-%s>" % (type(self).__name__, name,
                                   self.start_pos[0], self.end_pos[0])

    def walk(self):
        yield self
        for s in self.subscopes:
            for scope in s.walk():
                yield scope

        for r in self.statements:
            while isinstance(r, Flow):
                for scope in r.walk():
                    yield scope
                r = r.next


class Module(Base):
    """
    For isinstance checks. fast_parser.Module also inherits from this.
    """
    def is_scope(self):
        return True


class SubModule(Scope, Module):
    """
    The top scope, which is always a module.
    Depending on the underlying parser this may be a full module or just a part
    of a module.
    """
    __slots__ = ('path', 'global_names', 'used_names',
                 'line_offset', 'use_as_parent')

    def __init__(self, children):
        """
        Initialize :class:`SubModule`.

        :type path: str
        :arg  path: File path to this module.

        .. todo:: Document `top_module`.
        """
        super(SubModule, self).__init__(children)
        self.path = None  # Set later.
        # this may be changed depending on fast_parser
        self.line_offset = 0

        if 0:
            self.use_as_parent = top_module or self

    def set_global_names(self, names):
        """
        Global means in these context a function (subscope) which has a global
        statement.
        This is only relevant for the top scope.

        :param names: names of the global.
        :type names: list of Name
        """
        self.global_names = names

    def add_global(self, name):
        # set no parent here, because globals are not defined in this scope.
        self.global_vars.append(name)

    def get_defined_names(self):
        n = super(SubModule, self).get_defined_names()
        # TODO uncomment
        #n += self.global_names
        return n

    @property
    @cache.underscore_memoization
    def name(self):
        """ This is used for the goto functions. """
        if self.path is None:
            string = ''  # no path -> empty name
        else:
            sep = (re.escape(os.path.sep),) * 2
            r = re.search(r'([^%s]*?)(%s__init__)?(\.py|\.so)?$' % sep, self.path)
            # Remove PEP 3149 names
            string = re.sub('\.[a-z]+-\d{2}[mud]{0,3}$', '', r.group(1))
        # Positions are not real, but a module starts at (1, 0)
        p = (1, 0)
        name = Name(string, p)
        name.parent = self
        return name

    @property
    def has_explicit_absolute_import(self):
        """
        Checks if imports in this module are explicitly absolute, i.e. there
        is a ``__future__`` import.
        """
        for imp in self.imports:
            if not imp.from_names or not imp.namespace_names:
                continue

            namespace, feature = imp.from_names[0], imp.namespace_names[0]
            if unicode(namespace) == "__future__" and unicode(feature) == "absolute_import":
                return True

        return False


class ClassOrFunc(Scope):
    __slots__ = ()

    @property
    def name(self):
        return self.children[1]


class Class(ClassOrFunc):
    """
    Used to store the parsed contents of a python class.

    :param name: The Class name.
    :type name: str
    :param supers: The super classes of a Class.
    :type supers: list
    :param start_pos: The start position (line, column) of the class.
    :type start_pos: tuple(int, int)
    """
    __slots__ = ('decorators')

    def __init__(self, children):
        super(Class, self).__init__(children)
        self.decorators = []

    def get_super_arglist(self):
        if len(self.children) == 4:  # Has no parentheses
            return None
        else:
            if self.children[3] == ')':  # Empty parentheses
                return None
            else:
                return self.children[3]

    @property
    def doc(self):
        """
        Return a document string including call signature of __init__.
        """
        docstr = ""
        if self._doc_token is not None:
            docstr = self.raw_doc
        for sub in self.subscopes:
            if unicode(sub.name) == '__init__':
                return '%s\n\n%s' % (
                    sub.get_call_signature(funcname=self.name), docstr)
        return docstr

    def scope_names_generator(self, position=None):
        yield self, filter_after_position(self.get_defined_names(), position)


class Function(ClassOrFunc):
    """
    Used to store the parsed contents of a python function.

    :param name: The Function name.
    :type name: str
    :param params: The parameters (Statement) of a Function.
    :type params: list
    :param start_pos: The start position (line, column) the Function.
    :type start_pos: tuple(int, int)
    """
    __slots__ = ('decorators', 'listeners', 'params')

    def __init__(self, children):
        super(Function, self).__init__(children)
        self.decorators = []
        self.listeners = set()  # not used here, but in evaluation.
        self.params = self._params()

    @property
    def name(self):
        return self.children[1]  # First token after `def`

    def _params(self):
        node = self.children[2].children[1:-1]  # After `def foo`
        if not node:
            return []
        if is_node(node[0], 'typedargslist'):
            params = []
            iterator = iter(node[0].children)
            for n in iterator:
                stars = 0
                if n in ('*', '**'):
                    stars = len(n.value)
                    n = next(iterator)

                op = next(iterator, None)
                if op == '=':
                    default = next(iterator)
                    next(iterator, None)
                else:
                    default = None
                params.append(Param(n, self, default, stars))
            return params
        else:
            return [Param(node[0], self)]

    def annotation(self):
        try:
            return self.children[6]  # 6th element: def foo(...) -> bar
        except IndexError:
            return None

    def get_defined_names(self):
        n = super(Function, self).get_defined_names()
        for p in self.params:
            try:
                n.append(p.get_name())
            except IndexError:
                debug.warning("multiple names in param %s", n)
        return n

    def scope_names_generator(self, position=None):
        yield self, filter_after_position(self.get_defined_names(), position)

    def get_call_signature(self, width=72, funcname=None):
        """
        Generate call signature of this function.

        :param width: Fold lines if a line is longer than this value.
        :type width: int
        :arg funcname: Override function name when given.
        :type funcname: str

        :rtype: str
        """
        l = unicode(funcname or self.name) + '('
        lines = []
        for (i, p) in enumerate(self.params):
            code = p.get_code(False)
            if i != len(self.params) - 1:
                code += ', '
            if len(l + code) > width:
                lines.append(l[:-1] if l[-1] == ' ' else l)
                l = code
            else:
                l += code
        if l:
            lines.append(l)
        lines[-1] += ')'
        return '\n'.join(lines)

    @property
    def doc(self):
        """ Return a document string including call signature. """
        docstr = ""
        if self._doc_token is not None:
            docstr = self.raw_doc
        return '%s\n\n%s' % (self.get_call_signature(), docstr)


class Lambda(Function):
    def __init__(self, module, params, start_pos, parent):
        super(Lambda, self).__init__(module, None, params, start_pos, None)
        self.parent = parent

    def __repr__(self):
        return "<%s @%s (%s-%s)>" % (type(self).__name__, self.start_pos[0],
                                     self.start_pos[1], self.end_pos[1])


class Flow(Scope):
    """
    Used to describe programming structure - flow statements,
    which indent code, but are not classes or functions:

    - for
    - while
    - if
    - try
    - with

    Therefore statements like else, except and finally are also here,
    they are now saved in the root flow elements, but in the next variable.

    :param command: The flow command, if, while, else, etc.
    :type command: str
    :param inputs: The initializations of a flow -> while 'statement'.
    :type inputs: list(Statement)
    :param start_pos: Position (line, column) of the Flow statement.
    :type start_pos: tuple(int, int)
    """
    __slots__ = ('next', 'previous', 'command', '_parent', 'inputs', 'set_vars')

    def __init__(self, module, command, inputs, start_pos):
        self.next = None
        self.previous = None
        self.command = command
        super(Flow, self).__init__(module, start_pos)
        self._parent = None
        # These have to be statements, because of with, which takes multiple.
        self.inputs = inputs
        for s in inputs:
            s.parent = self.use_as_parent
        self.set_vars = []

    def add_name_calls(self, name, calls):
        """Add a name to the names_dict."""
        parent = self.parent
        if isinstance(parent, Module):
            # TODO this also looks like code smell. Look for opportunities to
            # remove.
            parent = self._sub_module
        parent.add_name_calls(name, calls)

    @property
    def parent(self):
        return self._parent

    @parent.setter
    def parent(self, value):
        self._parent = value
        try:
            self.next.parent = value
        except AttributeError:
            return

    def get_defined_names(self, is_internal_call=False):
        """
        Get the names for the flow. This includes also a call to the super
        class.

        :param is_internal_call: defines an option for internal files to crawl
            through this class. Normally it will just call its superiors, to
            generate the output.
        """
        if is_internal_call:
            n = list(self.set_vars)
            for s in self.inputs:
                n += s.get_defined_names()
            if self.next:
                n += self.next.get_defined_names(is_internal_call)
            n += super(Flow, self).get_defined_names()
            return n
        else:
            return self.get_parent_until((Class, Function)).get_defined_names()

    def get_imports(self):
        i = super(Flow, self).get_imports()
        if self.next:
            i += self.next.get_imports()
        return i

    def set_next(self, next):
        """Set the next element in the flow, those are else, except, etc."""
        if self.next:
            return self.next.set_next(next)
        else:
            self.next = next
            self.next.parent = self.parent
            self.next.previous = self
            return next

    def scope_names_generator(self, position=None):
        # For `with` and `for`.
        yield self, filter_after_position(self.get_defined_names(), position)


class ForFlow(Flow):
    """
    Used for the for loop, because there are two statement parts.
    """
    def __init__(self, module, inputs, start_pos, set_stmt):
        super(ForFlow, self).__init__(module, 'for', inputs, start_pos)

        self.set_stmt = set_stmt

        if set_stmt is not None:
            set_stmt.parent = self.use_as_parent
            self.set_vars = set_stmt.get_defined_names()

            for s in self.set_vars:
                s.parent.parent = self.use_as_parent
                s.parent = self.use_as_parent


class Import(Simple):
    """
    Stores the imports of any Scopes.

    :param start_pos: Position (line, column) of the Import.
    :type start_pos: tuple(int, int)
    :param namespace_names: The import, can be empty if a star is given
    :type namespace_names: list of Name
    :param alias: The alias of a namespace(valid in the current namespace).
    :type alias: list of Name
    :param from_names: Like the namespace, can be equally used.
    :type from_names: list of Name
    :param star: If a star is used -> from time import *.
    :type star: bool
    :param defunct: An Import is valid or not.
    :type defunct: bool
    """
    def __init__old(self, module, start_pos, end_pos, namespace_names, alias=None,
                 from_names=(), star=False, relative_count=0, defunct=False):
        super(Import, self).__init__(module, start_pos, end_pos)

        self.namespace_names = namespace_names
        self.alias = alias
        if self.alias:
            alias.parent = self
        self.from_names = from_names
        for n in namespace_names + list(from_names):
            n.parent = self.use_as_parent

        self.star = star
        self.relative_count = relative_count
        self.defunct = defunct

    def get_defined_names(self):
        if self.children[0] == 'import':
            return self.children[1:]
        else:  # from
# <Operator: '.'>, <Name: decoder@110,6>, <Keyword: 'import'>, <Name: JSONDecoder@110,21>
            return [self.children[-1]]

        # TODO remove
        if self.defunct:
            return []
        if self.star:
            return [self]
        if self.alias:
            return [self.alias]
        if len(self.namespace_names) > 1:
            return [self.namespace_names[0]]
        else:
            return self.namespace_names

    def get_all_import_names(self):
        n = []
        if self.from_names:
            n += self.from_names
        if self.namespace_names:
            n += self.namespace_names
        if self.alias is not None:
            n.append(self.alias)
        return n

    def _paths(self):
        if self.children[0] == 'import':
            return [self.children[1:]]
        else:
            raise NotImplementedError

    def path_for_name(self, name):
        for path in self._paths():
            if name in path:
                return path

    @property
    def level(self):
        """The level parameter of ``__import__``."""
        # TODO implement
        return 0

    def is_nested(self):
        """
        This checks for the special case of nested imports, without aliases and
        from statement::

            import foo.bar
        """
        return False
        # TODO use this check differently?
        return not self.alias and not self.from_names \
            and len(self.namespace_names) > 1


class KeywordStatement(Simple):
    """
    For the following statements: `assert`, `del`, `global`, `nonlocal`,
    `raise`, `return`, `yield`, `pass`, `continue`, `break`, `return`, `yield`.
    """
    @property
    def keyword(self):
        return self.children[0].value


class GlobalStmt(Simple):
    def names(self):
        return self.children[1::2]


class ReturnStmt(Simple):
    pass


class Statement(Simple, DocstringMixin):
    """
    This is the class for all the possible statements. Which means, this class
    stores pretty much all the Python code, except functions, classes, imports,
    and flow functions like if, for, etc.

    :type  token_list: list
    :param token_list:
        List of tokens or names.  Each element is either an instance
        of :class:`Name` or a tuple of token type value (e.g.,
        :data:`tokenize.NUMBER`), token string (e.g., ``'='``), and
        start position (e.g., ``(1, 0)``).
    :type   start_pos: 2-tuple of int
    :param  start_pos: Position (line, column) of the Statement.
    """
    __slots__ = ('_token_list', '_set_vars', 'as_names', '_expression_list',
                 '_assignment_details', '_names_are_set_vars', '_doc_token')

    def __init__old(self, children, parent=None,):
        super(Statement, self).__init__(module, start_pos, end_pos, parent)
        self._token_list = token_list
        self._names_are_set_vars = names_are_set_vars
        if set_name_parents:
            for n in as_names:
                n.parent = self.use_as_parent
        self._doc_token = None
        self._set_vars = None
        self.as_names = list(as_names)

        # cache
        self._assignment_details = []
        # For now just generate the expression list, even if its not needed.
        # This will help to adapt a better new AST.
        self.expression_list()

    def get_defined_names(self):
        def check_tuple(current):
            names = []
            if is_node(current, 'testlist_star_expr') or is_node(current, 'testlist_comp'):
                for child in current.children[::2]:
                    names += check_tuple(child)
            elif is_node(current, 'atom'):
                names += check_tuple(current.children[1])
            elif is_node(current, 'power'):
                if current.children[-2] != '**':  # Just if there's no operation
                    trailer = current.children[-1]
                    if trailer.children[0] == '.':
                        names.append(trailer.children[1])
            else:
                names.append(current)
            return names

        return list(chain.from_iterable(check_tuple(self.children[i])
                                        for i in range(0, len(self.children) - 2, 2)
                                        if self.children[i + 1].value == '='))


        """Get the names for the statement."""
        if self._set_vars is None:

            def search_calls(calls):
                for call in calls:
                    if isinstance(call, Array) and call.type != Array.DICT:
                        for stmt in call:
                            search_calls(stmt.expression_list())
                    elif isinstance(call, Call):
                        # Check if there's an execution in it, if so this is
                        # not a set_var.
                        if not call.next:
                            self._set_vars.append(call.name)
                        continue

            self._set_vars = []
            for calls, operation in self.assignment_details:
                search_calls(calls)

            if not self.assignment_details and self._names_are_set_vars:
                # In the case of Param, it's also a defining name without ``=``
                search_calls(self.expression_list())
        return self._set_vars + self.as_names

    def get_rhs(self):
        """Returns the right-hand-side of the equals."""
        return self.children[-1]

    def get_names_dict(self):
        """The future of name resolution. Returns a dict(str -> Call)."""
        dct = defaultdict(lambda: [])

        def search_calls(calls):
            for call in calls:
                if isinstance(call, Array) and call.type != Array.DICT:
                    for stmt in call:
                        search_calls(stmt.expression_list())
                elif isinstance(call, Call):
                    c = call
                    # Check if there's an execution in it, if so this is
                    # not a set_var.
                    while True:
                        if c.next is None or isinstance(c.next, Array):
                            break
                        c = c.next
                    dct[unicode(c.name)].append(call)

        for calls, operation in self.assignment_details:
            search_calls(calls)

        if not self.assignment_details and self._names_are_set_vars:
            # In the case of Param, it's also a defining name without ``=``
            search_calls(self.expression_list())

        for as_name in self.as_names:
            dct[unicode(as_name)].append(Call(self._sub_module, as_name,
                                         as_name.start_pos, as_name.end_pos, self))
        return dct

    def is_global(self):
        p = self.parent
        return isinstance(p, KeywordStatement) and p.name == 'global'

    @property
    def assignment_details(self):
        """
        Returns an array of tuples of the elements before the assignment.

        For example the following code::

            x = (y, z) = 2, ''

        would result in ``[(Name(x), '='), (Array([Name(y), Name(z)]), '=')]``.
        """
        return []

    def set_expression_list(self, lst):
        """It's necessary for some "hacks" to change the expression_list."""
        self._expression_list = lst


class ExprStmt(Statement):
    """
    This class exists temporarily, to be able to distinguish real statements
    (``small_stmt`` in Python grammar) from the so called ``test`` parts, that
    may be used to defined part of an array, but are never a whole statement.

    The reason for this class is purely historical. It was easier to just use
    Statement nested, than to create a new class for Test (plus Jedi's fault
    tolerant parser just makes things very complicated).
    """


class ArrayStmt(Statement):
    """
    This class exists temporarily. Like ``ExprStatement``, this exists to
    distinguish between real statements and stuff that is defined in those
    statements.
    """


class Param(Base):
    """
    The class which shows definitions of params of classes and functions.
    But this is not to define function calls.

    A helper class for functions. Read only.
    """
    __slots__ = ('tfpdef', 'default', 'stars', 'parent', 'annotation_stmt')

    def __init__(self, tfpdef, parent, default=None, stars=0):
        self.tfpdef = tfpdef  # tfpdef: see grammar.txt
        self.default = default
        self.stars = stars
        self.parent = parent
        # Here we reset the parent of our name. IMHO this is ok.
        self.get_name().parent = self

    @property
    def children(self):
        return []

    @property
    def start_pos(self):
        return self.tfpdef.start_pos

    def get_name(self):
        if is_node(self.tfpdef, 'tfpdef'):
            return self.tfpdef.children[0]
        else:
            return self.tfpdef

    @property
    def position_nr(self):
        return self.parent.params.index(self)

    @property
    def parent_function(self):
        return self.get_parent_until(IsScope)

    def __init__old(self):
        kwargs.pop('names_are_set_vars', None)
        super(Param, self).__init__(*args, names_are_set_vars=True, **kwargs)

        # this is defined by the parser later on, not at the initialization
        # it is the position in the call (first argument, second...)
        self.position_nr = None
        self.is_generated = False
        self.annotation_stmt = None
        self.parent_function = None

    def add_annotation(self, annotation_stmt):
        annotation_stmt.parent = self.use_as_parent
        self.annotation_stmt = annotation_stmt

    def __repr__(self):
        default = '' if self.default is None else '=%s' % self.default
        return '<%s: %s>' % (type(self).__name__, str(self.tfpdef) + default)


class StatementElement(Simple):
    __slots__ = ('next', 'previous')

    def __init__(self, module, start_pos, end_pos, parent):
        super(StatementElement, self).__init__(module, start_pos, end_pos, parent)
        self.next = None
        self.previous = None

    def set_next(self, call):
        """ Adds another part of the statement"""
        call.parent = self.parent
        if self.next is not None:
            self.next.set_next(call)
        else:
            self.next = call
            call.previous = self

    def next_is_execution(self):
        return Array.is_type(self.next, Array.TUPLE, Array.NOARRAY)

    def generate_call_path(self):
        """ Helps to get the order in which statements are executed. """
        try:
            yield self.name
        except AttributeError:
            yield self
        if self.next is not None:
            for y in self.next.generate_call_path():
                yield y


class Call(StatementElement):
    __slots__ = ('name',)

    def __init__(self, module, name, start_pos, end_pos, parent=None):
        super(Call, self).__init__(module, start_pos, end_pos, parent)
        name.parent = self
        self.name = name

    def names(self):
        """
        Generate an array of string names. If a call is not just names,
        raise an error.
        """
        def check(call):
            while call is not None:
                if not isinstance(call, Call):  # Could be an Array.
                    break
                yield unicode(call.name)
                call = call.next

        return list(check(self))


    def __repr__(self):
        return "<%s: %s>" % (type(self).__name__, self.name)


class Array(StatementElement):
    """
    Describes the different python types for an array, but also empty
    statements. In the Python syntax definitions this type is named 'atom'.
    http://docs.python.org/py3k/reference/grammar.html
    Array saves sub-arrays as well as normal operators and calls to methods.

    :param array_type: The type of an array, which can be one of the constants
        below.
    :type array_type: int
    """
    __slots__ = ('type', 'end_pos', 'values', 'keys')
    NOARRAY = None  # just brackets, like `1 * (3 + 2)`
    TUPLE = 'tuple'
    LIST = 'list'
    DICT = 'dict'
    SET = 'set'

    def __init__(self, module, start_pos, arr_type=NOARRAY, parent=None):
        super(Array, self).__init__(module, start_pos, (None, None), parent)
        self.end_pos = None, None
        self.type = arr_type
        self.values = []
        self.keys = []

    def add_statement(self, statement, is_key=False):
        """Just add a new statement"""
        statement.parent = self
        if is_key:
            self.type = self.DICT
            self.keys.append(statement)
        else:
            self.values.append(statement)

    @staticmethod
    def is_type(instance, *types):
        """
        This is not only used for calls on the actual object, but for
        ducktyping, to invoke this function with anything as `self`.
        """
        try:
            if instance.type in types:
                return True
        except AttributeError:
            pass
        return False

    def __len__(self):
        return len(self.values)

    def __getitem__(self, key):
        if self.type == self.DICT:
            raise TypeError('no dicts allowed')
        return self.values[key]

    def __iter__(self):
        if self.type == self.DICT:
            raise TypeError('no dicts allowed')
        return iter(self.values)

    def items(self):
        if self.type != self.DICT:
            raise TypeError('only dicts allowed')
        return zip(self.keys, self.values)

    def __repr__(self):
        if self.type == self.NOARRAY:
            typ = 'noarray'
        else:
            typ = self.type
        return "<%s: %s%s>" % (type(self).__name__, typ, self.values)


class ListComprehension(ForFlow):
    """ Helper class for list comprehensions """
    def __init__(self, module, stmt, middle, input, parent):
        self.input = input
        nested_lc = input.expression_list()[0]
        if isinstance(nested_lc, ListComprehension):
            # is nested LC
            input = nested_lc.stmt
            nested_lc.parent = self

        super(ListComprehension, self).__init__(module, [input],
                                                stmt.start_pos, middle)
        self.parent = parent
        self.stmt = stmt
        self.middle = middle
        for s in middle, input:
            s.parent = self
        # The stmt always refers to the most inner list comprehension.
        stmt.parent = self._get_most_inner_lc()

    def _get_most_inner_lc(self):
        nested_lc = self.input.expression_list()[0]
        if isinstance(nested_lc, ListComprehension):
            return nested_lc._get_most_inner_lc()
        return self

    @property
    def end_pos(self):
        return self.stmt.end_pos

    def __repr__(self):
        return "<%s: %s>" % (type(self).__name__, self.get_code())
