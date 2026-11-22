# Sign-in

Lumio accepts four ways in. Every one ends in the same session cookie, on the web and in the desktop app.

| Way in | For | Set up with |
| --- | --- | --- |
| Google | Gmail and Google Workspace users; also connects their mail and calendar | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` |
| Microsoft | Outlook and Microsoft 365 users; also connects their mail and calendar | `MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET` |
| Email and password | Anyone with an email address | a mail provider (below) |
| SSO | Companies with Okta, Entra ID, Google Workspace, Auth0, Keycloak, OneLogin, JumpCloud, Ping or any other OpenID Connect provider | `SSO_CONNECTIONS` |

A person who signs in with email or SSO has no mailbox connected yet. They can connect Gmail or Outlook later under
Connectors.

## Email and password

Pages: `/signup`, `/login`, `/verify-email`, `/forgot-password`, `/reset-password`, and a change-password card under
Your data (`/account`). API: `apps/api/app/auth/email.py` (`/api/auth/email/*`).

- **Sign up:** name, email, password. Lumio emails a confirmation link, valid for 24 hours. The account is created and
  signed in when the link is opened. The page confirms with a POST, so a mail scanner that opens the link signs no
  one in.
- **No account enumeration:** sign-up and forgot-password always answer "check your email". If the address already
  has an account, the email tells the owner to sign in or reset. If it signs in with Google, Microsoft or SSO, the
  forgot-password email says so.
- **Pre-registration is harmless:** an unconfirmed address can't sign in. Signing up again replaces its password and
  makes older confirmation links invalid. Only the person who can read the inbox finishes sign-up.
- **Reset:** the link works for one hour, and only once, because it dies when the password changes. Resetting signs
  out every other session and emails a "your password was changed" notice. Changing the password while signed in
  signs out the other devices.
- **Passwords:** at least 10 characters, not a common password, not containing the email name. Stored as salted
  scrypt hashes (Python's standard library) and never logged.
- **Brute force:** sign-in is limited per address (20 per 15 minutes) and per IP (60 per 15 minutes). Eight wrong
  passwords lock the address for 15 minutes. Sign-up, resend and forgot are limited too.
- **SSO-only companies:** a domain whose SSO connection has `"enforce": true` can't sign up or sign in with a
  password.
- **Desktop app:** the email form works inside the app window itself. Google, Microsoft and SSO use the system
  browser and hand the session back with the one-time desktop code (docs/LLD.md "Desktop sign-in").

### Mail

Lumio sends its own mail for confirmation, password reset and password changed. It uses the first provider that is
set:

1. **Resend:** `RESEND_API_KEY`. Create the key at resend.com → API Keys, and verify your sending domain under
   Domains.
2. **SendGrid:** `SENDGRID_API_KEY`. Create the key with the "Mail Send" permission, and authenticate your domain
   under Settings → Sender Authentication. Click tracking is turned off for these messages, so links arrive exactly
   as written.
3. **SMTP:** `SMTP_HOST`, `SMTP_PORT` (587 uses STARTTLS, 465 uses TLS), `SMTP_USER`, `SMTP_PASSWORD`.

`MAIL_FROM` is the sender, for example `Lumio <no-reply@yourdomain.com>`. Its domain must be verified with the
provider.

With no provider set, email sign-up is hidden in production (`/api/auth/status` reports `"email": false`). On a local
server the links are written to the API log instead. Set `EMAIL_LOGIN=false` to turn email sign-in off altogether.

## SSO (OpenID Connect)

`apps/api/app/auth/sso.py` (`/api/auth/sso/*`). On the sign-in page, **Continue with SSO** asks for a work email. Lumio
finds the company by the email's domain and opens its sign-in page.

Add one connection per company to `SSO_CONNECTIONS`, a JSON list:

```json
[{"id": "acme", "name": "Acme", "issuer": "https://acme.okta.com", "client_id": "…", "client_secret": "…",
  "domains": ["acme.com", "acme.co.uk"], "enforce": false}]
```

- `id`: lowercase letters, digits and dashes, up to 16 characters. It is part of the redirect URI.
- `issuer`: the provider's issuer URL. Lumio reads `{issuer}/.well-known/openid-configuration`; set `discovery_url`
  to use another address. Examples:
  - Okta: `https://{org}.okta.com`
  - Entra ID: `https://login.microsoftonline.com/{tenant-id}/v2.0`
  - Google: `https://accounts.google.com`
  - Auth0: `https://{tenant}.auth0.com`
  - Keycloak: `https://{host}/realms/{realm}`
- `domains`: the email domains this connection signs in. A person whose email isn't on one of them is refused.
- `enforce`: true makes SSO the only way in for these domains.
- `scopes`: optional. The default is `openid email profile`.

In the identity provider, register a web application with:

- the redirect URI `{PUBLIC_WEB_URL}/api/auth/sso/{id}/callback`
- the authorization code grant

Then copy its client ID and secret into the connection.

What the callback checks:

- the state cookie, and PKCE (S256)
- the ID token's signature, against the provider's published keys
- the token's issuer, audience, expiry (60 seconds of leeway) and nonce
- that the email isn't marked unverified, and is on one of the connection's domains

A new person is created on their first sign-in. After that they are found by the provider's stable subject id, so a
changed email doesn't create a second account.

Not covered yet: SAML-only providers (most also offer OIDC), and SCIM provisioning.

## Tests

- `apps/api/tests/test_auth_email_sso_db.py`: sign-up, verify, sign-in, enumeration, pre-registration, reset, lockout,
  change password, and SSO with a fake provider (forged signature, wrong audience, wrong domain, unverified email,
  forged state)
- `apps/api/tests/test_auth_mailer.py`: Resend and SendGrid requests, and password hashing

Who may sign up, and the daily cap on each person's model use, are in docs/ROLLOUT.md.
