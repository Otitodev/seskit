# SESKit design system

The brief every dashboard page is built against. Read this before adding to
`apps/api/src/seskit_api/static/css/app.css` or the component macros in
`apps/api/src/seskit_api/templates/components/`.

---

## The one rule

**Do not make it look like an admin template. Make it look like a real
developer infrastructure product.**

Server-rendered does not mean old-fashioned. Everything below is achievable
with Jinja, HTMX, and hand-written CSS: sidebar navigation, dark mode, polished
cards, dropdowns, modals, toasts, command palettes, loading and skeleton states,
live status updates, interactive tables, charts, copy-to-clipboard, expandable
detail rows, syntax-highlighted API examples, and genuinely good empty states.
None of that requires React.

## Reference products

Resend · Linear · Vercel · Stripe Dashboard · GitHub

## Principles

- Minimal
- Dense but readable
- Professional
- Developer-focused
- Strong typography
- Subtle borders
- Restrained use of colour
- Clear information hierarchy
- Excellent empty states
- Excellent loading states
- Responsive
- Keyboard accessible

## Avoid

- Generic Bootstrap appearance
- Excessive gradients
- Huge hero sections
- Excessively rounded cards
- Excessive shadows
- Dashboard "template" aesthetics
- Unnecessary animation

---

## Tokens

Every colour, size, and font is a CSS custom property defined in
`static/css/app.css`. Components read tokens; they never hard-code a colour.
A literal hex value inside a component rule is a bug - it will break one of the
two themes.

### Colour

Neutrals carry a slight blue bias. A pure grey reads as unconsidered.

The accent is a single ultramarine, used only for **primary actions and the
active navigation state**. If a page has more than a couple of accent-coloured
elements, something is being over-emphasised.

Semantic colours - `--success`, `--warning`, `--danger`, `--neutral` - encode
**delivery state only**. They are never decoration, and they are deliberately
distinct from the accent so "this is the primary action" and "this bounced"
never look alike.

### Theme

Three states, not two:

| State | How it is expressed |
|---|---|
| Explicit light | `data-theme="light"` on `<html>` |
| Explicit dark | `data-theme="dark"` on `<html>` |
| System (the default) | no attribute; `prefers-color-scheme` decides |

The stylesheet defines the full light palette on bare `:root`, redefines the
tokens under `@media (prefers-color-scheme: dark)` guarded by
`:root:not([data-theme="light"])`, and redefines them again under
`:root[data-theme="dark"]`. All three are required: a colour whose only
definition sits inside a media query never applies in the un-stamped state.

`base.html` applies the stored theme in an inline script **before** the
stylesheet loads, so a dark-theme user never sees a white flash.

### Type

`IBM Plex Sans` for UI, `IBM Plex Mono` for identifiers, code, and API keys -
a family designed for technical products - each with a full system fallback
stack. **No webfont is fetched.** A self-hosted dashboard may run with no
internet access, so a `<link>` to a font CDN is not an option.

Anything with digits that line up in a column gets
`font-variant-numeric: tabular-nums`.

---

## Components

Pages compose macros from `templates/components/ui.html`. They do not restyle
them. If a page needs a variant, add the variant to the component.

| Macro | Use |
|---|---|
| `button` | `default`, `primary`, `ghost`, `danger`; sizes `""` / `sm` |
| `badge` | Delivery state. `tone` is semantic, never decorative |
| `card_open` / `card_close` | Section container, optional header and actions |
| `metric` | A single number on the Overview |
| `field` | Labelled input with hint and error slots |
| `empty` | Empty state - see below |
| `code_block` | API examples, with copy-to-clipboard |

Icons are inline SVG from `templates/components/icons.html`, stroke-based on a
24px grid, inheriting `currentColor` so they theme for free.

### Empty states get real design attention

On a fresh install, the empty state is the **first thing a user sees on every
page**. It is not a placeholder. It should say what will appear here and what
to do next - never just "No data".

Never invent sample data to make a screen look populated. A new install has
genuinely sent nothing, and fake numbers misrepresent the product.

### Loading states

HTMX toggles `.htmx-indicator` automatically on any element with
`hx-indicator`. Use `.spinner` for inline waits and `.skeleton` where the shape
of the incoming content is known. All animation respects
`prefers-reduced-motion`.

---

## Writing

Name things the way a user would, not the way the system is built - a person
manages *notifications*, not *webhook config*. Active voice. A control says
exactly what will happen, and the confirmation echoes it ("Publish" →
"Published"). Errors say what went wrong and how to fix it: no apologies, no
vagueness.

This matters most where SESKit hides AWS complexity. The user should see
"Domain verified · DKIM configured · Sending enabled", not "SES Identity",
"MAIL FROM", and "Configuration Set". That translation is the product.

---

## Accessibility

Non-negotiable, and cheap if done from the start:

- One visible focus treatment, applied globally via `:focus-visible`
- State encoded in **form as well as colour** - badges carry a dot, not just a hue
- `aria-current="page"` on the active nav item
- A skip-to-content link as the first focusable element
- `prefers-reduced-motion` honoured for every animation
- Wide content scrolls inside `.table-wrap`; the page body never scrolls sideways
