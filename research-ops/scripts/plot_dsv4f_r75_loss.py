#!/usr/bin/env python3
"""Extract and plot the deduplicated loss curve for the DSV4 Flash r75 job."""

import argparse
import csv
import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


METRIC_RE = re.compile(
    r"\[(?P<timestamp>\d{4}-\d{2}-\d{2} [0-9:.]+) [^]]+\].*?"
    r"log_utils\.py:\d+ - step (?P<step>\d+): \{"
    r"'train/loss': (?P<loss>[-+0-9.eE]+), "
    r"'train/grad_norm': (?P<grad_norm>[-+0-9.eE]+), "
    r"'train/lr-pg_0': (?P<lr>[-+0-9.eE]+)"
)


def parse_metrics(log_path: Path) -> list[dict[str, float | int | str]]:
    by_step: dict[int, dict[str, float | int | str]] = {}
    with log_path.open(errors="replace") as handle:
        for line in handle:
            match = METRIC_RE.search(line)
            if not match:
                continue
            step = int(match.group("step"))
            by_step[step] = {
                "step": step,
                "loss": float(match.group("loss")),
                "grad_norm": float(match.group("grad_norm")),
                "lr": float(match.group("lr")),
                "timestamp": match.group("timestamp"),
            }
    if not by_step:
        raise RuntimeError(f"No train/loss metrics found in {log_path}")
    return [by_step[step] for step in sorted(by_step)]


def moving_average(values: np.ndarray, window: int) -> np.ndarray:
    result = np.full(values.shape, np.nan, dtype=float)
    if len(values) >= window:
        result[window - 1 :] = np.convolve(values, np.ones(window) / window, mode="valid")
    return result


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def draw_dashed_line(draw: ImageDraw.ImageDraw, points: list[tuple[float, float]], fill: str, width: int) -> None:
    for index in range(len(points) - 1):
        x1, y1 = points[index]
        x2, y2 = points[index + 1]
        length = float(np.hypot(x2 - x1, y2 - y1))
        if length == 0:
            continue
        dash, gap = 13.0, 8.0
        distance = 0.0
        while distance < length:
            start = distance / length
            end = min(distance + dash, length) / length
            draw.line(
                (
                    x1 + (x2 - x1) * start,
                    y1 + (y2 - y1) * start,
                    x1 + (x2 - x1) * end,
                    y1 + (y2 - y1) * end,
                ),
                fill=fill,
                width=width,
            )
            distance += dash + gap


def render_plot(
    png_path: Path,
    job: str,
    steps: np.ndarray,
    losses: np.ndarray,
    smooth: np.ndarray,
    trend: np.ndarray,
) -> None:
    width, height = 1760, 992
    left, right, top, bottom = 145, 70, 145, 120
    plot_left, plot_top = left, top
    plot_right, plot_bottom = width - right, height - bottom
    plot_width, plot_height = plot_right - plot_left, plot_bottom - plot_top

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((plot_left, plot_top, plot_right, plot_bottom), fill="#fbfbfa")

    title_font = load_font(34, bold=True)
    subtitle_font = load_font(22)
    label_font = load_font(22)
    tick_font = load_font(18)
    legend_font = load_font(19)
    annotation_font = load_font(18, bold=True)

    draw.text((left, 35), "DeepSeek-V4-Flash true-packing training loss", fill="#161616", font=title_font)
    draw.text((left, 85), job, fill="#555b61", font=subtitle_font)

    y_min = float(min(losses.min(), trend.min()))
    y_max = float(max(losses.max(), trend.max()))
    padding = max((y_max - y_min) * 0.12, 0.002)
    y_min -= padding
    y_max += padding
    x_min, x_max = float(steps.min()), float(steps.max())

    def xy(step: float, loss: float) -> tuple[float, float]:
        x = plot_left + (step - x_min) / max(x_max - x_min, 1.0) * plot_width
        y = plot_bottom - (loss - y_min) / max(y_max - y_min, 1e-9) * plot_height
        return x, y

    for value in np.linspace(y_min, y_max, 7):
        _, y = xy(x_min, float(value))
        draw.line((plot_left, y, plot_right, y), fill="#d7d7d4", width=1)
        label = f"{value:.3f}"
        bbox = draw.textbbox((0, 0), label, font=tick_font)
        draw.text((plot_left - 16 - (bbox[2] - bbox[0]), y - 10), label, fill="#5f6368", font=tick_font)

    x_ticks = sorted(set(int(value) for value in np.linspace(x_min, x_max, 10)))
    for value in x_ticks:
        x, _ = xy(float(value), y_min)
        draw.line((x, plot_top, x, plot_bottom), fill="#ececea", width=1)
        label = str(value)
        bbox = draw.textbbox((0, 0), label, font=tick_font)
        draw.text((x - (bbox[2] - bbox[0]) / 2, plot_bottom + 18), label, fill="#5f6368", font=tick_font)

    draw.line((plot_left, plot_bottom, plot_right, plot_bottom), fill="#72767b", width=2)
    draw.line((plot_left, plot_top, plot_left, plot_bottom), fill="#72767b", width=2)

    raw_points = [xy(float(step), float(loss)) for step, loss in zip(steps, losses)]
    smooth_points = [
        xy(float(step), float(loss))
        for step, loss in zip(steps, smooth)
        if not np.isnan(loss)
    ]
    trend_points = [xy(float(step), float(loss)) for step, loss in zip(steps, trend)]
    draw.line(raw_points, fill="#2563a6", width=3, joint="curve")
    for x, y in raw_points:
        draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill="#2563a6")
    if smooth_points:
        draw.line(smooth_points, fill="#c65d21", width=5, joint="curve")
    draw_dashed_line(draw, trend_points, fill="#4f6658", width=3)

    latest_x, latest_y = raw_points[-1]
    draw.ellipse((latest_x - 7, latest_y - 7, latest_x + 7, latest_y + 7), fill="#b42318")
    latest_label = f"step {int(steps[-1])}: {losses[-1]:.4f}"
    latest_bbox = draw.textbbox((0, 0), latest_label, font=annotation_font)
    label_width = latest_bbox[2] - latest_bbox[0]
    draw.rounded_rectangle(
        (latest_x - label_width - 31, latest_y - 50, latest_x - 8, latest_y - 14),
        radius=5,
        fill="#fff3f1",
        outline="#e6a7a0",
        width=1,
    )
    draw.text((latest_x - label_width - 20, latest_y - 44), latest_label, fill="#7a271a", font=annotation_font)

    x_label = "Training step"
    bbox = draw.textbbox((0, 0), x_label, font=label_font)
    draw.text(((width - (bbox[2] - bbox[0])) / 2, height - 54), x_label, fill="#303438", font=label_font)
    y_label = "Assistant-token cross-entropy loss"
    label_image = Image.new("RGBA", (plot_height, 42), (255, 255, 255, 0))
    label_draw = ImageDraw.Draw(label_image)
    label_draw.text((0, 6), y_label, fill="#303438", font=label_font)
    label_image = label_image.rotate(90, expand=True)
    image.paste(label_image, (24, int((height - label_image.height) / 2)), label_image)

    legend_x, legend_y = plot_right - 370, plot_top + 22
    legend_items = [
        ("#2563a6", "train/loss", False),
        ("#c65d21", "5-step moving average", False),
        ("#4f6658", "linear trend", True),
    ]
    draw.rounded_rectangle((legend_x - 18, legend_y - 14, plot_right - 14, legend_y + 104), radius=6, fill="#ffffff", outline="#dededb")
    for index, (color, label, dashed) in enumerate(legend_items):
        y = legend_y + index * 34
        if dashed:
            draw.line((legend_x, y + 10, legend_x + 20, y + 10), fill=color, width=3)
            draw.line((legend_x + 29, y + 10, legend_x + 49, y + 10), fill=color, width=3)
        else:
            draw.line((legend_x, y + 10, legend_x + 49, y + 10), fill=color, width=4)
        draw.text((legend_x + 64, y), label, fill="#303438", font=legend_font)

    image.save(png_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--job", required=True)
    args = parser.parse_args()

    metrics = parse_metrics(args.log)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / "loss.csv"
    png_path = args.output_dir / "loss_curve.png"

    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["step", "loss", "grad_norm", "lr", "timestamp"])
        writer.writeheader()
        writer.writerows(metrics)

    steps = np.array([row["step"] for row in metrics], dtype=float)
    losses = np.array([row["loss"] for row in metrics], dtype=float)
    smooth = moving_average(losses, 5)
    slope, intercept = np.polyfit(steps, losses, 1)
    trend = slope * steps + intercept

    render_plot(png_path, args.job, steps, losses, smooth, trend)

    first_window = losses[: min(5, len(losses))]
    last_window = losses[-min(5, len(losses)) :]
    print(f"points={len(metrics)} first_step={int(steps[0])} last_step={int(steps[-1])}")
    print(f"first_loss={losses[0]:.10f} latest_loss={losses[-1]:.10f}")
    print(f"first5_mean={first_window.mean():.10f} last5_mean={last_window.mean():.10f}")
    print(f"min_loss={losses.min():.10f} min_step={int(steps[losses.argmin()])}")
    print(f"max_loss={losses.max():.10f} max_step={int(steps[losses.argmax()])}")
    print(f"linear_slope_per_step={slope:.10f}")
    print(f"csv={csv_path}")
    print(f"png={png_path}")


if __name__ == "__main__":
    main()
