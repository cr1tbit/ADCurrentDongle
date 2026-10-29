"""Shared look for the protocol figures (scripts/plot_*.py). Needs the `plots` extra (matplotlib).

Categorical colours in fixed order (validated: CVD-separated, normal-vision floor OK; aqua and
yellow are below 3:1 on the surface, so every figure carries a legend and the protocol a table).
"""

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
SURFACE = "#fcfcfb"
INK = "#2b2b29"         # primary text
INK_2 = "#5c5b56"       # secondary text, axis labels
GRID = "#e4e3de"
RULE = "#9a998f"        # reference lines

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "font.family": "sans-serif", "font.size": 10,
    "text.color": INK, "axes.labelcolor": INK_2, "xtick.color": INK_2, "ytick.color": INK_2,
    "axes.edgecolor": GRID, "axes.linewidth": 0.8,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.titlesize": 11, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "legend.frameon": False, "legend.fontsize": 9,
    "lines.linewidth": 2.0, "lines.solid_capstyle": "round",
})


def figure(w=8.0, h=4.2):
    return plt.subplots(figsize=(w, h), layout="constrained")


def save(fig, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=200)
    plt.close(fig)
    print(f"-> {path}")
