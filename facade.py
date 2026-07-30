"""Support for the `database_manager` and `llm_handler` facades.

Both modules used to be single large namespaces. They are now thin facades over
the `db/` and `llm/` packages, and that change quietly breaks two things that
callers had always been able to do:

**Monkeypatching.** A test that does `llm_handler._triage_job = stub` used to
affect every caller, because there was only one namespace. After a split it
rebinds the facade's name only — the real function inside `llm/analysis.py`
still runs. Nothing raises. A test can appear to pass while exercising the
production code path, which for this codebase means real network calls to an
LLM endpoint and real writes to a database.

**Mutable module state.** `database_manager.DB_FILE` is repointed at a
throwaway database by the test suite. A plain re-export would create a second
binding, so the assignment would move the facade's copy while
`db.connection.get_db_connection()` kept opening the original file. See
`tests/conftest.py` for the incident that makes this non-negotiable.

`install()` fixes both by giving the facade a module type that forwards
attribute writes into the package. It is deliberately small and boring: no
import hooks, no lazy loading, and every name is still statically visible in the
facade's own import block so linters and editors keep working.
"""
import sys
from types import ModuleType


def install(facade_name, modules, proxied=(), proxy_owner=None):
    """Give the module named `facade_name` write-forwarding behaviour.

    `modules` is the package's submodules, in layer order. Setting an attribute
    on the facade also sets it on every submodule that already binds that name,
    which reproduces the single-namespace patching semantics callers relied on.
    Binding on *every* holder matters: a module that imported a helper from an
    earlier layer has its own reference, and patching only the defining module
    would leave that one stale.

    `proxied` names are not re-exported onto the facade at all. Reads fall
    through to `proxy_owner` and writes go only there, so exactly one binding
    exists. Use it for mutable process state.
    """
    facade = sys.modules[facade_name]
    if proxied and proxy_owner is None:
        raise ValueError("proxied names need a proxy_owner module")

    for name in proxied:
        if name in vars(facade):
            raise ImportError(
                f"{name} must not be re-exported into {facade_name}: it is mutable state "
                f"owned by {proxy_owner.__name__} and has to stay proxied so that exactly "
                "one binding exists. See facade.py."
            )

    holders = {}
    for module in modules:
        for name in vars(module):
            if not name.startswith("__"):
                holders.setdefault(name, []).append(module)

    class Facade(ModuleType):
        __doc__ = facade.__doc__

        def __getattr__(self, name):
            # Only reached when normal lookup fails, which for a correctly
            # constructed facade means one of the proxied names.
            if name in proxied:
                return getattr(proxy_owner, name)
            raise AttributeError(f"module {facade_name!r} has no attribute {name!r}")

        def __setattr__(self, name, value):
            if name in proxied:
                setattr(proxy_owner, name, value)
                return
            for module in holders.get(name, ()):
                setattr(module, name, value)
            super().__setattr__(name, value)

        def __delattr__(self, name):
            for module in holders.get(name, ()):
                try:
                    delattr(module, name)
                except AttributeError:
                    pass
            super().__delattr__(name)

    facade.__class__ = Facade
    return facade
