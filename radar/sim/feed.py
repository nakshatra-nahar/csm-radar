"""Feed files and identity truth: what ingestion reads, and what the evaluation checks it
against (design §3.5). Task 3 uses byte offsets into these files as its cursor."""

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from radar.sim import emit_gmail, emit_product, emit_salesforce, emit_stripe, emit_zendesk
from radar.sim.delivery import iso
from radar.sim.params import HISTORY_END
from radar.sim.truth import write_truth
from radar.sim.world import simulate, tenant_rng
from radar.tenants import TENANTS

EMITTERS = {"salesforce": emit_salesforce.emit, "stripe": emit_stripe.emit,
            "zendesk": emit_zendesk.emit, "gmail": emit_gmail.emit,
            "product": emit_product.emit}


def _sorted_lines(envelopes: list[dict]) -> list[tuple[str, str]]:
    """(deliver_at, line) pairs. Lines are compact, key-sorted JSON, so a Stripe body is
    serialised exactly as its signature covers it; the line text breaks deliver_at ties."""
    return sorted((iso(x["deliver_at"]), json.dumps(x | {"deliver_at": iso(x["deliver_at"])},
                                                    separators=(",", ":"), sort_keys=True))
                  for x in envelopes)


def generate(out_dir: Path, seed: int = 7, scale: float = 1.0, tenants: list[str] | None = None,
             sim_end: date = date(2027, 4, 30),
             history_end: datetime = HISTORY_END,
             count_only: bool = False) -> dict:
    """Simulate each tenant and write out_dir/feed/<slug>/<source>.jsonl, the truth parquet
    files and out_dir/truth/<slug>_identity.parquet. count_only writes nothing."""
    out_dir, cutoff, summary = Path(out_dir), iso(history_end), {}
    for slug in tenants or list(TENANTS):
        world = simulate(slug, seed, scale, sim_end, history_end)
        feed, identity = {}, set()
        for k, (source, emit) in enumerate(EMITTERS.items()):
            envelopes, handles = emit(world, tenant_rng(slug, seed, salt=600 + k))
            feed[source] = _sorted_lines(envelopes)
            identity |= {(source, ref, true) for ref, true in handles}
        summary[slug] = {
            "accounts": len(world.accounts),
            "records_by_source": {s: len(v) for s, v in feed.items()},
            "history_records": sum(at <= cutoff for v in feed.values() for at, _ in v),
        }
        if count_only:
            continue
        folder = out_dir / "feed" / slug
        folder.mkdir(parents=True, exist_ok=True)
        for source, lines in feed.items():
            (folder / f"{source}.jsonl").write_text("".join(x + "\n" for _, x in lines))
        write_truth(world, out_dir)
        pd.DataFrame(sorted(identity), columns=["source", "account_ref", "true_sf_account_id"]
                     ).to_parquet(out_dir / "truth" / f"{slug}_identity.parquet", index=False)
    return {"tenants": summary}
