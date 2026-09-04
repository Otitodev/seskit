# Python SDK

!!! warning "Not written yet"
    `pip install seskit` currently installs a package that reserves the name
    and contains **no working client**. It arrives in Phase 12.

    Until then, call the [HTTP API](api.md) directly — that is the supported
    path and what every example in these docs uses. See
    [sending from your app](../getting-started/sending-from-your-app.md).

## What it will be

A thin client over the HTTP API:

```python
from seskit import SesKit

client = SesKit(api_key="sk_live_...", base_url="https://seskit.example.com")

client.emails.send(
    to=["user@example.com"],
    from_="hello@example.com",
    subject="Welcome",
    html="<h1>Welcome!</h1>",
)
```

## And why it is optional

Business logic lives in the API and is never duplicated in a client. That is a
deliberate constraint, and it has a consequence worth stating plainly: **the
SDK can never do anything a `curl` command cannot.**

So reaching for it is a convenience — typed responses, less boilerplate — and
never a requirement. If your language is not Python, or you would rather not
add a dependency, an HTTP request is a first-class way to use SESKit rather
than a fallback.

It also means a Python call and a `curl` command cannot drift apart into
disagreeing about what SESKit does, which is a failure mode that quietly ruins
client libraries.

## The two halves, again

`pip install seskit` goes in **your application**. It does not install SESKit
itself — that is the repository, running as a server. See
[the home page](../index.md) if that distinction is new.
