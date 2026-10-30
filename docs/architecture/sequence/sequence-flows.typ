// Entry point for the CairnML sequence diagrams (chronos package).
// Compile: typst compile sequence-flows.typ   (needs @preview/chronos 0.3.0)
#set document(title: "CairnML: sequence diagrams")
#set page(paper: "a4", margin: 1.5cm, numbering: "1 / 1")
#set text(size: 9pt)
#set heading(numbering: "1.")

#align(center)[
  #text(size: 16pt, weight: "bold")[CairnML: sequence diagrams]
  #linebreak()
  #text(size: 9pt, fill: rgb("#6b7280"))[Common operations between the SDK, the server, the identity provider, the agent, the web app, object storage and Git.]
]
#v(4mm)

Participants: *SDK* (Python client), *Server* (CairnML API), *IDP* (Keycloak), *Agent* (job runner), *WebUI* (browser app), *RustFS* (object storage), *Git* (source repositories). Solid arrows are requests and dashed arrows are replies. Grey notes are processing inside a party. Endpoint names are those in the code.

#include "01-enrollment.typ"
#include "02-webui-login.typ"
#include "03-task-logging.typ"
#include "04-job-execution.typ"
#include "05-database-management.typ"
