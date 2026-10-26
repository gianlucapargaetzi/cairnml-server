# Page archetypes

Every page in the app is one of these shapes or a combination of two. Pick the shape first; it decides the shell, where the actions sit and which components belong. Each entry names the **twin files** to copy from and the **live URL** to open and look at before you build (ids in `<...>` are yours).

All paths are under `web/src/app/`.

## How to choose

| Your content looks like | Archetype |
|---|---|
| A few key numbers, groups of something, a chart, recent activity | **A. Dashboard** |
| Things you open (projects, datasets, tasks): each has a name, a state, some counts | **B. Entity card grid** |
| Many records with many attributes to scan, sort, filter, compare | **C. Full-bleed table page** |
| A list where each item has detail that needs room (tables, charts, tabs) | **D. Master / detail** |
| One record with deep, tabbed detail | **E. Tabbed detail** |
| Several records side by side | **F. Compare** |
| Preferences and configuration | **G. Settings** |
| No data yet | **H. Empty state** (inside any of the above) |

Combinations that already exist: the Workers page is **A + B + D** (tiles, a grid of machine cards, a detail panel). The Jobs page is **A + C + a feed** (tiles, a table, an activity list). The project dashboard is **A + B**.

---

## A. Dashboard: tiles, groups, chart, feed, table

**Use when** the page answers "how is it going?" and the user mostly reads and occasionally drills in.

**Shell:** `:host {display:block; height:100%; overflow:auto}` and `.page {display:grid; gap:24px; padding:0 24px 24px}`. Sections stack with 24px between them. No toolbar.

**Typical sections, top to bottom**
1. **KPI tiles**: `.summary` grid of `.card`s with `.card-title`, `.big.num`, `.sub` (`repeat(auto-fit, minmax(220px, 1fr))`, gap 16px).
2. **A titled section of groups or cards**: `.section-title` then a grid of `.card`s (`minmax(300px, 1fr)`), each with a name, a state badge and `usage-row`s (label, `mat-progress-bar.usage-bar`, value).
3. **A chart panel next to a log**: a two-column grid; the chart panel has a `.panel-header` (title left, range selector `mat-form-field` right) and a `.chart-box` of fixed height holding `sm-line-chart`; the log is a `ul` of events with a colour tone.
4. **A table**: `sm-table` inside a bordered box (`border: 1px solid var(--color-outline-variant); border-radius: 4px; overflow: hidden`).

**Twins:** `features/workers-and-queues/dashboard/resources-dashboard.component.{ts,html,scss}` (the model); `features/dashboard/dashboard.component.html` with `webapp-common/dashboard/containers/*` (projects row + recent experiments); `features/task-folders/task-folders.component.*` (project dashboard); `webapp-common/project-workloads/workloads-page/*` (charts page with a period selector, donut and graphs).
**Live:** `/workers-and-queues/dashboard`, `/dashboard`, `/projects/<id>/folders`, `/projects/<id>/workloads`.
**Styles to include:** `mixins/panels.scss`.
**Pair:** tiles (the number) + chart (the trend) + table (the exact rows).

## B. Entity card grid

**Use when** the entities are few to dozens, are opened one at a time, and have a state worth seeing on the card.

**Shell:** `.sm-card-list-layout` (grid of 352px cards, 24px gap, centred) with the `.sm-card-list-header` toolbar as its first grid row. Cards are `sm-project-card` / `sm-nested-card` / `sm-card` with `[showFolder]="true"` (the folder tab). Add `sm-plus-card` as the last card to create a new one.

**Toolbar (left to right):** sort menu (`sm-menu`, "RECENT"), filter (`sm-main-pages-header-filter`), view toggle (`sm-button-toggle`), search (`sm-common-search`); on the right the secondary then primary action (`OPEN ARCHIVE`, `NEW PROJECT`). Use `sm-projects-header` as it is; do not rebuild it.

**Card body:** a title; for counts by state use three `sm-circle-counter`s (TOTAL, RUNNING, COMPLETED) with a compute-time footer, as the project card does. Map your entity to the card's inputs rather than drawing a new card (the task folders do this: they map each folder to a project-shaped object and use `sm-project-card`).

**Twins:** `webapp-common/nested-project-view/nested-project-view-page/*`, `webapp-common/projects/containers/projects-page`, `webapp-common/datasets/open-datasets`, `webapp-common/pipelines/pipelines-page`, `webapp-common/reports/*`; the card: `webapp-common/shared/ui-components/panel/project-card`.
**Live:** `/projects`, `/datasets`, `/pipelines`, `/reports`.
**Pair:** card (the glance) + open into the table page (the detail).

## C. Full-bleed table page

**Use when** the user scans, sorts, filters and acts on many records.

**Shell:** a toolbar row (height `$project-header-height`, flex, `justify-content-between`) then the table filling the rest, edge to edge, no page gutter.

**Toolbar (left to right):** primary action (`NEW EXPERIMENT`), secondary (`OPEN ARCHIVE` via `sm-toggle-archive`), view toggle (`sm-button-toggle`: list / details / compare), search. **Right group:** `sm-clear-filters-button`, download (`sm-menu` with an `al-ico-download` icon), columns settings, `sm-refresh-button`. Build it with `d-flex justify-content-between align-items-center`, `ms-3` and `gap-3`, as `experiment-header.component.html` does.

**Table:** `sm-table` (or the entity's own wrapper such as `sm-experiments-table`): sortable header cells with `sm-table-filter-sort`, a column picker, row selection with a selection footer bar (`SHOW N <ENTITIES> SELECTED` with bulk actions), `sm-dots-load-more` for paging. Every row needs a unique `id`. Empty table: the muted "No data to show" (`table-empty-message()`); empty page: archetype H.

**Twins:** `webapp-common/experiments/experiments.component.html` + `dumb/experiments-table` + `dumb/experiment-header`; `webapp-common/models/models.component.html`; `webapp-common/serving/*` (also shows feature tabs in the header: ACTIVE / LOADING).
**Live:** `/projects/<id>/tasks`, `/projects/<id>/models`, `/endpoints/active`.
**Pair:** the table (precise) with the details view (picture): selecting a row opens archetype D.

## D. Master / detail

**Use when** a list item has detail that needs room: a table, a chart, tabs. **This is the app's answer to "it does not fit".**

**Shell:** `as-split` (`angular-split`), `[gutterSize]="1"`, two `as-split-area`s: the master (list, table or card grid) and the detail, `[visible]` only when something is selected. The split fills the page and each side scrolls itself (`overflow: auto`). Double-click on the gutter closes the panel. Call `table.afterTableInit()` on `(transitionEnd)` if the master holds a table.

**Master:** a table or a list of cards. A selected card or row is marked (primary outline for cards, `--color-primary` fill for list rows).
**Detail:** a header (name, status, id badge `sm-id-badge`, a close `mat-icon-button`) then either tabs (`sm-experiment-info-navbar` / `sm-router-tab-nav-bar`) or titled sections (`.section-title` + `.card` / table box). Put the **chart and the full table here**, never squeezed into the master.

**Twins:** `webapp-common/experiments/experiments.component.html` (table master, tabbed detail via a router outlet); `webapp-common/serving/serving.component.html`; `features/workers-and-queues/scheduler/workers-tree.component.*` (card-grid master, sectioned detail with a load chart, capacity rows and a jobs table).
**Live:** `/projects/<id>/tasks/<experiment>/execution`, `/workers-and-queues/workers` (click a machine).

## E. Tabbed detail (one record)

**Shell:** the record's header: type icon, name, tags (`sm-tag-list`), id (`sm-id-badge`), action icon buttons, a coloured status ribbon on top ("COMPLETED"); under it a tab bar (EXECUTION, CONFIGURATION, ARTIFACTS, INFO, CONSOLE, SCALARS, PLOTS, DEBUG SAMPLES). Each tab is one of a few sub-shapes:

| Tab content | Shape | Twin |
|---|---|---|
| Key/value details | `sm-section-header` (uppercase blue title) + `sm-labeled-row`s (uppercase label, value, divider) + `sm-code-editor` blocks; editable sections use `sm-editable-section` | `experiments/containers/experiment-info-execution`, `experiment-info-model` |
| Time series | settings rail on the left (group by, axis, smoothing) + chart grid (plotly, 2 columns) | `experiments/containers/experiment-output-scalars` |
| Plots and tables | rail with "Find plots" + eye list; collapsible titled panels, each with a `</>` toggle | `experiments/containers/experiment-output-plots` |
| Images | toolbar (metric select, iteration pager) + thumbnail tiles | `webapp-common/debug-images/*` |
| Log | toolbar (hostname, download, regex filter) + monospace lines with a timestamp column | `experiment-output-log` |
| Files | left list grouped by type, right: labeled rows + a PREVIEW section | `experiments/containers/experiment-info-aritfacts` |

**Live:** `/projects/<id>/tasks/<experiment>/execution`, `.../output/metrics/scalar`, `.../output/metrics/plots`, `.../output/debugImages`, `.../output/log`, `.../artifacts`.

## F. Compare

A colour-coded selection list on the left (a coloured bar per selected record, checkboxes), one collapsible titled panel per metric on the right with one plot per record side by side, a view select (Scalars / Plots ...) top right, and a floating selection bar at the bottom (`SHOW N <ENTITIES> SELECTED`, `COMPARE`, bulk actions). **Twin:** `webapp-common/experiments-compare/*`. **Live:** `/projects/<id>/tasks/compare/scalars;ids=<a>,<b>`.

## G. Settings

`mat-drawer-container` with a side drawer holding a `mat-list` of `routerLink` items (the active one uses `routerLinkActive` with the tint) and a content area with a `router-outlet` and a version footer. Content blocks are `sm-section-header` + rows. **Twin:** `features/settings/settings.component.html`. **Live:** `/settings/profile`.

## H. Empty state

Centre of the area: a large muted icon, an **uppercase title** ("NO DATASETS TO SHOW"), one line of explanation, then either a primary button ("NEW REPORT") or, for features that are used from code, a getting-started block: a short paragraph, CLI/SDK tabs and a code block with an illustration beside it. Use `mixins/empty.scss`; on a dashboard-type page use a `.card.empty` (title in `.empty-title`, hint in `.sub`). **Twins:** `webapp-common/datasets` (`sm-dataset-empty`), `webapp-common/pipelines` (`sm-pipelines-empty-state`), `webapp-common/project-workloads/workloads-page` (`empty-state`), the Workers page. **Live:** `/datasets`, `/pipelines`, `/reports`.

---

## Wiring a new page (the minimum)

1. Component in `features/<feature>/` (standalone, `ChangeDetectionStrategy.OnPush`, `inject()`, signals). A container reads the store or a service and passes data down; presentational pieces take inputs and emit outputs.
2. Lazy route in `features/<feature>/<feature>.routes.ts` (`loadComponent`), `data: {staticBreadcrumb: [[{name: 'TITLE', type: CrumbTypeEnum.Feature}]]}`, `canDeactivate: [resetContextMenuGuard]` when it sets header tabs.
3. Register it in `app.routes.ts`; one side-nav item if it is a top-level feature.
4. Header tabs if the feature has sub-pages: `headerActions.setTabs(...)` in the container, `headerActions.reset()` on destroy.
5. Lists that poll: `pollWhileVisible()` from `features/workers-and-queues/scheduler/scheduler-live.ts` (5 s, only while the tab is visible, keeps the last value on error).
6. `pnpm run build` must pass before you say you are done.
