"""Verify TLS against the operating system's trust store.

Python's default context is not good enough here for two reasons: the python.org
macOS builds ship with an empty CA bundle until someone runs
``Install Certificates.command``, and a self-hosted botc-scripts instance often
sits behind a private or corporate CA that the machine already trusts but a
bundled certifi list never will. ``truststore`` delegates to the OS, which
covers both, and it must run before any SSL context is created.
"""

from __future__ import annotations

import logging

_LOGGER = logging.getLogger(__name__)


def install_os_trust_store() -> bool:
    try:
        import truststore
    except ImportError:
        _LOGGER.debug("truststore is not installed; using Python's default CA bundle.")
        return False

    truststore.inject_into_ssl()
    return True
