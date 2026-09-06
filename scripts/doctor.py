#!/usr/bin/env python3
"""Check a local SESKit setup.

    uv run python scripts/doctor.py

A wrapper so this is reachable without knowing the package layout - the person
who most needs it is the one whose first hour is going badly. The checks
themselves live in ``seskit_api.doctor``, where the container can reach them
too:

    docker compose exec api python -m seskit_api.doctor
"""

from __future__ import annotations

import sys

from seskit_api.doctor import main

if __name__ == "__main__":
    sys.exit(main())
