"""Normalize an already bounded, decoded image consistently for preview and OCR."""
from PIL import Image, ImageOps


def normalize_image(image: Image.Image) -> Image.Image:
    # Caller validates format, frame count and pixel count before decoding.
    oriented = ImageOps.exif_transpose(image)
    # Copy onto a fresh canvas to flatten transparency and avoid forwarding metadata.
    canvas = Image.new("RGB", oriented.size, "white")
    if "A" in oriented.getbands() or "transparency" in oriented.info:
        rgba = oriented.convert("RGBA")
        canvas.paste(rgba, mask=rgba.getchannel("A"))
    else:
        canvas.paste(oriented.convert("RGB"))
    return canvas
