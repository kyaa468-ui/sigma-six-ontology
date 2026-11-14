"""Historical fixtures pin their historical wire contract explicitly.

Compact integration tests opt into the new protocol; production's default is
tested with the environment unset. This does not change storage or validation.
"""
import pytest


def pytest_configure(config):
    config.addinivalue_line('markers', 'semantic: exercise the staged semantic judgment protocol')
    config.addinivalue_line('markers', 'compact_wire: exercise the compact AI wire protocol')


@pytest.fixture(autouse=True)
def wire_contract(request, monkeypatch):
    monkeypatch.setenv('SION_SEMANTIC_MODE', 'strict' if request.node.get_closest_marker('semantic') else 'legacy')
    monkeypatch.setenv('SION_EVIDENCE_WIRE_FORMAT',
                      'COMPACT_EVIDENCE@v1' if request.node.get_closest_marker('compact_wire') else 'legacy')
