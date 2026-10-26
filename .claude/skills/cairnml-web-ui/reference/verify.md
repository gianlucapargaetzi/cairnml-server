# Verify your page visually

A page that compiles is not a page that works. You must look at it. This file is the recipe; every step below was needed at least once in this repo.

## 0. Do not judge a screenshot until you know the data is real

Pages look broken when the API is down or the database is empty: wrong titles ("All Experiments" in place of a project name), missing tabs, empty tables. Before you judge a layout:

1. The stack is up and **not being rebuilt right now**: `curl -s -o /dev/null -w "%{http_code}" http://cairnml:8008/debug.ping` and `...:8080/` both answer 200.
2. The data you need exists. If the dev stack is empty (it can be reset by someone else), seed it with a small SDK script (`Task.init`, `report_scalar`, `report_histogram`, `report_table`, `report_image`, `upload_artifact`, `Task.clone`) or **mock the API in the browser** (section 4). Never invent screenshots of "what it should look like".
3. Wait for loading to finish: 6 seconds after `goto`, then check that the spinner is gone.

## 1. The two loops to see a change

| Loop | When | How |
|---|---|---|
| **Build check** (seconds) | After every edit | `cd web && pnpm run build`. Catches template and type errors. A budget warning on `table.component.scss` is old and ignorable. |
| **See it in the running stack** (about 5 minutes) | To look at the page | Rebuild the server image, which contains the built web app: `docker compose -f deploy/compose.yaml build apiserver`, then `docker compose -f deploy/compose.yaml up -d --force-recreate --no-deps apiserver webserver`. **Always `--no-deps`**: without it the database and search containers are recreated too. The recreate drops the browser session: sign in again. Do not take screenshots while the build's last step runs; the API is briefly down. |
| Hot reload (optional) | Many small visual iterations | `pnpm start` on `http://localhost:4200`; needs the stack's sign-in addresses set for localhost: read `web/docs/dev-proxy.md` first. Do not change the deployed stack's settings without asking. |

Run the image build in the background and keep working on the next view or the report while it runs.

## 2. Browser setup (Playwright MCP)

- Open pages with `browser_navigate` or, for several pages, `browser_run_code_unsafe` and a loop. Screenshots must be written **inside the project** (`.playwright-mcp/` is fine and git-ignored); writing to `/tmp` is refused.
- **Signing in:** an unauthenticated request lands on the identity provider page ("Sign in to your account"). Fill the two textboxes and click "Sign In". The dev stack credentials are in `deploy/.env` (`KC_ADMIN`, `KC_ADMIN_PASSWORD`). **Ask the user before you read or use them if this session has not already been given permission**, never print them into a file or a commit, and never put them in a skill or a note. Expect to sign in again after every container recreate.
- Read screenshots with the image `Read` tool, one file per call, and describe what you see against your checklist; do not assume.

```js
// one page, light, full height
await page.goto('http://cairnml:8080/<path>');
await page.waitForTimeout(6000);
await page.screenshot({path: '.playwright-mcp/<name>-light.png', fullPage: true});
```

## 3. What to capture: the matrix

For every page you build or change:

| Axis | Values |
|---|---|
| Theme | light **and** dark |
| Width | wide (about 1850 px) and narrow (about 1200 px) |
| Data | empty, one item, typical, many (30+), a very long name, a failed/lost/disabled item |
| Interaction | the state after a click that opens detail, a hover, an expanded row, a dialog if the page has one |

**Dark mode** follows the system setting, so emulate it:

```js
await page.emulateMedia({colorScheme: 'dark'});
// check it took effect: the dark surface token is #0f141a
await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--color-surface'));
```

**Narrow width:** `await page.setViewportSize({width: 1200, height: 800});`

## 4. Realistic data without touching the server: mock the API

Intercept the page's own requests and answer with made-up data. Nothing on the server changes. The API answers are wrapped as `{data: {...}}` and the endpoint name is `service.action` (e.g. `scheduler.get_overview`).

```js
const ago = s => new Date(Date.now() - s * 1000).toISOString();
await page.route('**/scheduler.get_overview', r => r.fulfill({json: {data: {workers: [ /* ... */ ]}}}));
await page.route('**/scheduler.get_jobs',     r => r.fulfill({json: {data: {jobs:    [ /* ... */ ]}}}));
await page.goto(url);
```

Always include the awkward cases in the mock: an item in each state (active, draining, lost; running, pending), a name that is 60 characters long, a value of zero, a value of 100 %, a list long enough to scroll.

## 5. What to look for

Compare against your twin, side by side, at the same width and theme.

- **Gutters and top spacing** identical to the twin (24px sides; nothing doubled from a parent).
- **Surfaces:** panels use the card surface with the thin outline; nothing is a hard-coded white or black; text has enough contrast in dark.
- **Overflow:** no horizontal page scroll; no element overlapping a neighbour; long text truncates with an ellipsis. Detect it, do not guess:

```js
await page.evaluate(() => ({
  pageOverflowsX: document.documentElement.scrollWidth > document.documentElement.clientWidth,
  wideElements: [...document.querySelectorAll('*')]
    .filter(e => e.getBoundingClientRect().right > window.innerWidth + 1)
    .slice(0, 8).map(e => e.tagName.toLowerCase() + '.' + [...e.classList].join('.')),
}));
```

- **Type:** titles uppercase like their neighbours; buttons uppercase; numbers align (`num` class); no text smaller than 11px.
- **Status colours:** the same word has the same colour as on the other pages.
- **The picture:** is there a visual summary (tiles, chart, counters) **and** a precise view (table)? If the page is only one of the two, go back to workflow step 4.
- **The detail:** open the detail state. Is all of the information still there, readable, not squeezed? If you removed anything to make it fit, undo that and change the layout instead.
- **Empty and error states** look like the app's, not like a blank area.
- **Console:** open `.playwright-mcp/console-*.log` (the tool prints its path). New errors that mention your component are yours. (The Experiments page logs a `reading 'disable'` TypeError on every load; that one is old.)

## 6. Clean up after yourself

- Delete the screenshots you took when you are finished; they are not part of the work. `.playwright-mcp/` holds files from other sessions: remove only your own.
- Remove any seeded demo data you created if the user did not ask for it to stay, and say which data you created.
- Remove mock routes (`page.unroute`) before you use the same browser page for something else.
