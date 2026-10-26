// CairnML: system architecture (5 physical machines)
// Compile: typst compile cairnml-architecture.typ
// Needs the @preview/cetz package (fetched automatically on first compile).
//
// Layout is absolute: every box is placed by its centre (x, y) on a grid.
// Units are canvas units (0.9 cm each). Node size: W x H.
// Rows, top to bottom: external services, client | server | agents, NAS.

#import "@preview/cetz:0.4.2": canvas, draw

#set page(width: 400mm, height: auto, margin: 12mm, fill: white)
#set text(font: ("Liberation Sans", "DejaVu Sans"), size: 9pt, fill: rgb("#1f2937"))

// ---------- Palette per physical machine ----------
#let ext = (fill: rgb("#f3f4f6"), stroke: rgb("#6b7280"))
#let srv = (fill: rgb("#dbeafe"), stroke: rgb("#2563eb"))
#let cli = (fill: rgb("#dcfce7"), stroke: rgb("#16a34a"))
#let agt = (fill: rgb("#fef3c7"), stroke: rgb("#d97706"))
#let nas = (fill: rgb("#ede9fe"), stroke: rgb("#7c3aed"))
#let ink = rgb("#374151")

#let W = 4.4 // node width
#let H = 1.2 // node height

// ---------- Helpers ----------
#let node(x, y, title, sub, c) = {
  draw.rect(
    (x - W / 2, y - H / 2), (x + W / 2, y + H / 2),
    fill: c.fill, stroke: 0.8pt + c.stroke, radius: 3pt,
  )
  draw.content(
    (x, y), width: W - 0.2,
    align(center)[
      #text(weight: "bold", size: 8.5pt)[#title] \
      #text(size: 7pt, fill: ink)[#sub]
    ],
  )
}

#let frame(x0, y0, x1, y1, c, title) = {
  draw.rect(
    (x0, y0), (x1, y1),
    fill: c.fill.transparentize(55%),
    stroke: (paint: c.stroke, thickness: 0.6pt, dash: "dashed"),
    radius: 5pt,
  )
  draw.content(
    (x0 + 0.3, y1 - 0.45), anchor: "west",
    text(weight: "bold", size: 9pt, fill: c.stroke)[#title],
  )
}

// Orthogonal wire through the given points
#let wire(pts, dash: false, both: false) = draw.line(
  ..pts,
  stroke: if dash {
    (paint: ink, thickness: 0.7pt, dash: "dashed")
  } else {
    0.7pt + ink
  },
  mark: if both { (start: ">", end: ">") } else { (end: ">") },
)

// Small label with white background so it stays readable over lines
#let tag(x, y, body) = draw.content(
  (x, y),
  box(fill: white, inset: 1.5pt, text(size: 6.8pt, fill: ink)[#body]),
)

#align(center)[
  #text(size: 16pt, weight: "bold")[CairnML: System Architecture]
  #linebreak()
  #text(size: 9pt, fill: rgb("#6b7280"))[Five physical machines. Datasets and artifacts live in object storage; the server stores their metadata and references, never the files in its databases.]
]
#v(4mm)

#canvas(length: 0.9cm, {
  // ============ Row 1: external services ============
  frame(0, 19.0, 41.2, 23.6, ext, "5 · External services")

  // ============ Row 2: Client | Server | Agents ============
  frame(0, 10.4, 5.8, 18.0, cli, "1 · Client")
  frame(7.0, 10.4, 23.2, 18.0, srv, "2 · Server")
  frame(24.8, 10.4, 41.2, 18.0, agt, "4 · Agents")
  draw.content(
    (25.1, 17.1), anchor: "west",
    text(size: 6.8pt, fill: ink)[auth: device key → agent token · per-job token · GitHub token · presigned S3 URLs],
  )

  // ============ Row 3: NAS ============
  frame(0, 4.7, 41.2, 8.0, nas, "3 · NAS")

  // ============ Client (one node) ============
  draw.rect(
    (0.4, 13.3), (5.4, 15.1),
    fill: cli.fill, stroke: 0.8pt + cli.stroke, radius: 3pt,
  )
  draw.content(
    (2.9, 14.2), width: 4.8,
    align(center)[
      #text(weight: "bold", size: 8.5pt)[Python SDK] \
      #text(size: 7pt, fill: ink)[auto-logged local runs, \ job dispatching, dataset management]
    ],
  )

  // ============ Server ============
  node(9.9, 16.1, "API server", "clients + agents, REST", srv)
  node(15.1, 16.1, "Experiment Tracking", "runs, metrics, artifacts", srv)
  node(9.9, 13.9, "Job queues", "agents take the next job", srv)
  node(15.1, 11.7, "Database", "MongoDB · Redis · Elasticsearch", srv)
  node(20.3, 11.7, "Auth middleware", "token check, admin role", srv)
  node(20.3, 16.1, "Identity Provider", "Keycloak: OIDC, cairnml realm", srv)

  // ============ Agents ============
  node(32.9, 16.1, "Resource usage", "CPU, RAM, GPU, disk", agt)
  node(27.7, 13.9, "Agent daemon", "cairnml-agent daemon --queue", agt)
  node(32.9, 13.9, "Python runner", "venv per job", agt)
  node(38.1, 13.9, "Container runner", "Docker: host or dind", agt)

  // ============ External ============
  node(2.9, 21.9, "GitHub Enterprise", "source repos, read by GitHub App", ext)
  node(20.3, 21.9, "Dockerhub", "container images", ext)

  // ============ NAS ============
  draw.rect(
    (6.0, 5.2), (35.2, 6.9),
    fill: nas.fill, stroke: 0.8pt + nas.stroke, radius: 3pt,
  )
  draw.content(
    (20.6, 6.05), width: 20.0,
    align(center)[
      #text(weight: "bold", size: 8.5pt)[Object storage (RustFS)] \
      #text(size: 7pt, fill: ink)[S3 API · buckets: dataset, model, artifact, debug]
    ],
  )

  // ============ Wires: client ============
  wire(((5.4, 14.6), (6.4, 14.6), (6.4, 16.1), (7.7, 16.1)))
  tag(6.4, 15.3, [REST])
  wire(((2.9, 15.1), (2.9, 21.3)))
  tag(2.9, 16.6, [git clone / push])
  wire(((5.4, 13.6), (6.4, 13.6), (6.4, 9.2), (26.0, 9.2), (26.0, 6.9)))
  tag(14.0, 9.2, [artifact transfer: presigned URLs; dataset transfer: short-lived S3 keys])

  // ============ Wires: server ============
  wire(((12.1, 16.1), (12.9, 16.1)))
  wire(((9.9, 15.5), (9.9, 14.5)))
  wire(((15.1, 15.5), (15.1, 12.3)))
  tag(16.7, 13.0, [runs, metrics, artifacts])
  wire(((9.9, 13.3), (9.9, 11.7), (12.9, 11.7)))
  tag(11.2, 12.9, [queued tasks])
  wire(((20.3, 12.3), (20.3, 15.5)), dash: true, both: true)
  tag(21.8, 14.6, [OIDC: login, user sync])

  // Server -> GitHub: read-only GitHub App (fetch remote status)
  wire(((7.6, 18.0), (7.6, 19.6), (3.8, 19.6), (3.8, 21.3)), dash: true)
  tag(5.7, 19.6, [GitHub App, read])

  // ============ Wires: queues -> agents ============
  wire(((12.1, 13.9), (24.0, 13.9), (24.0, 16.1), (30.7, 16.1)), both: true)
  tag(18.0, 14.3, [REST: register, take jobs, logs, job tokens])

  // ============ Wires: agents -> NAS / Dockerhub / GitHub ============
  wire(((27.7, 13.3), (27.7, 6.9)))
  tag(29.0, 9.2, [dataset pull, short-lived S3 keys])
  wire(((32.9, 13.3), (32.9, 6.9)))
  tag(33.6, 9.2, [upload artifacts])
  wire(((38.1, 14.5), (38.1, 21.0), (19.0, 21.0), (19.0, 21.3)))
  tag(30.5, 21.0, [pull images])
  wire(((26.4, 18.0), (26.4, 20.4), (4.6, 20.4), (4.6, 21.3)))
  tag(14.0, 20.4, [git clone with job token])
})

#v(5mm)

// ---------- Legend and notes ----------
#grid(
  columns: (1fr, 1fr),
  gutter: 10mm,
  [
    #text(weight: "bold")[Machine rules]
    - *Client* is the Python SDK. It talks to the Server (REST), to GitHub Enterprise (git) and to the object store on the NAS (artifacts through presigned S3 URLs; datasets through DVC, with short-lived keys from the server) and keeps a per-user cache of datasets.
    - *Server* runs the API, the job queues, the database (MongoDB for tasks and metadata, Redis and Elasticsearch for caches and events) and the sign-in service (Keycloak), in the same deploy stack.
    - *NAS* runs the object store: one S3-compatible service (RustFS) with four buckets (dataset, model, artifact, debug), and one service user that may only use those buckets. The server uses it to issue short-lived keys limited to the dataset bucket.
    - *Agents* are enrolled by an admin with a device key. Each job gets a token, and a short-lived GitHub token that clones its repository from GitHub Enterprise. Jobs run in containers (Docker on the host, or a Docker daemon in a `dind` container). They pull images from Dockerhub, move artifacts with presigned S3 URLs and datasets with short-lived S3 keys, and keep a dataset cache that persists between jobs.
  ],
  [
    #text(weight: "bold")[Data and identity]
    - Artifacts are CairnML artifacts. Datasets are GitHub repositories with their files in DVC: the commit is the version, and a run stores the repository, the commit and a hash of the files, not the files. Outside datasets (Hugging Face and others) are read through adapters kept in an external-datasets repository.
    - Source repositories are on GitHub Enterprise. The server reads them with a read-only GitHub App; a job clones its repository with its own token.
    - Clients enroll once through a Keycloak login, then use their device key only. Agents are enrolled by an admin only.
    - Presigned S3 URLs name the object store's public address, so clients must be able to reach it.
    - Dashed arrows: identity and authentication checks.
    - Users sign in through Keycloak, and the server syncs users from it.
  ],
)
