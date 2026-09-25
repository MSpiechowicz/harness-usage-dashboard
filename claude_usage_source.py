#!/usr/bin/env python3
"""Local Claude Code statusLine allowance windows; never infer an account quota."""
from __future__ import annotations

import argparse
import json
import math
import sys
import time

from session_usage import database, owner_key, project_id
from claude_bridge import _tables, capture_availability

MAX_AGE = 300


def fetch_usage(profile=None, owner=None, cwd=None, now=None):
    """Return only fresh, observed Pro/Max statusLine windows for this project and owner."""
    now = time.time() if now is None else now
    capture = capture_availability(profile, owner=owner, cwd=cwd, now=now)
    with database(profile, cwd, host='claude') as db:
        _tables(db)
        active = db.execute('SELECT session, activation FROM active WHERE project=? AND owner=?',
                            (project_id(cwd), owner_key(owner))).fetchone()
        row = (db.execute('SELECT * FROM claude_limits WHERE project=? AND owner=?',
                          (project_id(cwd), owner_key(owner))).fetchone()
               if active and active['activation'] else None)
    if (not row or not active or (row['session'], row['activation']) !=
            (active['session'], active['activation']) or
            row['observed'] > now + 60 or now - row['observed'] > MAX_AGE):
        return {'reports': [], 'dashboardNote':
                'Claude rate limits unavailable or stale; usage is unknown, not zero. ' + capture['reason'],
                'capture': capture}

    limits = []
    for key, label, prefix in (('five_hour', '5 hour', 'five'), ('seven_day', '7 day', 'seven')):
        used, reset = row[prefix + '_pct'], row[prefix + '_reset']
        if used is None or not math.isfinite(used) or not 0 <= used <= 100:
            continue
        if reset is not None and (not math.isfinite(reset) or reset <= now):
            continue
        limit = {
            'id': 'claude:' + key,
            'label': label,
            'scope': {'provider': 'anthropic', 'shared': True},
            'amount': {'usedFraction': used / 100, 'unit': 'percent'},
        }
        if reset is not None:
            limit['window'] = {'resetsAt': int(reset * 1000), 'resetLabel': 'reset'}
        limits.append(limit)
    if not limits:
        return {'reports': [], 'dashboardNote':
                'Claude reported no current rate-limit windows; availability is unknown, not zero. ' +
                capture['reason'], 'capture': capture}
    return {'reports': [{
        'provider': 'anthropic', 'fetchedAt': int(row['observed'] * 1000),
        'limits': limits, 'metadata': {'source': 'claude-statusline'},
    }], 'dashboardNote': capture['reason'], 'capture': capture}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile')
    parser.add_argument('--owner')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(fetch_usage(args.profile, owner=args.owner), allow_nan=False))
        return 0
    except (ValueError, OSError):
        print('Could not read local Claude usage.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
