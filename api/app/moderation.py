# HeArt v1 API — invented by John Pierce Davis Jr., Inventor and CEO.
"""Moderation pipeline.

=====================================================================
SAFE DEFAULT (v1, Nov 8 2026 — T2 red-pen item 3): uploads that pass
the hard structural rules are queued for HUMAN REVIEW
(`status="review"`), NOT auto-approved. The v1 content classifier is
still a stub — no content judgment happens at all — so auto-approving
would put unmoderated content straight on the public feed. A work in
"review" is invisible on the feed and visible only to its maker until
an admin approves it via POST /admin/works/{id}/approve (or rejects
it via .../reject). A future real classifier should return "approved"
only for content it is confident about, "review" for uncertain
content, and "rejected" for disallowed content, keeping the same
ScanResult shape. The interface below is the seam it will plug into:
keep it stable.
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
    """Content gate for uploads.

    Hard structural rules reject executables/scriptable files outright.
    Everything else is queued for human review (status="review") — the
    v1 classifier is a stub, so nothing is auto-approved. An admin
    approves or rejects the work via the /admin/works review endpoints;
    only "approved" works appear on the public feed. A future classifier
    should return "approved" only for content it is confident about,
    keeping the same ScanResult shape.
    """
    violations = hard_check(filename, mime, size_bytes)
    if violations:
        return ScanResult(status="rejected", reasons=violations, stub=True)
    return ScanResult(
        status="review",
        reasons=["queued for human review; no content classifier in v1"],
        stub=True,
    )
