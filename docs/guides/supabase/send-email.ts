// Supabase Auth "Send Email" hook -> SESKit.
//
// Supabase calls this with a signed request each time Auth needs to send an
// email. It checks the signature, writes the message, and hands it to SESKit.
//
// Secrets (supabase secrets set ...):
//   SEND_EMAIL_HOOK_SECRET  the hook secret from Auth > Hooks, "v1,whsec_..."
//   SESKIT_URL              where SESKit runs, e.g. https://mail.example.com
//   SESKIT_API_KEY          a SESKit API key made for this function
//   FROM_ADDRESS            a sender on a domain verified in SESKit
//   APP_NAME                optional, used in subjects and bodies
// SUPABASE_URL is provided by Supabase.

const encoder = new TextEncoder();

// How long we wait for SESKit. Supabase gives the whole hook 5 seconds.
const SESKIT_TIMEOUT_MS = 3000;

// A signature older or newer than this is refused, so a captured request
// cannot be replayed later.
const TOLERANCE_SECONDS = 5 * 60;

export type Config = {
  hookSecret: string;
  seskitUrl: string;
  seskitApiKey: string;
  fromAddress: string;
  authUrl: string;
  appName: string;
};

export type HookPayload = {
  user: { id: string; email: string; new_email?: string };
  email_data: {
    token?: string;
    token_hash?: string;
    token_new?: string;
    token_hash_new?: string;
    redirect_to?: string;
    email_action_type: string;
  };
};

export type Mail = { to: string; token: string; tokenHash: string };

export type Message = { subject: string; text: string; html: string };

function base64ToBytes(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function toHex(bytes: ArrayBuffer): string {
  return [...new Uint8Array(bytes)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Standard Webhooks: the signature is HMAC-SHA256 over "id.timestamp.body",
// sent as "v1,<base64>" (several may be listed, separated by spaces).
export async function verifySignature(
  body: string,
  headers: Headers,
  secret: string,
  nowSeconds: number = Math.floor(Date.now() / 1000),
): Promise<boolean> {
  const id = headers.get("webhook-id");
  const timestamp = headers.get("webhook-timestamp");
  const signatures = headers.get("webhook-signature");
  if (!id || !timestamp || !signatures) return false;

  const sent = Number(timestamp);
  if (!Number.isInteger(sent) || Math.abs(nowSeconds - sent) > TOLERANCE_SECONDS) return false;

  let key: CryptoKey;
  try {
    const raw = base64ToBytes(secret.replace(/^v1,whsec_/, ""));
    key = await crypto.subtle.importKey("raw", raw, { name: "HMAC", hash: "SHA-256" }, false, [
      "verify",
    ]);
  } catch {
    return false;
  }

  const signed = encoder.encode(`${id}.${timestamp}.${body}`);
  for (const candidate of signatures.split(" ")) {
    const [version, value] = candidate.split(",");
    if (version !== "v1" || !value) continue;
    let given: Uint8Array;
    try {
      given = base64ToBytes(value);
    } catch {
      continue;
    }
    // verify() compares in constant time.
    if (await crypto.subtle.verify("HMAC", key, given, signed)) return true;
  }
  return false;
}

// The link Supabase's own templates build: the user's browser goes to Auth,
// which checks the token and sends them on to redirect_to.
export function confirmationLink(
  authUrl: string,
  tokenHash: string,
  type: string,
  redirectTo: string | undefined,
): string {
  const url = new URL("/auth/v1/verify", authUrl);
  url.searchParams.set("token", tokenHash);
  url.searchParams.set("type", type);
  if (redirectTo) url.searchParams.set("redirect_to", redirectTo);
  return url.toString();
}

// One email per call, except a secure email change, which needs two. Supabase
// names the pairs backwards for compatibility: the "_new" suffix does NOT mean
// the new address. The current address gets token + token_hash_new and the new
// address gets token_new + token_hash.
export function mailsFor(payload: HookPayload): Mail[] {
  const { user, email_data: data } = payload;
  if (data.email_action_type !== "email_change") {
    return [{ to: user.email, token: data.token ?? "", tokenHash: data.token_hash ?? "" }];
  }
  if (data.token_hash && data.token_hash_new) {
    const mails: Mail[] = [
      { to: user.email, token: data.token ?? "", tokenHash: data.token_hash_new },
    ];
    if (user.new_email) {
      mails.push({ to: user.new_email, token: data.token_new ?? "", tokenHash: data.token_hash });
    }
    return mails;
  }
  const to = user.new_email ?? user.email;
  return [
    {
      to,
      token: data.token || data.token_new || "",
      tokenHash: data.token_hash || data.token_hash_new || "",
    },
  ];
}

// Stable for one email and different for the next one, so Supabase retrying
// the same hook cannot send twice but a second request for a reset still
// sends. Only a hash of the token goes in, and a hash of the whole thing.
export async function idempotencyKey(
  action: string,
  userId: string,
  mail: Mail,
): Promise<string> {
  const input = `${action}\n${userId}\n${mail.to}\n${mail.tokenHash}\n${mail.token}`;
  return `supabase-${toHex(await crypto.subtle.digest("SHA-256", encoder.encode(input)))}`;
}

function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

type Copy = { subject: string; intro: string; button?: string; link: boolean; code: boolean };

const COPY: Record<string, (app: string) => Copy> = {
  signup: (app) => ({
    subject: `Confirm your email for ${app}`,
    intro: `Confirm your email address to finish creating your ${app} account.`,
    button: "Confirm email",
    link: true,
    code: false,
  }),
  invite: (app) => ({
    subject: `You have been invited to ${app}`,
    intro: `You have been invited to ${app}. Accept the invitation to set up your account.`,
    button: "Accept invitation",
    link: true,
    code: false,
  }),
  magiclink: (app) => ({
    subject: `Your sign-in link for ${app}`,
    intro: `Use this to sign in to ${app}.`,
    button: "Sign in",
    link: true,
    code: true,
  }),
  recovery: (app) => ({
    subject: `Reset your ${app} password`,
    intro: `Someone asked to reset the password for your ${app} account.`,
    button: "Reset password",
    link: true,
    code: false,
  }),
  email_change: (app) => ({
    subject: `Confirm your new email for ${app}`,
    intro: `Confirm this change to the email address on your ${app} account.`,
    button: "Confirm change",
    link: true,
    code: false,
  }),
  email: (app) => ({
    subject: `Your ${app} sign-in code`,
    intro: `Enter this code to sign in to ${app}.`,
    link: false,
    code: true,
  }),
  reauthentication: (app) => ({
    subject: `Confirm it is you on ${app}`,
    intro: `Enter this code to confirm a change to your ${app} account.`,
    link: false,
    code: true,
  }),
};

export const SUPPORTED_ACTIONS = Object.keys(COPY);

const IGNORE_NOTE = "If you did not ask for this, you can ignore this email.";

// Everything interpolated into the HTML is escaped. The link is built by
// confirmationLink() from a URL we construct, never taken from the payload.
export function render(
  action: string,
  appName: string,
  mail: Mail,
  link: string,
): Message | null {
  const make = COPY[action];
  if (!make) return null;
  const copy = make(appName);

  const textParts = [copy.intro, ""];
  const htmlParts = [`<p>${escapeHtml(copy.intro)}</p>`];
  if (copy.link && mail.tokenHash) {
    textParts.push(`${copy.button}: ${link}`, "");
    htmlParts.push(
      `<p><a href="${escapeHtml(link)}">${escapeHtml(copy.button ?? "Continue")}</a></p>`,
    );
  }
  if (copy.code && mail.token) {
    textParts.push(`Your code: ${mail.token}`, "");
    htmlParts.push(`<p>Your code: <strong>${escapeHtml(mail.token)}</strong></p>`);
  }
  textParts.push(IGNORE_NOTE);
  htmlParts.push(`<p>${escapeHtml(IGNORE_NOTE)}</p>`);

  return { subject: copy.subject, text: textParts.join("\n"), html: htmlParts.join("\n") };
}

function reply(status: number, message?: string): Response {
  const body = message ? { error: { http_code: status, message } } : {};
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// Logs carry a category and a status, never an address, a token or a link.
function log(event: string, detail: Record<string, string | number> = {}): void {
  console.error(JSON.stringify({ event, ...detail }));
}

export async function handle(
  req: Request,
  config: Config,
  send: typeof fetch = fetch,
): Promise<Response> {
  if (req.method !== "POST") return reply(405, "Method not allowed");

  const body = await req.text();
  if (!(await verifySignature(body, req.headers, config.hookSecret))) {
    log("signature_rejected");
    return reply(401, "Invalid signature");
  }

  let payload: HookPayload;
  try {
    payload = JSON.parse(body);
    if (typeof payload.user?.email !== "string" || typeof payload.user?.id !== "string") {
      throw new Error("no user");
    }
    if (typeof payload.email_data?.email_action_type !== "string") throw new Error("no action");
  } catch {
    log("payload_rejected");
    return reply(400, "Unreadable payload");
  }

  const action = payload.email_data.email_action_type;
  if (!SUPPORTED_ACTIONS.includes(action)) {
    // A signed request for something we have no template for. Failing it would
    // block the user's sign-in over an email; skipping it is visible in logs.
    log("unsupported_action", { action: /^[a-z_]{1,40}$/.test(action) ? action : "invalid" });
    return reply(200);
  }

  for (const mail of mailsFor(payload)) {
    const link = confirmationLink(
      config.authUrl,
      mail.tokenHash,
      action,
      payload.email_data.redirect_to,
    );
    const message = render(action, config.appName, mail, link);
    if (!message) return reply(200);

    let response: Response;
    try {
      response = await send(`${config.seskitUrl}/v1/emails`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${config.seskitApiKey}`,
          "Content-Type": "application/json",
          "Idempotency-Key": await idempotencyKey(action, payload.user.id, mail),
        },
        body: JSON.stringify({
          from: config.fromAddress,
          to: mail.to,
          subject: message.subject,
          text: message.text,
          html: message.html,
        }),
        signal: AbortSignal.timeout(SESKIT_TIMEOUT_MS),
      });
    } catch {
      // Timed out or could not connect. Supabase retries a 503, and the
      // idempotency key makes the retry safe.
      log("seskit_unreachable");
      return reply(503, "Could not reach SESKit");
    }

    if (response.status === 429 || response.status >= 500) {
      log("seskit_unavailable", { status: response.status });
      return reply(503, "SESKit could not take the message");
    }
    if (!response.ok) {
      // A key or a sender that SESKit refuses will be refused again. Retrying
      // would not help, so say so once.
      log("seskit_refused", { status: response.status });
      return reply(400, "SESKit refused the message");
    }
  }
  return reply(200);
}

type DenoRuntime = {
  env: { get(name: string): string | undefined };
  serve(handler: (req: Request) => Promise<Response> | Response): void;
};

function configFromEnv(env: DenoRuntime["env"]): Config | null {
  const get = (name: string) => env.get(name)?.trim() ?? "";
  const config = {
    hookSecret: get("SEND_EMAIL_HOOK_SECRET"),
    seskitUrl: get("SESKIT_URL").replace(/\/+$/, ""),
    seskitApiKey: get("SESKIT_API_KEY"),
    fromAddress: get("FROM_ADDRESS"),
    authUrl: get("SUPABASE_URL"),
    appName: get("APP_NAME") || "your account",
  };
  const missing = Object.entries({
    SEND_EMAIL_HOOK_SECRET: config.hookSecret,
    SESKIT_URL: config.seskitUrl,
    SESKIT_API_KEY: config.seskitApiKey,
    FROM_ADDRESS: config.fromAddress,
    SUPABASE_URL: config.authUrl,
  })
    .filter(([, value]) => !value)
    .map(([name]) => name);
  if (missing.length) {
    log("config_missing", { missing: missing.join(",") });
    return null;
  }
  return config;
}

const deno = (globalThis as { Deno?: DenoRuntime }).Deno;
if (deno) {
  deno.serve((req) => {
    const config = configFromEnv(deno.env);
    return config ? handle(req, config) : reply(500, "Not configured");
  });
}
