#!/usr/bin/env python3
"""Crop Kaspi Express platform waybills to the product-label panel.

This script is intentionally local-only: it does not call Kaspi APIs, generate
waybills, mutate orders, or print. Feed it the platform waybill PDF and it
returns the single "НА ТОВАР" panel as a thermal-label PDF.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_WIDTH_MM = 75.0
DEFAULT_HEIGHT_MM = 120.0
DEFAULT_DPI = 203.2
DEFAULT_PANEL = "on_product"


@dataclass(frozen=True)
class CropBox:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True)
class CropResult:
    input_pdf: str
    output_pdf: str
    page_pixels: tuple[int, int]
    crop_box: CropBox
    output_pixels: tuple[int, int]
    label_mm: tuple[float, float]
    dpi: float
    panel: str

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["crop_box"] = asdict(self.crop_box)
        return data


def mm_to_pixels(mm: float, dpi: float) -> int:
    return max(1, round(mm * dpi / 25.4))


def product_panel_crop_box(page_width_px: int, page_height_px: int) -> CropBox:
    """Return the Kaspi platform "НА ТОВАР" panel crop box.

    The observed Kaspi Express waybill template is a 2x2 platform sheet where
    the product label is the lower-right panel. Ratios are used instead of
    absolute coordinates so this remains stable across render DPI changes.
    """

    return CropBox(
        left=round(page_width_px * 0.5045),
        top=round(page_height_px * 0.5028),
        right=round(page_width_px * 0.9958),
        bottom=page_height_px,
    )


def aspect_fit_size(src_width: int, src_height: int, max_width: int, max_height: int) -> tuple[int, int]:
    if src_width <= 0 or src_height <= 0:
        raise ValueError("source dimensions must be positive")
    scale = min(max_width / src_width, max_height / src_height)
    return max(1, round(src_width * scale)), max(1, round(src_height * scale))


def resolve_output_path(input_pdf: Path, output: Path | None) -> Path:
    if output is not None:
        return output
    repo_root = Path(__file__).resolve().parents[1]
    output_dir = repo_root / "output" / "pdf"
    return output_dir / f"{input_pdf.stem}_on_product_75x120.pdf"


def render_first_page(input_pdf: Path, temp_dir: Path, dpi: float) -> Path:
    pdftoppm = shutil.which("pdftoppm")
    if pdftoppm is None:
        raise RuntimeError("pdftoppm not found. Install Poppler first, then rerun this script.")

    output_prefix = temp_dir / "kaspi_waybill_page"
    command = [
        pdftoppm,
        "-png",
        "-r",
        f"{dpi:g}",
        "-f",
        "1",
        "-singlefile",
        str(input_pdf),
        str(output_prefix),
    ]
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"pdftoppm failed with exit {completed.returncode}: {detail}")

    rendered = output_prefix.with_suffix(".png")
    if not rendered.exists():
        raise RuntimeError(f"pdftoppm did not create expected render: {rendered}")
    return rendered


def crop_to_product_label(
    *,
    input_pdf: Path,
    output_pdf: Path,
    width_mm: float = DEFAULT_WIDTH_MM,
    height_mm: float = DEFAULT_HEIGHT_MM,
    dpi: float = DEFAULT_DPI,
    panel: str = DEFAULT_PANEL,
    margin_mm: float = 0.0,
    mask_guides: bool = True,
) -> CropResult:
    if panel != DEFAULT_PANEL:
        raise ValueError(f"Unsupported panel: {panel!r}. Only {DEFAULT_PANEL!r} is supported.")
    if not input_pdf.exists():
        raise FileNotFoundError(input_pdf)
    if width_mm <= 0 or height_mm <= 0:
        raise ValueError("label dimensions must be positive")
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    if margin_mm < 0:
        raise ValueError("margin must be non-negative")

    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:  # pragma: no cover - depends on local runtime
        raise RuntimeError("Pillow is required. Install it with `python3 -m pip install Pillow`.") from exc

    with tempfile.TemporaryDirectory(prefix="kaspi_waybill_crop_") as raw_temp:
        temp_dir = Path(raw_temp)
        rendered = render_first_page(input_pdf, temp_dir, dpi)
        source = Image.open(rendered).convert("RGB")
        page_width, page_height = source.size
        crop_box = product_panel_crop_box(page_width, page_height)
        panel_image = source.crop((crop_box.left, crop_box.top, crop_box.right, crop_box.bottom))

    output_width = mm_to_pixels(width_mm, dpi)
    output_height = mm_to_pixels(height_mm, dpi)
    margin_px = mm_to_pixels(margin_mm, dpi) if margin_mm else 0
    max_width = max(1, output_width - 2 * margin_px)
    max_height = max(1, output_height - 2 * margin_px)
    fitted_width, fitted_height = aspect_fit_size(panel_image.width, panel_image.height, max_width, max_height)

    resized = panel_image.resize((fitted_width, fitted_height))
    canvas = Image.new("RGB", (output_width, output_height), "white")
    left = (output_width - fitted_width) // 2
    top = (output_height - fitted_height) // 2
    canvas.paste(resized, (left, top))

    if mask_guides:
        draw = ImageDraw.Draw(canvas)
        # The crop ratios exclude the horizontal A4 separator. Mask only the
        # thin left edge where a separator dash can bleed into some renders.
        draw.rectangle([0, 0, round(output_width * 0.02), output_height], fill="white")

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_pdf, "PDF", resolution=dpi)

    return CropResult(
        input_pdf=str(input_pdf),
        output_pdf=str(output_pdf),
        page_pixels=(page_width, page_height),
        crop_box=crop_box,
        output_pixels=(output_width, output_height),
        label_mm=(width_mm, height_mm),
        dpi=dpi,
        panel=panel,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Kaspi platform waybill PDF.")
    parser.add_argument("--output", type=Path, help="Output thermal-label PDF path.")
    parser.add_argument("--width-mm", type=float, default=DEFAULT_WIDTH_MM, help="Output label width in mm.")
    parser.add_argument("--height-mm", type=float, default=DEFAULT_HEIGHT_MM, help="Output label height in mm.")
    parser.add_argument("--dpi", type=float, default=DEFAULT_DPI, help="Rasterization/output DPI.")
    parser.add_argument("--margin-mm", type=float, default=0.0, help="Optional white page margin in mm.")
    parser.add_argument(
        "--panel",
        choices=[DEFAULT_PANEL],
        default=DEFAULT_PANEL,
        help="Waybill panel to extract. Only the product label is supported.",
    )
    parser.add_argument(
        "--keep-guides",
        action="store_true",
        help="Keep the source A4 separator/cut-guide marks instead of masking them.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable result metadata.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_pdf = args.input.expanduser().resolve()
    output_pdf = resolve_output_path(input_pdf, args.output.expanduser().resolve() if args.output else None)
    result = crop_to_product_label(
        input_pdf=input_pdf,
        output_pdf=output_pdf,
        width_mm=args.width_mm,
        height_mm=args.height_mm,
        dpi=args.dpi,
        panel=args.panel,
        margin_mm=args.margin_mm,
        mask_guides=not args.keep_guides,
    )
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(f"output_pdf={result.output_pdf}")
        print(f"label_mm={result.label_mm[0]:g}x{result.label_mm[1]:g}")
        print(f"output_pixels={result.output_pixels[0]}x{result.output_pixels[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
