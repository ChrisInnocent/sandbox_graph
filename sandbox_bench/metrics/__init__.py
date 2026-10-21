"""Versioned pure benchmark statistics (awk capture is the explicit I/O adapter)."""
from .latency import summarize_latency
from .throughput import throughput
from .capacity import projection, capture_awk_rounding
from .resources import cpu_intervals, cpu_segments, cpu_summary, resource_mean, cohort_pss
from .quality import summarize_quality, summarize_repeats

__all__=['summarize_latency','throughput','projection','capture_awk_rounding',
         'cpu_intervals','cpu_segments','cpu_summary','resource_mean','cohort_pss',
         'summarize_quality','summarize_repeats']
