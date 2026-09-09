# FreeCAD community support and project status

Use these official channels as external research sources when the live MCP tools, skill references, and a minimal reproduction do not answer a question. The available FreeCAD MCP tools do not post to these sites. Use `read` for public research and browser automation only when an authenticated interactive action is explicitly authorized; otherwise return a draft diagnostic report without submitting it.

## Choose the research source

| Need | Source |
|---|---|
| Usage, modeling, workbench, or troubleshooting question | [FreeCAD Forum](https://forum.freecad.org/) |
| Community question, proposal, or developer conversation | [FreeCAD GitHub Discussions](https://github.com/FreeCAD/FreeCAD/discussions) |
| Reproducible software bug or scoped feature request | [FreeCAD GitHub Issues](https://github.com/FreeCAD/FreeCAD/issues) |
| Version-specific changes, regressions, and fixes | [FreeCAD GitHub Releases](https://github.com/FreeCAD/FreeCAD/releases) |

Research existing topics, discussions, issues, and release notes before drafting a new report. Do not classify an ordinary support question as a software issue without reproducible evidence.

## Access with available tools

These are known public URLs; read them directly rather than using general web search first:

```text
https://forum.freecad.org/
https://github.com/FreeCAD/FreeCAD/issues
https://github.com/FreeCAD/FreeCAD/discussions
https://github.com/FreeCAD/FreeCAD/releases
```

Use `read` on the forum URL to inspect its public index/feed, then follow returned topic URLs for specific evidence. The forum is phpBB and may expose an alternate feed. Do not claim that a topic was searched, posted, authenticated, or attached unless the corresponding tool operation returned that evidence.

Use `read` on the GitHub Issues and Discussions URLs to inspect current public lists. For targeted research, follow canonical issue/discussion URLs or use URL filters returned by GitHub. Preserve repository owner/name `FreeCAD/FreeCAD` and distinguish issue numbers from discussion numbers.

Use `read` on the Releases URL to inspect current tags and release notes. Follow the exact tag URL for the release under investigation. The release Atom feed is also available at `https://github.com/FreeCAD/FreeCAD/releases.atom`. Use release notes to test whether behavior is version-specific; do not equate a weekly/development build with a stable release.

## Build a diagnostic packet from MCP state

Before drafting a question or issue, collect a machine-readable packet from the live session:

- FreeCAD version and platform;
- workbench/module involved;
- exact MCP tool name and synchronous code snippet;
- document/object internal `Name`, `Label`, and `TypeId`;
- object state, shape validity, dimensions, and a minimal reproduction file when safe;
- expected and actual result;
- complete error text, including `GUI_DISPATCH_STUCK` if present;
- steps exercised and relevant release/tag.

Use `inspect_objects`, `validate_geometry`, `run_script`, and `discover_capabilities` to collect these fields. Redact credentials, tokens, private paths, customer data, and unrelated geometry before producing a draft. A reduced `.FCStd` or deterministic script is generally more useful than a private production model.

## Draft issue or discussion content

When generating a report for later submission:

1. Research open and closed issues for duplicates.
2. Check release notes and recent discussions for an existing fix or known regression.
3. Include exact FreeCAD version, OS, build type, and MCP/tool context.
4. Provide deterministic reproduction steps and a minimal file/script.
5. Separate observed facts from hypotheses; include expected versus actual behavior.
6. Preserve project-provided template, label, and security-report requirements if browser automation is authorized.
7. Never expose secrets or attach a user file without explicit authorization.

Use Discussions for questions/proposals that need community feedback. Recommend an Issue only when behavior is reproducible and actionable or maintainers request it. The final output should state whether it is research evidence, an unsent draft, or a successfully submitted report; never imply submission from text generation alone.

## Sources

- [FreeCAD Forum](https://forum.freecad.org/)
- [FreeCAD Issues](https://github.com/FreeCAD/FreeCAD/issues)
- [FreeCAD Discussions](https://github.com/FreeCAD/FreeCAD/discussions)
- [FreeCAD Releases](https://github.com/FreeCAD/FreeCAD/releases)
- [FreeCAD release Atom feed](https://github.com/FreeCAD/FreeCAD/releases.atom)
- [FreeCAD repository](https://github.com/FreeCAD/FreeCAD)
