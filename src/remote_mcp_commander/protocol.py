from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

PROTOCOL_MIN_SUPPORTED = 1
PROTOCOL_MAX_SUPPORTED = 1

AGENT_ID_PATTERN = r"^[A-Za-z0-9_.-]{1,128}$"
SESSION_ID_PATTERN = r"^[a-f0-9]{32}$"
CommandArg = Annotated[str, Field(min_length=1, max_length=4096)]
EnvKey = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
EnvValue = Annotated[str, Field(max_length=4096)]
CommandEnv = dict[EnvKey, EnvValue]
FilePath = Annotated[str, Field(min_length=1, max_length=4096)]
CapabilityName = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
]


class CommandRequest(BaseModel):
    type: Literal["command_request"] = "command_request"
    request_id: str
    argv: list[CommandArg] = Field(min_length=1, max_length=64)
    cwd: str | None = Field(default=None, min_length=1, max_length=4096)
    env: CommandEnv = Field(default_factory=dict, max_length=32)
    timeout_s: float | None = Field(default=None, ge=0.1, le=60.0)


class CommandResult(BaseModel):
    type: Literal["command_result"] = "command_result"
    request_id: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    rejected: bool = False
    error: str | None = None


class PingRequest(BaseModel):
    type: Literal["ping_request"] = "ping_request"
    request_id: str


class PingResult(BaseModel):
    type: Literal["ping_result"] = "ping_result"
    request_id: str


class AgentHello(BaseModel):
    type: Literal["hello"] = "hello"
    agent_id: str
    hostname: str
    platform: str
    version: str = "unknown"
    protocol_min: int = Field(default=1, ge=1, le=65_535)
    protocol_max: int = Field(default=1, ge=1, le=65_535)
    capabilities: list[CapabilityName] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_protocol_range(self) -> AgentHello:
        if self.protocol_min > self.protocol_max:
            raise ValueError("protocol_min must be <= protocol_max")
        return self


class Heartbeat(BaseModel):
    type: Literal["heartbeat"] = "heartbeat"
    agent_id: str


class ExecuteBody(BaseModel):
    argv: list[CommandArg] = Field(min_length=1, max_length=64)
    cwd: str | None = Field(default=None, min_length=1, max_length=4096)
    env: CommandEnv = Field(default_factory=dict, max_length=32)
    timeout_s: float | None = Field(default=None, ge=0.1, le=60.0)


class AgentInfo(BaseModel):
    agent_id: str
    hostname: str | None = None
    platform: str | None = None
    version: str | None = None
    protocol_version: int | None = None
    capabilities: list[CapabilityName] = Field(default_factory=list, max_length=64)
    connected_at: datetime
    last_seen: datetime


class AgentList(BaseModel):
    agents: list[AgentInfo]


class PingResponse(BaseModel):
    agent_id: str
    round_trip_ms: float
    last_seen: datetime


class EnrollmentCreateBody(BaseModel):
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)


class EnrollmentTicket(BaseModel):
    agent_id: str
    code: str
    expires_at: datetime


class EnrollmentClaimBody(BaseModel):
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    code: str = Field(min_length=8, max_length=256)


class EnrollmentClaimResult(BaseModel):
    agent_id: str
    agent_token: str


class RegisteredDevice(BaseModel):
    agent_id: str
    created_at: datetime
    revoked_at: datetime | None = None


class RegisteredDeviceList(BaseModel):
    devices: list[RegisteredDevice]


class RevokeResult(BaseModel):
    agent_id: str
    revoked: bool


class FileReadRequest(BaseModel):
    type: Literal["file_read_request"] = "file_read_request"
    request_id: str
    path: str = Field(min_length=1, max_length=4096)
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=65_536, ge=4, le=262_144)


class FileReadResult(BaseModel):
    type: Literal["file_read_result"] = "file_read_result"
    request_id: str
    path: str = ""
    content: str = ""
    size: int = 0
    offset: int = 0
    next_offset: int = 0
    eof: bool = True
    sha256: str | None = None
    rejected: bool = False
    error: str | None = None


class FileReadManyRequest(BaseModel):
    type: Literal["file_read_many_request"] = "file_read_many_request"
    request_id: str
    paths: list[FilePath] = Field(min_length=1, max_length=20)
    max_bytes_per_file: int = Field(default=32_768, ge=4, le=65_536)
    max_total_bytes: int = Field(default=262_144, ge=4, le=524_288)


class FileReadManyItem(BaseModel):
    path: str
    content: str = ""
    size: int = 0
    eof: bool = True
    sha256: str | None = None
    rejected: bool = False
    error: str | None = None


class FileReadManyResult(BaseModel):
    type: Literal["file_read_many_result"] = "file_read_many_result"
    request_id: str
    files: list[FileReadManyItem] = Field(default_factory=list)
    requested_count: int = 0
    total_bytes: int = 0
    truncated: bool = False


class FileReadManyBody(BaseModel):
    paths: list[FilePath] = Field(min_length=1, max_length=20)
    max_bytes_per_file: int = Field(default=32_768, ge=4, le=65_536)
    max_total_bytes: int = Field(default=262_144, ge=4, le=524_288)


class FileLineReadRequest(BaseModel):
    type: Literal["file_line_read_request"] = "file_line_read_request"
    request_id: str
    path: str = Field(min_length=1, max_length=4096)
    offset: int = Field(default=0, ge=-1000, le=1_000_000)
    max_lines: int = Field(default=200, ge=1, le=1000)


class FileLineReadResult(BaseModel):
    type: Literal["file_line_read_result"] = "file_line_read_result"
    request_id: str
    path: str = ""
    content: str = ""
    total_lines: int = 0
    start_line: int = 0
    next_line: int = 0
    eof: bool = True
    sha256: str | None = None
    rejected: bool = False
    error: str | None = None


class FileLineReadBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    offset: int = Field(default=0, ge=-1000, le=1_000_000)
    max_lines: int = Field(default=200, ge=1, le=1000)


class FileWriteRequest(BaseModel):
    type: Literal["file_write_request"] = "file_write_request"
    request_id: str
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=1_048_576)
    overwrite: bool = False
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class FileWriteResult(BaseModel):
    type: Literal["file_write_result"] = "file_write_result"
    request_id: str
    path: str = ""
    bytes_written: int = 0
    sha256: str | None = None
    rejected: bool = False
    error: str | None = None


class FileAppendRequest(BaseModel):
    type: Literal["file_append_request"] = "file_append_request"
    request_id: str
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(min_length=1, max_length=1_048_576)
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class FileAppendResult(BaseModel):
    type: Literal["file_append_result"] = "file_append_result"
    request_id: str
    path: str = ""
    bytes_appended: int = 0
    size: int = 0
    sha256: str | None = None
    rejected: bool = False
    error: str | None = None


class FileAppendBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(min_length=1, max_length=1_048_576)
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)


class FileEditRequest(BaseModel):
    type: Literal["file_edit_request"] = "file_edit_request"
    request_id: str
    path: str = Field(min_length=1, max_length=4096)
    old_text: str = Field(min_length=1, max_length=1_048_576)
    new_text: str = Field(max_length=1_048_576)
    replace_all: bool = False


class FileEditResult(BaseModel):
    type: Literal["file_edit_result"] = "file_edit_result"
    request_id: str
    path: str = ""
    replacements: int = 0
    bytes_written: int = 0
    sha256: str | None = None
    rejected: bool = False
    error: str | None = None


class FileEditBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    old_text: str = Field(min_length=1, max_length=1_048_576)
    new_text: str = Field(max_length=1_048_576)
    replace_all: bool = False


class FileReadBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=65_536, ge=4, le=262_144)


class FileWriteBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=1_048_576)
    overwrite: bool = False
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)


FileEntryKind = Literal["file", "directory", "symlink", "other"]


class FileRootListRequest(BaseModel):
    type: Literal["file_root_list_request"] = "file_root_list_request"
    request_id: str


class FileRootListResult(BaseModel):
    type: Literal["file_root_list_result"] = "file_root_list_result"
    request_id: str
    roots: list[str] = Field(default_factory=list)


class DirectoryEntry(BaseModel):
    name: str
    path: str
    kind: FileEntryKind
    size: int | None = None
    modified_at: datetime | None = None


class DirectoryListRequest(BaseModel):
    type: Literal["directory_list_request"] = "directory_list_request"
    request_id: str
    path: str = Field(min_length=1, max_length=4096)
    limit: int = Field(default=200, ge=1, le=500)


class DirectoryListResult(BaseModel):
    type: Literal["directory_list_result"] = "directory_list_result"
    request_id: str
    path: str = ""
    entries: list[DirectoryEntry] = Field(default_factory=list)
    truncated: bool = False
    rejected: bool = False
    error: str | None = None


class DirectoryListBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    limit: int = Field(default=200, ge=1, le=500)


class DirectoryTreeEntry(BaseModel):
    name: str
    path: str
    relative_path: str
    kind: FileEntryKind
    depth: int = Field(ge=1, le=5)
    size: int | None = None
    modified_at: datetime | None = None


class DirectoryTreeRequest(BaseModel):
    type: Literal["directory_tree_request"] = "directory_tree_request"
    request_id: str
    path: str = Field(min_length=1, max_length=4096)
    depth: int = Field(default=2, ge=1, le=5)
    include_hidden: bool = False
    per_directory_limit: int = Field(default=100, ge=1, le=200)
    max_entries: int = Field(default=1000, ge=1, le=2000)


class DirectoryTreeResult(BaseModel):
    type: Literal["directory_tree_result"] = "directory_tree_result"
    request_id: str
    path: str = ""
    entries: list[DirectoryTreeEntry] = Field(default_factory=list)
    scanned_directories: int = 0
    truncated: bool = False
    rejected: bool = False
    error: str | None = None


class DirectoryTreeBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    depth: int = Field(default=2, ge=1, le=5)
    include_hidden: bool = False
    per_directory_limit: int = Field(default=100, ge=1, le=200)
    max_entries: int = Field(default=1000, ge=1, le=2000)


class FileInfoRequest(BaseModel):
    type: Literal["file_info_request"] = "file_info_request"
    request_id: str
    path: str = Field(min_length=1, max_length=4096)


class FileInfoResult(BaseModel):
    type: Literal["file_info_result"] = "file_info_result"
    request_id: str
    path: str = ""
    kind: FileEntryKind | None = None
    size: int | None = None
    modified_at: datetime | None = None
    mode: str | None = None
    rejected: bool = False
    error: str | None = None


class FileInfoBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


FileSearchMode = Literal["files", "content"]


class FileSearchRequest(BaseModel):
    type: Literal["file_search_request"] = "file_search_request"
    request_id: str
    root: str = Field(min_length=1, max_length=4096)
    query: str = Field(min_length=1, max_length=256)
    mode: FileSearchMode = "files"
    file_glob: str | None = Field(default=None, max_length=256)
    case_sensitive: bool = False
    max_results: int = Field(default=100, ge=1, le=200)


class FileSearchMatch(BaseModel):
    path: str
    line: int | None = None
    preview: str | None = None


class FileSearchResult(BaseModel):
    type: Literal["file_search_result"] = "file_search_result"
    request_id: str
    matches: list[FileSearchMatch] = Field(default_factory=list)
    scanned_files: int = 0
    truncated: bool = False
    rejected: bool = False
    error: str | None = None


class FileSearchBody(BaseModel):
    root: str = Field(min_length=1, max_length=4096)
    query: str = Field(min_length=1, max_length=256)
    mode: FileSearchMode = "files"
    file_glob: str | None = Field(default=None, max_length=256)
    case_sensitive: bool = False
    max_results: int = Field(default=100, ge=1, le=200)


class FileSearchSessionStartRequest(BaseModel):
    type: Literal["file_search_session_start_request"] = "file_search_session_start_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    root: str = Field(min_length=1, max_length=4096)
    query: str = Field(min_length=1, max_length=256)
    mode: FileSearchMode = "files"
    file_glob: str | None = Field(default=None, max_length=256)
    case_sensitive: bool = False
    include_hidden: bool = False
    page_size: int = Field(default=50, ge=1, le=200)
    max_results: int = Field(default=1000, ge=1, le=2000)


class FileSearchSessionMoreRequest(BaseModel):
    type: Literal["file_search_session_more_request"] = "file_search_session_more_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    offset: int | None = Field(default=None, ge=-2000, le=1999)
    limit: int = Field(default=50, ge=1, le=200)


class FileSearchSessionListRequest(BaseModel):
    type: Literal["file_search_session_list_request"] = "file_search_session_list_request"
    request_id: str


class FileSearchSessionStopRequest(BaseModel):
    type: Literal["file_search_session_stop_request"] = "file_search_session_stop_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)


class FileSearchSessionPage(BaseModel):
    type: Literal["file_search_session_page"] = "file_search_session_page"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    matches: list[FileSearchMatch] = Field(default_factory=list)
    scanned_files: int = 0
    returned_count: int = 0
    total_matches: int = 0
    offset: int = 0
    next_offset: int = 0
    remaining: int = 0
    exhausted: bool = False
    truncated: bool = False
    rejected: bool = False
    error: str | None = None


class FileSearchSessionInfo(BaseModel):
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    root: str
    query: str
    mode: FileSearchMode
    file_glob: str | None = None
    case_sensitive: bool = False
    include_hidden: bool = False
    scanned_files: int = 0
    total_matches: int = 0
    cursor: int = 0
    truncated: bool = False


class FileSearchSessionListResult(BaseModel):
    type: Literal["file_search_session_list_result"] = "file_search_session_list_result"
    request_id: str
    sessions: list[FileSearchSessionInfo] = Field(default_factory=list)


class FileSearchSessionStopResult(BaseModel):
    type: Literal["file_search_session_stop_result"] = "file_search_session_stop_result"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    stopped: bool = False
    rejected: bool = False
    error: str | None = None


class FileSearchSessionStartBody(BaseModel):
    root: str = Field(min_length=1, max_length=4096)
    query: str = Field(min_length=1, max_length=256)
    mode: FileSearchMode = "files"
    file_glob: str | None = Field(default=None, max_length=256)
    case_sensitive: bool = False
    include_hidden: bool = False
    page_size: int = Field(default=50, ge=1, le=200)
    max_results: int = Field(default=1000, ge=1, le=2000)


class FileSearchSessionMoreBody(BaseModel):
    offset: int | None = Field(default=None, ge=-2000, le=1999)
    limit: int = Field(default=50, ge=1, le=200)


class PathMutationRequest(BaseModel):
    type: Literal["path_mutation_request"] = "path_mutation_request"
    request_id: str
    operation: Literal["mkdir", "copy", "move", "delete"]
    path: str = Field(min_length=1, max_length=4096)
    destination: str | None = Field(default=None, max_length=4096)
    parents: bool = False
    overwrite: bool = False


class PathMutationResult(BaseModel):
    type: Literal["path_mutation_result"] = "path_mutation_result"
    request_id: str
    operation: Literal["mkdir", "copy", "move", "delete"]
    path: str = ""
    destination: str | None = None
    changed: bool = False
    rejected: bool = False
    error: str | None = None


class PathMutationBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    destination: str | None = Field(default=None, max_length=4096)
    parents: bool = False
    overwrite: bool = False


class TreeInspectRequest(BaseModel):
    type: Literal["tree_inspect_request"] = "tree_inspect_request"
    request_id: str
    path: str = Field(min_length=1, max_length=4096)
    max_entries: int = Field(default=5000, ge=1, le=50_000)
    max_total_bytes: int = Field(default=268_435_456, ge=1, le=1_073_741_824)


class TreeInspectResult(BaseModel):
    type: Literal["tree_inspect_result"] = "tree_inspect_result"
    request_id: str
    path: str = ""
    entries: int = 0
    total_bytes: int = 0
    tree_sha256: str | None = None
    rejected: bool = False
    error: str | None = None


class TreeInspectBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    max_entries: int = Field(default=5000, ge=1, le=50_000)
    max_total_bytes: int = Field(default=268_435_456, ge=1, le=1_073_741_824)


class TreeMutationRequest(BaseModel):
    type: Literal["tree_mutation_request"] = "tree_mutation_request"
    request_id: str
    operation: Literal["copy_tree", "delete_tree"]
    path: str = Field(min_length=1, max_length=4096)
    destination: str | None = Field(default=None, max_length=4096)
    expected_tree_sha256: str = Field(min_length=64, max_length=64)
    max_entries: int = Field(default=5000, ge=1, le=50_000)
    max_total_bytes: int = Field(default=268_435_456, ge=1, le=1_073_741_824)


class TreeMutationResult(BaseModel):
    type: Literal["tree_mutation_result"] = "tree_mutation_result"
    request_id: str
    operation: Literal["copy_tree", "delete_tree"]
    path: str = ""
    destination: str | None = None
    entries: int = 0
    total_bytes: int = 0
    changed: bool = False
    rejected: bool = False
    error: str | None = None


class TreeMutationBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    destination: str | None = Field(default=None, max_length=4096)
    expected_tree_sha256: str = Field(min_length=64, max_length=64)
    max_entries: int = Field(default=5000, ge=1, le=50_000)
    max_total_bytes: int = Field(default=268_435_456, ge=1, le=1_073_741_824)


class ProcessInfo(BaseModel):
    pid: int
    create_time_ms: int
    name: str
    username: str | None = None
    status: str | None = None
    memory_rss: int | None = None


class ProcessListRequest(BaseModel):
    type: Literal["process_list_request"] = "process_list_request"
    request_id: str
    limit: int = Field(default=100, ge=1, le=500)


class ProcessListResult(BaseModel):
    type: Literal["process_list_result"] = "process_list_result"
    request_id: str
    processes: list[ProcessInfo] = Field(default_factory=list)
    truncated: bool = False
    rejected: bool = False
    error: str | None = None


class ProcessListBody(BaseModel):
    limit: int = Field(default=100, ge=1, le=500)


class ServiceStatusRequest(BaseModel):
    type: Literal["service_status_request"] = "service_status_request"
    request_id: str
    unit: str = Field(min_length=1, max_length=256)


class ServiceStatusResult(BaseModel):
    type: Literal["service_status_result"] = "service_status_result"
    request_id: str
    unit: str
    id: str | None = None
    description: str | None = None
    load_state: str | None = None
    active_state: str | None = None
    sub_state: str | None = None
    unit_file_state: str | None = None
    returncode: int | None = None
    rejected: bool = False
    error: str | None = None


class ServiceStatusBody(BaseModel):
    unit: str = Field(min_length=1, max_length=256)


class SystemHealthRequest(BaseModel):
    type: Literal["system_health_request"] = "system_health_request"
    request_id: str


class SystemHealthResult(BaseModel):
    type: Literal["system_health_result"] = "system_health_result"
    request_id: str
    boot_time: datetime | None = None
    uptime_seconds: int | None = None
    cpu_count: int | None = None
    cpu_percent: float | None = None
    load_1m: float | None = None
    load_5m: float | None = None
    load_15m: float | None = None
    memory_total: int | None = None
    memory_available: int | None = None
    memory_percent: float | None = None
    swap_total: int | None = None
    swap_used: int | None = None
    swap_percent: float | None = None
    disk_total: int | None = None
    disk_free: int | None = None
    disk_percent: float | None = None
    rejected: bool = False
    error: str | None = None


class PortListener(BaseModel):
    local_address: str
    pid: int | None = None
    process_name: str | None = None


class PortLookupRequest(BaseModel):
    type: Literal["port_lookup_request"] = "port_lookup_request"
    request_id: str
    port: int = Field(ge=1, le=65_535)


class PortLookupResult(BaseModel):
    type: Literal["port_lookup_result"] = "port_lookup_result"
    request_id: str
    port: int
    listeners: list[PortListener] = Field(default_factory=list)
    truncated: bool = False
    rejected: bool = False
    error: str | None = None


class PortLookupBody(BaseModel):
    port: int = Field(ge=1, le=65_535)


class ServiceLogsRequest(BaseModel):
    type: Literal["service_logs_request"] = "service_logs_request"
    request_id: str
    unit: str = Field(min_length=1, max_length=256)
    lines: int = Field(default=100, ge=1, le=500)


class ServiceLogsResult(BaseModel):
    type: Literal["service_logs_result"] = "service_logs_result"
    request_id: str
    unit: str
    text: str = ""
    returncode: int | None = None
    truncated: bool = False
    rejected: bool = False
    error: str | None = None


class ServiceLogsBody(BaseModel):
    unit: str = Field(min_length=1, max_length=256)
    lines: int = Field(default=100, ge=1, le=500)


class GitStatusRequest(BaseModel):
    type: Literal["git_status_request"] = "git_status_request"
    request_id: str
    path: str = Field(min_length=1, max_length=4096)


class GitStatusResult(BaseModel):
    type: Literal["git_status_result"] = "git_status_result"
    request_id: str
    path: str = ""
    repo_root: str | None = None
    branch: str | None = None
    head_oid: str | None = None
    porcelain: str = ""
    clean: bool | None = None
    truncated: bool = False
    rejected: bool = False
    error: str | None = None


class GitStatusBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


MutationOperation = Literal[
    "process.terminate",
    "process.signal.term",
    "process.signal.kill",
    "process.signal.int",
    "process.signal.hup",
    "service.start",
    "service.stop",
    "service.restart",
]
ServiceAction = Literal["start", "stop", "restart"]


class ApprovalCreateBody(BaseModel):
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    operation: MutationOperation
    target: str = Field(min_length=1, max_length=512)


class ApprovalTicket(BaseModel):
    approval_id: str
    approval_secret: str
    agent_id: str
    operation: MutationOperation
    target: str
    expires_at: datetime


class ApprovalUse(BaseModel):
    approval_id: str = Field(min_length=8, max_length=256)
    approval_secret: str = Field(min_length=16, max_length=512)


class ProcessTerminateRequest(BaseModel):
    type: Literal["process_terminate_request"] = "process_terminate_request"
    request_id: str
    pid: int = Field(ge=2)
    expected_create_time_ms: int = Field(gt=0)


class ProcessTerminateResult(BaseModel):
    type: Literal["process_terminate_result"] = "process_terminate_result"
    request_id: str
    pid: int
    signal_sent: bool = False
    exited: bool = False
    rejected: bool = False
    error: str | None = None


class ProcessTerminateBody(ApprovalUse):
    pid: int = Field(ge=2)
    expected_create_time_ms: int = Field(gt=0)


ProcessSignal = Literal["term", "kill", "int", "hup"]


class ProcessSignalRequest(BaseModel):
    type: Literal["process_signal_request"] = "process_signal_request"
    request_id: str
    pid: int = Field(ge=2)
    expected_create_time_ms: int = Field(gt=0)
    signal: ProcessSignal


class ProcessSignalResult(BaseModel):
    type: Literal["process_signal_result"] = "process_signal_result"
    request_id: str
    pid: int
    signal: ProcessSignal
    signal_sent: bool = False
    exited: bool = False
    rejected: bool = False
    error: str | None = None


class ProcessSignalBody(BaseModel):
    pid: int = Field(ge=2)
    expected_create_time_ms: int = Field(gt=0)
    signal: ProcessSignal
    approval_id: str | None = Field(default=None, min_length=8, max_length=256)
    approval_secret: str | None = Field(default=None, min_length=16, max_length=512)


class ServiceActionRequest(BaseModel):
    type: Literal["service_action_request"] = "service_action_request"
    request_id: str
    unit: str = Field(min_length=1, max_length=256)
    action: ServiceAction


class ServiceActionResult(BaseModel):
    type: Literal["service_action_result"] = "service_action_result"
    request_id: str
    unit: str
    action: ServiceAction
    returncode: int | None = None
    rejected: bool = False
    error: str | None = None


class ServiceActionBody(ApprovalUse):
    unit: str = Field(min_length=1, max_length=256)
    action: ServiceAction


class AuditQueryResult(BaseModel):
    records: list[dict[str, Any]] = Field(default_factory=list)
    scan_truncated: bool = False


class AuditVerificationResult(BaseModel):
    valid: bool
    checked_records: int = 0
    legacy_records: int = 0
    retained_files: int = 0
    chain_id: str | None = None
    first_sequence: int | None = None
    last_sequence: int | None = None
    anchor_hash: str | None = None
    head_hash: str | None = None
    error: str | None = None


CommandSessionState = Literal["running", "completed", "cancelled", "timed_out", "failed"]
RuntimeSessionKind = Literal["command", "pty"]
RuntimeSessionFilter = Literal["all", "command", "pty"]


class RuntimeSessionInfo(BaseModel):
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    kind: RuntimeSessionKind
    executable: str = ""
    state: CommandSessionState
    cwd: str | None = None
    timeout_s: float | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    returncode: int | None = None
    output_chars: int = 0
    output_truncated: bool = False


class SessionListRequest(BaseModel):
    type: Literal["session_list_request"] = "session_list_request"
    request_id: str
    kind: RuntimeSessionFilter = "all"
    include_completed: bool = False
    limit: int = Field(default=100, ge=1, le=200)


class SessionListResult(BaseModel):
    type: Literal["session_list_result"] = "session_list_result"
    request_id: str
    sessions: list[RuntimeSessionInfo] = Field(default_factory=list)
    total_count: int = 0
    truncated: bool = False
    rejected: bool = False
    error: str | None = None


class SessionListBody(BaseModel):
    kind: RuntimeSessionFilter = "all"
    include_completed: bool = False
    limit: int = Field(default=100, ge=1, le=200)


class CommandSessionStartRequest(BaseModel):
    type: Literal["command_session_start_request"] = "command_session_start_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    argv: list[CommandArg] = Field(min_length=1, max_length=64)
    cwd: str | None = Field(default=None, min_length=1, max_length=4096)
    env: CommandEnv = Field(default_factory=dict, max_length=32)
    timeout_s: float | None = Field(default=None, ge=1.0, le=3600.0)


class CommandSessionStatusRequest(BaseModel):
    type: Literal["command_session_status_request"] = "command_session_status_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)


class CommandSessionCancelRequest(BaseModel):
    type: Literal["command_session_cancel_request"] = "command_session_cancel_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)


class CommandSessionOutputRequest(BaseModel):
    type: Literal["command_session_output_request"] = "command_session_output_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    stdout_offset: int = Field(default=0, ge=0)
    stderr_offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=8192, ge=1, le=65_536)


CommandOutputStream = Literal["stdout", "stderr"]


class CommandSessionLineOutputRequest(BaseModel):
    type: Literal["command_session_line_output_request"] = "command_session_line_output_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    stream: CommandOutputStream = "stdout"
    offset: int = Field(default=0, ge=-1000)
    max_lines: int = Field(default=200, ge=1, le=1000)


class CommandSessionInputRequest(BaseModel):
    type: Literal["command_session_input_request"] = "command_session_input_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    data: str = Field(min_length=1, max_length=16_384)


class CommandSessionInputResult(BaseModel):
    type: Literal["command_session_input_result"] = "command_session_input_result"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    accepted_bytes: int = 0
    rejected: bool = False
    error: str | None = None


class CommandSessionStdinCloseRequest(BaseModel):
    type: Literal["command_session_stdin_close_request"] = "command_session_stdin_close_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)


class CommandSessionStdinCloseResult(BaseModel):
    type: Literal["command_session_stdin_close_result"] = "command_session_stdin_close_result"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    closed: bool = False
    rejected: bool = False
    error: str | None = None


class CommandSessionDiscardRequest(BaseModel):
    type: Literal["command_session_discard_request"] = "command_session_discard_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)


class CommandSessionSnapshot(BaseModel):
    type: Literal["command_session_snapshot"] = "command_session_snapshot"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    executable: str = ""
    state: CommandSessionState
    cwd: str | None = None
    timeout_s: float | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    rejected: bool = False
    error: str | None = None


class CommandSessionOutput(BaseModel):
    type: Literal["command_session_output"] = "command_session_output"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    state: CommandSessionState
    stdout: str = ""
    stderr: str = ""
    next_stdout_offset: int = 0
    next_stderr_offset: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    rejected: bool = False
    error: str | None = None


class CommandSessionLineOutput(BaseModel):
    type: Literal["command_session_line_output"] = "command_session_line_output"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    state: CommandSessionState
    stream: CommandOutputStream = "stdout"
    content: str = ""
    total_lines: int = 0
    start_line: int = 0
    next_line: int = 0
    eof: bool = False
    pending_partial: bool = False
    output_truncated: bool = False
    rejected: bool = False
    error: str | None = None


class CommandSessionDiscardResult(BaseModel):
    type: Literal["command_session_discard_result"] = "command_session_discard_result"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    discarded: bool = False
    rejected: bool = False
    error: str | None = None


class CommandSessionStartBody(BaseModel):
    argv: list[CommandArg] = Field(min_length=1, max_length=64)
    cwd: str | None = Field(default=None, min_length=1, max_length=4096)
    env: CommandEnv = Field(default_factory=dict, max_length=32)
    timeout_s: float | None = Field(default=None, ge=1.0, le=3600.0)


class CommandSessionLineOutputBody(BaseModel):
    stream: CommandOutputStream = "stdout"
    offset: int = Field(default=0, ge=-1000)
    max_lines: int = Field(default=200, ge=1, le=1000)


class CommandSessionInputBody(BaseModel):
    data: str = Field(min_length=1, max_length=16_384)


class CommandSessionOutputBody(BaseModel):
    stdout_offset: int = Field(default=0, ge=0)
    stderr_offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=8192, ge=1, le=65_536)


class PtySessionStartRequest(BaseModel):
    type: Literal["pty_session_start_request"] = "pty_session_start_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    argv: list[CommandArg] = Field(min_length=1, max_length=64)
    cwd: str | None = Field(default=None, min_length=1, max_length=4096)
    env: CommandEnv = Field(default_factory=dict, max_length=32)
    columns: int = Field(default=80, ge=20, le=500)
    rows: int = Field(default=24, ge=5, le=200)


class PtySessionStatusRequest(BaseModel):
    type: Literal["pty_session_status_request"] = "pty_session_status_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)


class PtySessionInputRequest(BaseModel):
    type: Literal["pty_session_input_request"] = "pty_session_input_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    data: str = Field(min_length=1, max_length=65_536)


class PtySessionResizeRequest(BaseModel):
    type: Literal["pty_session_resize_request"] = "pty_session_resize_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    columns: int = Field(ge=20, le=500)
    rows: int = Field(ge=5, le=200)


class PtySessionCancelRequest(BaseModel):
    type: Literal["pty_session_cancel_request"] = "pty_session_cancel_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)


class PtySessionOutputRequest(BaseModel):
    type: Literal["pty_session_output_request"] = "pty_session_output_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=8192, ge=1, le=65_536)


class PtySessionLineOutputRequest(BaseModel):
    type: Literal["pty_session_line_output_request"] = "pty_session_line_output_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    offset: int = Field(default=0, ge=-1000)
    max_lines: int = Field(default=200, ge=1, le=1000)


class PtySessionDiscardRequest(BaseModel):
    type: Literal["pty_session_discard_request"] = "pty_session_discard_request"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)


class PtySessionSnapshot(BaseModel):
    type: Literal["pty_session_snapshot"] = "pty_session_snapshot"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    executable: str = ""
    state: CommandSessionState
    cwd: str | None = None
    columns: int = 80
    rows: int = 24
    started_at: datetime | None = None
    finished_at: datetime | None = None
    returncode: int | None = None
    output: str = ""
    output_truncated: bool = False
    rejected: bool = False
    error: str | None = None


class PtySessionOutput(BaseModel):
    type: Literal["pty_session_output"] = "pty_session_output"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    state: CommandSessionState
    output: str = ""
    next_offset: int = 0
    output_truncated: bool = False
    rejected: bool = False
    error: str | None = None


class PtySessionLineOutput(BaseModel):
    type: Literal["pty_session_line_output"] = "pty_session_line_output"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    state: CommandSessionState
    content: str = ""
    total_lines: int = 0
    start_line: int = 0
    next_line: int = 0
    eof: bool = False
    pending_partial: bool = False
    output_truncated: bool = False
    rejected: bool = False
    error: str | None = None


class PtySessionInputResult(BaseModel):
    type: Literal["pty_session_input_result"] = "pty_session_input_result"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    accepted_bytes: int = 0
    rejected: bool = False
    error: str | None = None


class PtySessionResizeResult(BaseModel):
    type: Literal["pty_session_resize_result"] = "pty_session_resize_result"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    columns: int = 80
    rows: int = 24
    rejected: bool = False
    error: str | None = None


class PtySessionDiscardResult(BaseModel):
    type: Literal["pty_session_discard_result"] = "pty_session_discard_result"
    request_id: str
    session_id: str = Field(pattern=SESSION_ID_PATTERN)
    discarded: bool = False
    rejected: bool = False
    error: str | None = None


class PtySessionStartBody(BaseModel):
    argv: list[CommandArg] = Field(min_length=1, max_length=64)
    env: CommandEnv = Field(default_factory=dict, max_length=32)
    approval_id: str | None = Field(default=None, min_length=8, max_length=256)
    approval_secret: str | None = Field(default=None, min_length=16, max_length=512)
    cwd: str | None = Field(default=None, min_length=1, max_length=4096)
    columns: int = Field(default=80, ge=20, le=500)
    rows: int = Field(default=24, ge=5, le=200)


class PtySessionInputBody(BaseModel):
    data: str = Field(min_length=1, max_length=65_536)


class PtySessionResizeBody(BaseModel):
    columns: int = Field(ge=20, le=500)
    rows: int = Field(ge=5, le=200)


class PtySessionOutputBody(BaseModel):
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=8192, ge=1, le=65_536)


class PtySessionLineOutputBody(BaseModel):
    offset: int = Field(default=0, ge=-1000)
    max_lines: int = Field(default=200, ge=1, le=1000)
