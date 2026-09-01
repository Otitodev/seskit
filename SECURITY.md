# Security Policy

## Reporting a vulnerability

**Please do not open a public issue.**

Report privately through GitHub's
[security advisory form](https://github.com/Otitodev/seskit/security/advisories/new),
which opens a channel visible only to the maintainers.

Please include what you can of:

- the type of issue and where it is in the code,
- the steps or configuration needed to reproduce it,
- what an attacker gains.

You will get an acknowledgement, and a fix or an explanation of why it is not
one. SESKit is a small project maintained in spare time, so please allow a
reasonable window before disclosing publicly.

## Supported versions

SESKit is pre-1.0 and under active development. Fixes land on `main`; there are
no maintained release branches yet.

## Scope

SESKit is **self-hosted software**, so the security boundary is whatever you
deploy it into. Reports about the code, its defaults, or its documented
deployment guidance are in scope. The AWS account it connects to is yours.

Areas worth particular attention, because they are where an untrusted party
meets the system:

- **`POST /v1/events/ses`** — unauthenticated by necessity, since Amazon SNS has
  no credential to present. It is protected by RSA signature verification over
  SNS's canonical string, with the signing certificate fetched only from a
  validated `sns.<region>.amazonaws.com` host. Both that URL and `SubscribeURL`
  are attacker-supplied and are validated *before* any request is made.
- **API key authentication** — keys are SHA-256 hashed at rest, shown once, and
  revocation invalidates the cache immediately rather than waiting for a TTL.
- **Project tenancy** — every query is scoped by project. An id belonging to
  another project must resolve to nothing, not to someone else's data.
- **Stored HTML** — message bodies are attacker-controlled (whoever holds an API
  key chose them) and are rendered as escaped source in the dashboard, never as
  live markup.
- **AWS credentials** — never stored, never accepted through a form, and never
  named as a setting. They are resolved by boto3 from the environment.

## Out of scope

- Vulnerabilities in Amazon SES, SNS, or SQS themselves — report those to AWS.
- Issues that require an attacker to already hold a valid API key or a
  dashboard session, unless the issue is privilege escalation across projects.
- Findings from automated scanners with no demonstrated impact.
- Misconfiguration of a deployment (for example, exposing the dashboard to the
  internet with `ALLOW_SIGNUP=true`), unless SESKit's defaults or documentation
  led you there — in which case that is a real report and worth sending.
