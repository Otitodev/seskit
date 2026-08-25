"""SESKit core: configuration, logging, persistence, and shared domain logic.

This package owns everything that both the API and the worker need. It must not
import from either of them - the dependency arrow points inward (§32.12).
"""

__all__: list[str] = []
