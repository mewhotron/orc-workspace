"""Public deterministic personal training reports; no model calls or advice."""

from .service import (
    REPORT_VERSION, PersonalTrainingReport, TrainingReportError,
    build_personal_training_report, read_personal_training_report,
)
from .rendering import render_training_report_markdown, render_training_report_json

__all__ = [
    'REPORT_VERSION', 'PersonalTrainingReport', 'TrainingReportError',
    'build_personal_training_report', 'read_personal_training_report',
    'render_training_report_markdown', 'render_training_report_json',
]
