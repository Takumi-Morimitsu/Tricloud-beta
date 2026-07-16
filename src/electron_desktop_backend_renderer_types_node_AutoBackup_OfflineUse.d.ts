// electron_desktop_backend_renderer_types_node_AutoBackup_OfflineUse.d.ts
export {};

type Phase1NodeLaunch = {
  node_id: string;
  node_api_key: string;
  server: string;
  storage_dir: string;
  capacity_gb: number;
  runner_path?: string;
  python_path?: string;
};

type Phase1NodeStartPayload = {
  launch: Phase1NodeLaunch;
};

type Phase1NodeRuntimeState = {
  is_running: boolean;
  pid: number | null;
  started_at: string | null;
  stopped_at: string | null;
  exit_code: number | null;
  signal: string | null;
  launch: Phase1NodeLaunch | null;
  log_file: string | null;
  error: string | null;
  resume_enabled?: boolean;
  open_at_login_enabled?: boolean;
  resumed_from_disk?: boolean;
};

type Phase1NodeBridgeResponse = {
  ok: boolean;
  alreadyRunning?: boolean;
  alreadyStopped?: boolean;
  pid?: number | null;
  state?: Phase1NodeRuntimeState;
  message?: string;
  error?: string;
  statePath?: string;
  logDir?: string;
};

type Phase1BackupTarget = {
  local_path: string;
  remote_path: string;
  item_type: 'file' | 'folder';
  display_name?: string;
  source_device_label?: string;
  remote_item_id?: string | null;
  baseline_snapshot?: any;
  target_kind?: 'backup' | 'offline';
};

type Phase1BackupStartPayload = {
  api_base: string;
  access_token?: string;
  email: string;
  polling_interval_sec: number;
  ignore_hidden: boolean;
  local_root_display: string;
  targets: Phase1BackupTarget[];
};

type Phase1BackupRuntimeState = {
  is_running: boolean;
  started_at: string | null;
  stopped_at: string | null;
  last_scan_at: string | null;
  last_sync_at: string | null;
  last_heartbeat_at: string | null;
  client_id: string | null;
  status: string;
  pending_changes: number;
  local_root_display: string | null;
  targets: Phase1BackupTarget[];
  current_device_label?: string | null;
  error: string | null;
  resume_enabled?: boolean;
  open_at_login_enabled?: boolean;
  resumed_from_disk?: boolean;
};

type Phase1BackupBridgeResponse = {
  ok: boolean;
  alreadyRunning?: boolean;
  alreadyStopped?: boolean;
  state?: Phase1BackupRuntimeState;
  message?: string;
  error?: string;
  statePath?: string;
};

type Phase1BackupUpdatePayload = {
  api_base?: string;
  access_token?: string;
  email?: string;
  polling_interval_sec?: number;
  ignore_hidden?: boolean;
  local_root_display?: string;
  targets: Phase1BackupTarget[];
};

type Phase1BackupFolderUploadPayload = {
  api_base: string;
  access_token: string;
  email?: string;
  polling_interval_sec?: number;
  ignore_hidden?: boolean;
  local_root_display?: string;
};

type Phase1BackupFolderUploadResponse = {
  ok: boolean;
  cancelled?: boolean;
  target?: Phase1BackupTarget;
  uploaded_count?: number;
  folder_count?: number;
  message?: string;
  error?: string;
};

type Phase1OfflineFileEntry = {
  item_id?: string;
  download_token: string;
  remote_path: string;
  display_name?: string;
  size_bytes?: number | null;
};

type Phase1OfflineFolderEntry = {
  item_id?: string;
  remote_path: string;
  display_name?: string;
};

type Phase1OfflineEnablePayload = {
  download_url?: string;
  api_base?: string;
  access_token?: string;
  item_id?: string;
  remote_item_id?: string;
  item_type?: 'file' | 'folder';
  remote_path: string;
  display_name?: string;
  files?: Phase1OfflineFileEntry[];
  folders?: Phase1OfflineFolderEntry[];
};

type Phase1OfflineEnableResponse = {
  ok: boolean;
  local_path?: string;
  remote_path?: string;
  display_name?: string;
  offline_root_path?: string;
  source_device_label?: string;
  file_count?: number;
  folder_count?: number;
  bytes?: number;
  baseline_snapshot?: any;
  message?: string;
  error?: string;
};

type Phase1OfflineDisablePayload = {
  local_path?: string;
  remote_path?: string;
  remote_item_id?: string | null;
  item_id?: string;
  item_type?: 'file' | 'folder';
  display_name?: string;
  delete_local?: boolean;
};

type Phase1OfflineDisableResponse = {
  ok: boolean;
  removed_count?: number;
  deleted_local_count?: number;
  state?: Phase1BackupRuntimeState;
  message?: string;
  error?: string;
};


type Phase1DownloadToDownloadsPayload = {
  api_base: string;
  download_token: string;
  access_token: string;
  file_name?: string;
};

type Phase1DownloadToDownloadsResponse = {
  ok: boolean;
  local_path?: string;
  file_name?: string;
  bytes?: number;
  downloads_dir?: string;
  message?: string;
  error?: string;
};

type Phase1OpenCloudFilePayload = Phase1DownloadToDownloadsPayload;

type Phase1OpenCloudFileResponse = {
  ok: boolean;
  local_path?: string;
  file_name?: string;
  bytes?: number;
  message?: string;
  error?: string;
};

type Phase1DesktopBridge = {
  startNode: (payload: Phase1NodeStartPayload) => Promise<Phase1NodeBridgeResponse>;
  stopNode: () => Promise<Phase1NodeBridgeResponse>;
  getNodeStatus: () => Promise<Phase1NodeBridgeResponse>;
  getNodeStatePath: () => Promise<Phase1NodeBridgeResponse>;
  getPathForFiles: (filesLike: File[] | FileList) => string[];
  startBackup: (payload: Phase1BackupStartPayload) => Promise<Phase1BackupBridgeResponse>;
  stopBackup: () => Promise<Phase1BackupBridgeResponse>;
  getBackupStatus: () => Promise<Phase1BackupBridgeResponse>;
  updateBackupTargets: (payload: Phase1BackupUpdatePayload) => Promise<Phase1BackupBridgeResponse>;
  uploadBackupFolderFromDialog: (payload: Phase1BackupFolderUploadPayload) => Promise<Phase1BackupFolderUploadResponse>;
  getBackupStatePath: () => Promise<Phase1BackupBridgeResponse>;
  enableOfflineFile: (payload: Phase1OfflineEnablePayload) => Promise<Phase1OfflineEnableResponse>;
  disableOfflineUse: (payload: Phase1OfflineDisablePayload) => Promise<Phase1OfflineDisableResponse>;
  downloadFileToDownloads: (payload: Phase1DownloadToDownloadsPayload) => Promise<Phase1DownloadToDownloadsResponse>;
  openCloudFile: (payload: Phase1OpenCloudFilePayload) => Promise<Phase1OpenCloudFileResponse>;
};

declare global {
  interface Window {
    electronAPI?: Phase1DesktopBridge;
    phase1Desktop?: Phase1DesktopBridge;
  }
}
