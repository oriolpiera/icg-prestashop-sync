# OpenSpec

This directory is the git-tracked home for shared planning artifacts in `icg-prestashop-sync`.

Use it when the team needs a reviewable artifact trail for changes such as:
- proposals
- specifications
- design notes
- implementation tasks

## Workflow modes

| Mode | Use when | Storage |
|------|----------|---------|
| `engram` | Fast personal or agent working memory is enough | Engram only |
| `openspec` | The team needs shareable planning artifacts in git | `openspec/` only |
| `hybrid` | You want both shareable files and cross-session memory | `openspec/` + Engram |

Default rule:
- use `engram` for transient working memory and execution notes
- use `openspec` for change artifacts other people should review in git
- use `hybrid` for larger changes where both matter

## Directory layout

```text
openspec/
  README.md
  changes/
    README.md
    templates/
      proposal.md
      spec.md
      design.md
      tasks.md
```

## How to add a new change

1. Create a new folder under `openspec/changes/<change-name>/`.
2. Copy the templates you need into that folder.
3. Fill only the artifacts that make sense for the change.
4. Keep the file names predictable: `proposal.md`, `spec.md`, `design.md`, `tasks.md`.

Example:

```text
openspec/changes/catalog-sync-retry/
  proposal.md
  spec.md
  design.md
  tasks.md
```

## Naming guidance

- use lowercase kebab-case change names
- name the folder after the outcome, not the implementation detail
- keep one change per folder

## Relationship with AGENTS.md

`AGENTS.md` defines repository working agreements.
`openspec/` stores the change-specific planning artifacts that follow those agreements.
