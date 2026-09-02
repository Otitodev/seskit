# Vendored JavaScript

§5: **there is no Node.js in this project.** No npm, no `node_modules`, no build
step. Libraries that the dashboard genuinely needs are committed here as the
files they are published as, and upgrading one is a deliberate act with a diff
somebody can read.

Not a CDN, either. A self-hosted dashboard that breaks when `cdn.example` is
unreachable — or that reports every page view to a third party — contradicts the
product's own pitch (§1).

## Chart.js

| | |
|---|---|
| File | `chart.umd.js` |
| Version | **4.4.7** |
| Size | 205,615 bytes |
| SHA-256 | `2812cb8825fdc57469eb2f7bb055e9429244e599920511ee477e828499b632cb` |
| Licence | MIT |
| Upstream | https://github.com/chartjs/Chart.js |

### Where it came from, and how that was checked

Taken from the npm-published tarball rather than from a CDN, and verified before
being committed:

```bash
curl -sLO https://registry.npmjs.org/chart.js/-/chart.js-4.4.7.tgz
sha1sum chart.js-4.4.7.tgz
# 7a01ee0b4dac3c03f2ab0589af888db296d896fa
# matches registry.npmjs.org/chart.js/4.4.7 -> dist.shasum

tar xzf chart.js-4.4.7.tgz package/dist/chart.umd.js
sha256sum package/dist/chart.umd.js
# 2812cb8825fdc57469eb2f7bb055e9429244e599920511ee477e828499b632cb
```

**This is why the check was worth doing.** The same file fetched from a CDN was
*not* byte-identical to the published artifact — the CDN prepends its own banner,
including a note that its dynamically generated files should not be used with
subresource integrity. Nothing sinister, but "I downloaded it from a CDN" and "I
verified it is what the maintainers published" are different claims, and only
the second one is worth writing down.

### Verifying this copy

```bash
sha256sum apps/api/src/seskit_api/static/js/vendor/chart.umd.js
```

### Upgrading

Repeat the steps above with the new version, update the table, and say in the
commit message what changed and why. Do not fetch it from a CDN.

## What is deliberately *not* here

**Alpine.js.** §31 lists it alongside Chart.js to be vendored "when there is
finally something that needs them". Chart.js is needed; Alpine is not, yet.
Nothing on the dashboard holds client state that HTMX does not already handle —
the range control swaps a fragment, and forms post. Vendoring a library nothing
imports is weight on every page load and supply-chain surface for no behaviour.
It goes in when a component genuinely needs it.
