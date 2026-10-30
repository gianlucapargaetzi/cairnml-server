// Vocabulary for the sequence diagrams, built on the @preview/chronos package.
// Each diagram is a list of steps; a step is turned into chronos elements.
//
//   call(from, to, label)      synchronous request (solid arrow, opens an activation)
//   reply(from, to, label)     answer to the most recent call (dashed arrow, closes it)
//   process(at, label)         internal work of one party, drawn as a note beside its lifeline
//   alt((guard: g, steps: s), ...)  combined fragment with one branch per guard
//
// A diagram is drawn with `seq-diagram(parties, steps)`.

#import "@preview/chronos:0.3.0": _par, _seq, _alt, _note, _col, diagram

#let call(from, to, label) = (kind: "call", from: from, to: to, label: label)
#let reply(from, to, label) = (kind: "reply", from: from, to: to, label: label)
#let process(at, label) = (kind: "process", at: at, label: label)
#let alt(branches) = (kind: "alt", branches: branches)

// Break a label into lines of at most `n` characters, at word boundaries,
// so that a note is narrow and tall instead of one long line.
// Best split of the words `ws` into exactly `k` lines. Lines are compared by the
// longest line first, then by the sum of squared line lengths, which keeps the
// lines equally wide. Returns (longest, sum-of-squares, lines).
#let best-split(ws, k) = {
  if k == 1 {
    let t = ws.join(" ")
    return (t.len(), t.len() * t.len(), (t,))
  }
  let best = none
  for cut in range(1, ws.len() - k + 2) {
    let head = ws.slice(0, cut).join(" ")
    let rest = best-split(ws.slice(cut), k - 1)
    let longest = calc.max(head.len(), rest.at(0))
    let squares = head.len() * head.len() + rest.at(1)
    let better = best == none or longest < best.at(0) or (longest == best.at(0) and squares < best.at(1))
    if better {
      best = (longest, squares, (head,) + rest.at(2))
    }
  }
  best
}

// Break a label into the fewest lines of at most `n` characters, balanced so
// that the lines have nearly equal width (no short orphan on the last line).
#let wrap-lines(label, n) = {
  let ws = label.split(" ").filter(w => w != "")
  if ws.len() == 0 { return () }
  for k in range(1, ws.len() + 1) {
    let res = best-split(ws, k)
    if res.at(0) <= n {
      return res.at(2)
    }
  }
  ws
}

#let wrap-words(label, n) = wrap-lines(label, n).join(linebreak())

// Label for a message arrow: centred on the arrow in X (comment-align "center"),
// with its own lines centred against each other.
#let arrow-label(label) = align(center, text(size: 7pt, wrap-words(label, 44)))

// Elements for one step. Calls enable activation on the callee so that the
// matching reply can close it; processing is a note over the party's lifeline.
#let elements(s, parties) = {
  if s.kind == "call" {
    _seq(s.from, s.to, comment: arrow-label(s.label), enable-dst: true, comment-align: "center")
  } else if s.kind == "reply" {
    // a reply is drawn directly (not with _ret) so that its label can be centred;
    // disable-src closes the activation that the matching call opened
    _seq(s.from, s.to, comment: arrow-label(s.label), dashed: true, disable-src: true, comment-align: "center")
  } else if s.kind == "process" {
    // a note on an outer party sits on the inside of its lifeline, so that the
    // party can stand exactly at the page edge
    let side = if s.at == parties.first() { "right" } else if s.at == parties.last() { "left" } else { "over" }
    _note(side, text(size: 7.5pt, wrap-words(s.label, 34)), pos: s.at)
  } else if s.kind == "alt" {
    // first branch is the opening guard; each further branch is an "else" pair
    let body-of(b) = {
      let body = ()
      for t in b.steps { body += elements(t, parties) }
      body
    }
    let first = s.branches.at(0)
    let rest = ()
    for b in s.branches.slice(1) {
      rest += (b.guard, body-of(b))
    }
    _alt(first.guard, body-of(first), ..rest)
  }
}

/// Draws a sequence diagram. The participants are spread evenly across the
/// text width: two parties at the left and right edges, three at 0%, 50% and
/// 100%, and so on. Notes on an edge party sit inside the diagram, so no
/// margin is needed.
#let seq-diagram(parties, steps) = layout(size => {
  let n = parties.len()
  let gap = if n > 1 { size.width / (n - 1) } else { 0pt }
  let els = ()
  for p in parties { els += _par(p) }
  for i in range(n - 1) {
    els += _col(parties.at(i), parties.at(i + 1), width: gap)
  }
  for s in steps { els += elements(s, parties) }
  diagram(els)
})
