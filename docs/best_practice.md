# Best practices for repository notes

[Documentation](README.md)

Write notes that help the next person or agent understand a topic, make a
decision, or continue the work without reconstructing the original conversation.
A useful note explains what we know, why we believe it, and what remains uncertain.
It should contain enough context, code, and evidence to stand on its own.

These are suggested conventions, not a required schema. Use ordinary Markdown,
omit sections that add nothing, and keep small notes small.

## Organize by the kind of record

Under your configured notes directory, usually `.agent/notes/`, use these
directories when needed:

| Directory | Purpose | Example filename |
| --- | --- | --- |
| `design/` | Proposals, architectural decisions, tradeoffs, and intended behavior | `worker-pool-scaling.md` |
| `implementation/` | How things are built, important code, constraints, and operational details | `worker-launch-sequence.md` |
| `performance/` | Benchmarks, bottlenecks, optimization experiments, and results | `worker-startup-latency.md` |
| `investigations/` | Debugging, exploratory research, questions, and findings | `intermittent-worker-timeouts.md` |

These are different kinds of records, not stages a file moves through. A design
stays in `design/` after implementation. Update its status and describe deviations
from the proposal. An investigation stays in `investigations/` after resolution;
put the conclusion near the top.

Create a separate implementation or performance note only when it adds useful
detail, and link related notes. A small change may need just one design note
with an implementation section. Avoid maintaining the same explanation in
several places.

Start flat if the notebook is small. Create directories as useful topics emerge,
keep nesting shallow, and use descriptive filenames. Prefer
`worker-startup-latency.md` to `findings.md` or `notes-final-v2.md`. Choose paths
deliberately: Notion exposes directories as nested pages, and ShyNote does not
currently synchronize renames or moves.

An optional root `README.md` can explain the notebook's scope and point to a few
important notes. It does not need to repeat the entire directory listing.

## Give readers a useful starting point

Near the top of each note, include:

- **A descriptive title.** Name the component and the question, behavior, or decision.
- **A summary.** State the conclusion or current proposal in a few sentences.
- **A status.** Use a meaningful label such as proposed, implemented, investigating,
  resolved, or superseded. Distinguish an accepted design from a shipped implementation.
- **Scope and freshness.** Identify relevant components and assumptions. Include
  the date, commit, environment, or dependency version when findings depend on it.

For an unresolved question, summarize what is known and what is still blocking
a conclusion. A recent edit date is not evidence that a finding was reverified;
state what was checked and against which version.

## Tailor the body to the subject

| Kind of note | Useful contents |
| --- | --- |
| Design | Problem, requirements, proposed approach, concrete examples, alternatives, tradeoffs, decision, and open questions |
| Implementation | Current behavior, relevant code and configuration, interfaces and invariants, failure handling, limitations, and how to verify or operate it |
| Performance | Workload, environment, baseline, measurement commands, changes tested, results, variability, and correctness or resource tradeoffs |
| Investigation | Question or symptom, reproduction, evidence, hypotheses tested, failed approaches, conclusion, or the next useful experiment |

Treat this as a selection of useful sections, not a form to fill out. An
investigation that is still open should explain which experiment could distinguish
the remaining hypotheses. A performance claim should include enough measurement
detail to reproduce or challenge it.

## Include the code needed to understand the finding

Include the relevant function, condition, data structure, command, or configuration
in the note. Give enough surrounding context to explain its behavior. Readers
should not have to open several source files just to understand the main point.

Identify the source file and symbol alongside the excerpt. Add a commit when the
exact version matters. References support verification and further exploration;
they do not replace the explanation or code excerpt. Line numbers can help
navigation, but paths and symbol names remain useful when lines shift.

For proposed changes, show a concrete example or before/after snippet. Label
proposed code, pseudocode, and shortened excerpts clearly. Preserve conditions
and error handling that affect the conclusion. Copy an entire file when its full
contents are relevant; otherwise include the portions needed for the explanation.

For example, an investigation into duplicate retries could include the current
retry loop, explain which operations it repeats, and show a proposed loop that
retries only the failed request. The explanation should connect the code to the
observed behavior rather than leaving readers to infer the connection.

## Make evidence and uncertainty explicit

Separate observations from explanations and proposals. “The test reproduced the
failure twice” is evidence; “the shared cache caused it” may still be a hypothesis.
Say what was actually tested, what passed or failed, and what remains unverified.

Include reproduction commands, relevant output, and measurement conditions.
Preserve the details needed to interpret the result, without dumping unrelated
logs. Remove credentials and sensitive values from examples and captured output.

Record failed approaches when they could prevent repeated work. Explain the
failure and the conditions under which it occurred. A failed experiment may rule
out one explanation without proving another.

## Keep the current answer easy to find

Lead with the result, then explain the reasoning and evidence. Use chronology
only when the sequence itself matters. A note should not require reading an
entire session transcript to find the conclusion.

When conclusions change, update the summary and status. Preserve earlier reasoning
when it remains useful, but label it as historical. Mark superseded notes and
link to their replacements instead of leaving contradictory guidance unexplained.

Prefer updating the existing note about a topic over creating another near-copy.
Keep substantial proposals, findings, and handoff context in the shared notebook.
Temporary reasoning and short-lived task checklists can remain local scratch work.

Before publishing, check that a reader can understand the main point without the
original conversation, see the code or evidence supporting it, and tell which
parts are established and which still need work.
