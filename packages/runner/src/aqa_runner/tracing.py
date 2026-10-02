"""Tracing stays off (SECURITY §10, ADR-0007 amendment). A customer's environment
may carry LangChain's tracing variables for their own tooling; an aqa run must
not export through them, and nothing in M1 turns export on."""

import os

import langsmith

# The version 1 switches. With tracing disabled and either one set, langchain_core
# raises on every call ("Tracing using LangChainTracerV1 is no longer
# supported", callbacks/manager.py), so they are dropped rather than ignored.
_VERSION_1 = ("LANGCHAIN_TRACING", "LANGCHAIN_HANDLER")


def ignore_ambient_tracing() -> None:
    """Switch LangSmith export off for the whole process, whatever the
    environment says: `langsmith.configure(enabled=False)` outranks
    LANGSMITH_TRACING, LANGCHAIN_TRACING_V2 and LANGCHAIN_TRACING."""
    langsmith.configure(enabled=False)
    for name in _VERSION_1:
        os.environ.pop(name, None)
