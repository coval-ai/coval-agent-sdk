# Security Policy

## Report privately

Do not open a public issue with credentials, exploitable details, customer data, audio, or transcripts. If the repository's Security tab offers **Report a vulnerability**, use that private reporting channel.

If private GitHub reporting is unavailable, contact [Coval support](mailto:support@coval.dev) to request a private security reporting channel. Start with a request to coordinate privately, and share sensitive reproduction details only after a maintainer confirms the channel.

Include the SDK version or commit, Python version, affected entry point, impact, and a minimal synthetic reproduction. Explain whether the issue affects credentials, connection tokens, workspace isolation, unbounded resources, or conversation data. Do not include live API keys or other people's recordings.

## Supported versions

Reports against the latest published release and the current development version are welcome. The SDK is classified as alpha; security fixes and backports are assessed by maintainers. No response-time or bounty commitment is made by this document.

## Handling data

Keep credentials in the environment or an approved secret source. One-use relay offers are credentials. OpenTelemetry spans can contain prompts, transcripts, and model output, so trace export is an explicit application choice. Use synthetic fixtures for tests and sanitize diagnostics before sharing them.
