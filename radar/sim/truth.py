"""Ground truth files. Only the evaluation CLI reads them; never the app database."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from radar.sim.world import World


def write_truth(world: World, out_dir: Path) -> None:
    """<out_dir>/truth/<slug>_accounts.parquet and <slug>_weekly.parquet."""
    path = Path(out_dir) / "truth"
    path.mkdir(parents=True, exist_ok=True)
    world.truth.to_parquet(path / f"{world.slug}_accounts.parquet", index=False)
    world.weekly.to_parquet(path / f"{world.slug}_weekly.parquet", index=False)
