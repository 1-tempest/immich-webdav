"""Image format classification.

This mirrors Immich's own `webSupportedImage` set from
`server/src/utils/mime-types.ts`, which is the *exact* predicate Immich
itself uses (`mimeTypes.isWebSupportedImage`, see `media.service.ts`) to
decide whether a `fullsize` derivative gets generated for an asset at all.

This is not a matter of taste or of what Windows happens to render nicely --
if a mimetype is in Immich's web-supported set, no `fullsize` file will ever
exist for it, and requesting one will fail. So this list must track Immich's
own list exactly, not our own judgment about codec support.

Source, pinned to the Immich server version this was last checked against
(v3.1.0):
https://github.com/immich-app/immich/blob/v3.1.0/server/src/utils/mime-types.ts

If you upgrade your Immich server across a major version, re-check that file
for changes before assuming this list is still accurate.
"""

from __future__ import annotations

SUPPORTED_IMAGE_MIMETYPES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/apng",
        "image/gif",
        "image/bmp",
        "image/webp",
        "image/avif",
    }
)

# Extension to present for a given mimetype, so the virtual filename always
# matches the bytes actually being served rather than trusting
# `originalFileName`'s extension (which can be wrong or obscure -- .jpe,
# .mpo, and .insp are all real JPEG bytes under unusual extensions).
_EXTENSION_FOR_MIMETYPE: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/apng": ".png",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
    "image/avif": ".avif",
}

# Extension used for anything routed through `?size=fullsize`. Must match
# your Immich instance's admin System Settings -> Preview format (jpeg or
# webp) -- fullsize derivatives are generated in that same configured format.
FULLSIZE_EXTENSION = ".jpg"
FULLSIZE_CONTENT_TYPE = "image/jpeg"


def is_web_supported(mimetype: str | None) -> bool:
    """True if Immich would never generate a `fullsize` derivative for this."""
    return bool(mimetype) and mimetype.lower() in SUPPORTED_IMAGE_MIMETYPES


def output_extension(mimetype: str | None) -> str:
    """Extension to present in the virtual filename for this mimetype."""
    if mimetype and mimetype.lower() in _EXTENSION_FOR_MIMETYPE:
        return _EXTENSION_FOR_MIMETYPE[mimetype.lower()]
    return FULLSIZE_EXTENSION
