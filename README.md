# AI Change Impact Analyzer

An AI-powered developer tool that analyzes GitHub code changes and identifies what parts of a software system may be affected.

The system analyzes commits and Pull Requests using Python static code analysis, AST-based dependency mapping, and AI-assisted reasoning. It provides an impact map, risk assessment, affected components, test recommendations, and a downloadable report.

## How It Works

```text
GitHub Repository
        ↓
Commit / Pull Request
        ↓
Change Detection
        ↓
AST + Dependency Analysis
        ↓
Impact Mapping
        ↓
AI Risk Analysis
        ↓
Test Recommendations
        ↓
Impact Report

## Project Documentation Generator

Open the **Documentation** tab, paste a GitHub repository URL and click **Generate documentation**. The tool shallow-clones the repository, inspects it, and produces a structured document with:

- Overview and purpose (what the project is and what problem it solves)
- Key features, tech stack (languages by share of code, frameworks, tooling)
- Architecture, entry points and an annotated project structure tree
- Components / modules reference, detected API endpoints, environment variables
- Getting started (prerequisites, install and run commands), scripts, testing, CI/CD
- Dependencies, project statistics and notes

Download it as **Markdown** or **HTML** (the HTML view is print-friendly for PDF). Generated docs are stored in the local history database and can be reopened or deleted.

Works for Python, JavaScript/TypeScript, Go, Rust, Java, Ruby and PHP projects. With `ANTHROPIC_API_KEY` set, Claude writes the overview, purpose, features and architecture from the repository facts; without it, a rule-based draft is built from the README, manifests and code.

API: `POST /api/docs` (`{"repo": "owner/repo"}`), `GET /api/docs`, `GET /api/docs/{id}`, `GET /api/docs/{id}/file?format=md|html&download=true`, `DELETE /api/docs/{id}`.
