# Component catalog: what to use when

93 components live in `webapp-common/shared/ui-components/` and `webapp-common/shared/components/`. This is the catalog of the ones you will actually need, grouped by the job you are doing. Inputs are the real ones (shortened; read the component for the full list). "Seen in" is a template that uses it, which is your example.

**Rule: find your job in the left column. If a component is named, use it. If none is named, search `webapp-common/shared/` for it before you write markup.** `grep -rlE "<sm-NAME(\$|[[:space:]>])" --include=*.html .` shows every use (tags often continue on the next line, so a plain `<sm-NAME ` search misses most).

All paths under `web/src/app/webapp-common/` unless noted.

## Lists and tables

| Job | Use | Key inputs | Seen in |
|---|---|---|---|
| A table of records (the default for every list) | `sm-table` (`shared/ui-components/data/table`) with `pTemplate="body"` for cells | `columns` (ISmCol), `tableData` (every row needs a unique `id`), `selectionMode` (`"single"`, `null`), `simple`, `rowHeight`, `resizableColumns`, `columnResizeMode`, `expandableRows`, `expandedRowKeys`, `scrollable` | `dashboard/dumb/recent-experiment-table` |
| A table with nested rows | `sm-table` with `[expandableRows]="true"` and a `pTemplate="rowexpansion"` containing a second `sm-table` | `expandedRowKeys`, `(rowExpanded)`, `(rowCollapsed)` | the previous Workers page; `serving/serving-table` |
| A small fixed list in a side panel | `sm-table` with `[simple]="true"` (as the Jobs and Workers pages do). `sm-simple-table-2` exists but no template uses it today, so do not choose it. | `columns`, `tableData`, `selectionMode`, `columnResizeMode` | `features/workers-and-queues/scheduler/jobs-list.component.html` |
| Sort/filter in a column header | `sm-table-filter-sort`, `sm-table-filter-duration`, `sm-table-filter-duration-numeric` | `column`, `options`, `value`, `sortOrder` | `shared/components/main-pages-header-filter` |
| A table whose rows are cards when the panel is narrow | `sm-table-card` | `columns`, `rowData`, `selected`, `checked` | `serving/serving-table` |
| Clear all filters | `sm-clear-filters-button` | `tableFilters`, `(clearTableFilters)` | experiments toolbar |
| Choose columns | `sm-custom-columns-list` | `tableCols`, `selectable` | experiments toolbar |
| Paging at the end of a list | `sm-dots-load-more` | `loading`, `(loadMore)` | table component, card grids |
| A grid of cards or tiles that loads more | `sm-virtual-grid` | `items`, `cardWidth`, `cardHeight`, `autoLoadMore` | `debug-images/debug-images-view` |

## Cards and panels

| Job | Use | Notes |
|---|---|---|
| An entity you **open** (project, dataset, folder) | `sm-project-card` | Inputs: `project`, `hideMenu`, `isRootProject`; output `projectCardClicked`. Shows a folder tab, the name, three circle counters and a compute-time footer. Map your entity to a project-shaped object (`id`, `name`, `basename`, `stats.active.{total_tasks, status_count.in_progress, completed_tasks_24h, total_runtime}`, `company.id`) to reuse it. Seen in `dashboard/containers/dashboard-projects`. |
| A filled folder card with your own body | `sm-card` | Inputs: `header`, `showFolder`, `oneTabMode`, `isExample`, `height`; content slots `header-content`, `footer`. Not for KPI tiles. Seen in `nested-project-view/nested-card`. |
| A titled panel with a header strip | `sm-card2` | Inputs: `header`, `headerClassName`, `showBorder`. Seen in `experiments-compare/dumbs/compare-card-list`. |
| An add-new slot in a card grid | `sm-plus-card` | Input `folder`; output `plusCardClick`. Seen in `dashboard/containers/dashboard-reports`. |
| Entity cards for experiments, models, reports, pipelines | `sm-experiment-card`, `sm-model-card`, `sm-report-card`, `sm-pipeline-card` | Use the one for your entity. |
| **KPI tile, chart panel, group card on a dashboard** | **the plain `.card`** from `styles/mixins/panels.scss` (not a component): `.card` > `.card-title`, `.big.num`, `.sub` | `@include panels.dashboard-panels();`. Seen in `features/workers-and-queues/dashboard`. |
| Collapsible area | `mat-expansion-panel` with the app's expansion style | Seen in `project-info`. |
| A side drawer panel | `al-drawer` (`panel/drawer`) | Inputs `label`, `displayOnHover`. |

## Figures, status and identity

| Job | Use | Key inputs |
|---|---|---|
| A count with a label, in a circle | `sm-circle-counter` | `counter`, `label`, `type` (total / running / completed / empty), `underLabel` |
| A status with an icon and text | `sm-status-icon-label` | `status` |
| A status as a coloured circle | `sm-circle-status` | `status` (a `TaskStatusEnum`) |
| An experiment type with its icon | `sm-experiment-type-icon-label` | `type` |
| A state or alert badge | `.badge` + `.caps` + `state-ok / state-warn / state-bad / state-idle` | from `mixins/panels.scss` |
| An id the user may copy | `sm-id-badge` | `id`, `short`, `caption`; output `copied` |
| A value the user may copy | `sm-copy-clipboard` | `clipboardText`, `label`, `tooltipText` |
| Tags | `sm-tag-list` (editable), `sm-mini-tags-list` (compact), `sm-user-tag` | `tags`, `readonly`, `showAddTag` |
| Chips | `sm-chips`, `sm-chips-list` | `items`, `allowRemove` |
| A row of overlapping circles | `sm-circles-in-row` | `data`, `limit`, `stagger` |
| A search or result line in a list | `sm-result-line` | `label`, `fontIcon`, `status`, `tags` |
| Usage against a limit | `mat-progress-bar.usage-bar` or the two-layer `.bar` | thresholds via `.high`, `.full` |

## Details of one record (key/value, sections, editing)

| Job | Use | Key inputs |
|---|---|---|
| One labeled value (uppercase label left, value right, divider) | `sm-labeled-row` | `label`, `labelClass`, `showRow` (content is projected). Seen in `serving/serving-general-info`. |
| The same, label above the value | `sm-vertical-labeled-row` (exists, currently unused: check your twin before choosing it) | `label`, `tooltip` |
| A section title inside a detail view | `sm-section-header` | `label`, `helpText`, `error`. Seen in `experiments/containers/experiment-info-execution`. |
| A section the user can edit with Save/Cancel | `sm-editable-section` | `editable`, `saving`, `disableSave`; outputs `saveClicked`, `cancelClicked`, `activateEditClicked`. Seen in `experiment-info-model`. |
| Click-to-edit text | `sm-inline-edit` | `originalText`, `editable`, `required`, `pattern`; output `textChanged` |
| Code | `sm-code-editor` | `code`, `mode`, `readonly`, `showCopyButton` |
| JSON | `sm-json-viewer` (read), `sm-edit-json` (dialog) | `json`, `search` |
| Markdown | `sm-markdown-editor` | `data`, `readOnly`; seen in `project-info` |
| A divider with a label | `sm-divider` | `label`, `margin` |

## Charts

| Job | Use | Notes |
|---|---|---|
| A time series on a dashboard | `sm-line-chart` (`shared/components/charts/line-chart`, chart.js) | Inputs `data` (`Topic[]`: `topicID`, `topicName`, `topic`, `dates: {value, date}[]`, optional `secondAxis`, `dashed`), `unit`, `yLabel`, `colorScheme` (hex array: reuse `['#f57e23', '#fbc08f']`), `hideLegend`, `showGrid`. Put it in a fixed-height `.chart-box` (e.g. 240 to 300px) with `class="h-100"`. Seen in `features/workers-and-queues/dashboard`. |
| Part of a whole | `sm-donut` | Inputs `data`, `colors`, `units`. Seen in `project-workloads/workloads-page`. |
| A scatter comparison | `sm-scatter-plot` | Seen in `experiments-compare/containers/compare-scatter-plot`. |
| Experiment results: scalars, plots, histograms, tables | `sm-experiment-graphs`, `sm-single-graph` (plotly), `sm-graph-settings-bar` | The settings rail (group by, axis, smoothing) is `sm-graph-settings-bar`. Seen in `experiments-compare/containers/experiment-compare-metric-charts`. |
| One number per record shown as a table | `sm-single-value-summary-table` | Seen in `shared/experiment-graphs`. |

Charts always sit in a bordered panel with a title; each chart has a title and a legend; plots are collapsible when there are several.

## Search, filters, selection, menus

| Job | Use | Key inputs |
|---|---|---|
| Search above a list | `sm-search` (local), `sm-common-search` (the app's global-search aware one, used in toolbars) | `placeholder`, `debounceTime`, `minimumChars`; output `valueChanged` |
| Switch between 2 to 3 views of the same data | `sm-button-toggle` | `options` (`{value, icon, label}`), `ngModel`, `disabled` |
| Filter on groups or with checkboxes | `sm-grouped-checked-filter-list`, `sm-selectable-grouped-filter-list`, `sm-checkbox-three-state-list` | `list`, `checkedList`, `searchTerm` |
| A dropdown menu | `sm-menu` with `sm-menu-item`s | `header`, `iconClass`, `enableSearch`; items: `itemLabel`, `itemValue`, `sortOrder`, `checked` |
| Show only my work | `sm-show-only-user-work` | |
| Archive toggle | `sm-toggle-archive` | `showArchived`, `minimize` |
| Auto-refresh in a toolbar | `sm-refresh-button` | `allowAutoRefresh`; output `setAutoRefresh` |
| Pick a time range | `sm-period-selector`, or the `mat-select` range selector on the Workers dashboard | `timeFrameOptions` |
| Pick an entity with paging | `sm-paginated-entity-selector`, `sm-paginated-project-selector` | `data`, `label`; output `getEntities`, `loadMore` |
| Autocomplete with chips | `sm-select-autocomplete-with-chips`, `sm-select-autocomplete-for-template-forms` | `items`, `multiple`, `clearable` |
| Durations | `sm-duration-input-list`, `sm-duration-input` | `durationValue` |
| Colour of a tag | `sm-tag-color-menu`, `sm-color-picker-wrapper` | |

## Dialogs, overlays, feedback

| Job | Use |
|---|---|
| Any dialog | `sm-dialog-template` (inputs `header`, `iconClass`, `displayX`, `subHeader`); open it with `MatDialog` and `panelClass: 'dialog-md'` (`dialog-sm`, `dialog-lg`, `full-screen` exist). 25 dialogs use it. |
| Confirm a destructive action | `sm-confirm-dialog` (always, for delete / revoke / reset) |
| Rename | `sm-rename-dialog` |
| Show an error from the server | `sm-operation-error-dialog`, or the error service. Never `alert()`. |
| Multi-step flow | `sm-wizard-dialog-step` (exists, currently unused in templates: copy a wizard from the code that uses `wizard-template.scss`) |
| A long hint on hover | `smTooltip` (the directive), `sm-multi-line-tooltip` for several lines |
| Page-wide loading | `sm-spinner` (store-driven; dispatch `activateLoader` / `deactivateLoader`) |
| Small inline loading | `mat-spinner` with `[diameter]="24"` |
| A toast message | the notifier (`addMessage` action) |

## Navigation

| Job | Use |
|---|---|
| The feature's tab strip in the header | `headerActions.setTabs` (not a component you place; see `reference/tokens-and-layout.md`) |
| Tabs inside a page | `sm-router-tab-nav-bar` (`links`) or `mat-tab-group` |
| A record's own tab bar | `sm-experiment-info-navbar` |
| Left navigation of a settings-like page | `mat-drawer-container` + `mat-list` (see `features/settings`) |
| Resizable master/detail | `as-split` + `as-split-area` (`angular-split`) |
| The header toolbar of a list page | `sm-projects-header` (card pages), the entity's `*-header` (table pages) |

## Material pieces the app styles for you

`mat-flat-button` (primary), `mat-stroked-button` (secondary), `mat-button` (text), `mat-icon-button`, `mat-form-field` (outline), `mat-select`, `mat-checkbox`, `mat-slide-toggle`, `mat-menu`, `mat-tab-group`, `mat-progress-bar`, `mat-expansion-panel`, `mat-drawer`. They already carry the theme (heights, pill buttons, colours). Do not restyle them; do not wrap them in your own look.

## Things that do not exist and must not be invented

A second card style, a second status colour set, a custom modal, a custom dropdown, a custom tooltip, a custom table, a custom tab bar, a custom spinner, a custom toast, a custom empty state, a custom page header. If you think you need one, you have not found the twin yet (workflow step 2).
