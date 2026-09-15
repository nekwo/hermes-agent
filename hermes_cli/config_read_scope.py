"""Context-local read projection; explicit config writes keep the persisted view."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

_projection = ContextVar("hermes_readonly_config_projection", default=None)


@contextmanager
def readonly_config_scope(project):
    token = _projection.set(project)
    try:
        yield
    finally:
        _projection.reset(token)


def project_readonly_config(config):
    project = _projection.get()
    return config if project is None else project(deepcopy(config))
