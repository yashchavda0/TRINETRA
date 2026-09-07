"""Long-running background workers for the TRINETRA pipeline.

`handoff_worker` is the process that makes `engine/` and `adapters/` live: it
consumes the analytics bus, drives the Model 4 Re-ID matcher and predictive
handoff manager, mirrors their state to PostgreSQL, and screens plate reads
against the external state registries.
"""

__all__: list[str] = []
