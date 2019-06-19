"""
Jedi is a static analysis tool for Python that can be used in IDEs/editors.
Jedi has a focus on autocompletion and goto functionality. Jedi is fast and is
very well tested. It understands Python on a very deep level.

Jedi has support for different goto functions. It's possible to search for
usages and to list names in a Python file and get information about them. Jedi
understands docstrings.

Jedi uses a very simple API to connect with IDE's. There's a reference
implementation as a `VIM-Plugin <https://github.com/davidhalter/jedi-vim>`_,
which uses Jedi's autocompletion.  We encourage you to use Jedi in your IDEs.
There's also native support for Jedi within IPython and you can install it in
your REPL if you want.

Here's a simple example of the autocompletion feature:

>>> import jedi
>>> source = '''
... import json
... json.lo'''
>>> script = jedi.Script(source, 3, len('json.lo'), 'example.py')
>>> script
<Script: 'example.py' ...>
>>> completions = script.completions()
>>> completions
[<Completion: load>, <Completion: loads>]
>>> print(completions[0].complete)
ad
>>> print(completions[0].name)
load

As you see Jedi is pretty simple and allows you to concentrate on writing a
good text editor, while still having very good IDE features for Python.
"""

__version__ = '0.14.0'

from jedi.api import Script, Interpreter, set_debug_function, \
    preload_module, names
from jedi import settings
from jedi.api.environment import find_virtualenvs, find_system_environments, \
    get_default_environment, InvalidPythonEnvironment, create_environment, \
    get_system_environment
from jedi.api.exceptions import InternalError
