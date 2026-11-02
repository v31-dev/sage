---
name: release-notes
description: Write Sage's release notes, including the technical notes in the GitHub release body. Use when preparing a release or when asked for release notes.
---

# Release Notes

- The release notes are the GitHub release body. Settings → Upgrade renders them as Markdown with raw HTML allowed (sanitized), so the collapsible block works. Put every code identifier, path and `<placeholder>` in backticks: unquoted, `<name>` is read as an HTML tag and disappears.
- The draft release PR's body is GitHub's generated notes followed by `## Developer Notes` holding `None`, and `publish.yml` puts that body under `## Docker Image` in the release. Not every release needs notes of its own; when one does, replace `None` with them. `## Docker Image`, `## What's Changed` and `## Developer Notes` are the only `##` headings: everything written under Developer Notes starts at `###`.
- For every release, generate technical release notes against the previous release: list the files changed since its tag (`git diff <previous tag> --stat`), then read the diffs. Cover config and env vars, startup order, endpoints added and removed, data and file formats (backups, S3 keys), the task queue, the Cloudflare and Tailscale resources Sage creates, and the image and CI. Leave test status out of the notes. The Upgrade page also renders GitHub alerts (`> [!IMPORTANT]`, `[!NOTE]`, `[!TIP]`, `[!WARNING]`, `[!CAUTION]`) as highlighted blocks; installs older than the release that added this show the marker as plain text, so write the alert so it still reads well that way. Links into the repo (README sections, docs) point at the release tag, `blob/v<version>/…`, not at `main`, so they keep describing that release. Give names, constants and formats exactly, so a reviewer can check each against the code.
- Put them at the end of the release notes in a collapsible block (`<details><summary><h3 style="display:inline">Technical notes</h3></summary> … </details>`) with its sections as `####` headings (GitHub inlines a heading in a summary by itself; the style keeps it beside the marker in plain renderers). The user-facing notes above it stay short.
