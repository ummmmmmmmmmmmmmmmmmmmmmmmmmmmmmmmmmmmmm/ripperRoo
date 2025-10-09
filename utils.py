import re
import time

def progress_bar(percent: float, length: int = 10) -> str:
    """Return a visual progress bar."""
    filled = int(length * percent)
    return "[" + "■" * filled + "□" * (length - filled) + f"] {int(percent * 100)}%"

def validate_link(link: str, allowed_domains: set[str]) -> bool:
    """Simple URL domain check."""
    return any(domain in link for domain in allowed_domains)

def simulate_rip_process(total: int, callback):
    """Simulate ripping process with callback updates."""
    for i in range(total):
        time.sleep(1)
        callback(i + 1, total)
