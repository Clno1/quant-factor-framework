"""Restartable head polling and bounded overlapping backfill of FMP latest feeds.

Offset pagination is not a provider snapshot. Missing late/reordered records
remain an explicit coverage limitation, never a claim of whole-market recall.
"""
from datetime import datetime, timedelta

from .models import NEW_YORK, digest, timestamp


def collect_articles(radar, run_id, feed, start, end, budget):
    key = 'feed:' + feed
    previous = radar.pipeline.checkpoint(key) or {}
    cursor = max(1, int(previous.get('next_page', 1)))
    # Always inspect the newest page; replay one prior offset on resumed tails.
    pages = [0]
    tail = max(1, cursor - 1) if radar.settings.max_pages >= 3 else cursor
    pages += list(range(tail, tail + max(0, radar.settings.max_pages - 1)))
    rows_seen = invalid_total = 0
    seen = set()
    status = 'PAGINATION_LIMIT_REACHED'
    next_page = cursor
    newest = None
    previous_oldest = None
    ordered = True
    completed_through = previous.get('completed_through')
    cutoff = None
    attempted_pages = []
    if completed_through:
        cutoff = datetime.fromisoformat(completed_through) - timedelta(hours=24)
    for page in pages:
        before = budget.requests
        rows, request_status = budget.call(radar.provider.articles, feed, page, radar.settings.page_size)
        if budget.requests > before:
            attempted_pages.append(page)
        count, invalid = radar._page(run_id, feed, 'incremental-latest', page, rows, request_status, 'ARTICLE', start, end)
        rows_seen += count
        invalid_total += invalid
        if request_status != 'OK':
            status = request_status
            break
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            status = 'INVALID_RECORDS'
            break
        # Rejected rows are already quarantined in the page receipt. A bad
        # headline/URL must not prevent all later offsets from being visited.
        if invalid:
            ordered = False
        fingerprint = digest(rows)
        if fingerprint in seen:
            status = 'REPEATED_PAGE'
            break
        seen.add(fingerprint)
        times = []
        for row in rows:
            try:
                raw = datetime.fromisoformat(str(row.get('publishedDate') or '').replace('Z', '+00:00'))
                times.append(raw.replace(tzinfo=NEW_YORK) if raw.tzinfo is None else raw)
            except ValueError:
                ordered = False
        if page == 0 and times:
            newest = max(times)
        if times:
            ordered = ordered and times == sorted(times, reverse=True)
            ordered = ordered and (previous_oldest is None or max(times) <= previous_oldest)
            previous_oldest = min(times)
        if page > 0 or cursor <= 1:
            next_page = page + 1
        if count < radar.settings.page_size:
            status = 'END_OF_FEED'
            break
        if ordered and times and min(times).astimezone(NEW_YORK).date().isoformat() < start:
            status = 'WINDOW_BOUNDARY_REACHED'
            break
        if ordered and times and cutoff and max(times) < cutoff:
            status = 'INCREMENTAL_BOUNDARY_REACHED'
            break
    completed = status in {'END_OF_FEED', 'WINDOW_BOUNDARY_REACHED', 'INCREMENTAL_BOUNDARY_REACHED'}
    # Do not advance the watermark to this round's head if a previous unfinished
    # sweep may have skipped those intermediate offsets as the live feed moved.
    anchor = previous.get('sweep_anchor') or (timestamp(newest) if newest else None)
    checkpoint = {'next_page': 1 if completed else next_page,
                  'completed_through': anchor if completed and anchor and not invalid_total else completed_through,
                  'sweep_anchor': None if completed else anchor,
                  'last_status': status, 'window_start': start, 'received_at': timestamp(radar.clock()),
                  'market_complete': False}
    radar.pipeline.save_checkpoint(key, checkpoint, radar.clock())
    return {'feed': feed, 'status': 'PARTIAL_INVALID_RECORDS' if invalid_total else status,
            'pagination_status': status, 'rows': rows_seen, 'invalid_rows': invalid_total,
            'pages_requested': attempted_pages,
            'resume_page': checkpoint['next_page'], 'sweep_complete': completed and not invalid_total,
            'ordering_verified_in_sample': ordered,
            'coverage_gap_possible': True, 'coverage_reason': 'MOVING_OFFSET_FEED_NOT_A_SNAPSHOT',
            'window_advanced_with_unfinished_tail': bool(previous.get('window_start') and not previous.get('completed_through')
                                                        and previous['window_start'] != start)}
