"""Draw the descriptive results; all points are retained, no forecast fitted."""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

OUT = Path(__file__).resolve().parent


def main():
    samples = pd.read_csv(OUT / "qa/independent_one_minute_samples.csv")
    stats = pd.read_csv(OUT / "lagged_feature_correlations.csv")
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(1, 3, figsize=(15, 5.6), gridspec_kw={"width_ratios": [1, 1, 1.35]})
    fig.patch.set_facecolor("#f8fafc")
    palette = {"CE": "#047857", "PE": "#7c3aed"}
    for ax, side, label in zip(axes[:2], ["CE", "PE"], ["Calls move with NIFTY", "Puts move against NIFTY"]):
        d = samples.loc[samples["optiontype"].eq(side)]
        r = d["spot_change"].corr(d["midpoint_change"])
        ax.scatter(d["spot_change"], d["midpoint_change"], s=13, alpha=0.25,
                   color=palette[side], edgecolors="none", rasterized=True)
        ax.axhline(0, color="#cbd5e1", linewidth=0.8)
        ax.axvline(0, color="#cbd5e1", linewidth=0.8)
        ax.set(xlim=(-26, 26), ylim=(-17, 17), xlabel="Same-minute NIFTY change (points)",
               ylabel="Same-minute option midpoint change (points)",
               title=f"{label}\nPearson r = {r:+.3f} | n = {len(d):,}")
        ax.grid(alpha=0.12)

    features = ["past_spot_return_pct", "past_midpoint_return_pct", "past_oi_change_pct",
                "past_volume_increment", "iv_decimal", "past_iv_change_pp"]
    labels = ["Prior NIFTY return", "Prior option return", "Prior OI change",
              "Prior volume increment", "Available IV level", "Prior IV change"]
    ax = axes[2]
    pooled = stats.loc[stats["date"].eq("POOLED")]
    for side, offset in [("CE", -0.13), ("PE", 0.13)]:
        d = pooled.loc[pooled["optiontype"].eq(side)].set_index("feature").loc[features]
        ax.scatter(d["spearman"], [i + offset for i in range(len(features))],
                   s=42, color=palette[side], label=side, zorder=3)
    ax.axvline(0, color="#64748b", linewidth=0.8)
    ax.set(yticks=range(len(features)), yticklabels=labels, xlim=(-1, 1),
           xlabel="Spearman correlation with next-minute return",
           title="Earlier features: weak associations\nSeven days | 1,748–1,788 windows per side")
    ax.invert_yaxis()
    ax.grid(axis="x", alpha=0.16)
    ax.legend(frameon=False, loc="lower left", ncol=2)
    fig.suptitle("NIFTY ATM options: explanation and prediction are different questions",
                 fontsize=15, fontweight="bold", x=0.055, ha="left", y=0.99)
    fig.text(0.055, 0.055,
             "Left: 9 stored sessions, 7–18 Sep 2026; 1,373 shared CE/PE time windows. "
             "Right: v2 receipt records, 9–18 Sep.\n"
             "Same-contract windows only; application times are not verified exchange times. "
             "Exploratory associations; no forecasting or trading advantage established.",
             fontsize=9, color="#475569", va="bottom")
    fig.tight_layout(rect=(0.02, 0.15, 1, 0.94), w_pad=2)
    fig.savefig(OUT / "feature_relationships.png", dpi=180, facecolor=fig.get_facecolor())
    fig.savefig(OUT / "feature_relationships.pdf", facecolor=fig.get_facecolor())
    plt.close(fig)


if __name__ == "__main__":
    main()
