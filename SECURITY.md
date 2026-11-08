# Security

## Reporting a vulnerability

Do not open a public issue for a vulnerability that could expose session data,
credentials, or local paths. When the repository is public, use GitHub Security Advisories in the Security tab. Before that, contact the repository owner privately.

Include the affected version, the command or mode used, and a minimal synthetic
reproduction. Do not attach a real `opencode export`.

## Sensitive data

An OpenCode export can contain conversation text, file contents, tool output,
paths, tokens, and API keys. Treat every export as untrusted and sensitive.

- Never upload an export to an issue, gist, CI artifact, or benchmark report.
- The skill reads exports locally; it does not send them to a network service.
- `feedback.py` writes local records. Do not put transcript text, paths, names,
  or keys into feedback fields.
- Test fixtures in this repository are synthetic and must stay synthetic.

## Prompt injection through exports

An export is third-party content. It can contain text that looks like an
instruction, a URL, or a command. The skill treats it as data: it never follows
or executes export content, reads only the file the user named, quotes bounded
slices, and reports suspicious content instead of acting on it. Static scanners
flag this exposure (for example Snyk W011); it is inherent to reading session
history and is mitigated by these rules, not by a scanner toggle.

## Supported versions

Security fixes are applied to the current `0.14.x` line. Older versions are not
supported.
