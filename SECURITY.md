# Security Policy

Tricloud is an early public beta. It has not completed an independent security audit and must not be used as the only storage location for important or irreplaceable files.

## Supported versions

Security fixes are provided only for the latest published beta release.

| Version | Supported |
| --- | --- |
| Latest published beta | Yes |
| Older beta releases | No |
| Unreleased or modified builds | No |

## Report a vulnerability privately

**Do not disclose a suspected vulnerability, exploit details, credentials, private file contents, or other sensitive information in a public Issue.**

Use [GitHub private vulnerability reporting](https://github.com/Takumi-Morimitsu/Tricloud-beta/security/advisories/new) to send a confidential report to the repository maintainer.

Include, when possible:

- A clear description of the vulnerability and its potential impact
- The affected Tricloud version or commit
- Reproduction steps or a minimal proof of concept
- Relevant environment details
- Any suggested mitigation
- Whether you believe the issue is already public or actively exploited

Use synthetic test data. Remove passwords, authentication tokens, API keys, node API keys, personal information, private file contents, and unrelated logs.

This is a solo, early-beta project. Reports are handled on a best-effort basis. The maintainer will try to acknowledge a report within seven days, validate the issue, and coordinate a disclosure timeline before details are made public.

Tricloud does not currently operate a bug-bounty program and cannot promise payment or another reward for a report.

## Safe testing

Security research must stay within the following boundaries:

- Test only accounts, files, computers, and storage-provider nodes that you own or are explicitly authorized to use.
- Do not access, alter, download, or delete another user's data.
- Do not perform denial-of-service testing, destructive testing, social engineering, or high-volume automated scanning against the public beta infrastructure.
- Stop testing and report privately if you encounter another user's data, credentials, or confidential information.

## Test-data warning

Do not upload important, confidential, personal, business-critical, or irreplaceable files to this beta.

## Repository secrets

Never commit `.env`, `.env.local`, `.env.production`, database credentials, JWT secrets, Stripe secrets, node API keys, certificates, or private keys.
