"""Tests for raise_stack_limit() in utils."""
import sys
import logging
import types
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest

import src.utils as utils


class _FakeResource:
    """Minimal stand-in for the `resource` module."""

    RLIMIT_STACK = 3
    RLIM_INFINITY = 2**63 - 1

    def __init__(self, initial, raise_on_set=None):
        self.limits = dict(initial)  # {resource: (soft, hard)}
        self.raise_on_set = raise_on_set  # exception class or None
        self.set_calls = []

    def getrlimit(self, which):
        return self.limits[which]

    def setrlimit(self, which, value):
        self.set_calls.append((which, value))
        if self.raise_on_set is not None:
            raise self.raise_on_set("Operation not permitted")
        self.limits[which] = value


@pytest.fixture
def restore_resource():
    """Save utils' view of the resource module and restore it after a test."""
    import resource as real_resource
    saved = sys.modules["resource"]
    yield
    sys.modules["resource"] = saved
    # Re-import inside raise_stack_limit each call, so nothing else to reset.


def _inject(fake):
    sys.modules["resource"] = fake


def test_raises_soft_to_hard(restore_resource):
    fake = _FakeResource({_FakeResource.RLIMIT_STACK: (8 * 1024 * 1024, _FakeResource.RLIM_INFINITY)})
    _inject(fake)
    assert utils.raise_stack_limit() is True
    soft, hard = fake.getrlimit(_FakeResource.RLIMIT_STACK)
    assert soft == _FakeResource.RLIM_INFINITY
    assert hard == _FakeResource.RLIM_INFINITY
    assert len(fake.set_calls) == 1


def test_noop_when_already_at_hard(restore_resource):
    fake = _FakeResource({_FakeResource.RLIMIT_STACK: (64 * 1024 * 1024, 64 * 1024 * 1024)})
    _inject(fake)
    assert utils.raise_stack_limit() is True
    assert fake.set_calls == []  # no setrlimit attempted


def test_noop_when_already_unlimited(restore_resource):
    fake = _FakeResource({
        _FakeResource.RLIMIT_STACK: (_FakeResource.RLIM_INFINITY, _FakeResource.RLIM_INFINITY)
    })
    _inject(fake)
    assert utils.raise_stack_limit() is True
    assert fake.set_calls == []


def test_failure_returns_false_and_does_not_raise(restore_resource):
    # macOS-like: any raise is refused (EPERM)
    fake = _FakeResource(
        {_FakeResource.RLIMIT_STACK: (8 * 1024 * 1024, 64 * 1024 * 1024)},
        raise_on_set=OSError,
    )
    _inject(fake)
    logger = logging.getLogger("t.stack")
    assert utils.raise_stack_limit(logger) is False  # no exception, graceful
    assert len(fake.set_calls) == 1  # did attempt


def test_missing_resource_module(restore_resource):
    sys.modules["resource"] = None  # forces `import resource` to ImportError
    assert utils.raise_stack_limit() is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
