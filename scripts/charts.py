"""Evaluation chart for the docs and the deck, drawn from the committed eval/*.json:

    uv run --with matplotlib python scripts/charts.py [--out docs/img]

matplotlib is not a project dependency; it is only needed here.
"""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TENANTS = ("hearthline", "tallyworks")
# gbm is left out: it is a diagnostic in eval/*.json (how much a flexible model would add),
# not a policy anyone serves
POLICIES = {"random": "random", "renewal_sort": "renewal-date\nsort", "arr_sort": "ARR sort",
            "rules": "rules", "model": "model", "oracle": "oracle\n(latent state)"}
COLOURS = ("#0072B2", "#E69F00")  # Okabe-Ito blue and orange: safe for colour-blind readers
LABELS = ("white", "black")  # value labels that keep contrast on each bar colour
SHORT_BAR = 6  # %: below this a label inside the bar would sit on the background


def recall_rows(ev: dict) -> list[tuple[str, float, float, float]]:
    """(policy, recall@K, CI low, CI high) in POLICIES order; a missing policy is a KeyError."""
    ci = ev["ci"]["recall"]
    return [(p, ci[p]["diff"], ci[p]["lo"], ci[p]["hi"]) for p in POLICIES]


def recall_chart(evals: dict[str, dict], out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 5.2), dpi=150)
    width = 0.38
    for i, (tenant, ev) in enumerate(evals.items()):
        rows = recall_rows(ev)
        xs = [j + (i - 0.5) * width for j in range(len(rows))]
        ys = [100 * r[1] for r in rows]
        err = [[100 * (r[1] - r[2]) for r in rows], [100 * (r[3] - r[1]) for r in rows]]
        ax.bar(xs, ys, width, color=COLOURS[i], label=f"{tenant} (K = {ev['k']})",
               yerr=err, capsize=3, error_kw={"elinewidth": 1, "ecolor": "#333333"})
        for x, y, hi in zip(xs, ys, err[1], strict=True):
            inside = y >= SHORT_BAR  # a short bar has no room: the label goes above its CI
            ax.text(x, 2 if inside else y + hi + 1.5, f"{y:.0f}", ha="center", va="bottom",
                    fontsize=9, color=LABELS[i] if inside else "black", fontweight="bold")
    ax.set_xticks(range(len(POLICIES)), POLICIES.values(), fontsize=11)
    ax.set_ylabel("Churned ARR on the top-K lists (%)", fontsize=11)
    ax.set_ylim(0, 100)
    ax.set_title("Recall@K (ARR-weighted) by ranking policy, with 95% bootstrap CIs",
                 fontsize=13, loc="left")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#dddddd", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, fontsize=11, loc="upper left")
    fig.tight_layout()
    fig.savefig(out / "recall.png")
    plt.close(fig)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "img")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    recall_chart({t: json.loads((ROOT / "eval" / f"{t}.json").read_text()) for t in TENANTS},
                 args.out)


if __name__ == "__main__":
    main()
