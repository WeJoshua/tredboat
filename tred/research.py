"""Process limits for CLI research; never applied to the paper runner."""
from contextlib import contextmanager
import math
import os
import resource
import signal


class ResearchLimitError(RuntimeError):
    pass


@contextmanager
def scan_limits(cpu_budget=1200, wall_seconds=86400):
    """Low-priority long-window work: one CPU, nice 19, 512 MiB, cumulative CPU.

    Uses a generous wall alarm instead of the 60-second research limit so a
    multi-hour chunked scan can run without ever stealing a core from the PC.
    """
    os.nice(max(0, 19 - os.nice(0)))
    os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    old_memory = resource.getrlimit(resource.RLIMIT_AS)
    old_cpu = resource.getrlimit(resource.RLIMIT_CPU)
    memory = min(512 * 1024 * 1024, old_memory[0]) if old_memory[0] != resource.RLIM_INFINITY else 512 * 1024 * 1024
    usage = resource.getrusage(resource.RUSAGE_SELF)
    cpu = math.ceil(usage.ru_utime + usage.ru_stime) + cpu_budget
    if old_cpu[0] != resource.RLIM_INFINITY:
        cpu = min(cpu, old_cpu[0])

    def exceeded(signum, frame):
        raise ResearchLimitError("Scan CPU budget reached; reduce the window or split it")

    previous_alarm = signal.signal(signal.SIGALRM, exceeded)
    previous_cpu = signal.signal(signal.SIGXCPU, exceeded)
    try:
        resource.setrlimit(resource.RLIMIT_AS, (memory, old_memory[1]))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, old_cpu[1]))
        signal.alarm(wall_seconds)
        yield
    except MemoryError:
        raise ResearchLimitError("Scan memory limit reached; reduce the window or split it") from None
    finally:
        signal.alarm(0)
        resource.setrlimit(resource.RLIMIT_AS, old_memory)
        resource.setrlimit(resource.RLIMIT_CPU, old_cpu)
        signal.signal(signal.SIGALRM, previous_alarm)
        signal.signal(signal.SIGXCPU, previous_cpu)


@contextmanager
def research_limits(wall_seconds=60):
    """One low-priority CPU, 512 MiB address space, 30 CPU / wall seconds.

    Intended for one-shot CLI processes. CPU affinity and niceness deliberately
    remain lowered until process exit; no worker pool is created.
    """
    if type(wall_seconds) is not int or wall_seconds < 1:
        raise ValueError("wall_seconds must be a positive integer")
    os.nice(max(0, 19 - os.nice(0)))
    os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    old_memory = resource.getrlimit(resource.RLIMIT_AS)
    old_cpu = resource.getrlimit(resource.RLIMIT_CPU)
    memory = min(512 * 1024 * 1024, old_memory[0]) if old_memory[0] != resource.RLIM_INFINITY else 512 * 1024 * 1024
    usage = resource.getrusage(resource.RUSAGE_SELF)
    cpu = math.ceil(usage.ru_utime + usage.ru_stime) + 30
    if old_cpu[0] != resource.RLIM_INFINITY:
        cpu = min(cpu, old_cpu[0])
    def exceeded(signum, frame):
        raise ResearchLimitError("Research time limit reached; choose a shorter --start/--end window")
    previous_alarm = signal.signal(signal.SIGALRM, exceeded)
    previous_cpu = signal.signal(signal.SIGXCPU, exceeded)
    try:
        resource.setrlimit(resource.RLIMIT_AS, (memory, old_memory[1]))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, old_cpu[1]))
        signal.alarm(wall_seconds)
        yield
    except MemoryError:
        raise ResearchLimitError("Research memory limit reached; choose a shorter --start/--end window") from None
    finally:
        signal.alarm(0)
        resource.setrlimit(resource.RLIMIT_AS, old_memory)
        resource.setrlimit(resource.RLIMIT_CPU, old_cpu)
        signal.signal(signal.SIGALRM, previous_alarm)
        signal.signal(signal.SIGXCPU, previous_cpu)
