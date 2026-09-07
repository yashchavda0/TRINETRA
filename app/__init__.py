"""Model 1 - Central GIS Camera Registry service.

Part of the Gujarat Police CCTV Integration System. Owns the authoritative
record of every camera in the integrated fleet and answers spatial coverage
queries against it.

Invariants enforced throughout this package:
  * Spatial data is WGS84 / EPSG:4326 only.
  * Timestamps are UTC. Epoch values crossing a wire are int64 milliseconds.
"""

__version__ = "1.0.0"
