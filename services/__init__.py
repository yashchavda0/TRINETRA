"""Standalone services that feed the TRINETRA analytics bus.

These run as their own processes rather than inside the API or the handoff
worker: they are CPU-bound, they scale by adding more of them, and a stall in
one must not stall the request path.
"""

__all__: list[str] = []
