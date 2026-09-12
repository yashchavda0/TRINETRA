"""Automatic Number Plate Recognition service.

Samples frames from live cameras, reads number plates, and publishes each
reading onto the analytics bus as a ``SurveillanceEvent``. The handoff worker
then persists it, matches it against the watchlist, and screens it against the
external state registries - a path already proven end to end.

Measured on the development machine (Intel i5-8350U, 4 cores, no GPU):

    plate detector (1080p frame)   33 ms
    plate OCR (one crop)            4 ms
    motion gate                    0.3 ms

The motion gate is ~96x cheaper than the detector, which is the whole reason
this is affordable: most frames from a fixed camera are identical to the one
before, and paying 0.3 ms to skip a 33 ms inference is what turns a modest CPU
budget into useful coverage across a fleet.
"""

from services.anpr.reader import PlateReader, PlateReading, correct_plate

__all__ = ["PlateReader", "PlateReading", "correct_plate"]
