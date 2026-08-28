"""Project servisleri (EPIC 1).

Inventory servisleri ayrı bir domain paketindedir: ``app.services.inventories``.
"""

from app.services.projects.discovery import (
    DiscoveredPlaybook,
    PlaybookScanResult,
    ScanLimits,
    discover_playbooks,
    is_excluded_directory,
    is_role_subdirectory,
    looks_like_playbook,
    path_has_excluded_directory,
)
from app.services.projects.service import (
    ProjectAlreadyExistsError,
    ProjectInactiveError,
    ProjectPathUnavailableError,
    create_project,
    deactivate_project,
    find_project_by_path,
    get_project,
    list_project_playbooks,
    list_projects,
)

__all__ = [
    "DiscoveredPlaybook",
    "PlaybookScanResult",
    "ProjectAlreadyExistsError",
    "ProjectInactiveError",
    "ProjectPathUnavailableError",
    "ScanLimits",
    "create_project",
    "deactivate_project",
    "discover_playbooks",
    "find_project_by_path",
    "get_project",
    "is_excluded_directory",
    "is_role_subdirectory",
    "list_project_playbooks",
    "list_projects",
    "looks_like_playbook",
    "path_has_excluded_directory",
]
