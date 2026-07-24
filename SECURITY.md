# Security Policy

## Supported version

VNMaster is pre-1.0 software. Security fixes are made on the current `main`
branch; older commits and local installations are not maintained separately.

## Reporting a vulnerability

Please do not disclose an exploitable vulnerability or credential in a public
issue. Use GitHub's private vulnerability-reporting flow under the repository's
**Security** tab. If private reporting is unavailable, open an issue containing
only a request for private maintainer contact and no sensitive details.

Include the affected command or module, impact, reproduction conditions using
fake credentials or local mocks, and any suggested mitigation. Do not test
against accounts, services, or content you do not own or have permission to
access.

## Security model

- Anthropic credentials, Discord credentials, and F95Zone cookies belong only
  in `~/.config/vnmaster/secrets.toml`, which VNMaster writes with mode `0600`.
- F95Zone cookies are domain-scoped and must never be sent to metadata APIs,
  redirect targets on other origins, or download hosts.
- Download URLs and archives are attacker-controlled input. Direct HTTPS,
  DataNodes, PixelDrain, and VikingFile downloads resolve each redirect once,
  reject non-public destinations, and connect to the vetted numeric address
  while retaining the original hostname for HTTP and TLS. Dedicated adapters
  also restrict their initial input to expected hosts.
- Archive handling blocks unsafe paths and links, excessive member counts, and
  listed extraction sizes that threaten available disk. ZIP extraction
  additionally enforces the size limit and free-space reserve against bytes
  actually written.
- Downloads are staged and are not executed automatically. A user may later
  execute a downloaded game, so archive validation is not malware detection or
  publisher authentication.
- Optional mods can overwrite game files after a preview. Only install content
  from sources you trust.

Rotate or invalidate a credential immediately if it appears in Git history,
logs, diagnostics, an issue, or any request to an unintended host.
