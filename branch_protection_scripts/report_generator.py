"""Report generator: produces migration and validation reports in multiple formats."""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .migration_engine import MigrationResult
from .validation_engine import ValidationResult

log = logging.getLogger(__name__)


class ReportGenerator:
    """Generates migration and validation reports in CSV, JSON, and Markdown."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        report_config = config.get("reporting", {})
        self.output_dir = Path(report_config.get("output_dir", "reports"))
        self.formats = report_config.get("formats", ["json", "csv", "markdown"])
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_migration_report(self, results: list[MigrationResult]) -> dict[str, Path]:
        """Generate migration report in configured formats.

        Returns:
            Dict mapping format to output file path.
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        files = {}

        summary = self._build_migration_summary(results)

        if "json" in self.formats:
            path = self.output_dir / f"migration_report_{timestamp}.json"
            self._write_json(path, summary)
            files["json"] = path

        if "csv" in self.formats:
            path = self.output_dir / f"migration_report_{timestamp}.csv"
            self._write_migration_csv(path, results)
            files["csv"] = path

        if "markdown" in self.formats:
            path = self.output_dir / f"migration_report_{timestamp}.md"
            self._write_migration_markdown(path, summary, results)
            files["markdown"] = path

        log.info(f"Migration reports generated: {', '.join(str(p) for p in files.values())}")
        return files

    def generate_validation_report(self, results: list[ValidationResult]) -> dict[str, Path]:
        """Generate validation report in configured formats."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        files = {}

        summary = self._build_validation_summary(results)

        if "json" in self.formats:
            path = self.output_dir / f"validation_report_{timestamp}.json"
            self._write_json(path, summary)
            files["json"] = path

        if "csv" in self.formats:
            path = self.output_dir / f"validation_report_{timestamp}.csv"
            self._write_validation_csv(path, results)
            files["csv"] = path

        if "markdown" in self.formats:
            path = self.output_dir / f"validation_report_{timestamp}.md"
            self._write_validation_markdown(path, summary, results)
            files["markdown"] = path

        log.info(f"Validation reports generated: {', '.join(str(p) for p in files.values())}")
        return files

    def _build_migration_summary(self, results: list[MigrationResult]) -> dict:
        total = len(results)
        success = sum(1 for r in results if r.status == "success")
        failed = sum(1 for r in results if r.status == "failed")
        skipped = sum(1 for r in results if r.status == "skipped")
        dry_run = sum(1 for r in results if r.status == "dry_run")
        total_duration_ms = sum(r.duration_ms for r in results)

        return {
            "summary": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total": total,
                "success": success,
                "failed": failed,
                "skipped": skipped,
                "dry_run": dry_run,
                "total_duration_ms": total_duration_ms,
            },
            "results": [
                {
                    "gitlab_project": r.gitlab_project,
                    "github_repo": r.github_repo,
                    "branch": r.branch,
                    "status": r.status,
                    "message": r.message,
                    "duration_ms": r.duration_ms,
                }
                for r in results
            ],
            "failed_repos": [
                {"gitlab_project": r.gitlab_project, "github_repo": r.github_repo,
                 "branch": r.branch, "error": r.message}
                for r in results if r.status == "failed"
            ],
        }

    def _build_validation_summary(self, results: list[ValidationResult]) -> dict:
        total = len(results)
        passed = sum(1 for r in results if r.status == "pass")
        failed = sum(1 for r in results if r.status == "fail")
        errors = sum(1 for r in results if r.status == "error")

        return {
            "summary": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total": total,
                "passed": passed,
                "failed": failed,
                "errors": errors,
            },
            "results": [
                {
                    "github_repo": r.github_repo,
                    "branch": r.branch,
                    "status": r.status,
                    "message": r.message,
                    "checks": r.checks,
                }
                for r in results
            ],
        }

    def _write_json(self, path: Path, data: dict) -> None:
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    def _write_migration_csv(self, path: Path, results: list[MigrationResult]) -> None:
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["gitlab_project", "github_repo", "branch", "status",
                             "message", "duration_ms"])
            for r in results:
                writer.writerow([r.gitlab_project, r.github_repo, r.branch,
                                 r.status, r.message, r.duration_ms])

    def _write_validation_csv(self, path: Path, results: list[ValidationResult]) -> None:
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["github_repo", "branch", "status", "message", "failed_checks"])
            for r in results:
                failed = [c["name"] for c in r.checks if not c["pass"]]
                writer.writerow([r.github_repo, r.branch, r.status,
                                 r.message, "; ".join(failed)])

    def _write_migration_markdown(self, path: Path, summary: dict,
                                  results: list[MigrationResult]) -> None:
        s = summary["summary"]
        lines = [
            "# Branch Protection Migration Report",
            "",
            f"**Date:** {s['timestamp']}",
            "",
            "## Summary",
            "",
            f"| Metric | Count |",
            f"|--------|-------|",
            f"| Total | {s['total']} |",
            f"| Success | {s['success']} |",
            f"| Failed | {s['failed']} |",
            f"| Skipped | {s['skipped']} |",
            f"| Dry Run | {s['dry_run']} |",
            f"| Total Duration | {s['total_duration_ms']}ms |",
            "",
        ]

        if summary["failed_repos"]:
            lines.extend([
                "## Failed Repositories",
                "",
                "| GitLab Project | GitHub Repo | Branch | Error |",
                "|---------------|-------------|--------|-------|",
            ])
            for f in summary["failed_repos"]:
                lines.append(f"| {f['gitlab_project']} | {f['github_repo']} | {f['branch']} | {f['error']} |")
            lines.append("")

        lines.extend([
            "## All Results",
            "",
            "| GitLab Project | GitHub Repo | Branch | Status | Duration |",
            "|---------------|-------------|--------|--------|----------|",
        ])
        for r in results:
            lines.append(f"| {r.gitlab_project} | {r.github_repo} | {r.branch} | {r.status} | {r.duration_ms}ms |")

        path.write_text("\n".join(lines))

    def _write_validation_markdown(self, path: Path, summary: dict,
                                   results: list[ValidationResult]) -> None:
        s = summary["summary"]
        lines = [
            "# Branch Protection Validation Report",
            "",
            f"**Date:** {s['timestamp']}",
            "",
            "## Summary",
            "",
            f"| Metric | Count |",
            f"|--------|-------|",
            f"| Total | {s['total']} |",
            f"| Passed | {s['passed']} |",
            f"| Failed | {s['failed']} |",
            f"| Errors | {s['errors']} |",
            "",
            "## Results",
            "",
            "| GitHub Repo | Branch | Status | Details |",
            "|-------------|--------|--------|---------|",
        ]
        for r in results:
            lines.append(f"| {r.github_repo} | {r.branch} | {r.status} | {r.message} |")

        # Detail failed checks
        failed_results = [r for r in results if r.status == "fail"]
        if failed_results:
            lines.extend(["", "## Failed Check Details", ""])
            for r in failed_results:
                lines.append(f"### {r.github_repo} / {r.branch}")
                lines.append("")
                lines.append("| Check | Expected | Actual |")
                lines.append("|-------|----------|--------|")
                for c in r.checks:
                    if not c["pass"]:
                        lines.append(f"| {c['name']} | {c['expected']} | {c['actual']} |")
                lines.append("")

        path.write_text("\n".join(lines))
