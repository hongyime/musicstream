from __future__ import annotations

import sys
from datetime import datetime
from typing import Any, Protocol, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.prompt import Confirm
from rich.table import Table

console = Console()


class _SourceTypeLike(Protocol):
    value: str


class _SourceLike(Protocol):
    name: str
    source_type: _SourceTypeLike
    last_scraped_at: datetime | None


def print_header(title: str) -> None:
    """Print a section header using rich Panel with bold cyan title."""
    panel = Panel(
        "",
        title=f"[bold cyan]{title}[/bold cyan]",
        title_align="center",
        border_style="cyan",
    )
    console.print(panel)


def print_success(msg: str) -> None:
    """Print a green checkmark message."""
    console.print(f"  [green]✓[/green] {msg}")


def print_warning(msg: str) -> None:
    """Print a yellow warning message."""
    console.print(f"  [yellow]![/yellow] {msg}")


def print_error(msg: str) -> None:
    """Print a red error message."""
    console.print(f"  [red]✗[/red] {msg}")


def print_summary(stats: dict[str, int]) -> None:
    """Print a summary table of stats (key-value pairs) using rich Table.

    Table has two columns: 'Status' and 'Count'.
    """
    color_map: dict[str, str] = {
        "pending": "yellow",
        "resolved": "cyan",
        "downloading": "blue",
        "downloaded": "green",
        "failed": "red",
        "failed_validation": "red",
        "total": "bold white",
    }

    table = Table(title="Summary")
    table.add_column("Status", style="bold")
    table.add_column("Count", justify="right")

    for status, count in stats.items():
        style = color_map.get(status, "white")
        table.add_row(f"[{style}]{status}[/{style}]", f"[{style}]{count}[/{style}]")

    console.print(table)


def print_sources_table(sources: Sequence[Any]) -> None:
    """Print sources as a rich Table with columns: Name, Type, Last Scraped.

    Format datetime as 'YYYY-MM-DD HH:MM' or 'never' if None.
    """
    table = Table(title="Sources")
    table.add_column("Name", style="bold")
    table.add_column("Type", style="cyan")
    table.add_column("Last Scraped", style="magenta")

    encoding = sys.stdout.encoding or "utf-8"
    for source in sources:
        last_scraped = (
            source.last_scraped_at.strftime("%Y-%m-%d %H:%M")
            if source.last_scraped_at is not None
            else "never"
        )
        safe_name = source.name.encode(encoding, errors="replace").decode(encoding)
        table.add_row(safe_name, source.source_type.value, last_scraped)

    console.print(table)


def create_progress() -> Progress:
    """Return a configured Progress instance for shared console output."""
    return Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TaskProgressColumn(text_format="[{task.completed}/{task.total}]"),
        TimeRemainingColumn(),
        TimeElapsedColumn(),
        console=console,
    )


def confirm_resume(command: str, completed: int, total: int) -> bool:
    """Prompt whether to continue from interrupted progress."""
    console.print(
        Panel(
            f"Previous {command} was interrupted at {completed}/{total}.",
            border_style="yellow",
        )
    )
    return Confirm.ask("Continue from where you left off?")


def print_interrupted(command: str, completed: int, total: int) -> None:
    """Print a styled interruption panel with resume guidance."""
    console.print(
        Panel(
            "\n".join(
                [
                    f"⏸ {command} interrupted! Progress saved. {completed}/{total} completed.",
                    "Run the same command to resume, or use --fresh to start over.",
                ]
            ),
            border_style="yellow",
        )
    )


def print_fresh_start(command: str) -> None:
    """Print message indicating fresh start with reset progress."""
    console.print(f"🔄 Starting {command} fresh — previous progress reset.")


__all__ = [
    "console",
    "print_header",
    "print_success",
    "print_warning",
    "print_error",
    "print_summary",
    "print_sources_table",
    "create_progress",
    "confirm_resume",
    "print_interrupted",
    "print_fresh_start",
    "print_daemon_banner",
    "print_integrity_result",
]


def print_daemon_banner(
    last_run_str: str,
    downloaded: int,
    failed: int,
    requeued: int,
    db_tracks: int,
    missing: int,
    corrupt: int,
    lb_total: int,
    lb_ingested: int,
    errors_mb: float,
) -> None:
    """Print the daemon startup banner using rich Panel (PRD §13.3 format).

    Renders a box matching the PRD §13.3 layout:

        ╔════════════════════════════════════════════════════╗
        ║         MUSICSTREAM DAEMON v3.0                    ║
        ╠════════════════════════════════════════════════════╣
        ║ Last full run:   2026-04-22 03:00 SGT              ║
        ║ Downloaded:  44  │  Failed:    1  │  Requeued:  0  ║
        ║ DB tracks:  4219  │  Missing:   0  │  Corrupt:  0  ║
        ║ LB recs:    200   │  Ingested: 12                  ║
        ║ errors.log: 1.2MB / 5MB                            ║
        ╚════════════════════════════════════════════════════╝
    """
    lines = [
        "[bold cyan]MUSICSTREAM DAEMON v3.0[/bold cyan]",
        "",
        f"Last full run:   {last_run_str}",
        (
            f"Downloaded: {downloaded:>3}  │  Failed: {failed:>4}  │  Requeued: {requeued:>2}"
        ),
        (
            f"DB tracks: {db_tracks:>5}  │  Missing: {missing:>3}  │  Corrupt: {corrupt:>2}"
        ),
        f"LB recs:   {lb_total:>5}  │  Ingested: {lb_ingested:>2}",
        f"errors.log: {errors_mb:.1f}MB / 5MB",
    ]

    panel = Panel(
        "\n".join(lines),
        border_style="cyan",
        padding=(0, 2),
    )
    console.print(panel)


def print_integrity_result(result: Any) -> None:
    """Print an IntegrityResult summary using rich Table.

    Expects *result* to have attributes: total_checked, ok, missing, corrupt.
    """
    table = Table(title="Integrity Check Results")
    table.add_column("Metric", style="bold")
    table.add_column("Count", justify="right")

    total = getattr(result, "total_checked", 0)
    ok = getattr(result, "ok", 0)
    missing = getattr(result, "missing", 0)
    corrupt = getattr(result, "corrupt", 0)

    table.add_row("[white]Total checked[/white]", f"[white]{total}[/white]")
    table.add_row("[green]OK[/green]", f"[green]{ok}[/green]")
    table.add_row("[yellow]Missing[/yellow]", f"[yellow]{missing}[/yellow]")
    table.add_row("[red]Corrupt[/red]", f"[red]{corrupt}[/red]")

    console.print(table)
