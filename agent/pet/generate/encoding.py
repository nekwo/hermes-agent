"""Lossless encoding shared by pet and character-sheet consumers."""
import io

def atlas_to_webp_bytes(atlas) -> bytes:
    """Encode an atlas image to lossless WebP bytes (the on-disk pet format)."""
    buf = io.BytesIO()
    atlas.save(buf, format="WEBP", lossless=True, quality=100, method=6, exact=True)
    return buf.getvalue()
