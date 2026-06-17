"""Shared pause, resume, and cancel primitives for long-running tasks."""
import threading

# --- Threading Control Events ---
paused = threading.Event()
cancel_event = threading.Event()
paused.set()

class OperationCancelledError(Exception):
    """Custom exception to indicate a user-cancelled operation."""
    pass

def pause_activity(log_callback=None):
    """Pauses the current scraping or analysis activity."""
    paused.clear()
    if log_callback:
        log_callback("Activity paused.")

def resume_activity(log_callback=None):
    """Resumes the current scraping or analysis activity."""
    paused.set()
    if log_callback:
        log_callback("Activity resumed.")

def cancel_activity(log_callback=None):
    """Flags the current activity for cancellation."""
    cancel_event.set()
    if log_callback:
        log_callback("Cancellation requested. Finishing current step...")

def reset_cancel_flag():
    """Resets the cancellation flag for a new operation."""
    cancel_event.clear()
