"""
Centralized evidence gathering for E2E tests.

Collects and organizes test evidence including JSON data, screenshots,
container logs, and database snapshots. Produces an INDEX.md manifest
listing all evidence files with requirement mapping.

Requirements: R29.1, R29.2, R29.3, R29.4, R29.5, R29.6
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class EvidenceCollector:
    """Collects and organizes test evidence for E2E test runs."""

    def __init__(self, base_dir: Path):
        """
        Initialize the evidence collector.

        Creates the e2e-evidence/ directory at workspace root if it doesn't exist.

        Args:
            base_dir: The workspace root directory. Evidence will be stored
                      in base_dir / "e2e-evidence/".
        """
        self.workspace_root = base_dir
        self.evidence_dir = base_dir / "e2e-evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self._files: list[dict[str, Any]] = []

    def emit_json(self, requirement_id: str, filename: str, data: dict) -> Path:
        """
        Write structured JSON evidence to the evidence directory.

        Args:
            requirement_id: The requirement this evidence validates (e.g. "R29.1").
            filename: The output filename (e.g. "01-preflight.json").
            data: The structured data to serialize as JSON.

        Returns:
            Path to the written evidence file.
        """
        filepath = self.evidence_dir / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)

        evidence_envelope = {
            "requirement_id": requirement_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }

        filepath.write_text(
            json.dumps(evidence_envelope, indent=2, default=str),
            encoding="utf-8",
        )

        self._register_file(
            path=filepath,
            requirement_id=requirement_id,
            file_type="json",
        )

        return filepath

    def save_screenshot(self, requirement_id: str, filename: str, screenshot_bytes: bytes) -> Path:
        """
        Save a PNG screenshot to the evidence directory.

        Args:
            requirement_id: The requirement this evidence validates (e.g. "R3.3").
            filename: The output filename (e.g. "03-dashboard-initial.png").
            screenshot_bytes: Raw PNG bytes of the screenshot.

        Returns:
            Path to the saved screenshot file.
        """
        filepath = self.evidence_dir / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)

        filepath.write_bytes(screenshot_bytes)

        self._register_file(
            path=filepath,
            requirement_id=requirement_id,
            file_type="png",
        )

        return filepath

    def capture_container_logs(self, service: str, lines: int = 200) -> str:
        """
        Capture the last N lines from a Docker container via docker compose logs.

        Args:
            service: The compose service name (e.g. "automation-service").
            lines: Number of tail lines to capture (default 200).

        Returns:
            The captured log output as a string. Returns an error message
            if the command fails.
        """
        try:
            result = subprocess.run(
                ["docker", "compose", "logs", "--tail", str(lines), service],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(self.workspace_root / "platform"),
            )
            log_output = result.stdout or result.stderr or "(no output)"
        except subprocess.TimeoutExpired:
            log_output = f"[ERROR] Timeout capturing logs for service '{service}'"
        except FileNotFoundError:
            log_output = f"[ERROR] docker compose not found when capturing logs for '{service}'"
        except Exception as e:
            log_output = f"[ERROR] Failed to capture logs for '{service}': {e}"

        # Save the log to a file as well
        log_filename = f"{service}-logs-{int(time.time())}.log"
        log_path = self.evidence_dir / log_filename
        log_path.write_text(log_output, encoding="utf-8")

        self._register_file(
            path=log_path,
            requirement_id="container-logs",
            file_type="log",
        )

        return log_output

    def capture_db_snapshot(self, query: str) -> dict:
        """
        Execute a psql query via docker compose exec and return result as dict.

        Runs the query against the postgres container using docker compose exec.

        Args:
            query: SQL query to execute (e.g. "SELECT * FROM automation.audit_events LIMIT 10").

        Returns:
            A dict with keys:
                - "query": the executed query
                - "timestamp": ISO timestamp of execution
                - "rows": list of row dicts (column_name -> value)
                - "row_count": number of rows returned
                - "error": error message if query failed, None otherwise
        """
        snapshot: dict[str, Any] = {
            "query": query,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "rows": [],
            "row_count": 0,
            "error": None,
        }

        try:
            # Use psql with CSV output for easier parsing
            psql_cmd = [
                "docker", "compose", "exec", "-T", "postgres",
                "psql", "-U", "postgres", "-d", "automation",
                "--csv", "-c", query,
            ]

            result = subprocess.run(
                psql_cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(self.workspace_root / "platform"),
            )

            if result.returncode != 0:
                snapshot["error"] = result.stderr.strip() or f"psql exited with code {result.returncode}"
                return snapshot

            # Parse CSV output: first line is headers, rest are data rows
            output_lines = result.stdout.strip().split("\n")
            if len(output_lines) >= 1:
                headers = output_lines[0].split(",")
                rows = []
                for line in output_lines[1:]:
                    if line.strip():
                        values = line.split(",")
                        row = dict(zip(headers, values))
                        rows.append(row)
                snapshot["rows"] = rows
                snapshot["row_count"] = len(rows)

        except subprocess.TimeoutExpired:
            snapshot["error"] = "Timeout executing database query"
        except FileNotFoundError:
            snapshot["error"] = "docker compose not found"
        except Exception as e:
            snapshot["error"] = str(e)

        return snapshot

    def generate_index(self) -> Path:
        """
        Produce e2e-evidence/INDEX.md listing all evidence files with
        requirement mapping, sizes, and timestamps.

        Returns:
            Path to the generated INDEX.md file.
        """
        index_path = self.evidence_dir / "INDEX.md"

        # Refresh file list from disk to catch any files not registered via methods
        self._scan_evidence_directory()

        lines = [
            "# E2E Evidence Index",
            "",
            f"Generated: {datetime.now(timezone.utc).isoformat()}",
            "",
            f"Total files: {len(self._files)}",
            f"Total size: {self._total_size_str()}",
            "",
            "## Evidence Files",
            "",
            "| File | Requirement | Type | Size | Timestamp |",
            "|------|-------------|------|------|-----------|",
        ]

        for entry in sorted(self._files, key=lambda f: f["path"]):
            rel_path = entry["path"]
            req_id = entry["requirement_id"]
            file_type = entry["file_type"]
            size = self._format_size(entry["size_bytes"])
            timestamp = entry["timestamp"]
            lines.append(f"| `{rel_path}` | {req_id} | {file_type} | {size} | {timestamp} |")

        lines.append("")
        lines.append("---")
        lines.append(f"*Index generated by EvidenceCollector at {datetime.now(timezone.utc).isoformat()}*")
        lines.append("")

        index_path.write_text("\n".join(lines), encoding="utf-8")
        return index_path

    # ─── Private helpers ───────────────────────────────────────────────

    def _register_file(self, path: Path, requirement_id: str, file_type: str) -> None:
        """Register a file in the internal manifest."""
        try:
            size = path.stat().st_size
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
        except OSError:
            size = 0
            mtime = datetime.now(timezone.utc).isoformat()

        rel_path = str(path.relative_to(self.evidence_dir))

        # Avoid duplicates
        for existing in self._files:
            if existing["path"] == rel_path:
                existing["size_bytes"] = size
                existing["timestamp"] = mtime
                return

        self._files.append({
            "path": rel_path,
            "requirement_id": requirement_id,
            "file_type": file_type,
            "size_bytes": size,
            "timestamp": mtime,
        })

    def _scan_evidence_directory(self) -> None:
        """Scan the evidence directory and register any untracked files."""
        if not self.evidence_dir.exists():
            return

        known_paths = {entry["path"] for entry in self._files}

        for filepath in self.evidence_dir.rglob("*"):
            if filepath.is_file() and filepath.name != "INDEX.md":
                rel_path = str(filepath.relative_to(self.evidence_dir))
                if rel_path not in known_paths:
                    # Infer file type from extension
                    ext = filepath.suffix.lstrip(".")
                    file_type = ext if ext in ("json", "png", "har", "log", "md") else "other"

                    self._register_file(
                        path=filepath,
                        requirement_id="unknown",
                        file_type=file_type,
                    )

    def _total_size_str(self) -> str:
        """Calculate total size of all evidence files as human-readable string."""
        total = sum(entry["size_bytes"] for entry in self._files)
        return self._format_size(total)

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """Format byte count as human-readable string."""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
