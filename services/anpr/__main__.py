"""Entrypoint: ``python -m services.anpr``."""

from __future__ import annotations

from services.anpr.service import main

raise SystemExit(main())
