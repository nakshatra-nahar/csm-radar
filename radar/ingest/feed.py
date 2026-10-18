"""Demo feed delivery: read data/feed/<slug>/<source>.jsonl forward from a byte offset per
(tenant, source) and ingest every record whose deliver_at has come.

Sources are merged in deliver_at order, so replay sees records in knowledge-time order. Each
batch and its offsets commit in one transaction: a crash never skips or repeats a record.
"""

import heapq
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from radar.ingest import pipeline

SOURCES = ("gmail", "product", "salesforce", "stripe", "zendesk")


def _lines(path: Path, offset: int, until: str) -> Iterator[tuple[str, str, dict, int]]:
    """(deliver_at, source, record, end offset) for each complete line due by `until`."""
    if not path.exists():
        return
    with path.open("rb") as f:
        f.seek(offset)
        for line in iter(f.readline, b""):
            if not line.endswith(b"\n"):  # a line still being written
                return
            rec = json.loads(line)
            if rec["deliver_at"] > until:
                return
            offset += len(line)
            yield rec["deliver_at"], rec["source"], rec, offset


def deliver_due(db, tenant_id, now: datetime, data_dir: Path, batch: int = 2000,
                on_batch=None) -> pipeline.IngestStats:
    """on_batch(conn, stats) runs in each batch's transaction, before it commits."""
    until = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # the feed's own format
    total = pipeline.IngestStats()
    while True:
        with db.tenant_tx(tenant_id) as conn:
            slug = conn.execute("SELECT slug FROM tenants WHERE id = %s",
                                (tenant_id,)).fetchone()["slug"]
            for source in SOURCES:
                conn.execute("INSERT INTO source_status (source) VALUES (%s)"
                             " ON CONFLICT DO NOTHING", (source,))
            # FOR UPDATE: a concurrent tick for this tenant waits, then reads our offsets
            offsets = {r["source"]: r["feed_offset"] for r in conn.execute(
                "SELECT source, feed_offset FROM source_status WHERE source = ANY(%s)"
                " FOR UPDATE", (list(SOURCES),))}
            folder = Path(data_dir) / "feed" / slug
            merged = heapq.merge(*(_lines(folder / f"{s}.jsonl", offsets[s], until)
                                   for s in SOURCES), key=lambda x: (x[0], x[1]))
            records, ends = [], {}
            for deliver_at, source, rec, end in merged:
                # end a batch only between seconds, so the batch order matches a full sort
                if len(records) >= batch and deliver_at != records[-1]["deliver_at"]:
                    break
                records.append(rec)
                ends[source] = end
            if not records:
                return total
            stats = pipeline.ingest(conn, tenant_id, records)
            if on_batch:
                on_batch(conn, stats)
            total += stats
            for source, end in ends.items():
                conn.execute("UPDATE source_status SET feed_offset = %s WHERE source = %s",
                             (end, source))
