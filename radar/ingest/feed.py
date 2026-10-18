"""Demo feed delivery: read data/feed/<slug>/<source>.jsonl forward from a byte offset per
(tenant, source) and ingest every record whose deliver_at has come.

Sources are merged in deliver_at order, so replay sees records in knowledge-time order. Each
batch and its offsets commit in one transaction: a crash never skips or repeats a record.

A deployment without the files (serverless has no disk, Ruling Y) reads the same lines from
feed_buffer, which bootstrap fills. A buffer row's line_no is the file offset just past its
line, so both modes share one cursor; delivered rows are deleted to save storage.
"""

import heapq
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path

from psycopg.rows import tuple_row

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


def _buffered(conn, source: str, offset: int, until: str):
    """_lines, read from feed_buffer through a server-side cursor."""
    with conn.cursor(name=f"feed_{source}", row_factory=tuple_row) as cur:
        cur.itersize = 2000
        cur.execute("SELECT line_no, line FROM feed_buffer WHERE source = %s AND line_no > %s"
                    " AND deliver_at <= %s ORDER BY line_no", (source, offset, until))
        for line_no, line in cur:
            rec = json.loads(line)
            yield rec["deliver_at"], source, rec, line_no


def deliver_due(db, tenant_id, now: datetime, data_dir: Path, batch: int = 2000,
                on_batch=None, from_db: bool = False) -> pipeline.IngestStats:
    """on_batch(conn, stats) runs in each batch's transaction, before it commits. Reads
    feed_buffer when from_db or when the tenant has no feed folder."""
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
            if from_db or not folder.is_dir():
                streams = [_buffered(conn, s, offsets[s], until) for s in SOURCES]
            else:
                streams = [_lines(folder / f"{s}.jsonl", offsets[s], until) for s in SOURCES]
            merged = heapq.merge(*streams, key=lambda x: (x[0], x[1]))
            records, ends = [], {}
            for deliver_at, source, rec, end in merged:
                # end a batch only between seconds, so the batch order matches a full sort
                if len(records) >= batch and deliver_at != records[-1]["deliver_at"]:
                    break
                records.append(rec)
                ends[source] = end
            for stream in streams:  # closes the server-side cursors inside the transaction
                stream.close()
            if not records:
                return total
            stats = pipeline.ingest(conn, tenant_id, records)
            if on_batch:
                on_batch(conn, stats)
            total += stats
            for source, end in ends.items():
                conn.execute("UPDATE source_status SET feed_offset = %s WHERE source = %s",
                             (end, source))
                conn.execute("DELETE FROM feed_buffer WHERE source = %s AND line_no <= %s",
                             (source, end))


def load_buffer(db, tenant_id, data_dir: Path, batch: int = 5000) -> int:
    """Copy the tenant's feed lines past its cursors into feed_buffer, one multi-row insert
    per batch; returns the rows added (0 on a re-run)."""
    with db.tenant_tx(tenant_id) as conn:
        slug = conn.execute("SELECT slug FROM tenants WHERE id = %s",
                            (tenant_id,)).fetchone()["slug"]
        offsets = {r["source"]: r["feed_offset"] for r in conn.execute(
            "SELECT source, feed_offset FROM source_status")}
    added = 0
    for source in SOURCES:
        lines = _lines(Path(data_dir) / "feed" / slug / f"{source}.jsonl",
                       offsets.get(source, 0), "9999")  # every complete line
        while chunk := [(at, json.dumps(rec, separators=(",", ":")), end)
                        for at, _, rec, end in islice(lines, batch)]:
            at, text, end = map(list, zip(*chunk, strict=True))
            with db.tenant_tx(tenant_id) as conn:
                added += conn.execute(
                    "INSERT INTO feed_buffer (source, line_no, deliver_at, line)"
                    " SELECT %s, * FROM unnest(%s::bigint[], %s::timestamptz[], %s::text[])"
                    " ON CONFLICT DO NOTHING", (source, end, at, text)).rowcount
    return added
