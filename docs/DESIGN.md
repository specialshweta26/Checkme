# Lumio design system

How Lumio looks and behaves, on the web and in the desktop app (which loads the same web app). The tokens live at the
top of [`apps/web/app/globals.css`](../apps/web/app/globals.css), with light values on `:root` and dark values on
`body.app-dark`. Components use tokens, never raw colours or sizes, so both themes stay in step.

## Where it comes from

The principles are adapted from CRED's open-source **NeoPOP** design system
([neopop-web](https://github.com/CRED-CLUB/neopop-web), [neopop-android](https://github.com/CRED-CLUB/neopop-android)).
We take the ideas, not the brand: no CRED logo, typefaces, palette or loud colours. Lumio is an enterprise tool used
all day, so we turn NeoPOP's boldness down and keep its discipline.

| NeoPOP idea | What it means in NeoPOP | How Lumio uses it |
| --- | --- | --- |
| Depth from solid edges | Buttons are drawn with solid edge colours ("plunk"), not blurred shadows | Primary and secondary buttons carry a 2px solid bottom edge (`--accent-edge`, `--edge`). Pressing drops the face onto the edge |
| A few fixed button sizes | Big 50px, medium 40px, small 30px; primary and secondary; elevated and flat | Three control heights: 48 / 40 / 32px (`--control-lg/md/sm`), shared by buttons, fields, selects and pills |
| High contrast, few colours | Near-black and white surfaces; a small set of semantic colours (error, warning, info, success) | Neutral surfaces, one accent (blue), four semantic colours, each with a soft fill for badges |
| Type roles, not ad-hoc sizes | Named heading, body, caps and serif-heading styles | A fixed type scale (`--text-2xs` … `--text-4xl`), a serif for page titles, small caps for section labels |
| Labelled inputs with a clear caret | Inputs have a caps label, placeholder at 30% opacity, blue caret, red error | Every field has a visible label or `aria-label`; placeholder uses `--faint`; focus is an accent border plus ring; `aria-invalid` turns the border red |
| Dark and light component sets | Each component has a dark and a light colour set | Every token has a dark value; a component never sets its own theme colours |
| Purposeful motion | Press animation on touch; shimmer for attention | Short, functional motion only: 140ms for state changes, 220ms for panels; no bounce; honours `prefers-reduced-motion` |

## Tokens

### Spacing
A 4px base: `--space-1` 4, `--space-2` 8, `--space-3` 12, `--space-4` 16, `--space-5` 20, `--space-6` 24,
`--space-8` 32, `--space-10` 40, `--space-12` 48. Gaps inside a control use 8; between controls 8–12; between
sections 24–32.

### Typography
- UI face: IBM Plex Sans (`--font-ui`). Display face: Newsreader (`--font-display`) for page and hero titles only.
- Scale: 11, 12, 13, 14, 15 (body), 17, 20, 24, 32, 44px (`--text-2xs` … `--text-4xl`).
- Weights: 400, 500, 600, 700. Buttons and labels 600; section labels 600–700 caps at 11–12px with 0.08em tracking.
- Fields use 14px on desktop and 16px on phones, so iOS does not zoom when a field is focused.

### Colour
| Token | Use |
| --- | --- |
| `--bg` | Page background |
| `--surface` | Solid surfaces: dialogs, menus, fields, cards that must be opaque |
| `--panel` | Cards on the page (translucent glass in dark mode) |
| `--surface-sunken` | Disabled fields, recessed areas |
| `--lift` | Hover and selected rows |
| `--line`, `--line-strong` | Dividers and card borders |
| `--field`, `--field-border`, `--field-border-hover` | Inputs |
| `--text`, `--muted`, `--faint` | Primary, secondary and tertiary text |
| `--accent`, `--accent-hover`, `--accent-edge`, `--accent-soft`, `--on-accent` | The one brand colour: primary actions, focus, current step |
| `--danger`, `--ok`, `--warn` (+ `-text`, `-soft`) | Errors, success, waiting/attention; `-text` meets contrast on the page, `-soft` is the badge fill |
| `--scrim` | Behind dialogs |

### Shape and elevation
- Radius: `--radius-xs` 6 (small buttons, toasts), `--radius-control` 10 (buttons, fields), `--radius-sm` 12,
  `--radius` 18 (cards), `--radius-lg` 20 (dialogs), `--radius-pill` for pills and chips.
- Elevation: flat cards use a border only; `--elev-1` raised; `--elev-2` floating (menus, popovers, toasts);
  `--elev-3` dialogs. Buttons use the solid edge instead of a blur.

### Motion
`--motion-fast` 140ms, `--motion-standard` 220ms, easing `--ease-standard`. Buttons press down by the edge depth;
nothing scales or lifts on hover. All of it is turned off under `prefers-reduced-motion`.

## Components

### Buttons
- `.btn` primary (accent with edge), `.btn.secondary` (surface with neutral edge), `.btn.danger` (soft red),
  `.btn.small` 32px, default 40px, `.btn.large` 48px, `.btn.block` full width.
- One primary button per view or dialog. Destructive actions ask first, then show `.btn.danger`.
- Disabled buttons lose the edge and turn neutral; they never rely on opacity alone.
- `.icon-btn` is at least 32×32 and every icon-only button has an `aria-label`.
- `.pill` filters are 32px high; the selected one uses the accent.

### Inputs
- One style for text, email, search, number, date, select and textarea: 40px high, 10px radius, `--field`
  background, `--field-border`, accent border plus ring on focus, sunken background when disabled.
- Selects use a drawn chevron so they match fields in both themes.
- Checkboxes and radios are 16px with `accent-color`; file inputs get a secondary-style button.
- Fields inside a composite control (the chat composer, the Today composer) are bare; the control shows focus.

### Feedback
- Errors: `.err` text in `--danger-text`; notices: `.notice` on `--accent-soft`.
- Status chips (jobs, skills, flow nodes): running = accent, waiting = warn, done = ok, failed = danger, all on
  their soft fills.
- Toasts invert the theme (`--text` on `--bg`) so they read on any background.
- Empty states: dashed border, one sentence and at most one primary action.

### Sign-in
- Uses the same tokens as the app and follows the saved theme (`lumio.theme`, dark when nothing is saved).
- Google and Microsoft are 48px secondary-style buttons.
- **Local demo account.** For local development only, `DEV_LOGIN=true` adds **Continue with local demo account**
  (`demo@lumio.local`, no password). The API refuses `POST /api/auth/dev/login` with 404 unless `DEV_LOGIN` is on
  **and** `PUBLIC_WEB_URL` is `localhost`, `127.0.0.1`, `::1` or a `*.localhost` name. We do not ship a default
  username and password: in a commercial, multi-user product, a known login on a deployed server is an open door
  to every workspace. Tested in `apps/api/tests/test_dev_login.py`.

## Audit (November 2026)

Every screen in `apps/web/app` checked in light and dark, at 1440px and 390px wide.

| Area | Finding | Fix |
| --- | --- | --- |
| All fields, dark mode | Inputs kept a white background while dark mode made their text near-white, so typed text was barely visible (Account, Saved runs, Connectors search) | Fields use `--field` and `--field-border`, defined for both themes |
| Sidebar, `AgentsNav` | Agent rows stretched to fill the panel (a 150px-tall Chief card) because the list grid stretched its rows | `align-content: start` |
| Scheduled work | Filter pills stretched into tall ovals; the agent select and search stacked | Toolbar centred; pills 32px; filters on one row (wrapping on phones) |
| Selects | Native look, different heights from fields, white in dark mode | Shared field style with a drawn chevron |
| Buttons | Primary lifted on hover; danger pink was unreadable when disabled in dark mode; send button was black in light and blue in dark | One button system with edge and press; neutral disabled state; send uses the accent in both themes |
| Sign-in | Own purple accent and hard-coded colours; always light | Rebuilt on tokens; follows the theme; local demo button |
| Job timeline, skill chips, flow nodes | Hard-coded greens, reds and blues that clashed in dark mode | Semantic tokens with soft fills |
| Menus, toasts, dialogs | Mixed shadows; toast dark-on-dark in dark mode; translucent dialog scrim colours | `--elev-2` for menus, inverted toast, `--scrim`, dialogs scroll inside the viewport |
| Chat composer (phone) | Placeholder wrapped and was cut off | Shorter placeholders |
| Composite fields | A focus ring appeared inside the chat and Today composers | The composer shows focus with the accent border instead |
| Pages | Pages stretched to any width | `.page` capped at `--page-max` (1200px) |

Still open: page titles mix the serif (most pages) and sans (sign-in form, dialogs) on purpose: serif for page
titles, sans inside forms. Older one-off components still use some raw values; move them to tokens when you touch
them.
