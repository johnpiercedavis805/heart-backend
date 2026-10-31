# HeArt v1 API — invented by John Davis.
"""Moderation pipeline.

=====================================================================
STUB NOTICE (v1): the content classifier is NOT implemented. `scan_upload`
enforces the hard structural rules only (no executables, ever) and approves
everything else. A real scanner — text/image/audio classifiers for
instructions-to-humans, manipulation, and non-positive content — is a later
phase. The interface below is the seam it will plug into: keep it stable.
=====================================================================

Hard rules (§2, enforced structurally, not by AI judgment):
- No executable uploads: rejected by file extension AND mime type.
- No HTML/SVG (scriptable markup).
- Empty files rejected.
"""

from dataclasses import dataclass, field

# Extensions that can execute or script. Conservative on purpose.
BLOCKED_EXTENSIONS = frozenset(
    """
    exe msi dll com scr bat cmd ps1 vbs vbe jse wsf wsh lnk
    sh bash zsh py pyc pyo js mjs ts jsx tsx
    jar apk dmg pkg deb rpm so dylib bin run out
    pl rb php phar go rs c h cpp hpp o a class war ear
    html htm xhtml svg xml xsl xslt
    docm xlsm pptm
    """.split()
)

# MIME types that indicate executable / scriptable / active content.
BLOCKED_MIMES = frozenset(
    """
    application/x-executable application/x-msdos-program application/x-msdownload
    application/x-sh application/x-bash application/x-shellscript
    application/x-python application/x-python-code text/x-python
    application/javascript text/javascript application/x-javascript
    application/x-msi application/x-ms-installer
    application/java-archive application/x-java-applet
    application/x-debian-package application/x-rpm
    application/x-mach-binary application/x-elf
    text/html application/xhtml+xml image/svg+xml
    application/x-vbscript application/x-wsh
    """.split()
)


@dataclass
class ScanResult:
    status: str  # "approved" | "rejected" | "review"
    reasons: list[str] = field(default_factory=list)
    stub: bool = True  # True until a real classifier is wired in


def hard_check(filename: str, mime: str, size_bytes: int) -> list[str]:
    """Structural hard rules. Returns a list of violations (empty = pass)."""
    violations: list[str] = []
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    if not filename or not ext:
        violations.append("missing file extension")
    if ext in BLOCKED_EXTENSIONS:
        violations.append(f"blocked file type: .{ext} (executables and scriptable files are never allowed)")
    if (mime or "").lower().split(";")[0].strip() in BLOCKED_MIMES:
        violations.append(f"blocked mime type: {mime}")
    if size_bytes <= 0:
        violations.append("empty file")
    return violations


def scan_upload(filename: str, mime: str, size_bytes: int, sha256: str) -> ScanResult:
    """STUB classifier seam.

    v1 behavior: hard structural rules only. Anything that passes them is
    approved — flagged as stub so callers (and auditors) know no real content
    judgment happened. A future classifier should return "review" for
    uncertain content and "rejected" for disallowed content, keeping the
    same ScanResult shape.
    """
    violations = hard_check(filename, mime, size_bytes)
    if violations:
        return ScanResult(status="rejected", reasons=violations, stub=True)
    return ScanResult(
        status="approved",
        reasons=["stub: passed structural hard rules; no content classifier in v1"],
        stub=True,
    )
