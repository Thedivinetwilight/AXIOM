"""Dependency-free SVG charts for reports, API and the desktop package."""

from __future__ import annotations


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def line_chart_svg(title: str, points: list[tuple[float, float]], *,
                   width: int = 640, height: int = 240, color: str = "#4aa3ff") -> str:
    if not points:
        return (f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' "
                f"height='{height}'><text x='10' y='30'>{_esc(title)}: no data</text></svg>")
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    if y_max == y_min:
        y_max = y_min + 1
    pad = 36
    plot_w, plot_h = width - 2 * pad, height - 2 * pad

    def px(x):
        return pad + (x - x_min) / max(x_max - x_min, 1e-9) * plot_w

    def py(y):
        return height - pad - (y - y_min) / (y_max - y_min) * plot_h

    path = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in points)
    label_style = "font-family='monospace' font-size='10' fill='#8899aa'"
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}'>"
        f"<rect width='{width}' height='{height}' fill='#101820'/>"
        f"<text x='{pad}' y='18' font-family='monospace' font-size='12' "
        f"fill='#dce7f5'>{_esc(title)}</text>"
        f"<line x1='{pad}' y1='{height - pad}' x2='{width - pad}' "
        f"y2='{height - pad}' stroke='#33445a'/>"
        f"<line x1='{pad}' y1='{pad}' x2='{pad}' y2='{height - pad}' stroke='#33445a'/>"
        f"<text x='2' y='{pad + 8}' {label_style}>{_esc(f'{y_max:.2f}')}</text>"
        f"<text x='2' y='{height - pad}' {label_style}>{_esc(f'{y_min:.2f}')}</text>"
        f"<polyline fill='none' stroke='{color}' stroke-width='2' points='{path}'/>"
        f"</svg>")


def bar_chart_svg(title: str, values: list[tuple[str, float]], *,
                  width: int = 640, height: int = 240,
                  positive: str = "#3fae7a", negative: str = "#c0576b") -> str:
    if not values:
        return (f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' "
                f"height='{height}'><text x='10' y='30'>{_esc(title)}: no data</text></svg>")
    peak = max(abs(v) for _, v in values) or 1.0
    pad = 36
    plot_w, plot_h = width - 2 * pad, height - 2 * pad
    baseline = height - pad - plot_h // 2
    slot = plot_w / len(values)
    bar_w = max(4.0, slot * 0.6)
    rects, labels = [], []
    for index, (name, value) in enumerate(values):
        bar_h = abs(value) / peak * (plot_h // 2 - 6)
        x = pad + index * slot + (slot - bar_w) / 2
        y = baseline - bar_h if value >= 0 else baseline
        color = positive if value >= 0 else negative
        rects.append(f"<rect x='{x:.1f}' y='{y:.1f}' width='{bar_w:.1f}' "
                     f"height='{max(bar_h, 1.0):.1f}' fill='{color}'/>")
        labels.append(f"<text x='{x:.1f}' y='{height - pad + 12}' "
                      f"font-family='monospace' font-size='9' fill='#8899aa' "
                      f">{_esc(name)[:14]}</text>")
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}'>"
        f"<rect width='{width}' height='{height}' fill='#101820'/>"
        f"<text x='{pad}' y='18' font-family='monospace' font-size='12' "
        f"fill='#dce7f5'>{_esc(title)}</text>"
        f"<line x1='{pad}' y1='{baseline:.1f}' x2='{width - pad}' "
        f"y2='{baseline:.1f}' stroke='#33445a'/>"
        + "".join(rects) + "".join(labels) + "</svg>")
