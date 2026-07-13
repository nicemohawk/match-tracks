"""CLI wrapper for the satellite field-seeding worker (V2 §8).

Processes pending seed requests with the configured imagery provider:

    ENV=prod PYTHONPATH=. python scripts/run_seed_jobs.py

With no IMAGERY_PROVIDER configured this uses the NullImageryProvider and
requests fail cleanly — the imagery licensing decision (Apple Maps Server API
vs Mapbox/Maxar) is a human call; see docs/backend-v2-architecture.md §8.
"""

import sys


def run():
    from match_tracks import app
    from match_tracks.seeding import run_pending_seed_jobs

    with app.app_context():
        outcome = run_pending_seed_jobs()
    print(f"processed={outcome['processed']} fields_created={outcome['fields_created']}")


if __name__ == '__main__':
    sys.exit(run())
