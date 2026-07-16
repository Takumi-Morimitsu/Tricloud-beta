// electron_desktop_backend_main_node_AutoBackup_OfflineUse.cjs
// -*- coding: utf-8 -*-
/**
 * Phase1 デスクトップアプリ用のローカル実行バックエンド（Electron Main Process）
 *
 * 目的:
 * - Renderer(TSX) から安全に「ノード起動 / 停止 / 状態確認」を呼べるようにする
 * - node_phase1_runner.py をローカルで自動実行する
 * - バックアップ設定の保存後、ローカルファイル監視（ポーリング）による自動バックアップを行う
 * - ノード自動再開 / バックアップ自動再開 / オフライン保存の入口を一本化する
 *
 * 設計方針:
 * - Renderer には contextBridge 経由で最小限の API だけを公開する
 * - ローカルコマンド実行とファイル監視は Main Process でのみ行う
 * - shell を介さず spawn/execFile 系で起動する
 * - 起動中プロセス情報 / バックアップ実行状態は userData 配下に JSON で保持する
 */

const { app, ipcMain, safeStorage, shell, dialog } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const crypto = require('node:crypto');
const http = require('node:http');
const https = require('node:https');
const { spawn, spawnSync } = require('node:child_process');
const os = require('node:os');

const STATE_FILE_NAME = 'phase1_node_runtime_state.json';
const BACKUP_STATE_FILE_NAME = 'phase1_backup_runtime_state.json';
const LOG_DIR_NAME = 'phase1-node-logs';
const NODE_STORAGE_ROOT_NAME = 'Tri_Cloud';
const IPC_CHANNELS = {
  start: 'phase1-node:start',
  stop: 'phase1-node:stop',
  status: 'phase1-node:status',
  getStatePath: 'phase1-node:state-path',
  localCapacity: 'phase1-node:local-capacity',
};
const BACKUP_IPC_CHANNELS = {
  start: 'phase1-backup:start',
  stop: 'phase1-backup:stop',
  status: 'phase1-backup:status',
  getStatePath: 'phase1-backup:state-path',
  updateTargets: 'phase1-backup:update-targets',
  uploadFolderFromDialog: 'phase1-backup:upload-folder-from-dialog',
};
const OFFLINE_IPC_CHANNELS = {
  enable: 'phase1-offline:enable',
  disable: 'phase1-offline:disable',
};
const DOWNLOAD_IPC_CHANNELS = {
  saveToDownloads: 'phase1-download:to-downloads',
  openFile: 'phase1-file:open',
};

let _backupTimer = null;
let _backupConfig = null;
let _backupSecret = null;
let _backupScanInFlight = false;
let _backupTokenCache = null;
let _backupResumeAttempted = false;
let _nodeResumeAttempted = false;

function _ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}


function _isDefaultNodeStorageValue(value) {
  const raw = String(value || '').trim();
  if (!raw) return true;
  const normalized = raw.replace(/\\/g, '/').replace(/\/+$/g, '');
  return normalized === './node_store' || normalized === 'node_store' || normalized === NODE_STORAGE_ROOT_NAME;
}

function _defaultNodeStorageDir() {
  const explicit = String(process.env.TRI_CLOUD_NODE_STORAGE_DIR || process.env.NODE_STORAGE_DIR_DEFAULT || '').trim();
  if (explicit && !_isDefaultNodeStorageValue(explicit)) {
    return path.resolve(explicit);
  }
  return path.join(app.getPath('home'), NODE_STORAGE_ROOT_NAME);
}

function _protectNodeStorageRoot(storageDir) {
  _ensureDir(storageDir);

  // 誤操作防止用の目印。チャンク本体は暗号化済みだが、提供者が手で触る対象ではない。
  const markerPath = path.join(storageDir, 'DO_NOT_OPEN_TRI_CLOUD_STORAGE.txt');
  if (!fs.existsSync(markerPath)) {
    fs.writeFileSync(
      markerPath,
      [
        'Tri_Cloud internal storage folder.',
        'このフォルダはストレージ提供機能が自動管理します。',
        '手動で開く・移動する・削除する操作は、ノード提供や復元性を壊す可能性があります。',
      ].join('\n') + '\n',
      'utf-8',
    );
  }

  if (process.platform === 'win32') {
    // Windows Explorer で通常表示されにくくする。完全なアクセス禁止ではなく誤操作防止。
    spawnSync('attrib', ['+h', '+s', '+r', storageDir], { windowsHide: true });
    spawnSync('attrib', ['+h', '+s', '+r', markerPath], { windowsHide: true });
  } else {
    // POSIX系では同一ユーザーのアプリが読み書きできる範囲で、他ユーザーからは見えにくくする。
    try { fs.chmodSync(storageDir, 0o700); } catch { /* noop */ }
    try { fs.chmodSync(markerPath, 0o600); } catch { /* noop */ }
  }
}

function _resolveNodeStorageDir(value) {
  if (_isDefaultNodeStorageValue(value)) {
    return _defaultNodeStorageDir();
  }
  return path.resolve(String(value).trim());
}


function _numberFromStatFsValue(value, fallback = 0) {
  if (typeof value === 'bigint') return Number(value);
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function _windowsDriveCapacityFallback(storageDir) {
  if (process.platform !== 'win32') return null;
  const root = path.parse(storageDir).root || storageDir;
  const driveName = String(root).replace(/[\\/:]/g, '').trim();
  if (!driveName) return null;

  const script = [
    `$d = Get-PSDrive -Name '${driveName}'`,
    '$free = [int64]$d.Free',
    '$used = [int64]$d.Used',
    '[pscustomobject]@{ Free=$free; Total=($free+$used) } | ConvertTo-Json -Compress',
  ].join('; ');

  const result = spawnSync('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script], {
    windowsHide: true,
    encoding: 'utf8',
  });

  if (result.status !== 0 || !result.stdout) return null;
  try {
    const parsed = JSON.parse(result.stdout.trim());
    const freeBytes = Number(parsed.Free || 0);
    const totalBytes = Number(parsed.Total || 0);
    if (!Number.isFinite(freeBytes) || !Number.isFinite(totalBytes) || freeBytes < 0 || totalBytes <= 0) return null;
    return { totalBytes, freeBytes };
  } catch {
    return null;
  }
}

function _getLocalCapacityHint(input = {}) {
  const requestedPath = String(input?.path || input?.storage_dir || input?.storageDir || '').trim();
  const storageDir = _resolveNodeStorageDir(requestedPath);
  _ensureDir(storageDir);

  let totalBytes = 0;
  let freeBytes = 0;

  if (typeof fs.statfsSync === 'function') {
    try {
      const stat = fs.statfsSync(storageDir);
      const blockSize = _numberFromStatFsValue(stat.bsize || stat.frsize, 4096);
      const blocks = _numberFromStatFsValue(stat.blocks, 0);
      const availableBlocks = _numberFromStatFsValue(stat.bavail ?? stat.bfree, 0);
      totalBytes = Math.max(0, Math.floor(blocks * blockSize));
      freeBytes = Math.max(0, Math.floor(availableBlocks * blockSize));
    } catch {
      totalBytes = 0;
      freeBytes = 0;
    }
  }

  if (!totalBytes || !freeBytes) {
    const fallback = _windowsDriveCapacityFallback(storageDir);
    if (fallback) {
      totalBytes = fallback.totalBytes;
      freeBytes = fallback.freeBytes;
    }
  }

  if (!totalBytes || !freeBytes) {
    throw new Error('このPCの空き容量を取得できませんでした。');
  }

  const offerableBytes = Math.max(0, Math.floor(freeBytes * 0.9));
  const offerableGb = Math.max(0, Math.floor(offerableBytes / (1024 ** 3)));

  return {
    total_bytes: totalBytes,
    free_bytes: freeBytes,
    offerable_bytes: offerableBytes,
    offerable_gb: offerableGb,
    path: storageDir,
    source: 'electron_local_disk_90pct',
  };
}

function _nowIso() {
  return new Date().toISOString();
}

function _backupDebug(message, extra = null) {
  try {
    const prefix = `[phase1-backup ${_nowIso()}]`;
    if (extra == null) {
      console.log(prefix, message);
    } else {
      console.log(prefix, message, JSON.stringify(extra));
    }
  } catch {
    // ログ出力は補助処理なので失敗しても本体処理は止めない。
  }
}

function _backupSourceDeviceLabel() {
  return os.hostname() || 'このデバイス';
}

function _stateFilePath() {
  return path.join(app.getPath('userData'), STATE_FILE_NAME);
}

function _backupStateFilePath() {
  return path.join(app.getPath('userData'), BACKUP_STATE_FILE_NAME);
}

function _offlineRootPath() {
  return path.join(app.getPath('documents'), 'Phase1 Offline');
}

function _sanitizeOfflineSegment(value) {
  const safe = String(value || '').trim().replace(/[<>:"|?*\x00-\x1f-]/g, '_').replace(/[\/]+/g, '_').replace(/\.+$/g, '').trim();
  return safe || '_';
}

function _offlineLocalPathForRemotePath(remotePath, displayName) {
  const normalized = _normalizeRemotePath(remotePath || displayName || 'offline-item');
  const parts = normalized.split('/').filter(Boolean).map(_sanitizeOfflineSegment);
  if (!parts.length) {
    parts.push(_sanitizeOfflineSegment(displayName || 'offline-item'));
  }
  return path.join(_offlineRootPath(), ...parts);
}


function _isPathInside(parentPath, childPath) {
  const parent = path.resolve(parentPath);
  const child = path.resolve(childPath);
  if (parent === child) return false;
  const rel = path.relative(parent, child);
  return Boolean(rel) && !rel.startsWith('..') && !path.isAbsolute(rel);
}

function _offlineTargetMatchesDescriptor(target, descriptor = {}) {
  if (!_isOfflineTarget(target)) return false;

  // 停止対象は、その item 自身に対応する offline target だけに限定する。
  // ファイルだけをオフライン利用したときに、親フォルダ target まで
  // 巻き込まないよう、種別が分かる場合は必ず一致を要求する。
  const targetType = String(target?.item_type || '').trim();
  const descriptorType = String(descriptor?.item_type || '').trim();
  if (targetType && descriptorType && targetType !== descriptorType) return false;

  const targetId = String(target?.remote_item_id || '').trim();
  const descriptorId = String(descriptor?.remote_item_id || descriptor?.item_id || '').trim();
  if (targetId && descriptorId) return targetId === descriptorId;

  const targetLocalPath = path.resolve(String(target?.local_path || '') || '.');
  const descriptorLocalPath = String(descriptor?.local_path || '').trim();
  if (descriptorLocalPath && targetLocalPath === path.resolve(descriptorLocalPath)) return true;

  // 旧データ互換用の補助判定。remote_item_id がない target だけ、
  // remote_path の完全一致で同一対象とみなす。前方一致は使わない。
  const targetRemotePath = _normalizeRemotePath(target?.remote_path || target?.display_name || '');
  const descriptorRemotePath = _normalizeRemotePath(descriptor?.remote_path || descriptor?.display_name || '');
  if (!targetId && targetRemotePath && descriptorRemotePath && targetRemotePath === descriptorRemotePath) return true;

  return false;
}

async function _removeOfflineLocalCopy(localPath) {
  const safeLocalPath = String(localPath || '').trim();
  if (!safeLocalPath) return false;
  const resolved = path.resolve(safeLocalPath);
  const offlineRoot = _offlineRootPath();
  if (!_isPathInside(offlineRoot, resolved)) {
    throw new Error('安全のため、オフライン保存フォルダ外のファイルは削除できません');
  }
  await fsp.rm(resolved, { recursive: true, force: true });
  return true;
}

function _downloadUrlToFile(downloadUrl, destPath, redirectDepth = 0) {
  return new Promise((resolve, reject) => {
    if (redirectDepth > 5) {
      reject(new Error('ダウンロードのリダイレクトが多すぎます'));
      return;
    }

    let urlObject;
    try {
      urlObject = new URL(downloadUrl);
    } catch {
      reject(new Error('download_url が不正です'));
      return;
    }

    const client = urlObject.protocol === 'https:' ? https : http;
    const request = client.get(urlObject, (response) => {
      const statusCode = Number(response.statusCode || 0);
      if ([301, 302, 303, 307, 308].includes(statusCode) && response.headers.location) {
        const redirectedUrl = new URL(response.headers.location, urlObject).toString();
        response.resume();
        _downloadUrlToFile(redirectedUrl, destPath, redirectDepth + 1).then(resolve).catch(reject);
        return;
      }
      if (statusCode < 200 || statusCode >= 300) {
        response.resume();
        reject(new Error(`ダウンロードに失敗しました: ${statusCode}`));
        return;
      }

      const tempPath = `${destPath}.download`;
      const stream = fs.createWriteStream(tempPath);
      response.pipe(stream);
      stream.on('finish', () => {
        stream.close((closeErr) => {
          if (closeErr) {
            reject(closeErr);
            return;
          }
          fs.rename(tempPath, destPath, (renameErr) => {
            if (renameErr) {
              reject(renameErr);
              return;
            }
            resolve(destPath);
          });
        });
      });
      stream.on('error', (streamErr) => {
        fs.rm(tempPath, { force: true }, () => reject(streamErr));
      });
    });

    request.on('error', reject);
  });
}


function _sanitizeDownloadFileName(value) {
  const raw = String(value || 'download.bin').trim() || 'download.bin';
  const safe = raw.replace(/[<>:"/\\|?*\x00-\x1f]+/g, '_').replace(/\.+$/g, '').trim();
  return safe || 'download.bin';
}

async function _uniqueDownloadPath(downloadDir, fileName) {
  const safeName = _sanitizeDownloadFileName(fileName);
  const parsed = path.parse(safeName);
  let candidate = path.join(downloadDir, safeName);
  for (let i = 1; i < 1000; i += 1) {
    try {
      await fsp.access(candidate);
      candidate = path.join(downloadDir, `${parsed.name} (${i})${parsed.ext}`);
    } catch {
      return candidate;
    }
  }
  return path.join(downloadDir, `${parsed.name}-${Date.now()}${parsed.ext}`);
}

async function _downloadFileBufferFromApi(payload) {
  const apiBase = String(payload?.api_base || 'http://127.0.0.1:8000').replace(/\/+$/g, '');
  const downloadToken = _sanitizeString(payload?.download_token, 'download_token');
  const accessToken = _sanitizeString(payload?.access_token, 'access_token');
  const fileName = _sanitizeDownloadFileName(payload?.file_name || 'download.bin');

  const response = await fetch(`${apiBase}/ui/download`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${accessToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      download_token: downloadToken,
      file_name: fileName,
    }),
  });

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      detail = data?.detail || JSON.stringify(data);
    } catch {
      detail = await response.text().catch(() => detail);
    }
    throw new Error(detail || 'ダウンロードに失敗しました');
  }

  const arrayBuffer = await response.arrayBuffer();
  return {
    buffer: Buffer.from(arrayBuffer),
    fileName,
  };
}

async function _downloadFileToDownloadsProcess(payload) {
  const { buffer, fileName } = await _downloadFileBufferFromApi(payload);
  const downloadDir = app.getPath('downloads');
  await fsp.mkdir(downloadDir, { recursive: true });
  const destPath = await _uniqueDownloadPath(downloadDir, fileName);
  const tempPath = `${destPath}.download`;
  await fsp.writeFile(tempPath, buffer);
  await fsp.rename(tempPath, destPath);
  return {
    ok: true,
    local_path: destPath,
    file_name: path.basename(destPath),
    bytes: buffer.length,
    downloads_dir: downloadDir,
    message: 'ダウンロードフォルダへ保存しました',
  };
}

async function _openCloudFileWithDefaultAppProcess(payload) {
  const { buffer, fileName } = await _downloadFileBufferFromApi(payload);
  const openCacheDir = path.join(app.getPath('temp'), 'Tri_Cloud_OpenCache');
  await fsp.mkdir(openCacheDir, { recursive: true });
  const uniqueName = `${Date.now()}-${crypto.randomBytes(4).toString('hex')}-${_sanitizeDownloadFileName(fileName)}`;
  const localPath = path.join(openCacheDir, uniqueName);
  await fsp.writeFile(localPath, buffer);
  const openError = await shell.openPath(localPath);
  if (openError) {
    throw new Error(openError);
  }
  return {
    ok: true,
    local_path: localPath,
    file_name: path.basename(localPath),
    bytes: buffer.length,
    message: '既定のアプリで開きました',
  };
}

async function _writeBufferAtomic(destPath, buffer) {
  await fsp.mkdir(path.dirname(destPath), { recursive: true });
  const tempPath = `${destPath}.download`;
  await fsp.writeFile(tempPath, buffer);
  await fsp.rename(tempPath, destPath);
}

async function _enableOfflineFileProcess(payload) {
  const itemType = String(payload?.item_type || 'file').toLowerCase() === 'folder' ? 'folder' : 'file';
  const apiBase = String(payload?.api_base || '').replace(/\/+$/g, '');
  const accessToken = String(payload?.access_token || '');
  const remotePath = _normalizeRemotePath(String(payload?.remote_path || ''));
  const displayName = String(payload?.display_name || path.basename(remotePath) || 'offline-item');
  const files = Array.isArray(payload?.files) ? payload.files : [];
  const folders = Array.isArray(payload?.folders) ? payload.folders : [];

  if (!remotePath) {
    throw new Error('remote_path が空のため、オフライン利用にできません');
  }

  const localRootPath = _offlineLocalPathForRemotePath(remotePath, displayName);
  let totalBytes = 0;
  let fileCount = 0;
  let folderCount = 0;

  if ((files.length > 0 && apiBase && accessToken) || itemType === 'folder') {
    if (itemType === 'folder') {
      await fsp.mkdir(localRootPath, { recursive: true });
    } else {
      await fsp.mkdir(path.dirname(localRootPath), { recursive: true });
    }

    if (files.length > 0 && (!apiBase || !accessToken)) {
      throw new Error('api_base または access_token が不足しているため、オフライン利用用ファイルを保存できません');
    }

    for (const folderEntry of folders) {
      const folderRemotePath = _normalizeRemotePath(String(folderEntry?.remote_path || ''));
      if (!folderRemotePath) continue;
      await fsp.mkdir(_offlineLocalPathForRemotePath(folderRemotePath, folderEntry?.display_name || path.basename(folderRemotePath)), { recursive: true });
      folderCount += 1;
    }

    for (const fileEntry of files) {
      const downloadToken = _sanitizeString(fileEntry?.download_token, 'download_token');
      const fileRemotePath = _normalizeRemotePath(String(fileEntry?.remote_path || ''));
      if (!fileRemotePath) continue;
      const fileName = _sanitizeDownloadFileName(fileEntry?.display_name || path.basename(fileRemotePath) || 'download.bin');
      const { buffer } = await _downloadFileBufferFromApi({
        api_base: apiBase,
        access_token: accessToken,
        download_token: downloadToken,
        file_name: fileName,
      });
      const destPath = _offlineLocalPathForRemotePath(fileRemotePath, fileName);
      await _writeBufferAtomic(destPath, buffer);
      totalBytes += buffer.length;
      fileCount += 1;
    }

    const baselineSnapshot = await _readLocalTargetSnapshot({
      local_path: localRootPath,
      remote_path: remotePath,
      item_type: itemType,
      display_name: displayName,
      target_kind: 'offline',
    }, false);

    return {
      ok: true,
      local_path: localRootPath,
      remote_path: remotePath,
      display_name: displayName,
      offline_root_path: _offlineRootPath(),
      source_device_label: _backupSourceDeviceLabel(),
      file_count: fileCount,
      folder_count: folderCount,
      bytes: totalBytes,
      baseline_snapshot: baselineSnapshot.exists ? { files: baselineSnapshot.files || {}, dirs: baselineSnapshot.dirs || [] } : null,
      message: itemType === 'folder' ? 'オフライン利用フォルダを保存しました' : 'オフライン利用ファイルを保存しました',
    };
  }

  // 旧方式との互換: download_url が渡された場合だけ単一ファイルとして保存する。
  const downloadUrl = _sanitizeString(payload?.download_url, 'download_url');
  const localPath = localRootPath;
  await fsp.mkdir(path.dirname(localPath), { recursive: true });
  await _downloadUrlToFile(downloadUrl, localPath);
  const legacyBaselineSnapshot = await _readLocalTargetSnapshot({
    local_path: localPath,
    remote_path: remotePath,
    item_type: 'file',
    display_name: displayName,
    target_kind: 'offline',
  }, false);
  return {
    ok: true,
    local_path: localPath,
    remote_path: remotePath,
    display_name: displayName,
    offline_root_path: _offlineRootPath(),
    source_device_label: _backupSourceDeviceLabel(),
    file_count: 1,
    folder_count: 0,
    baseline_snapshot: legacyBaselineSnapshot.exists ? { files: legacyBaselineSnapshot.files || {}, dirs: legacyBaselineSnapshot.dirs || [] } : null,
    message: 'オフライン利用ファイルを保存しました',
  };
}


async function _disableOfflineUseProcess(payload) {
  const descriptor = {
    local_path: payload?.local_path || '',
    remote_path: payload?.remote_path || '',
    remote_item_id: payload?.remote_item_id || payload?.item_id || '',
    item_id: payload?.item_id || payload?.remote_item_id || '',
    display_name: payload?.display_name || '',
  };
  const deleteLocal = payload?.delete_local !== false;
  const current = _loadBackupRuntimeState();
  const currentTargets = Array.isArray(current.targets) ? current.targets : [];
  const removedTargets = currentTargets.filter((target) => _offlineTargetMatchesDescriptor(target, descriptor));
  const retainedTargets = currentTargets.filter((target) => !_offlineTargetMatchesDescriptor(target, descriptor));

  const fallbackLocalPath = String(payload?.local_path || '').trim();
  const localPathsToDelete = Array.from(new Set([
    ...removedTargets.map((target) => String(target.local_path || '').trim()).filter(Boolean),
    fallbackLocalPath,
  ].filter(Boolean)));

  const nextSnapshots = _filterBackupSnapshotsForTargets(current.snapshots, retainedTargets);
  const nextConfig = current.config
    ? { ...current.config, targets: retainedTargets, local_root_display: _summarizeLocalRootDisplay(retainedTargets) }
    : null;
  const nextState = {
    ...current,
    targets: retainedTargets,
    snapshots: nextSnapshots,
    local_root_display: _summarizeLocalRootDisplay(retainedTargets),
    config: nextConfig,
    error: null,
  };
  _saveBackupRuntimeState(nextState);

  if (_backupConfig) {
    _backupConfig = {
      ..._backupConfig,
      targets: (_backupConfig.targets || []).filter((target) => !_offlineTargetMatchesDescriptor(target, descriptor)),
      local_root_display: _summarizeLocalRootDisplay(retainedTargets),
    };
  }

  let deletedLocalCount = 0;
  if (deleteLocal) {
    for (const localPath of localPathsToDelete) {
      if (!localPath) continue;
      await _removeOfflineLocalCopy(localPath);
      deletedLocalCount += 1;
    }
  }

  _backupDebug('offline use disabled', {
    removed_target_count: removedTargets.length,
    delete_local: deleteLocal,
    deleted_local_count: deletedLocalCount,
    remote_path: descriptor.remote_path,
    remote_item_id: descriptor.remote_item_id,
  });

  return {
    ok: true,
    removed_count: removedTargets.length,
    deleted_local_count: deletedLocalCount,
    state: _publicBackupState(_loadBackupRuntimeState()),
    message: deleteLocal ? 'オフライン利用を停止し、ローカルコピーを削除しました' : 'オフライン利用を停止しました',
  };
}

function _logDirPath() {
  return path.join(app.getPath('userData'), LOG_DIR_NAME);
}

function _readJsonSafe(filePath, fallbackValue) {
  try {
    if (!fs.existsSync(filePath)) return fallbackValue;
    const raw = fs.readFileSync(filePath, 'utf-8');
    return JSON.parse(raw);
  } catch {
    return fallbackValue;
  }
}

function _writeJsonAtomic(filePath, value) {
  const tempPath = `${filePath}.tmp`;
  fs.writeFileSync(tempPath, JSON.stringify(value, null, 2), 'utf-8');
  fs.renameSync(tempPath, filePath);
}

function _defaultRuntimeState() {
  return {
    is_running: false,
    pid: null,
    started_at: null,
    stopped_at: null,
    exit_code: null,
    signal: null,
    launch: null,
    log_file: null,
    error: null,
    resume_enabled: false,
    open_at_login_enabled: false,
    resumed_from_disk: false,
  };
}

function _loadRuntimeState() {
  const loaded = _readJsonSafe(_stateFilePath(), _defaultRuntimeState());
  return {
    ..._defaultRuntimeState(),
    ...loaded,
  };
}

function _saveRuntimeState(nextState) {
  _writeJsonAtomic(_stateFilePath(), {
    ..._defaultRuntimeState(),
    ...nextState,
  });
}

function _defaultBackupState() {
  return {
    is_running: false,
    started_at: null,
    stopped_at: null,
    last_scan_at: null,
    last_sync_at: null,
    last_heartbeat_at: null,
    client_id: null,
    status: 'idle',
    pending_changes: 0,
    local_root_display: null,
    targets: [],
    snapshots: {},
    error: null,
    config: null,
    resume_enabled: false,
    open_at_login_enabled: false,
    resumed_from_disk: false,
    secret_encrypted_b64: null,
  };
}

function _loadBackupRuntimeState() {
  return _readJsonSafe(_backupStateFilePath(), _defaultBackupState());
}

function _saveBackupRuntimeState(nextState) {
  _writeJsonAtomic(_backupStateFilePath(), nextState);
}

function _publicBackupState(state) {
  const current = state || _loadBackupRuntimeState();
  return {
    is_running: Boolean(current.is_running),
    started_at: current.started_at || null,
    stopped_at: current.stopped_at || null,
    last_scan_at: current.last_scan_at || null,
    last_sync_at: current.last_sync_at || null,
    last_heartbeat_at: current.last_heartbeat_at || null,
    client_id: current.client_id || null,
    status: current.status || 'idle',
    pending_changes: Number(current.pending_changes || 0),
    local_root_display: current.local_root_display || null,
    targets: Array.isArray(current.targets) ? current.targets : [],
    current_device_label: _backupSourceDeviceLabel(),
    error: current.error || null,
    resume_enabled: Boolean(current.resume_enabled),
    open_at_login_enabled: Boolean(current.open_at_login_enabled),
    resumed_from_disk: Boolean(current.resumed_from_disk),
  };
}

function _targetKind(target) {
  const kind = String(target?.target_kind || target?.purpose || 'backup').toLowerCase();
  const localPath = String(target?.local_path || '').replace(/\\/g, '/');
  // 旧版で target_kind が付かずに保存されたオフライン対象も通常バックアップから分離する。
  if (kind === 'offline' || localPath.includes('/Phase1 Offline/') || localPath.endsWith('/Phase1 Offline')) return 'offline';
  return 'backup';
}

function _isOfflineTarget(target) {
  return _targetKind(target) === 'offline';
}

function _backupSettingTargetsOnly(targets) {
  return (Array.isArray(targets) ? targets : []).filter((target) => !_isOfflineTarget(target));
}

function _snapshotKeyForTarget(target) {
  return `${_targetKind(target)}:${target.item_type}:${target.local_path}=>${target.remote_path}`;
}

function _summarizeLocalRootDisplay(targets) {
  const all = Array.isArray(targets) ? targets : [];
  const list = _backupSettingTargetsOnly(all);
  if (!list.length) return all.length ? 'オフライン利用中' : 'バックアップ対象なし';
  if (list.length === 1) return list[0].local_path || list[0].display_name || 'バックアップ対象';
  return `${list[0].local_path || list[0].display_name || 'バックアップ対象'} ほか ${list.length - 1}件`;
}

function _filterBackupSnapshotsForTargets(snapshots, targets) {
  const current = snapshots && typeof snapshots === 'object' ? snapshots : {};
  const keys = new Set((Array.isArray(targets) ? targets : []).map(_snapshotKeyForTarget));
  return Object.fromEntries(Object.entries(current).filter(([key]) => keys.has(key)));
}

function _sanitizeString(value, fieldName) {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new Error(`${fieldName} が空です`);
  }
  return value.trim();
}

function _sanitizeInt(value, fieldName, minValue = 0) {
  const n = Number(value);
  if (!Number.isFinite(n) || !Number.isInteger(n) || n < minValue) {
    throw new Error(`${fieldName} は ${minValue} 以上の整数である必要があります`);
  }
  return n;
}

function _readFileTail(filePath, maxBytes = 12000) {
  try {
    if (!filePath || !fs.existsSync(filePath)) return '';
    const stat = fs.statSync(filePath);
    const size = Number(stat.size || 0);
    const start = Math.max(0, size - maxBytes);
    const fd = fs.openSync(filePath, 'r');
    try {
      const buffer = Buffer.alloc(size - start);
      fs.readSync(fd, buffer, 0, buffer.length, start);
      return buffer.toString('utf8').trim();
    } finally {
      try { fs.closeSync(fd); } catch {}
    }
  } catch {
    return '';
  }
}

function _pythonCandidates(explicitPythonPath = '') {
  const candidates = [];
  const add = (command, args = [], label = null) => {
    const cmd = String(command || '').trim();
    if (!cmd) return;
    const key = [cmd, ...(args || [])].join('\u0000');
    if (candidates.some((item) => item.key === key)) return;
    candidates.push({ key, command: cmd, args: Array.isArray(args) ? args : [], label: label || [cmd, ...(args || [])].join(' ') });
  };

  if (explicitPythonPath) add(explicitPythonPath, [], explicitPythonPath);

  const roots = [
    process.resourcesPath || '',
    app.getAppPath ? app.getAppPath() : '',
    process.cwd(),
  ];
  for (const root of roots) {
    if (!root) continue;
    add(path.join(root, 'python', 'python.exe'));
    add(path.join(root, 'python-embed', 'python.exe'));
    add(path.join(root, 'runtime', 'python', 'python.exe'));
    add(path.join(root, 'build', 'runtime', 'python', 'python.exe'));
    add(path.join(root, 'resources', 'runtime', 'python', 'python.exe'));
  }

  if (process.platform === 'win32') {
    add('py', ['-3'], 'py -3');
    add('python', [], 'python');
    add('python3', [], 'python3');
  } else {
    add('python3', [], 'python3');
    add('python', [], 'python');
  }
  return candidates;
}

function _pythonEnvForRunner(runnerDir) {
  const extraPaths = [runnerDir].filter(Boolean);
  const current = String(process.env.PYTHONPATH || '').trim();
  if (current) extraPaths.push(current);
  return {
    ...process.env,
    PYTHONIOENCODING: 'utf-8',
    PYTHONUNBUFFERED: '1',
    PYTHONPATH: extraPaths.join(path.delimiter),
  };
}

function _runPython(candidate, args, options = {}) {
  return spawnSync(candidate.command, [...candidate.args, ...args], {
    cwd: options.cwd || process.cwd(),
    env: options.env || process.env,
    windowsHide: true,
    encoding: 'utf8',
    timeout: options.timeout || 30000,
  });
}

function _inspectPython(candidate, runnerDir) {
  // Windows embeddable Python uses pythonXY._pth and can run in isolated mode.
  // Be defensive: add bundled site-packages and native wheel DLL folders before
  // checking zmq/backend modules, exactly like the runner does at runtime.
  const script = `
import os, sys, json, importlib.util, traceback
handles=[]
base=os.path.dirname(sys.executable)
site_packages=os.path.join(base, "Lib", "site-packages")
if os.path.isdir(site_packages) and site_packages not in sys.path:
    sys.path.insert(0, site_packages)
for name in ("pyzmq.libs", "zmq.libs"):
    p=os.path.join(site_packages, name)
    if os.path.isdir(p):
        if hasattr(os, "add_dll_directory"):
            try:
                handles.append(os.add_dll_directory(p))
            except Exception:
                pass
        os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")
try:
    for entry in os.listdir(site_packages):
        p=os.path.join(site_packages, entry)
        if os.path.isdir(p) and entry.endswith(".libs"):
            if hasattr(os, "add_dll_directory"):
                try:
                    handles.append(os.add_dll_directory(p))
                except Exception:
                    pass
            os.environ["PATH"] = p + os.pathsep + os.environ.get("PATH", "")
except Exception:
    pass
runner=os.getcwd()
if runner and runner not in sys.path:
    sys.path.insert(0, runner)
mods={}
errors={}
for name in ["zmq","crypto_common_keywrap","node_phase1_runner"]:
    try:
        mods[name] = importlib.util.find_spec(name) is not None
        if name == "zmq" and mods[name]:
            import zmq
            mods["zmq_import"] = True
            mods["zmq_version"] = getattr(zmq, "__version__", "")
    except Exception as exc:
        mods[name] = False
        errors[name] = traceback.format_exc()
print(json.dumps({"executable":sys.executable,"version":sys.version.split()[0],"cwd":os.getcwd(),"path0":sys.path[:8],"site_packages":site_packages,"mods":mods,"errors":errors}, ensure_ascii=False))
`;
  const result = _runPython(candidate, ['-c', script], { cwd: runnerDir, env: _pythonEnvForRunner(runnerDir), timeout: 15000 });
  if (result.status !== 0) {
    return { ok: false, error: `${candidate.label}: ${String(result.stderr || result.stdout || result.error?.message || 'Python起動失敗').trim()}` };
  }
  try {
    const parsed = JSON.parse(String(result.stdout || '').trim().split(/\r?\n/).pop() || '{}');
    return { ok: true, ...parsed };
  } catch {
    return { ok: false, error: `${candidate.label}: Python確認結果を解析できません: ${String(result.stdout || '').trim()}` };
  }
}

function _tryEnsurePip(candidate, runnerDir) {
  let result = _runPython(candidate, ['-m', 'pip', '--version'], { cwd: runnerDir, env: _pythonEnvForRunner(runnerDir), timeout: 20000 });
  if (result.status === 0) return { ok: true };

  result = _runPython(candidate, ['-m', 'ensurepip', '--upgrade'], { cwd: runnerDir, env: _pythonEnvForRunner(runnerDir), timeout: 60000 });
  if (result.status !== 0) {
    return { ok: false, error: String(result.stderr || result.stdout || result.error?.message || 'pip/ensurepip を利用できません').trim() };
  }
  return { ok: true };
}

function _pythonWheelDirs() {
  const appPath = app.getAppPath ? app.getAppPath() : '';
  const roots = [
    process.resourcesPath || '',
    process.cwd(),
    appPath,
    path.dirname(process.resourcesPath || ''),
  ].filter(Boolean);
  const candidates = [];
  for (const root of roots) {
    candidates.push(path.join(root, 'python-wheels'));
    candidates.push(path.join(root, 'build', 'python-wheels'));
    candidates.push(path.join(root, 'resources', 'python-wheels'));
  }
  const seen = new Set();
  return candidates.filter((dir) => {
    const resolved = path.resolve(dir);
    if (seen.has(resolved)) return false;
    seen.add(resolved);
    try {
      return fs.existsSync(resolved) && fs.statSync(resolved).isDirectory() && fs.readdirSync(resolved).some((name) => /\.whl$/i.test(name));
    } catch {
      return false;
    }
  });
}

function _tryInstallPyZmq(candidate, runnerDir) {
  const pip = _tryEnsurePip(candidate, runnerDir);
  if (!pip.ok) return pip;

  const wheelErrors = [];
  for (const wheelDir of _pythonWheelDirs()) {
    const offlineResult = _runPython(
      candidate,
      ['-m', 'pip', 'install', '--no-index', '--find-links', wheelDir, '--disable-pip-version-check', 'pyzmq'],
      { cwd: runnerDir, env: _pythonEnvForRunner(runnerDir), timeout: 180000 }
    );
    if (offlineResult.status === 0) {
      return { ok: true, source: `local wheel: ${wheelDir}` };
    }
    wheelErrors.push(`${wheelDir}: ${String(offlineResult.stderr || offlineResult.stdout || offlineResult.error?.message || 'failed').trim()}`);
  }

  const result = _runPython(
    candidate,
    ['-m', 'pip', 'install', '--user', '--disable-pip-version-check', 'pyzmq'],
    { cwd: runnerDir, env: _pythonEnvForRunner(runnerDir), timeout: 180000 }
  );
  if (result.status !== 0) {
    const onlineError = String(result.stderr || result.stdout || result.error?.message || 'pyzmq のインストールに失敗しました').trim();
    const wheelErrorText = wheelErrors.length ? ` ローカルwheelも失敗: ${wheelErrors.join(' / ')}` : '';
    return { ok: false, error: `${onlineError}${wheelErrorText}` };
  }
  return { ok: true, source: 'online pip' };
}

function _resolvePythonForRunner(explicitPythonPath, runnerDir) {
  const diagnostics = [];
  for (const candidate of _pythonCandidates(explicitPythonPath)) {
    let inspected = _inspectPython(candidate, runnerDir);
    if (!inspected.ok) {
      diagnostics.push(inspected.error);
      continue;
    }

    const mods = inspected.mods || {};
    if (!mods.crypto_common_keywrap) {
      diagnostics.push(`${candidate.label}: backend/crypto_common_keywrap.py をPythonからimport対象として認識できません。backend同梱またはsys.path設定を確認してください。cwd=${inspected.cwd || ""}`);
      continue;
    }

    if (!mods.zmq) {
      const install = _tryInstallPyZmq(candidate, runnerDir);
      if (!install.ok) {
        diagnostics.push(`${candidate.label}: pyzmq がありません。自動インストールにも失敗しました: ${install.error}`);
        continue;
      }
      inspected = _inspectPython(candidate, runnerDir);
      if (!inspected.ok || !inspected.mods?.zmq) {
        diagnostics.push(`${candidate.label}: pyzmq インストール後も import zmq に失敗しました。`);
        continue;
      }
    }

    return {
      command: candidate.command,
      args: candidate.args,
      label: candidate.label,
      executable: inspected.executable || candidate.command,
      version: inspected.version || '',
      diagnostics,
    };
  }

  throw new Error(
    'このPCでストレージノード用Python環境を準備できませんでした。' +
    ' Python 3 と pyzmq が必要です。詳細: ' + diagnostics.join(' / ')
  );
}


function _isLoopbackNodeEndpoint(value) {
  const raw = String(value || '').trim().toLowerCase();
  return !raw ||
    raw === 'tcp://127.0.0.1:9999' ||
    raw === 'tcp://localhost:9999' ||
    raw === 'tcp://0.0.0.0:9999' ||
    raw === 'tcp://*:9999' ||
    raw.includes('127.0.0.1') ||
    raw.includes('localhost') ||
    raw.includes('0.0.0.0') ||
    raw.includes('*:9999');
}

function _externalNodeEndpointFromLaunch(value) {
  const raw = String(value || '').trim();
  if (!_isLoopbackNodeEndpoint(raw)) return raw.replace(/\/$/, '');

  const envEndpoint = String(process.env.TRICLOUD_NODE_SERVER_ENDPOINT || process.env.NODE_SERVER_ENDPOINT || '').trim();
  if (envEndpoint && !_isLoopbackNodeEndpoint(envEndpoint)) return envEndpoint.replace(/\/$/, '');

  const apiBase = String(process.env.VITE_API_BASE || '').trim();
  try {
    if (apiBase) {
      const parsed = new URL(apiBase);
      if (parsed.hostname) return `tcp://${parsed.hostname}:9999`;
    }
  } catch {}

  return 'tcp://api.trytricloud.com:9999';
}

function _uniqueExistingPathCandidates(values) {
  const seen = new Set();
  const out = [];
  for (const value of values) {
    if (!value) continue;
    const resolved = path.resolve(String(value));
    if (seen.has(resolved)) continue;
    seen.add(resolved);
    out.push(resolved);
  }
  return out;
}

function _resolveRunnerPath(explicitRunnerPath) {
  const appPath = app.getAppPath();
  const here = __dirname;

  // launch がGCP/Linux側のパスや C:\opt\... のようなサーバー由来パスを含む場合がある。
  // ユーザーPC上に存在しない明示パスはエラーにせず、ローカル同梱 runner の探索へフォールバックする。
  const explicitCandidates = [];
  if (explicitRunnerPath && String(explicitRunnerPath).trim()) {
    explicitCandidates.push(String(explicitRunnerPath).trim());
  }

  const candidates = _uniqueExistingPathCandidates([
    ...explicitCandidates,

    // electron-builder で extraResources/backend に入れた場合。インストール版ではここが最優先。
    path.join(process.resourcesPath || '', 'backend', 'node_phase1_runner.py'),
    path.join(process.resourcesPath || '', 'node_phase1_runner.py'),

    // npm run electron:dev をプロジェクト直下で実行する場合
    path.join(process.cwd(), 'backend', 'node_phase1_runner.py'),
    path.join(process.cwd(), 'node_phase1_runner.py'),
    path.join(process.cwd(), 'desktop_node', 'node_phase1_runner.py'),

    // Electron main が electron/ 配下にある開発構成
    path.join(here, '..', 'backend', 'node_phase1_runner.py'),
    path.join(here, 'backend', 'node_phase1_runner.py'),
    path.join(here, 'node_phase1_runner.py'),
    path.join(here, 'desktop_node', 'node_phase1_runner.py'),

    // Electron が返すアプリケーションルート基準。ただし app.asar 内はPythonから直接実行できないため後段で除外する。
    path.join(appPath, 'backend', 'node_phase1_runner.py'),
    path.join(appPath, 'node_phase1_runner.py'),
    path.join(appPath, 'desktop_node', 'node_phase1_runner.py'),
  ]);

  const incomplete = [];
  for (const candidate of candidates) {
    // Electron の app.asar 内パスは Electron の fs では見えても、外部Pythonからは通常のファイルとして読めない。
    // node_phase1_runner.py は extraResources/backend など、実ファイルとして存在する場所を使う。
    if (String(candidate).includes('.asar')) continue;
    if (!fs.existsSync(candidate)) continue;

    const completeness = _runnerDirCompleteness(candidate);
    if (completeness.ok) {
      return candidate;
    }
    incomplete.push(`${candidate}: missing ${completeness.missing.join(', ')}`);
  }

  const explicitNote = explicitCandidates.length
    ? `サーバー由来の候補: ${explicitCandidates.join(' / ')}。`
    : '';
  const incompleteNote = incomplete.length ? ` 不完全なbackend候補: ${incomplete.join(' / ')}` : '';
  throw new Error(
    `${explicitNote}ローカルのストレージノード実行ファイル一式が見つかりません。backend/node_phase1_runner.py, backend/node.py, backend/crypto_common_keywrap.py を同じ backend フォルダへ同梱してください。検索候補: ${candidates.join(' / ')}${incompleteNote}`
  );
}


function _runnerDirCompleteness(runnerPath) {
  const dir = path.dirname(runnerPath);
  const required = ['node_phase1_runner.py', 'node.py', 'crypto_common_keywrap.py'];
  const missing = required.filter((name) => !fs.existsSync(path.join(dir, name)));
  return { ok: missing.length === 0, dir, missing };
}

function _processExists(pid) {
  if (!pid || Number(pid) <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function _buildSpawnArgs(payload) {
  const launch = payload?.launch || payload || {};

  const nodeId = _sanitizeString(launch.node_id, 'node_id');
  const nodeApiKey = _sanitizeString(launch.node_api_key, 'node_api_key');
  const server = _sanitizeString(_externalNodeEndpointFromLaunch(launch.server || 'tcp://127.0.0.1:9999'), 'server');
  const storageDir = _resolveNodeStorageDir(launch.storage_dir);
  _protectNodeStorageRoot(storageDir);
  const capacityGb = _sanitizeInt(launch.capacity_gb, 'capacity_gb', 0);

  const runnerPath = _resolveRunnerPath(launch.runner_path || launch.runnerPath);
  const runnerDir = path.dirname(runnerPath);
  const python = _resolvePythonForRunner(launch.python_path || launch.pythonPath, runnerDir);

  const args = [
    ...python.args,
    runnerPath,
    '--node-id', nodeId,
    '--node-api-key', nodeApiKey,
    '--server', server,
    '--storage-dir', storageDir,
    '--capacity-gb', String(capacityGb),
  ];

  return {
    pythonPath: python.command,
    args,
    pythonInfo: python,
    normalizedLaunch: {
      node_id: nodeId,
      node_api_key: nodeApiKey,
      server,
      storage_dir: storageDir,
      capacity_gb: capacityGb,
      runner_path: runnerPath,
      python_path: python.label,
      python_executable: python.executable,
      python_version: python.version,
    },
  };
}

function _publicNodeState(state) {
  const current = state || _loadRuntimeState();
  const logTail = _readFileTail(current.log_file, 12000);
  return {
    ..._defaultRuntimeState(),
    ...current,
    log_tail: logTail,
    resume_enabled: Boolean(current.resume_enabled),
    open_at_login_enabled: Boolean(current.open_at_login_enabled),
    resumed_from_disk: Boolean(current.resumed_from_disk),
  };
}

async function _startNodeProcess(payload, options = {}) {
  const { resumedFromDisk = false } = options;
  const current = _loadRuntimeState();
  if (current.is_running && _processExists(current.pid)) {
    return {
      ok: true,
      alreadyRunning: true,
      pid: current.pid,
      state: _publicNodeState(current),
      message: 'ノードはすでに起動中です',
    };
  }

  const { pythonPath, args, normalizedLaunch } = _buildSpawnArgs(payload);
  _ensureDir(_logDirPath());

  const logFile = path.join(
    _logDirPath(),
    `node-${normalizedLaunch.node_id}-${Date.now()}.log`
  );
  const logFd = fs.openSync(logFile, 'a');

  // Always write a launch record from Electron itself. This guarantees that
  // even a Python startup/import failure leaves a non-empty diagnostic log.
  try {
    fs.appendFileSync(
      logFile,
      `[electron ${_nowIso()}] node launch requested ${JSON.stringify({
        node_id: normalizedLaunch.node_id,
        server: normalizedLaunch.server,
        storage_dir: normalizedLaunch.storage_dir,
        capacity_gb: normalizedLaunch.capacity_gb,
        runner_path: normalizedLaunch.runner_path,
        python_path: normalizedLaunch.python_path,
        python_executable: normalizedLaunch.python_executable || null,
        python_version: normalizedLaunch.python_version || null,
        api_key_present: Boolean(normalizedLaunch.node_api_key),
      })}` + '\n',
      'utf8',
    );
  } catch {}

  const child = spawn(pythonPath, args, {
    cwd: path.dirname(normalizedLaunch.runner_path),
    detached: false,
    shell: false,
    stdio: ['ignore', logFd, logFd],
    windowsHide: true,
    env: {
      ..._pythonEnvForRunner(path.dirname(normalizedLaunch.runner_path)),
      PHASE1_ORIGINAL_NODE_PATH: path.join(path.dirname(normalizedLaunch.runner_path), 'node.py'),
    },
  });

  const startedAt = _nowIso();
  const nextState = {
    ...current,
    is_running: true,
    pid: child.pid,
    started_at: startedAt,
    stopped_at: null,
    exit_code: null,
    signal: null,
    launch: normalizedLaunch,
    log_file: logFile,
    error: null,
    resume_enabled: true,
    resumed_from_disk: Boolean(resumedFromDisk),
  };
  nextState.open_at_login_enabled = _applySharedOpenAtLogin(nextState, null);
  _saveRuntimeState(nextState);

  child.on('error', (err) => {
    try {
      fs.appendFileSync(
        logFile,
        `[electron ${_nowIso()}] spawn error: ${String(err?.stack || err?.message || err || 'unknown error')}` + '\n',
        'utf8',
      );
    } catch {}
    const failed = {
      ..._loadRuntimeState(),
      is_running: false,
      stopped_at: _nowIso(),
      error: String(err?.message || err || 'spawn error'),
      resumed_from_disk: false,
    };
    failed.open_at_login_enabled = _applySharedOpenAtLogin(failed, null);
    _saveRuntimeState(failed);
  });

  child.on('exit', (code, signal) => {
    try {
      fs.appendFileSync(
        logFile,
        `[electron ${_nowIso()}] node process exited: code=${String(code)} signal=${String(signal)}` + '\n',
        'utf8',
      );
    } catch {}
    const exited = {
      ..._loadRuntimeState(),
      is_running: false,
      stopped_at: _nowIso(),
      exit_code: code,
      signal: signal,
      resumed_from_disk: false,
    };
    exited.open_at_login_enabled = _applySharedOpenAtLogin(exited, null);
    _saveRuntimeState(exited);

    try {
      fs.closeSync(logFd);
    } catch {}
  });

  await new Promise((resolve) => setTimeout(resolve, 2500));
  const latest = _loadRuntimeState();
  const aliveAfterStart = latest.is_running && _processExists(latest.pid);
  if (!aliveAfterStart) {
    const logTail = _readFileTail(latest.log_file || logFile, 16000);
    const errorText = latest.error || logTail || 'ノードプロセスが起動直後に終了しました。';
    const failed = {
      ...latest,
      is_running: false,
      stopped_at: latest.stopped_at || _nowIso(),
      error: errorText,
      resumed_from_disk: false,
    };
    failed.open_at_login_enabled = _applySharedOpenAtLogin(failed, null);
    _saveRuntimeState(failed);
    return {
      ok: false,
      alreadyRunning: false,
      pid: child.pid,
      state: _publicNodeState(failed),
      error: errorText,
      message: 'ノードプロセスが起動直後に終了しました',
    };
  }

  return {
    ok: true,
    alreadyRunning: false,
    pid: child.pid,
    state: _publicNodeState(latest),
    message: resumedFromDisk ? '前回設定からノードを自動再開しました' : 'ノードを起動しました',
  };
}

function _stopNodeProcess() {
  const current = _loadRuntimeState();
  if (!current.is_running || !_processExists(current.pid)) {
    const nextState = {
      ...current,
      is_running: false,
      stopped_at: _nowIso(),
      resume_enabled: false,
      resumed_from_disk: false,
    };
    nextState.open_at_login_enabled = _applySharedOpenAtLogin(nextState, null);
    _saveRuntimeState(nextState);
    return {
      ok: true,
      alreadyStopped: true,
      state: _publicNodeState(nextState),
      message: 'ノードは停止済みです',
    };
  }

  try {
    if (process.platform === 'win32') {
      const killer = spawn('taskkill', ['/pid', String(current.pid), '/t', '/f'], {
        windowsHide: true,
        shell: false,
        stdio: 'ignore',
      });
      killer.on('exit', () => {});
    } else {
      process.kill(current.pid, 'SIGTERM');
    }
  } catch (err) {
    return {
      ok: false,
      error: String(err?.message || err || 'stop failed'),
      state: _publicNodeState(current),
      message: 'ノード停止に失敗しました',
    };
  }

  const nextState = {
    ...current,
    is_running: false,
    stopped_at: _nowIso(),
    resume_enabled: false,
    resumed_from_disk: false,
  };
  nextState.open_at_login_enabled = _applySharedOpenAtLogin(nextState, null);
  _saveRuntimeState(nextState);

  return {
    ok: true,
    alreadyStopped: false,
    state: _publicNodeState(nextState),
    message: 'ノード停止要求を送信しました',
  };
}

function _getNodeStatus() {
  const current = _loadRuntimeState();
  const alive = current.is_running && _processExists(current.pid);
  if (alive === current.is_running) {
    return {
      ok: true,
      state: _publicNodeState({
        ...current,
        open_at_login_enabled: _safeGetOpenAtLoginEnabled() || _desiredOpenAtLogin(current, null),
      }),
    };
  }

  const nextState = {
    ...current,
    is_running: alive,
    stopped_at: alive ? current.stopped_at : current.stopped_at || _nowIso(),
    resumed_from_disk: false,
  };
  nextState.open_at_login_enabled = _safeGetOpenAtLoginEnabled() || _desiredOpenAtLogin(nextState, null);
  _saveRuntimeState(nextState);
  return {
    ok: true,
    state: _publicNodeState(nextState),
  };
}

async function _resumeNodeOnAppReady() {
  if (_nodeResumeAttempted) return;
  _nodeResumeAttempted = true;

  const current = _loadRuntimeState();
  if (!current.launch || !current.resume_enabled) return;

  if (current.is_running && _processExists(current.pid)) {
    const nextState = {
      ...current,
      open_at_login_enabled: _safeGetOpenAtLoginEnabled() || _desiredOpenAtLogin(current, null),
      resumed_from_disk: false,
    };
    _saveRuntimeState(nextState);
    return;
  }

  try {
    await _startNodeProcess({ launch: current.launch }, { resumedFromDisk: true });
  } catch (err) {
    const failed = {
      ...current,
      is_running: false,
      stopped_at: _nowIso(),
      error: String(err?.message || err || 'ノード自動再開に失敗しました'),
      resumed_from_disk: false,
    };
    failed.open_at_login_enabled = _applySharedOpenAtLogin(failed, null);
    _saveRuntimeState(failed);
  }
}

function _sanitizeUrlBase(value, fieldName) {
  const raw = _sanitizeString(value, fieldName);
  return raw.replace(/\/$/, '');
}

function _loginItemSupported() {
  return process.platform === 'win32' || process.platform === 'darwin';
}

function _safeSetOpenAtLogin(enabled) {
  if (!_loginItemSupported()) return false;
  try {
    if (process.platform === 'win32') {
      app.setLoginItemSettings({
        openAtLogin: Boolean(enabled),
        enabled: Boolean(enabled),
      });
    } else {
      app.setLoginItemSettings({
        openAtLogin: Boolean(enabled),
      });
    }
    return Boolean(enabled);
  } catch {
    return false;
  }
}

function _safeGetOpenAtLoginEnabled() {
  if (!_loginItemSupported()) return false;
  try {
    const settings = app.getLoginItemSettings();
    return Boolean(settings?.openAtLogin);
  } catch {
    return false;
  }
}

function _desiredOpenAtLogin(nextNodeState = null, nextBackupState = null) {
  const nodeState = nextNodeState || _loadRuntimeState();
  const backupState = nextBackupState || _loadBackupRuntimeState();
  return Boolean(nodeState?.resume_enabled || backupState?.resume_enabled);
}

function _applySharedOpenAtLogin(nextNodeState = null, nextBackupState = null) {
  const desired = _desiredOpenAtLogin(nextNodeState, nextBackupState);
  return _safeSetOpenAtLogin(desired);
}

function _setBackupOpenAtLogin(enabled) {
  const nextBackupState = {
    ..._loadBackupRuntimeState(),
    resume_enabled: Boolean(enabled),
    open_at_login_enabled: Boolean(enabled),
  };
  return _applySharedOpenAtLogin(null, nextBackupState);
}

function _encryptBackupPassword(password) {
  if (!safeStorage || !safeStorage.isEncryptionAvailable || !safeStorage.isEncryptionAvailable()) {
    throw new Error('OS の安全な資格情報保存が利用できないため、自動再開を有効にできません');
  }
  return safeStorage.encryptString(String(password)).toString('base64');
}

function _decryptBackupPassword(secretEncryptedB64) {
  if (!secretEncryptedB64) {
    throw new Error('保存済みのバックアップ資格情報が見つかりません');
  }
  if (!safeStorage || !safeStorage.isEncryptionAvailable || !safeStorage.isEncryptionAvailable()) {
    throw new Error('OS の安全な資格情報保存が利用できないため、自動再開を復元できません');
  }
  return safeStorage.decryptString(Buffer.from(String(secretEncryptedB64), 'base64'));
}

function _buildPersistedBackupConfig(config) {
  return {
    api_base: config.api_base,
    email: config.email,
    polling_interval_sec: config.polling_interval_sec,
    ignore_hidden: config.ignore_hidden,
    local_root_display: config.local_root_display,
    targets: config.targets,
    client_id: config.client_id,
  };
}

function _normalizeRemotePath(value) {
  return String(value || '').replace(/\\/g, '/').replace(/^\/+/, '').replace(/\/+$/, '');
}

function _normalizeBackupTarget(target) {
  const localPath = path.resolve(_sanitizeString(target?.local_path, 'local_path'));
  const remotePath = _normalizeRemotePath(_sanitizeString(target?.remote_path, 'remote_path'));
  const itemType = String(target?.item_type || '').trim();
  if (!['file', 'folder'].includes(itemType)) {
    throw new Error('item_type は file または folder である必要があります');
  }
  return {
    local_path: localPath,
    remote_path: remotePath,
    item_type: itemType,
    display_name: String(target?.display_name || path.basename(localPath) || remotePath || localPath),
    source_device_label: String(target?.source_device_label || target?.device_label || _backupSourceDeviceLabel()),
    remote_item_id: target?.remote_item_id ? String(target.remote_item_id) : null,
    target_kind: _targetKind(target),
    baseline_snapshot: target?.baseline_snapshot || null,
  };
}

function _normalizeBackupPayload(payload) {
  const apiBase = _sanitizeUrlBase(payload?.api_base, 'api_base');
  const accessToken = String(payload?.access_token || '').trim();
  const email = _sanitizeString(payload?.email, 'email');
  const password = String(payload?.password || '').trim();
  const pollingIntervalSec = _sanitizeInt(payload?.polling_interval_sec, 'polling_interval_sec', 2);
  const ignoreHidden = Boolean(payload?.ignore_hidden);
  const localRootDisplay = String(payload?.local_root_display || '').trim() || 'バックアップ設定';
  const targets = Array.from(payload?.targets || []).map(_normalizeBackupTarget);
  if (!targets.length) {
    throw new Error('バックアップ対象がありません');
  }

  return {
    api_base: apiBase,
    access_token: accessToken,
    email,
    password,
    polling_interval_sec: pollingIntervalSec,
    ignore_hidden: ignoreHidden,
    local_root_display: localRootDisplay,
    targets,
    client_id: payload?.client_id ? String(payload.client_id) : `desktop-backup-${crypto.randomUUID()}`,
  };
}

function _joinRemotePath(...parts) {
  return parts
    .map((part) => _normalizeRemotePath(part))
    .filter(Boolean)
    .join('/');
}

function _dirnameRemote(remotePath) {
  const normalized = _normalizeRemotePath(remotePath);
  if (!normalized.includes('/')) return '';
  return normalized.split('/').slice(0, -1).join('/');
}

function _basenameRemote(remotePath) {
  const normalized = _normalizeRemotePath(remotePath);
  if (!normalized) return '';
  const parts = normalized.split('/');
  return parts[parts.length - 1] || '';
}

function _isHiddenName(name) {
  return typeof name === 'string' && name.startsWith('.');
}

async function _safeStat(filePath) {
  try {
    return await fsp.stat(filePath);
  } catch {
    return null;
  }
}

async function _hashFileSha256(filePath) {
  return await new Promise((resolve, reject) => {
    const hash = crypto.createHash('sha256');
    const stream = fs.createReadStream(filePath);
    stream.on('data', (chunk) => hash.update(chunk));
    stream.on('error', reject);
    stream.on('end', () => resolve(hash.digest('hex')));
  });
}

async function _buildLocalFileFingerprint(absPath, stat) {
  const size = Number(stat?.size || 0);
  const mtimeMs = Number(Math.floor(stat?.mtimeMs || 0));
  let sha256 = null;
  try {
    sha256 = await _hashFileSha256(absPath);
  } catch {
    // Word/Excel/PDFなどが保存中・ロック中の場合でも監視ループを止めない。
    // この場合は次回スキャンで再試行し、最低限 size/mtime で差分判定する。
    sha256 = null;
  }
  return {
    abs_path: absPath,
    size,
    mtime_ms: mtimeMs,
    sha256,
  };
}

function _fileSnapshotChanged(prevFile, currentFile) {
  if (!prevFile || !currentFile) return true;
  const prevHash = prevFile.sha256 || null;
  const currentHash = currentFile.sha256 || null;
  if (prevHash && currentHash && prevHash !== currentHash) return true;
  if (Number(prevFile.size || 0) !== Number(currentFile.size || 0)) return true;
  if (Number(prevFile.mtime_ms || 0) !== Number(currentFile.mtime_ms || 0)) return true;
  // sha256 が無い旧stateでも、size/mtime が同じなら変更なしとして扱う。
  // ここで true にすると、開始直後や旧state移行直後に同名ファイルを再アップロードしてしまう。
  return false;
}

function _snapshotFilesChanged(prevFiles, currentFiles) {
  const prev = prevFiles || {};
  const current = currentFiles || {};
  const keys = new Set([...Object.keys(prev), ...Object.keys(current)]);
  for (const key of keys) {
    if (!Object.prototype.hasOwnProperty.call(prev, key)) return true;
    if (!Object.prototype.hasOwnProperty.call(current, key)) return true;
    if (_fileSnapshotChanged(prev[key], current[key])) return true;
  }
  return false;
}

function _snapshotDirsChanged(prevDirs, currentDirs) {
  const prev = Array.isArray(prevDirs) ? prevDirs : [];
  const current = Array.isArray(currentDirs) ? currentDirs : [];
  if (prev.length !== current.length) return true;
  const prevSorted = [...prev].sort();
  const currentSorted = [...current].sort();
  return prevSorted.some((value, index) => value !== currentSorted[index]);
}


function _relDirname(relPath) {
  const normalized = _normalizeRemotePath(relPath);
  if (!normalized || !normalized.includes('/')) return '';
  return normalized.split('/').slice(0, -1).join('/');
}

function _fileIdentitySame(a, b) {
  if (!a || !b) return false;
  const aHash = a.sha256 || null;
  const bHash = b.sha256 || null;
  if (aHash && bHash) return aHash === bHash;
  return Number(a.size || 0) === Number(b.size || 0)
    && Number(a.mtime_ms || 0) === Number(b.mtime_ms || 0);
}

function _detectSameFolderFileRenames(prevFiles, currentFiles) {
  const prev = prevFiles || {};
  const current = currentFiles || {};
  const deleted = Object.keys(prev).filter((relPath) => !Object.prototype.hasOwnProperty.call(current, relPath));
  const added = Object.keys(current).filter((relPath) => !Object.prototype.hasOwnProperty.call(prev, relPath));
  const usedAdded = new Set();
  const pairs = [];

  for (const oldRel of deleted) {
    const oldDir = _relDirname(oldRel);
    const match = added.find((newRel) => {
      if (usedAdded.has(newRel)) return false;
      // 安全のため、まずは同一フォルダ内の「名前変更」だけをリネームとして扱う。
      // 別フォルダへの移動まで自動判定すると、コピー追加＋削除を誤って移動扱いするリスクがある。
      if (_relDirname(newRel) !== oldDir) return false;
      return _fileIdentitySame(prev[oldRel], current[newRel]);
    });
    if (!match) continue;
    usedAdded.add(match);
    pairs.push({ oldRel, newRel: match });
  }

  return pairs;
}

function _isPathInsideRelDir(relPath, dirRel) {
  const rel = _normalizeRemotePath(relPath);
  const dir = _normalizeRemotePath(dirRel);
  if (!dir) return Boolean(rel);
  return rel === dir || rel.startsWith(`${dir}/`);
}

function _stripRelDirPrefix(relPath, dirRel) {
  const rel = _normalizeRemotePath(relPath);
  const dir = _normalizeRemotePath(dirRel);
  if (!dir) return rel;
  if (rel === dir) return '';
  return rel.startsWith(`${dir}/`) ? rel.slice(dir.length + 1) : rel;
}

function _folderFileIdentitySignature(files, dirRel) {
  const result = [];
  const dir = _normalizeRemotePath(dirRel);
  for (const [relPath, file] of Object.entries(files || {})) {
    if (!_isPathInsideRelDir(relPath, dir)) continue;
    const inner = _stripRelDirPrefix(relPath, dir);
    if (!inner) continue;
    const hash = String(file?.sha256 || '');
    const size = Number(file?.size || 0);
    result.push(`${inner}|${size}|${hash}`);
  }
  return result.sort();
}

function _folderDirIdentitySignature(dirs, dirRel) {
  const result = [];
  const dir = _normalizeRemotePath(dirRel);
  for (const relPath of dirs || []) {
    const rel = _normalizeRemotePath(relPath);
    if (!rel || rel === dir) continue;
    if (!_isPathInsideRelDir(rel, dir)) continue;
    result.push(_stripRelDirPrefix(rel, dir));
  }
  return result.sort();
}

function _folderIdentitySame(prevFiles, currentFiles, oldDirRel, newDirRel, prevDirs = [], currentDirs = []) {
  const oldFileSig = _folderFileIdentitySignature(prevFiles, oldDirRel);
  const newFileSig = _folderFileIdentitySignature(currentFiles, newDirRel);
  const oldDirSig = _folderDirIdentitySignature(prevDirs, oldDirRel);
  const newDirSig = _folderDirIdentitySignature(currentDirs, newDirRel);
  if (!oldFileSig.length && !newFileSig.length && !oldDirSig.length && !newDirSig.length) {
    // 空フォルダの場合も、同一親配下の1対1候補ならリネームとして扱えるようにする。
    return true;
  }
  if (oldFileSig.length !== newFileSig.length || oldDirSig.length !== newDirSig.length) return false;
  for (let i = 0; i < oldFileSig.length; i += 1) {
    if (oldFileSig[i] !== newFileSig[i]) return false;
  }
  for (let i = 0; i < oldDirSig.length; i += 1) {
    if (oldDirSig[i] !== newDirSig[i]) return false;
  }
  return true;
}

function _detectSameFolderDirRenames(prevDirs, currentDirs, prevFiles, currentFiles) {
  const prevDirSet = new Set((prevDirs || []).map((entry) => _normalizeRemotePath(entry)).filter(Boolean));
  const currentDirSet = new Set((currentDirs || []).map((entry) => _normalizeRemotePath(entry)).filter(Boolean));
  const deleted = Array.from(prevDirSet).filter((dirRel) => !currentDirSet.has(dirRel));
  const added = Array.from(currentDirSet).filter((dirRel) => !prevDirSet.has(dirRel));
  const usedAdded = new Set();
  const pairs = [];

  // 親フォルダに近いものから処理する。親がリネームされたら、子はそのリネームに包含されるため個別処理しない。
  const sortedDeleted = deleted.sort((a, b) => a.length - b.length);
  for (const oldRel of sortedDeleted) {
    if (pairs.some((pair) => oldRel.startsWith(`${pair.oldRel}/`))) continue;
    const oldParent = _relDirname(oldRel);
    const match = added.find((newRel) => {
      if (usedAdded.has(newRel)) return false;
      if (pairs.some((pair) => newRel.startsWith(`${pair.newRel}/`))) return false;
      // 今回は安全のため、同じ親フォルダ内でのフォルダ名変更だけをリネームとして扱う。
      if (_relDirname(newRel) !== oldParent) return false;
      const siblingDeleted = sortedDeleted.filter((entry) => _relDirname(entry) === oldParent);
      const siblingAdded = added.filter((entry) => _relDirname(entry) === oldParent && !usedAdded.has(entry));
      const sameIdentity = _folderIdentitySame(prevFiles, currentFiles, oldRel, newRel, prevDirs, currentDirs);
      if (!sameIdentity) return false;
      const oldHasContent = _folderFileIdentitySignature(prevFiles, oldRel).length > 0 || _folderDirIdentitySignature(prevDirs, oldRel).length > 0;
      const newHasContent = _folderFileIdentitySignature(currentFiles, newRel).length > 0 || _folderDirIdentitySignature(currentDirs, newRel).length > 0;
      if (oldHasContent || newHasContent) return true;
      // 空フォルダ名変更は、同じ親の候補が1対1のときだけ許可する。
      return siblingDeleted.length === 1 && siblingAdded.length === 1;
    });
    if (!match) continue;
    usedAdded.add(match);
    pairs.push({ oldRel, newRel: match });
  }
  return pairs;
}

function _remapRelPathAfterDirRename(relPath, renamedDirPairs) {
  const rel = _normalizeRemotePath(relPath);
  for (const pair of renamedDirPairs || []) {
    if (rel === pair.oldRel) return pair.newRel;
    if (rel.startsWith(`${pair.oldRel}/`)) {
      return _joinRemotePath(pair.newRel, rel.slice(pair.oldRel.length + 1));
    }
  }
  return rel;
}

function _remapRemotePathPrefix(remoteMaps, oldPrefix, newPrefix) {
  const oldRoot = _normalizeRemotePath(oldPrefix);
  const newRoot = _normalizeRemotePath(newPrefix);
  if (!oldRoot || !newRoot || oldRoot === newRoot || !remoteMaps?.pathToItem) return;

  const updates = [];
  for (const [itemPath, item] of Array.from(remoteMaps.pathToItem.entries())) {
    const normalizedPath = _normalizeRemotePath(itemPath);
    if (normalizedPath !== oldRoot && !normalizedPath.startsWith(`${oldRoot}/`)) continue;
    const suffix = normalizedPath === oldRoot ? '' : normalizedPath.slice(oldRoot.length + 1);
    updates.push({ oldPath: normalizedPath, newPath: _joinRemotePath(newRoot, suffix), item });
  }

  for (const update of updates) {
    remoteMaps.pathToItem.delete(update.oldPath);
    if (remoteMaps.folderPathToId) remoteMaps.folderPathToId.delete(update.oldPath);
  }

  for (const update of updates) {
    const itemId = String(update.item?.item_id || '');
    const nextItem = {
      ...(update.item || {}),
      item_id: itemId,
      path: update.newPath,
    };
    remoteMaps.pathToItem.set(update.newPath, nextItem);
    if (itemId && remoteMaps.idToItem) remoteMaps.idToItem.set(itemId, nextItem);
    if (nextItem.type === 'folder' && itemId && remoteMaps.folderPathToId) {
      remoteMaps.folderPathToId.set(update.newPath, itemId);
    }
  }
}

function _offlineBaselineSnapshot(target) {
  if (!_isOfflineTarget(target)) return null;
  const baseline = target?.baseline_snapshot || null;
  if (!baseline || typeof baseline !== 'object') return null;
  return {
    files: baseline.files || {},
    dirs: Array.isArray(baseline.dirs) ? baseline.dirs : [],
  };
}

async function _walkLocalFolder(rootPath, ignoreHidden) {
  const files = {};
  const dirs = new Set(['']);

  async function visit(absDir, relDir) {
    let entries = [];
    try {
      entries = await fsp.readdir(absDir, { withFileTypes: true });
    } catch {
      return;
    }

    for (const entry of entries) {
      if (ignoreHidden && _isHiddenName(entry.name)) continue;
      const absPath = path.join(absDir, entry.name);
      const relPath = relDir ? path.posix.join(relDir, entry.name) : entry.name;
      if (entry.isDirectory()) {
        dirs.add(relPath);
        await visit(absPath, relPath);
        continue;
      }
      if (!entry.isFile()) continue;
      const stat = await _safeStat(absPath);
      if (!stat) continue;
      files[relPath] = await _buildLocalFileFingerprint(absPath, stat);
    }
  }

  await visit(rootPath, '');
  return {
    files,
    dirs: Array.from(dirs).sort(),
  };
}

async function _readLocalTargetSnapshot(target, ignoreHidden) {
  if (target.item_type === 'file') {
    const stat = await _safeStat(target.local_path);
    if (!stat || !stat.isFile()) {
      return {
        files: {},
        dirs: [],
        exists: false,
      };
    }
    return {
      files: {
        '': await _buildLocalFileFingerprint(target.local_path, stat),
      },
      dirs: [],
      exists: true,
    };
  }

  const stat = await _safeStat(target.local_path);
  if (!stat || !stat.isDirectory()) {
    return {
      files: {},
      dirs: [],
      exists: false,
    };
  }
  const walked = await _walkLocalFolder(target.local_path, ignoreHidden);
  return {
    ...walked,
    exists: true,
  };
}


async function _findOfflineSingleFileRenameCandidate(target, previousFile, ignoreHidden) {
  if (!_isOfflineTarget(target) || target.item_type !== 'file') return null;
  const oldLocalPath = String(target.local_path || '');
  if (!oldLocalPath) return null;
  const parentDir = path.dirname(oldLocalPath);
  const oldBaseName = path.basename(oldLocalPath);
  let entries = [];
  try {
    entries = await fsp.readdir(parentDir, { withFileTypes: true });
  } catch {
    return null;
  }

  const candidates = [];
  for (const entry of entries) {
    if (!entry.isFile()) continue;
    if (ignoreHidden && _isHiddenName(entry.name)) continue;
    if (entry.name === oldBaseName) continue;
    const candidatePath = path.join(parentDir, entry.name);
    const stat = await _safeStat(candidatePath);
    if (!stat || !stat.isFile()) continue;
    const file = await _buildLocalFileFingerprint(candidatePath, stat);
    candidates.push({ local_path: candidatePath, name: entry.name, file });
  }
  if (!candidates.length) return null;

  const exact = previousFile
    ? candidates.find((candidate) => _fileIdentitySame(previousFile, candidate.file))
    : null;
  if (exact) {
    return { ...exact, match_reason: 'same_file_identity' };
  }

  // 単体ファイルのオフライン利用では、旧パスが消え、同じ保存フォルダに候補が1つだけなら
  // 「名前変更」とみなす。これにより、リネーム直後に少し編集した場合でも監視が止まらない。
  // 複数候補がある場合は誤判定を避けるため停止扱いにする。
  if (candidates.length === 1) {
    return { ...candidates[0], match_reason: 'single_candidate_fallback' };
  }

  return null;
}


async function _findBackupSingleFileRenameCandidate(target, previousFile, ignoreHidden) {
  if (_isOfflineTarget(target) || target.item_type !== 'file') return null;
  const oldLocalPath = String(target.local_path || '');
  if (!oldLocalPath || !previousFile) return null;
  const parentDir = path.dirname(oldLocalPath);
  const oldBaseName = path.basename(oldLocalPath);
  let entries = [];
  try {
    entries = await fsp.readdir(parentDir, { withFileTypes: true });
  } catch {
    return null;
  }

  const candidates = [];
  for (const entry of entries) {
    if (!entry.isFile()) continue;
    if (ignoreHidden && _isHiddenName(entry.name)) continue;
    if (entry.name === oldBaseName) continue;
    const candidatePath = path.join(parentDir, entry.name);
    const stat = await _safeStat(candidatePath);
    if (!stat || !stat.isFile()) continue;
    const file = await _buildLocalFileFingerprint(candidatePath, stat);
    if (_fileIdentitySame(previousFile, file)) {
      candidates.push({ local_path: candidatePath, name: entry.name, file, match_reason: 'same_file_identity' });
    }
  }

  // 通常バックアップでは誤判定を避けるため、同一性が確認できる候補が1つだけの時だけリネーム扱いにする。
  if (candidates.length === 1) return candidates[0];
  return null;
}

async function _loginForBackup(config) {
  const response = await fetch(`${config.api_base}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      email: config.email,
      password: _backupSecret?.password || config.password,
    }),
  });
  if (!response.ok) {
    const detail = await response.text().catch(() => 'login failed');
    if (response.status === 401) {
      _backupTokenCache = null;
      throw new Error(`バックアップ用ログインに失敗しました: ${detail}`);
    }
    throw new Error(`バックアップ用ログインに失敗しました: ${detail}`);
  }
  const json = await response.json();
  const token = String(json?.access_token || '').trim();
  if (!token) {
    throw new Error('バックアップ用アクセストークンを取得できませんでした');
  }
  _backupTokenCache = token;
  return token;
}

async function _authorizedFetch(config, inputPath, init = {}, retry = true) {
  const token = _backupTokenCache || config.access_token || _backupSecret?.access_token || await _loginForBackup(config);
  const headers = new Headers(init.headers || {});
  if (!headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`);
  }
  const response = await fetch(`${config.api_base}${inputPath}`, {
    ...init,
    headers,
  });
  if (response.status === 401 && retry && (_backupSecret?.password || config.password)) {
    await _loginForBackup(config);
    return _authorizedFetch(config, inputPath, init, false);
  }
  return response;
}

async function _jsonApi(config, inputPath, options = {}, retry = true) {
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const response = await _authorizedFetch(config, inputPath, { ...options, headers }, retry);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      detail = data?.detail || JSON.stringify(data);
    } catch {
      try {
        detail = await response.text();
      } catch {}
    }
    throw new Error(detail || `HTTP ${response.status}`);
  }
  if (response.status === 204) return undefined;
  return response.json();
}

function _isBackupAuthError(value) {
  const message = String(value || '').toLowerCase();
  return message.includes('401')
    || message.includes('unauthorized')
    || message.includes('token expired')
    || message.includes('invalid or expired token')
    || message.includes('bad token')
    || message.includes('bad signature')
    || message.includes('sub missing')
    || message.includes('not authenticated')
    || message.includes('認証');
}

function _stopBackupBecauseAuthExpired(errorMessage, pendingChanges = 0) {
  _stopBackupTimer();
  _backupConfig = null;
  _backupSecret = null;
  _backupTokenCache = null;
  const current = _loadBackupRuntimeState();
  const nextState = {
    ...current,
    is_running: false,
    stopped_at: _nowIso(),
    status: 'stopped',
    pending_changes: Math.max(0, Number(pendingChanges || 0)),
    error: `認証期限切れのため自動バックアップを停止しました。再ログイン後に「自動バックアップを開始」を押してください。詳細: ${String(errorMessage || '')}`,
    resume_enabled: false,
    open_at_login_enabled: _setBackupOpenAtLogin(false),
    resumed_from_disk: false,
    secret_encrypted_b64: null,
    config: null,
  };
  _saveBackupRuntimeState(nextState);
  return nextState;
}

async function _heartbeat(config, status, pendingChanges, lastSyncAt, errorMessage) {
  const payload = {
    client_id: config.client_id,
    local_root_display: config.local_root_display,
    status,
    sync_mode: 'mirror',
    pending_changes: Math.max(0, Number(pendingChanges || 0)),
    app_version: 'desktop-backup-main',
    last_sync_at: lastSyncAt || null,
  };
  try {
    await _jsonApi(config, '/sync/heartbeat', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    const state = {
      ..._loadBackupRuntimeState(),
      last_heartbeat_at: _nowIso(),
      status,
      pending_changes: payload.pending_changes,
      error: errorMessage || null,
    };
    _saveBackupRuntimeState(state);
  } catch (err) {
    const message = String(err?.message || err || 'heartbeat error');
    if (_isBackupAuthError(message)) {
      _backupDebug('backup heartbeat auth failed; stopping backup loop', { error: message });
      _stopBackupBecauseAuthExpired(message, payload.pending_changes);
      return;
    }
    const state = {
      ..._loadBackupRuntimeState(),
      last_heartbeat_at: _nowIso(),
      status: 'error',
      pending_changes: payload.pending_changes,
      error: message,
    };
    _saveBackupRuntimeState(state);
  }
}

async function _fetchRemoteTree(config) {
  const res = await _jsonApi(config, '/sync/tree', { method: 'GET' });
  return Array.isArray(res?.items) ? res.items : [];
}

function _buildRemoteMaps(items) {
  const pathToItem = new Map();
  const idToItem = new Map();
  const folderPathToId = new Map();
  for (const item of items || []) {
    if (item?.trashed_at != null) continue;
    const itemId = String(item?.item_id || '');
    const itemPath = _normalizeRemotePath(item?.path || '');
    if (itemId) {
      idToItem.set(itemId, item);
    }
    if (!itemPath) continue;
    pathToItem.set(itemPath, item);
    if (item?.type === 'folder') {
      folderPathToId.set(itemPath, itemId);
    }
  }
  return { pathToItem, idToItem, folderPathToId };
}

function _remoteItemMatchesBackupPath(item, remotePath, expectedType = null) {
  if (!item) return false;
  if (expectedType && String(item.type || '') !== expectedType) return false;
  const itemPath = _normalizeRemotePath(item.path || '');
  return Boolean(remotePath) && itemPath === remotePath;
}

function _findRemoteItemForBackupUpload(remoteMaps, remoteFilePath, backupTarget = null, expectedType = null) {
  const remotePath = _normalizeRemotePath(remoteFilePath);
  const targetItemId = backupTarget?.remote_item_id ? String(backupTarget.remote_item_id) : '';

  // 自動バックアップは item_id を第一候補にするが、古い state に「フォルダ対象なのに配下ファイルID」
  // が入っている場合がある。そのため、必ず remotePath と type が合うか確認してから使う。
  if (targetItemId && remoteMaps?.idToItem?.has(targetItemId)) {
    const byId = remoteMaps.idToItem.get(targetItemId);
    if (_remoteItemMatchesBackupPath(byId, remotePath, expectedType)) {
      return byId;
    }
  }

  if (remotePath && remoteMaps?.pathToItem?.has(remotePath)) {
    const byPath = remoteMaps.pathToItem.get(remotePath);
    if (!expectedType || String(byPath?.type || '') === expectedType) {
      return byPath;
    }
  }

  return null;
}

function _rememberRemoteItem(remoteMaps, remotePath, item) {
  if (!item) return;
  const itemId = String(item?.item_id || '');
  const normalizedPath = _normalizeRemotePath(remotePath || item?.path || '');
  const normalizedItem = {
    ...item,
    item_id: itemId,
    path: normalizedPath || item?.path,
  };
  if (itemId && remoteMaps?.idToItem) {
    remoteMaps.idToItem.set(itemId, normalizedItem);
  }
  if (normalizedPath && remoteMaps?.pathToItem) {
    remoteMaps.pathToItem.set(normalizedPath, normalizedItem);
  }
  if (normalizedPath && normalizedItem.type === 'folder' && remoteMaps?.folderPathToId) {
    remoteMaps.folderPathToId.set(normalizedPath, itemId);
  }
}

async function _ensureRemoteFolder(config, remoteFolderPath, remoteMaps) {
  const normalized = _normalizeRemotePath(remoteFolderPath);
  if (!normalized) return null;
  if (remoteMaps.folderPathToId.has(normalized)) {
    return remoteMaps.folderPathToId.get(normalized);
  }

  const segments = normalized.split('/').filter(Boolean);
  let currentPath = '';
  let parentId = null;
  for (const segment of segments) {
    currentPath = _joinRemotePath(currentPath, segment);
    if (remoteMaps.folderPathToId.has(currentPath)) {
      parentId = remoteMaps.folderPathToId.get(currentPath);
      continue;
    }

    const created = await _jsonApi(config, '/items/folder', {
      method: 'POST',
      body: JSON.stringify({
        name: segment,
        parent_id: parentId,
      }),
    });
    const folderItem = {
      ...(created || {}),
      item_id: String(created?.item_id || ''),
      name: segment,
      type: 'folder',
      path: currentPath,
      parent_id: parentId,
      owner_user_id: created?.owner_user_id || '',
    };
    _rememberRemoteItem(remoteMaps, currentPath, folderItem);
    parentId = folderItem.item_id;
  }
  return parentId;
}

async function _uploadFileToRemote(config, absPath, remoteFilePath, remoteMaps, backupTarget = null) {
  const normalizedRemoteFilePath = _normalizeRemotePath(remoteFilePath);
  const remoteParentPath = _dirnameRemote(normalizedRemoteFilePath);
  const fileName = _basenameRemote(normalizedRemoteFilePath);
  const parentId = remoteParentPath ? await _ensureRemoteFolder(config, remoteParentPath, remoteMaps) : null;
  const existing = _findRemoteItemForBackupUpload(remoteMaps, normalizedRemoteFilePath, backupTarget, 'file');
  const fallbackTargetItemId = backupTarget?.item_type === 'file' && backupTarget?.remote_item_id
    ? String(backupTarget.remote_item_id)
    : null;
  const targetItemId = existing && existing.type === 'file' ? String(existing.item_id) : fallbackTargetItemId;

  const buffer = await fsp.readFile(absPath);
  _backupDebug('upload file to remote', {
    remote_path: normalizedRemoteFilePath,
    target_item_id: targetItemId || null,
    target_kind: backupTarget ? _targetKind(backupTarget) : 'unknown',
    bytes: buffer.length,
  });
  const form = new FormData();
  form.append('file', new Blob([buffer]), fileName);
  form.append('replace_existing', 'true');
  form.append('upload_context', 'backup');
  form.append('remote_path', normalizedRemoteFilePath);
  if (parentId) form.append('parent_id', parentId);
  if (targetItemId) form.append('target_item_id', targetItemId);

  const response = await _authorizedFetch(config, '/ui/upload', {
    method: 'POST',
    body: form,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      detail = data?.detail || JSON.stringify(data);
    } catch {
      detail = await response.text().catch(() => detail);
    }
    throw new Error(detail || 'upload failed');
  }

  const result = await response.json();
  const itemId = String(result?.item_id || targetItemId || '');
  const remembered = {
    item_id: itemId,
    type: 'file',
    name: fileName,
    path: normalizedRemoteFilePath,
    parent_id: parentId,
    size_bytes: Number(result?.size_bytes || buffer.length || 0),
    updated_at: Math.floor(Date.now() / 1000),
  };
  _rememberRemoteItem(remoteMaps, normalizedRemoteFilePath, remembered);

  if (backupTarget && backupTarget.item_type === 'file' && itemId) {
    backupTarget.remote_item_id = itemId;
  }

  return {
    ...result,
    item_id: itemId,
    target_item_id: targetItemId,
    remote_path: normalizedRemoteFilePath,
    versioned: Boolean(result?.versioned || targetItemId),
  };
}


async function _walkLocalFolderForUpload(rootPath, ignoreHidden = true) {
  const files = [];
  const directories = [];
  const root = path.resolve(String(rootPath || ''));

  async function walk(currentPath, relativeDir) {
    let entries = [];
    try {
      entries = await fsp.readdir(currentPath, { withFileTypes: true });
    } catch {
      return;
    }

    for (const entry of entries) {
      if (ignoreHidden && _isHiddenName(entry.name)) continue;
      const absolutePath = path.join(currentPath, entry.name);
      const relativePath = relativeDir ? `${relativeDir}/${entry.name}` : entry.name;
      if (entry.isDirectory()) {
        directories.push({ absolutePath, relativePath });
        await walk(absolutePath, relativePath);
      } else if (entry.isFile()) {
        files.push({ absolutePath, relativePath });
      }
    }
  }

  await walk(root, '');
  return { root, directories, files };
}

function _makeDialogBackupConfig(payload, selectedRootPath) {
  const apiBase = _sanitizeUrlBase(payload?.api_base, 'api_base');
  const accessToken = String(payload?.access_token || '').trim();
  if (!accessToken) {
    throw new Error('フォルダバックアップのアップロードにはログイン済みアクセストークンが必要です。');
  }
  const rootName = path.basename(path.resolve(selectedRootPath));
  return {
    api_base: apiBase,
    access_token: accessToken,
    email: String(payload?.email || 'desktop-local@example.invalid'),
    password: '',
    polling_interval_sec: Math.max(2, Number(payload?.polling_interval_sec || 5)),
    ignore_hidden: Boolean(payload?.ignore_hidden ?? true),
    local_root_display: String(payload?.local_root_display || selectedRootPath || rootName),
    targets: [],
    client_id: payload?.client_id ? String(payload.client_id) : `desktop-backup-${crypto.randomUUID()}`,
  };
}

async function _uploadBackupFolderFromDialogProcess(payload) {
  const result = await dialog.showOpenDialog({
    title: '自動バックアップするフォルダを選択',
    properties: ['openDirectory'],
  });
  if (result.canceled || !result.filePaths || !result.filePaths[0]) {
    return { ok: false, cancelled: true, message: 'フォルダ選択がキャンセルされました' };
  }

  const selectedRootPath = path.resolve(result.filePaths[0]);
  const config = _makeDialogBackupConfig(payload, selectedRootPath);
  const rootName = path.basename(selectedRootPath);
  const remoteRootPath = _normalizeRemotePath(rootName);
  const localTree = await _walkLocalFolderForUpload(selectedRootPath, config.ignore_hidden);
  const remoteItems = await _fetchRemoteTree(config);
  const remoteMaps = _buildRemoteMaps(remoteItems);

  _backupDebug('upload backup folder from dialog', {
    local_path: selectedRootPath,
    remote_path: remoteRootPath,
    files: localTree.files.length,
    directories: localTree.directories.length,
  });

  const rootFolderId = await _ensureRemoteFolder(config, remoteRootPath, remoteMaps);

  for (const directory of localTree.directories) {
    const remoteDirPath = _joinRemotePath(remoteRootPath, directory.relativePath);
    await _ensureRemoteFolder(config, remoteDirPath, remoteMaps);
  }

  let uploadedCount = 0;
  for (const file of localTree.files) {
    const remoteFilePath = _joinRemotePath(remoteRootPath, file.relativePath);
    await _uploadFileToRemote(config, file.absolutePath, remoteFilePath, remoteMaps, null);
    uploadedCount += 1;
  }

  const target = _normalizeBackupTarget({
    local_path: selectedRootPath,
    remote_path: remoteRootPath,
    item_type: 'folder',
    display_name: remoteRootPath,
    source_device_label: _backupSourceDeviceLabel(),
    remote_item_id: rootFolderId || null,
    target_kind: 'backup',
  });

  return {
    ok: true,
    target,
    uploaded_count: uploadedCount,
    folder_count: localTree.directories.length + 1,
    message: `${rootName} をバックアップ設定に追加しました`,
  };
}

function _forgetRemotePathPrefix(remoteMaps, remotePath) {
  const normalized = _normalizeRemotePath(remotePath);
  if (!normalized || !remoteMaps?.pathToItem) return;

  const deletePaths = [];
  for (const itemPath of Array.from(remoteMaps.pathToItem.keys())) {
    const current = _normalizeRemotePath(itemPath);
    if (current === normalized || current.startsWith(`${normalized}/`)) {
      deletePaths.push(current);
    }
  }

  for (const current of deletePaths) {
    const item = remoteMaps.pathToItem.get(current);
    const itemId = String(item?.item_id || '');
    remoteMaps.pathToItem.delete(current);
    if (remoteMaps.folderPathToId) remoteMaps.folderPathToId.delete(current);
    if (itemId && remoteMaps.idToItem) remoteMaps.idToItem.delete(itemId);
  }
}

async function _trashRemoteItem(config, itemId, remoteMaps, remotePath) {
  await _jsonApi(config, `/items/${itemId}/trash`, { method: 'POST' });
  _forgetRemotePathPrefix(remoteMaps, remotePath);
}


async function _renameRemoteItem(config, itemId, newName, remoteMaps, oldRemotePath, newRemotePath, newParentId = undefined) {
  const body = { name: String(newName || '').trim() };
  if (!body.name) return null;
  if (newParentId !== undefined) {
    body.parent_id = newParentId || null;
  }

  _backupDebug('rename remote item', {
    item_id: String(itemId || ''),
    old_remote_path: oldRemotePath,
    new_remote_path: newRemotePath,
    name: body.name,
  });

  const updated = await _jsonApi(config, `/items/${itemId}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });

  const oldPath = _normalizeRemotePath(oldRemotePath);
  const newPath = _normalizeRemotePath(newRemotePath || updated?.path || oldRemotePath);
  if (oldPath) {
    remoteMaps.pathToItem.delete(oldPath);
    remoteMaps.folderPathToId.delete(oldPath);
  }
  const normalized = {
    ...(updated || {}),
    item_id: String(updated?.item_id || itemId || ''),
    name: body.name,
    path: newPath,
  };
  _rememberRemoteItem(remoteMaps, newPath, normalized);
  return normalized;
}

function _filterTopLevelDeletedDirs(deletedDirs) {
  const sorted = [...deletedDirs].sort((a, b) => a.length - b.length);
  const roots = [];
  for (const entry of sorted) {
    if (!entry) continue;
    if (roots.some((root) => entry === root || entry.startsWith(`${root}/`))) {
      continue;
    }
    roots.push(entry);
  }
  return roots;
}

function _relPathIsInsideAnyDir(relPath, dirRels) {
  const normalized = _normalizeRemotePath(relPath);
  if (!normalized) return false;
  return (dirRels || []).some((dirRel) => {
    const dir = _normalizeRemotePath(dirRel);
    return Boolean(dir) && (normalized === dir || normalized.startsWith(`${dir}/`));
  });
}

function _remotePathForTargetEntry(target, relPath) {
  if (target.item_type === 'file') {
    return target.remote_path;
  }
  return relPath ? _joinRemotePath(target.remote_path, relPath) : target.remote_path;
}


async function _pruneStaleBackupFolderTargets(config, options = {}) {
  const targets = Array.isArray(config?.targets) ? config.targets : [];
  if (!targets.length) {
    return { config, changed: false, removed_count: 0 };
  }

  let remoteMaps = null;
  let changed = false;
  let removedCount = 0;
  const retained = [];

  for (const target of targets) {
    const kind = _targetKind(target);
    const itemType = String(target?.item_type || '').trim();
    if (kind !== 'backup' || !['file', 'folder'].includes(itemType)) {
      retained.push(target);
      continue;
    }

    if (!remoteMaps) {
      const remoteItems = await _fetchRemoteTree(config);
      remoteMaps = _buildRemoteMaps(remoteItems);
    }

    const remoteItem = _findRemoteItemForBackupUpload(remoteMaps, target.remote_path, target, itemType);
    if (remoteItem?.item_id) {
      const remoteItemId = String(remoteItem.item_id);
      if (remoteItemId !== String(target.remote_item_id || '')) {
        retained.push({ ...target, remote_item_id: remoteItemId });
        changed = true;
      } else {
        retained.push(target);
      }
      continue;
    }

    // クラウド側で削除済みの通常バックアップ target は、ローカルを根拠に復活アップロードしない。
    // file / folder の両方を runtime/DB から外し、次回開始時に過去 target が再注入される状態を防ぐ。
    changed = true;
    removedCount += 1;
  }

  if (!changed) {
    return { config, changed: false, removed_count: 0 };
  }

  const nextConfig = {
    ...config,
    targets: retained,
    local_root_display: _summarizeLocalRootDisplay(retained),
  };

  if (options.persist !== false) {
    try {
      await _jsonApi(nextConfig, '/backup/targets', {
        method: 'PUT',
        body: JSON.stringify({ targets: _backupSettingTargetsOnly(retained) }),
      });
    } catch {
      // 永続化に失敗しても、今回の runtime state からは除去する。
      // 次回開始時に同じ確認を再実行する。
    }
  }

  return { config: nextConfig, changed: true, removed_count: removedCount };
}

async function _scanBackupOnce(config) {
  if (_backupScanInFlight) return;
  _backupScanInFlight = true;
  let pendingChanges = 0;
  let lastSyncAtIso = null;

  try {
    await _heartbeat(config, 'scanning', 0, null, null);
    const remoteItems = await _fetchRemoteTree(config);
    const remoteMaps = _buildRemoteMaps(remoteItems);
    const state = _loadBackupRuntimeState();
    const nextSnapshots = {};
    const retainedTargets = [];

    for (const target of config.targets) {
      let snapshotKey = _snapshotKeyForTarget(target);
      const snapshots = state.snapshots || {};
      const hasPrevSnapshot = Object.prototype.hasOwnProperty.call(snapshots, snapshotKey);
      const prevSnapshot = snapshots[snapshotKey] || { files: {}, dirs: [] };
      let currentSnapshot = await _readLocalTargetSnapshot(target, config.ignore_hidden);

      // 失敗した「バックアップ設定フォルダアップロード」の残骸として、
      // remote_item_id を持たない folder target が runtime に残ることがある。
      // その状態で scan を進めると、クラウドに存在しないバックアップルートを毎回作成し、
      // フォルダ配下を無限にアップロードし続ける原因になる。
      // 既にクラウド上に対応ルートが見つかる場合だけIDを補完し、見つからない場合は監視対象から外す。
      if (target.item_type === 'folder' && !_isOfflineTarget(target) && !String(target.remote_item_id || '').trim()) {
        const existingRoot = _findRemoteItemForBackupUpload(remoteMaps, target.remote_path, target, 'folder');
        if (existingRoot?.item_id) {
          target.remote_item_id = String(existingRoot.item_id);
        } else {
          _backupDebug('skip incomplete folder backup target', {
            remote_path: target.remote_path,
            local_path: target.local_path,
          });
          continue;
        }
      }

      if (_isOfflineTarget(target)) {
        _backupDebug('scan offline target', {
          item_type: target.item_type,
          remote_path: target.remote_path,
          local_path: target.local_path,
          exists: Boolean(currentSnapshot.exists),
          file_count: Object.keys(currentSnapshot.files || {}).length,
        });
      }

      if (target.item_type === 'file') {
        let remoteItem = _findRemoteItemForBackupUpload(remoteMaps, target.remote_path, target, 'file');
        let effectiveTarget = target;
        if (remoteItem?.item_id && String(remoteItem.item_id) !== String(target.remote_item_id || '')) {
          effectiveTarget = { ...target, remote_item_id: String(remoteItem.item_id) };
        }

        // 通常バックアップ対象のクラウドitemが削除済みなら、ローカルを根拠に再アップロードしない。
        // ここで retainedTargets に入れずに抜けることで、runtime state と /backup/targets から外す。
        if (!_isOfflineTarget(effectiveTarget) && !remoteItem?.item_id) {
          _backupDebug('backup remote file missing; stop watching this target', {
            remote_path: effectiveTarget.remote_path,
            remote_item_id: effectiveTarget.remote_item_id || null,
          });
          pendingChanges += 1;
          lastSyncAtIso = _nowIso();
          continue;
        }

        const baselineSnapshotForRename = _offlineBaselineSnapshot(effectiveTarget);
        const baselineFileForRename = baselineSnapshotForRename?.files?.[''];
        const prevFileForRename = hasPrevSnapshot ? prevSnapshot?.files?.[''] : baselineFileForRename;

        if (!currentSnapshot.exists) {
          // 単体ファイルのオフライン利用・通常バックアップでファイル名だけを変更すると、旧local_pathは消える。
          // その場合は同じ保存フォルダ内の同一ファイル候補を探し、監視停止や削除ではなくクラウドitem名を更新する。
          if (remoteItem?.item_id) {
            const renameCandidate = _isOfflineTarget(target)
              ? await _findOfflineSingleFileRenameCandidate(target, prevFileForRename, config.ignore_hidden)
              : await _findBackupSingleFileRenameCandidate(target, prevFileForRename, config.ignore_hidden);
            if (renameCandidate) {
              const oldRemotePath = _normalizeRemotePath(target.remote_path);
              const newRemotePath = _joinRemotePath(_dirnameRemote(oldRemotePath), renameCandidate.name);
              _backupDebug(_isOfflineTarget(target) ? 'offline single file rename detected' : 'backup single file rename detected', {
                old_local_path: target.local_path,
                new_local_path: renameCandidate.local_path,
                old_remote_path: oldRemotePath,
                new_remote_path: newRemotePath,
                match_reason: renameCandidate.match_reason,
              });
              const updatedRemote = await _renameRemoteItem(
                config,
                String(remoteItem.item_id),
                renameCandidate.name,
                remoteMaps,
                oldRemotePath,
                newRemotePath,
              );
              effectiveTarget = {
                ...effectiveTarget,
                local_path: renameCandidate.local_path,
                remote_path: newRemotePath,
                display_name: renameCandidate.name,
                remote_item_id: String(updatedRemote?.item_id || remoteItem.item_id),
              };
              remoteItem = updatedRemote || {
                ...remoteItem,
                name: renameCandidate.name,
                path: newRemotePath,
              };
              currentSnapshot = {
                files: { '': renameCandidate.file },
                dirs: [],
                exists: true,
              };
              snapshotKey = _snapshotKeyForTarget(effectiveTarget);
              pendingChanges += 1;
              lastSyncAtIso = _nowIso();
            }
          }

          if (!currentSnapshot.exists) {
            // オフライン利用のローカルコピーが削除された場合は、クラウド側は削除せず監視だけ外す。
            if (!_isOfflineTarget(target) && remoteItem?.item_id) {
              pendingChanges += 1;
              await _trashRemoteItem(config, String(remoteItem.item_id), remoteMaps, target.remote_path);
              lastSyncAtIso = _nowIso();
            }
            continue;
          }
        }

        // オフライン利用元のクラウドitemが削除された場合は、ローカル側を新規アップロードせず監視だけ外す。
        if (_isOfflineTarget(effectiveTarget) && !remoteItem?.item_id) {
          _backupDebug('offline remote item missing; stop watching this target', {
            remote_path: effectiveTarget.remote_path,
            remote_item_id: effectiveTarget.remote_item_id || null,
          });
          continue;
        }

        const currentFile = currentSnapshot.files[''];
        const baselineSnapshot = _offlineBaselineSnapshot(effectiveTarget);
        const baselineFile = baselineSnapshot?.files?.[''];
        const prevFile = hasPrevSnapshot ? prevSnapshot?.files?.[''] : baselineFile;
        // 通常バックアップは初回登録直後に再アップロードしない。
        // オフライン利用は、ダウンロード直後の baseline_snapshot と比べて変わっていれば
        // 初回スキャンでもクラウドへ反映する。これにより、ダウンロード直後〜初回スキャン前の編集を取りこぼさない。
        const hasComparableSnapshot = hasPrevSnapshot || Boolean(baselineFile);
        const needsUpload = !remoteItem || (hasComparableSnapshot && _fileSnapshotChanged(prevFile, currentFile));
        if (_isOfflineTarget(effectiveTarget)) {
          _backupDebug('offline file decision', {
            remote_path: effectiveTarget.remote_path,
            remote_item_id: effectiveTarget.remote_item_id || null,
            local_path: effectiveTarget.local_path,
            remote_found: Boolean(remoteItem?.item_id),
            has_prev_snapshot: hasPrevSnapshot,
            has_baseline: Boolean(baselineFile),
            changed: hasComparableSnapshot && _fileSnapshotChanged(prevFile, currentFile),
            needs_upload: needsUpload,
          });
        }
        if (needsUpload) {
          pendingChanges += 1;
          const uploaded = await _uploadFileToRemote(config, currentFile.abs_path, effectiveTarget.remote_path, remoteMaps, effectiveTarget);
          const uploadedItemId = String(uploaded?.item_id || remoteItem?.item_id || effectiveTarget.remote_item_id || '');
          if (uploadedItemId) {
            effectiveTarget = { ...effectiveTarget, remote_item_id: uploadedItemId };
          }
          lastSyncAtIso = _nowIso();
        }
        retainedTargets.push(effectiveTarget);
        nextSnapshots[snapshotKey] = { files: { '': currentFile }, dirs: [] };
        continue;
      }

      const baselineSnapshot = _offlineBaselineSnapshot(target);
      const prevFiles = hasPrevSnapshot ? (prevSnapshot?.files || {}) : (baselineSnapshot?.files || {});
      const prevDirs = hasPrevSnapshot ? (Array.isArray(prevSnapshot?.dirs) ? prevSnapshot.dirs : []) : (baselineSnapshot?.dirs || []);
      const hasComparableFolderSnapshot = hasPrevSnapshot || Boolean(baselineSnapshot);
      const currentFiles = currentSnapshot.files || {};
      const currentDirs = Array.isArray(currentSnapshot.dirs) ? currentSnapshot.dirs : [];
      const currentDirSet = new Set(currentDirs);

      if (!currentSnapshot.exists) {
        // 自動バックアップ対象のルートフォルダ自体が見つからない場合は、誤削除防止のためクラウド側の削除までは行わない。
        // ここで反映する削除は、ルート配下のファイル・フォルダ削除に限定する。
        retainedTargets.push(target);
        nextSnapshots[snapshotKey] = {
          files: prevFiles,
          dirs: prevDirs,
        };
        continue;
      }

      let effectiveTarget = target;
      const remoteRootBeforeEnsure = _findRemoteItemForBackupUpload(remoteMaps, target.remote_path, target, 'folder');
      const hadRemoteRootBeforeEnsure = Boolean(remoteRootBeforeEnsure?.item_id);

      // オフライン利用元のクラウドフォルダが削除された場合は、ローカル側を新規アップロードせず監視だけ外す。
      if (_isOfflineTarget(target) && !hadRemoteRootBeforeEnsure) {
        _backupDebug('offline remote folder missing; stop watching this target', {
          remote_path: target.remote_path,
          remote_item_id: target.remote_item_id || null,
        });
        continue;
      }

      // 通常のフォルダ自動バックアップでも、クラウド側のルートフォルダが完全削除されたら
      // ローカル側を根拠に勝手に復活アップロードしない。ユーザーがごみ箱で完全削除した対象を
      // 監視から外すことで、「削除しても共有ウィンドウが再びアップロードされる」状態を止める。
      if (!_isOfflineTarget(target) && !hadRemoteRootBeforeEnsure) {
        // 既にクラウド側に存在しない通常バックアップ folder target は、
        // ログを出さずに runtime/DB から落とす。
        // 古い target が Electron ログへ毎回出続けるのを防ぐため。
        pendingChanges += 1;
        lastSyncAtIso = _nowIso();
        continue;
      }

      let remoteRootId = remoteRootBeforeEnsure?.item_id ? String(remoteRootBeforeEnsure.item_id) : '';
      const ensuredRootId = _isOfflineTarget(target) && remoteRootId ? remoteRootId : remoteRootId;
      if (!remoteRootId && ensuredRootId) {
        remoteRootId = String(ensuredRootId);
      }
      if (remoteRootId && remoteRootId !== String(target.remote_item_id || '')) {
        effectiveTarget = { ...target, remote_item_id: remoteRootId };
      }
      retainedTargets.push(effectiveTarget);

      // 初回スキャンでは、UIでアップロードしたクラウド側の状態を基準にする。
      // その時点でクラウド上にバックアップルートがあるなら、ここでは再アップロードも削除も行わない。
      if (!hasPrevSnapshot && hadRemoteRootBeforeEnsure && !hasComparableFolderSnapshot) {
        nextSnapshots[snapshotKey] = {
          files: currentFiles,
          dirs: currentDirs,
        };
        continue;
      }

      if (!hasPrevSnapshot && hadRemoteRootBeforeEnsure && hasComparableFolderSnapshot) {
        const sameAsDownloaded = !_snapshotFilesChanged(prevFiles, currentFiles) && !_snapshotDirsChanged(prevDirs, currentDirs);
        if (sameAsDownloaded) {
          nextSnapshots[snapshotKey] = {
            files: currentFiles,
            dirs: currentDirs,
          };
          continue;
        }
      }

      const renamedDirPairs = hasComparableFolderSnapshot
        ? _detectSameFolderDirRenames(prevDirs, currentDirs, prevFiles, currentFiles)
        : [];
      const renamedOldDirPaths = new Set();

      for (const pair of renamedDirPairs) {
        const oldRemoteFolderPath = _remotePathForTargetEntry(effectiveTarget, pair.oldRel);
        const newRemoteFolderPath = _remotePathForTargetEntry(effectiveTarget, pair.newRel);
        const remoteFolder = remoteMaps.pathToItem.get(oldRemoteFolderPath);
        if (!remoteFolder?.item_id || String(remoteFolder.type || '') !== 'folder') continue;

        const newParentRemotePath = _dirnameRemote(newRemoteFolderPath);
        const newParentId = newParentRemotePath ? await _ensureRemoteFolder(config, newParentRemotePath, remoteMaps) : null;
        const oldParentRemotePath = _dirnameRemote(oldRemoteFolderPath);
        const needsParentUpdate = newParentRemotePath !== oldParentRemotePath;

        pendingChanges += 1;
        await _renameRemoteItem(
          config,
          String(remoteFolder.item_id),
          _basenameRemote(newRemoteFolderPath),
          remoteMaps,
          oldRemoteFolderPath,
          newRemoteFolderPath,
          needsParentUpdate ? newParentId : undefined,
        );
        _remapRemotePathPrefix(remoteMaps, oldRemoteFolderPath, newRemoteFolderPath);
        renamedOldDirPaths.add(pair.oldRel);
        lastSyncAtIso = _nowIso();
      }

      const remappedPrevFiles = {};
      for (const [relPath, file] of Object.entries(prevFiles || {})) {
        remappedPrevFiles[_remapRelPathAfterDirRename(relPath, renamedDirPairs)] = file;
      }

      const renamedFilePairs = hasComparableFolderSnapshot
        ? _detectSameFolderFileRenames(remappedPrevFiles, currentFiles)
        : [];
      const renamedOldRelPaths = new Set();
      const renamedPrevFilesByNewRelPath = {};

      for (const pair of renamedFilePairs) {
        const oldRemoteFilePath = _remotePathForTargetEntry(effectiveTarget, pair.oldRel);
        const newRemoteFilePath = _remotePathForTargetEntry(effectiveTarget, pair.newRel);
        const remoteItem = remoteMaps.pathToItem.get(oldRemoteFilePath);
        if (!remoteItem?.item_id || String(remoteItem.type || '') !== 'file') continue;

        const newParentRemotePath = _dirnameRemote(newRemoteFilePath);
        const newParentId = newParentRemotePath ? await _ensureRemoteFolder(config, newParentRemotePath, remoteMaps) : null;
        const oldParentRemotePath = _dirnameRemote(oldRemoteFilePath);
        const needsParentUpdate = newParentRemotePath !== oldParentRemotePath;

        pendingChanges += 1;
        await _renameRemoteItem(
          config,
          String(remoteItem.item_id),
          _basenameRemote(newRemoteFilePath),
          remoteMaps,
          oldRemoteFilePath,
          newRemoteFilePath,
          needsParentUpdate ? newParentId : undefined,
        );
        renamedOldRelPaths.add(pair.oldRel);
        renamedPrevFilesByNewRelPath[pair.newRel] = remappedPrevFiles[pair.oldRel];
        lastSyncAtIso = _nowIso();
      }

      for (const dirRel of currentDirs) {
        if (!dirRel) continue;
        const remoteDirPath = _remotePathForTargetEntry(effectiveTarget, dirRel);
        const alreadyExists = remoteMaps.folderPathToId.has(remoteDirPath);
        const ensuredFolderId = await _ensureRemoteFolder(config, remoteDirPath, remoteMaps);
        if (!alreadyExists && ensuredFolderId) {
          pendingChanges += 1;
          lastSyncAtIso = _nowIso();
          _backupDebug('backup folder created remotely', {
            remote_path: remoteDirPath,
            item_id: ensuredFolderId,
          });
        }
      }

      for (const [relPath, currentFile] of Object.entries(currentFiles)) {
        const remoteFilePath = _remotePathForTargetEntry(effectiveTarget, relPath);
        const remoteItem = remoteMaps.pathToItem.get(remoteFilePath);
        const prevFile = Object.prototype.hasOwnProperty.call(renamedPrevFilesByNewRelPath, relPath)
          ? renamedPrevFilesByNewRelPath[relPath]
          : remappedPrevFiles[relPath];
        const needsUpload = !remoteItem || (hasComparableFolderSnapshot && _fileSnapshotChanged(prevFile, currentFile));
        if (_isOfflineTarget(effectiveTarget)) {
          _backupDebug('offline folder file decision', {
            remote_path: remoteFilePath,
            remote_found: Boolean(remoteItem?.item_id),
            has_prev_snapshot: hasPrevSnapshot,
            has_baseline: Boolean(baselineSnapshot),
            changed: hasComparableFolderSnapshot && _fileSnapshotChanged(prevFile, currentFile),
            needs_upload: needsUpload,
          });
        }
        if (!needsUpload) continue;
        pendingChanges += 1;
        await _uploadFileToRemote(config, currentFile.abs_path, remoteFilePath, remoteMaps, { remote_path: remoteFilePath, item_type: 'file', remote_item_id: remoteItem?.item_id || null });
        lastSyncAtIso = _nowIso();
      }

      if (hasComparableFolderSnapshot) {
        // 通常バックアップとフォルダのオフライン利用は、どちらも「対象フォルダ配下」の削除をクラウドへ反映する。
        // ただし対象ルート自体が消えた場合は currentSnapshot.exists === false の分岐で止めており、
        // 誤検出によるルート全削除は行わない。
        const deletionLogPrefix = _isOfflineTarget(effectiveTarget) ? 'offline' : 'backup';
        const remappedPrevDirs = (prevDirs || [])
          .map((dirRel) => _remapRelPathAfterDirRename(dirRel, renamedDirPairs))
          .filter(Boolean);
        const currentDirSetForDelete = new Set((currentDirs || []).map((dirRel) => _normalizeRemotePath(dirRel)).filter(Boolean));
        const deletedDirRoots = _filterTopLevelDeletedDirs(
          remappedPrevDirs.filter((dirRel) => dirRel && !currentDirSetForDelete.has(dirRel)),
        );

        for (const deletedDirRel of deletedDirRoots) {
          const remoteDirPath = _remotePathForTargetEntry(effectiveTarget, deletedDirRel);
          const remoteDir = remoteMaps.pathToItem.get(remoteDirPath);
          if (!remoteDir?.item_id || String(remoteDir.type || '') !== 'folder') continue;
          pendingChanges += 1;
          _backupDebug(`${deletionLogPrefix} folder deleted remotely`, {
            remote_path: remoteDirPath,
            item_id: String(remoteDir.item_id),
          });
          await _trashRemoteItem(config, String(remoteDir.item_id), remoteMaps, remoteDirPath);
          lastSyncAtIso = _nowIso();
        }

        for (const oldRelPath of Object.keys(remappedPrevFiles || {})) {
          const normalizedOldRelPath = _normalizeRemotePath(oldRelPath);
          if (!normalizedOldRelPath) continue;
          if (renamedOldRelPaths.has(normalizedOldRelPath)) continue;
          if (Object.prototype.hasOwnProperty.call(currentFiles, normalizedOldRelPath)) continue;
          if (_relPathIsInsideAnyDir(normalizedOldRelPath, deletedDirRoots)) continue;

          const remoteFilePath = _remotePathForTargetEntry(effectiveTarget, normalizedOldRelPath);
          const remoteFile = remoteMaps.pathToItem.get(remoteFilePath);
          if (!remoteFile?.item_id || String(remoteFile.type || '') !== 'file') continue;
          pendingChanges += 1;
          _backupDebug(`${deletionLogPrefix} file deleted remotely`, {
            remote_path: remoteFilePath,
            item_id: String(remoteFile.item_id),
          });
          await _trashRemoteItem(config, String(remoteFile.item_id), remoteMaps, remoteFilePath);
          lastSyncAtIso = _nowIso();
        }
      }

      nextSnapshots[snapshotKey] = {
        files: currentFiles,
        dirs: currentDirs,
      };
    }

    if (!retainedTargets.length) {
      const openAtLoginEnabled = _setBackupOpenAtLogin(false);
      _stopBackupTimer();
      _backupConfig = null;
      _backupSecret = null;
      _backupTokenCache = null;
      const stopped = {
        ..._loadBackupRuntimeState(),
        is_running: false,
        status: 'stopped',
        stopped_at: _nowIso(),
        pending_changes: 0,
        last_scan_at: _nowIso(),
        last_sync_at: lastSyncAtIso || state?.last_sync_at || null,
        local_root_display: 'バックアップ対象なし',
        targets: [],
        snapshots: {},
        error: null,
        config: null,
        resume_enabled: false,
        open_at_login_enabled: openAtLoginEnabled,
        resumed_from_disk: false,
        secret_encrypted_b64: null,
      };
      _saveBackupRuntimeState(stopped);
      await _heartbeat(config, 'stopped', 0, stopped.last_sync_at ? Math.floor(new Date(stopped.last_sync_at).getTime() / 1000) : null, null);
      return;
    }

    const runtimeConfig = {
      ...config,
      targets: retainedTargets,
      local_root_display: _summarizeLocalRootDisplay(retainedTargets),
    };
    await _persistBackupTargetsIfChanged(config, retainedTargets);
    _backupConfig = runtimeConfig;
    const saved = {
      ..._loadBackupRuntimeState(),
      is_running: true,
      status: 'running',
      pending_changes: pendingChanges,
      last_scan_at: _nowIso(),
      last_sync_at: lastSyncAtIso || state?.last_sync_at || null,
      local_root_display: runtimeConfig.local_root_display,
      targets: retainedTargets,
      error: null,
      snapshots: nextSnapshots,
      config: {
        ...(_loadBackupRuntimeState().config || _buildPersistedBackupConfig(runtimeConfig)),
        targets: retainedTargets,
        local_root_display: runtimeConfig.local_root_display,
      },
      resume_enabled: true,
      open_at_login_enabled: _setBackupOpenAtLogin(true),
      resumed_from_disk: Boolean(_loadBackupRuntimeState().resumed_from_disk),
    };
    _saveBackupRuntimeState(saved);
    await _heartbeat(runtimeConfig, 'running', pendingChanges, saved.last_sync_at ? Math.floor(new Date(saved.last_sync_at).getTime() / 1000) : null, null);
  } catch (err) {
    const errorMessage = String(err?.message || err || 'backup scan failed');
    const authFailed = _isBackupAuthError(errorMessage) || errorMessage.includes('invalid credentials') || errorMessage.includes('バックアップ用ログインに失敗');
    if (authFailed) {
      _backupDebug('backup scan auth failed; stopping backup loop', { error: errorMessage });
      _stopBackupBecauseAuthExpired(errorMessage, pendingChanges);
      return;
    }
    const failed = {
      ..._loadBackupRuntimeState(),
      is_running: !authFailed,
      status: 'error',
      stopped_at: authFailed ? _nowIso() : _loadBackupRuntimeState().stopped_at,
      pending_changes: pendingChanges,
      last_scan_at: _nowIso(),
      error: errorMessage,
      resume_enabled: authFailed ? false : Boolean(_loadBackupRuntimeState().resume_enabled),
      open_at_login_enabled: authFailed ? _setBackupOpenAtLogin(false) : Boolean(_loadBackupRuntimeState().open_at_login_enabled),
      resumed_from_disk: authFailed ? false : Boolean(_loadBackupRuntimeState().resumed_from_disk),
      secret_encrypted_b64: authFailed ? null : _loadBackupRuntimeState().secret_encrypted_b64,
      config: authFailed ? null : _loadBackupRuntimeState().config,
    };
    _saveBackupRuntimeState(failed);
    await _heartbeat(config, 'error', pendingChanges, null, failed.error).catch(() => {});
  } finally {
    _backupScanInFlight = false;
  }
}

function _stopBackupTimer() {
  if (_backupTimer) {
    clearInterval(_backupTimer);
    _backupTimer = null;
  }
}



async function _dedupeBackupTargetsOnce(config) {
  try {
    await _jsonApi(config, '/backup/targets/dedupe', {
      method: 'POST',
      body: JSON.stringify({ targets: _backupSettingTargetsOnly(config.targets || []) }),
    });
  } catch {
    // 重複整理は補助処理なので、失敗してもバックアップ開始・監視自体は止めない。
  }
}

async function _buildBackupBaselineSnapshots(config, previousSnapshots = {}) {
  const filtered = _filterBackupSnapshotsForTargets(previousSnapshots || {}, config.targets || []);
  const nextSnapshots = { ...filtered };

  for (const target of config.targets || []) {
    const snapshotKey = _snapshotKeyForTarget(target);
    if (Object.prototype.hasOwnProperty.call(nextSnapshots, snapshotKey)) {
      continue;
    }
    const baselineSnapshot = target?.baseline_snapshot && typeof target.baseline_snapshot === 'object'
      ? target.baseline_snapshot
      : null;

    if (baselineSnapshot && (baselineSnapshot.files || baselineSnapshot.dirs)) {
      nextSnapshots[snapshotKey] = {
        files: baselineSnapshot.files || {},
        dirs: Array.isArray(baselineSnapshot.dirs) ? baselineSnapshot.dirs : [],
      };
      _backupDebug('use provided baseline snapshot', {
        target_kind: _targetKind(target),
        item_type: target.item_type,
        remote_path: target.remote_path,
        file_count: Object.keys(nextSnapshots[snapshotKey].files || {}).length,
      });
      continue;
    }

    const currentSnapshot = await _readLocalTargetSnapshot(target, config.ignore_hidden);
    nextSnapshots[snapshotKey] = {
      files: currentSnapshot.exists ? (currentSnapshot.files || {}) : {},
      dirs: currentSnapshot.exists ? (Array.isArray(currentSnapshot.dirs) ? currentSnapshot.dirs : []) : [],
    };
  }

  return nextSnapshots;
}

async function _persistBackupTargetsIfChanged(config, retainedTargets) {
  const beforeBackupTargets = _backupSettingTargetsOnly(config.targets || []);
  const afterBackupTargets = _backupSettingTargetsOnly(retainedTargets || []);
  const before = JSON.stringify(beforeBackupTargets.map(_normalizeBackupTarget));
  const after = JSON.stringify(afterBackupTargets.map(_normalizeBackupTarget));
  if (before === after) return;
  try {
    await _jsonApi(config, '/backup/targets', {
      method: 'PUT',
      body: JSON.stringify({ targets: afterBackupTargets }),
    });
  } catch {
    // DB永続化に失敗してもローカルのバックアップ処理自体は止めない。
    // 次回の開始・更新時に再同期される。
  }
}

function _startBackupLoop(config) {
  _stopBackupTimer();
  _backupConfig = config;
  _backupTokenCache = config.access_token || null;
  _backupTimer = setInterval(() => {
    // updateBackupTargets() 後も、開始時の古い config ではなく常に最新の _backupConfig を使う。
    // これをしないと、後から追加されたオフライン利用対象が監視対象に入らない。
    const activeConfig = _backupConfig || config;
    if (!activeConfig || !Array.isArray(activeConfig.targets) || activeConfig.targets.length === 0) return;
    _scanBackupOnce(activeConfig).catch((err) => {
      _backupDebug('backup interval scan failed', { error: String(err?.message || err || '') });
    });
  }, Math.max(2, Number(config.polling_interval_sec || 5)) * 1000);
  _scanBackupOnce(_backupConfig || config).catch((err) => {
    _backupDebug('initial backup scan failed', { error: String(err?.message || err || '') });
  });
}

async function _startBackupProcess(payload) {
  let config = _normalizeBackupPayload(payload);
  if (!config.access_token) {
    throw new Error('自動バックアップ開始にはログイン済みアクセストークンが必要です。');
  }
  const pruned = await _pruneStaleBackupFolderTargets(config, { persist: true });
  config = pruned.config;
  if (!Array.isArray(config.targets) || config.targets.length === 0) {
    const cleared = {
      ..._defaultBackupState(),
      stopped_at: _nowIso(),
      status: 'stopped',
      local_root_display: 'バックアップ対象なし',
      targets: [],
      snapshots: {},
      resume_enabled: false,
      open_at_login_enabled: _setBackupOpenAtLogin(false),
    };
    _saveBackupRuntimeState(cleared);
    throw new Error('有効なバックアップ対象がありません。クラウド側で削除済みの古い対象は自動的に解除しました。');
  }
  const secretEncryptedB64 = _encryptBackupPassword(config.access_token);
  const openAtLoginEnabled = _setBackupOpenAtLogin(true);
  _backupSecret = { access_token: config.access_token };
  _backupTokenCache = config.access_token || null;
  const previousState = _loadBackupRuntimeState();
  const baselineSnapshots = await _buildBackupBaselineSnapshots(config, previousState.snapshots || {});
  const persistedConfig = _buildPersistedBackupConfig(config);
  const nextState = {
    ..._defaultBackupState(),
    is_running: true,
    started_at: _nowIso(),
    stopped_at: null,
    status: 'running',
    pending_changes: 0,
    local_root_display: config.local_root_display,
    targets: config.targets,
    client_id: config.client_id,
    config: persistedConfig,
    snapshots: baselineSnapshots,
    resume_enabled: true,
    open_at_login_enabled: openAtLoginEnabled,
    resumed_from_disk: false,
    secret_encrypted_b64: secretEncryptedB64,
  };
  _saveBackupRuntimeState(nextState);
  await _dedupeBackupTargetsOnce(config);
  _startBackupLoop(config);
  return {
    ok: true,
    alreadyRunning: false,
    state: _publicBackupState(nextState),
    message: openAtLoginEnabled ? 'バックアップを開始し、次回起動時の自動再開も有効にしました' : 'バックアップを開始しました',
  };
}



async function _updateBackupTargetsProcess(payload) {
  const current = _loadBackupRuntimeState();
  const rawNormalizedTargets = Array.isArray(payload?.targets)
    ? payload.targets.map(_normalizeBackupTarget)
    : [];
  const normalizedTargets = rawNormalizedTargets.filter((target) => {
    // 以前の失敗したフォルダバックアップ登録で、remote_item_id のない
    // backup folder target が runtime に残ると、存在しないルートへ毎回アップロードし直す。
    // フォルダ自動バックアップはクラウド上のルート item_id と紐付いたものだけ有効にする。
    if (_targetKind(target) === 'backup' && target.item_type === 'folder' && !String(target.remote_item_id || '').trim()) {
      _backupDebug('drop ghost folder backup target on update', {
        remote_path: target.remote_path,
        local_path: target.local_path,
      });
      return false;
    }
    return true;
  });
  let localRootDisplay = String(payload?.local_root_display || '').trim() || _summarizeLocalRootDisplay(normalizedTargets);
  const incomingAccessToken = String(payload?.access_token || '').trim();
  const incomingApiBase = String(payload?.api_base || '').trim();
  const incomingEmail = String(payload?.email || '').trim();
  const incomingPollingInterval = Number(payload?.polling_interval_sec || 0);
  const hasIncomingIgnoreHidden = Object.prototype.hasOwnProperty.call(payload || {}, 'ignore_hidden');
  const incomingIgnoreHidden = Boolean(payload?.ignore_hidden);
  let effectiveTargets = normalizedTargets;
  if (effectiveTargets.length) {
    const pruneConfig = _normalizeBackupPayload({
      ...(current.config || {}),
      api_base: incomingApiBase || current.config?.api_base || payload?.api_base,
      access_token: incomingAccessToken || _backupSecret?.access_token || _backupTokenCache || payload?.access_token || '',
      email: incomingEmail || current.config?.email || payload?.email,
      polling_interval_sec: incomingPollingInterval || current.config?.polling_interval_sec || payload?.polling_interval_sec || 5,
      ignore_hidden: hasIncomingIgnoreHidden ? incomingIgnoreHidden : current.config?.ignore_hidden,
      local_root_display: localRootDisplay,
      targets: effectiveTargets,
      client_id: current.client_id || current.config?.client_id || payload?.client_id,
    });
    const pruned = await _pruneStaleBackupFolderTargets(pruneConfig, { persist: true });
    effectiveTargets = pruned.config.targets;
    if (pruned.changed) {
      localRootDisplay = _summarizeLocalRootDisplay(effectiveTargets);
    }
  }
  if (incomingAccessToken) {
    _backupSecret = { ...(_backupSecret || {}), access_token: incomingAccessToken };
    _backupTokenCache = incomingAccessToken;
    if (_backupConfig) {
      _backupConfig = {
        ..._backupConfig,
        access_token: incomingAccessToken,
        api_base: incomingApiBase || _backupConfig.api_base,
        email: incomingEmail || _backupConfig.email,
        polling_interval_sec: incomingPollingInterval || _backupConfig.polling_interval_sec,
        ignore_hidden: hasIncomingIgnoreHidden ? incomingIgnoreHidden : _backupConfig.ignore_hidden,
      };
    }
  }
  const filteredSnapshots = _filterBackupSnapshotsForTargets(current.snapshots, effectiveTargets);
  _backupDebug('update backup targets', {
    total: effectiveTargets.length,
    backup: effectiveTargets.filter((target) => !_isOfflineTarget(target)).length,
    offline: effectiveTargets.filter((target) => _isOfflineTarget(target)).length,
    running: Boolean(current.is_running),
  });

  if (!effectiveTargets.length) {
    return _stopBackupProcess();
  }

  const snapshotConfig = _backupConfig
    ? { ..._backupConfig, targets: effectiveTargets, local_root_display: localRootDisplay }
    : {
        ..._normalizeBackupPayload({
          ...(current.config || {}),
          access_token: incomingAccessToken || _backupSecret?.access_token || _backupTokenCache || '',
          api_base: incomingApiBase || current.config?.api_base,
          email: incomingEmail || current.config?.email,
          polling_interval_sec: incomingPollingInterval || current.config?.polling_interval_sec,
          ignore_hidden: hasIncomingIgnoreHidden ? incomingIgnoreHidden : current.config?.ignore_hidden,
          targets: effectiveTargets,
          local_root_display: localRootDisplay,
        }),
        targets: effectiveTargets,
        local_root_display: localRootDisplay,
      };
  const nextSnapshots = current.is_running
    ? await _buildBackupBaselineSnapshots(snapshotConfig, filteredSnapshots)
    : filteredSnapshots;

  if (_backupConfig) {
    _backupConfig = {
      ..._backupConfig,
      access_token: incomingAccessToken || _backupConfig.access_token,
      api_base: incomingApiBase || _backupConfig.api_base,
      email: incomingEmail || _backupConfig.email,
      polling_interval_sec: incomingPollingInterval || _backupConfig.polling_interval_sec,
      ignore_hidden: hasIncomingIgnoreHidden ? incomingIgnoreHidden : _backupConfig.ignore_hidden,
      targets: effectiveTargets,
      local_root_display: localRootDisplay,
    };
  } else if (current.is_running) {
    // アプリ再開後など、state上は実行中でもメモリ上の _backupConfig が空の場合に備える。
    _backupConfig = snapshotConfig;
  }

  const nextState = {
    ...current,
    local_root_display: localRootDisplay,
    targets: effectiveTargets,
    snapshots: nextSnapshots,
    config: current.config
      ? {
          ...current.config,
          targets: effectiveTargets,
          local_root_display: localRootDisplay,
        }
      : _buildPersistedBackupConfig(snapshotConfig),
    resume_enabled: current.is_running ? true : Boolean(current.resume_enabled),
    open_at_login_enabled: current.is_running ? _setBackupOpenAtLogin(true) : Boolean(current.open_at_login_enabled),
  };
  _saveBackupRuntimeState(nextState);
  if (current.is_running) {
    await _dedupeBackupTargetsOnce(snapshotConfig);
    // 追加・削除されたオフライン利用対象を、次のinterval待ちにせず即座に監視へ反映する。
    // scan中なら _backupScanInFlight により安全にスキップされる。
    _scanBackupOnce(_backupConfig || snapshotConfig).catch((err) => {
      _backupDebug('backup scan after target update failed', { error: String(err?.message || err || '') });
    });
  }
  return {
    ok: true,
    state: _publicBackupState(nextState),
    message: 'バックアップ対象を更新しました',
  };
}


function _stopBackupProcess() {
  const current = _loadBackupRuntimeState();
  const openAtLoginEnabled = _setBackupOpenAtLogin(false);
  if (!current.is_running && !_backupTimer) {
    const nextState = {
      ...current,
      is_running: false,
      stopped_at: _nowIso(),
      status: 'stopped',
      resume_enabled: false,
      open_at_login_enabled: openAtLoginEnabled,
      resumed_from_disk: false,
      secret_encrypted_b64: null,
      config: null,
    };
    _saveBackupRuntimeState(nextState);
    return {
      ok: true,
      alreadyStopped: true,
      state: _publicBackupState(nextState),
      message: 'バックアップは停止済みです',
    };
  }

  _stopBackupTimer();
  _backupConfig = null;
  _backupSecret = null;
  _backupTokenCache = null;
  const nextState = {
    ...current,
    is_running: false,
    stopped_at: _nowIso(),
    status: 'stopped',
    resume_enabled: false,
    open_at_login_enabled: openAtLoginEnabled,
    resumed_from_disk: false,
    secret_encrypted_b64: null,
    config: null,
  };
  _saveBackupRuntimeState(nextState);
  return {
    ok: true,
    alreadyStopped: false,
    state: _publicBackupState(nextState),
    message: 'バックアップを停止しました',
  };
}

function _getBackupStatus() {
  const current = _loadBackupRuntimeState();
  const alive = Boolean(_backupTimer && _backupConfig);
  if (alive === Boolean(current.is_running)) {
    return {
      ok: true,
      state: _publicBackupState(current),
    };
  }
  const nextState = {
    ...current,
    is_running: alive,
    status: alive ? current.status || 'running' : 'stopped',
    stopped_at: alive ? current.stopped_at : current.stopped_at || _nowIso(),
  };
  _saveBackupRuntimeState(nextState);
  return {
    ok: true,
    state: _publicBackupState(nextState),
  };
}

async function _resumeBackupIfConfigured() {
  if (_backupResumeAttempted) return;
  _backupResumeAttempted = true;

  const current = _loadBackupRuntimeState();
  if (!current?.resume_enabled || !current?.config) {
    return;
  }

  try {
    const restoredAccessToken = _decryptBackupPassword(current.secret_encrypted_b64);
    const config = _normalizeBackupPayload({
      ...current.config,
      access_token: restoredAccessToken,
      password: '',
    });

    _backupSecret = { access_token: restoredAccessToken };
    const openAtLoginEnabled = _setBackupOpenAtLogin(true);
    const nextState = {
      ..._defaultBackupState(),
      ...current,
      is_running: true,
      started_at: current.started_at || _nowIso(),
      stopped_at: null,
      status: 'restoring',
      error: null,
      local_root_display: config.local_root_display,
      targets: config.targets,
      client_id: config.client_id,
      config: _buildPersistedBackupConfig(config),
      resume_enabled: true,
      open_at_login_enabled: openAtLoginEnabled,
      resumed_from_disk: true,
    };
    _saveBackupRuntimeState(nextState);
    _startBackupLoop(config);
  } catch (err) {
    const failedState = {
      ...current,
      is_running: false,
      status: 'error',
      stopped_at: _nowIso(),
      error: String(err?.message || err || 'バックアップ自動再開に失敗しました'),
      resumed_from_disk: false,
    };
    _saveBackupRuntimeState(failedState);
  }
}

function registerPhase1NodeBridgeIpc() {
  ipcMain.handle(IPC_CHANNELS.start, async (_event, payload) => {
    return _startNodeProcess(payload, { resumedFromDisk: false });
  });

  ipcMain.handle(IPC_CHANNELS.stop, async () => {
    return _stopNodeProcess();
  });

  ipcMain.handle(IPC_CHANNELS.status, async () => {
    return _getNodeStatus();
  });

  ipcMain.handle(IPC_CHANNELS.getStatePath, async () => {
    return {
      ok: true,
      statePath: _stateFilePath(),
      logDir: _logDirPath(),
    };
  });

  ipcMain.handle(IPC_CHANNELS.localCapacity, async (_event, payload) => {
    return _getLocalCapacityHint(payload || {});
  });

  ipcMain.handle(BACKUP_IPC_CHANNELS.start, async (_event, payload) => {
    return _startBackupProcess(payload);
  });

  ipcMain.handle(BACKUP_IPC_CHANNELS.stop, async () => {
    return _stopBackupProcess();
  });

  ipcMain.handle(BACKUP_IPC_CHANNELS.status, async () => {
    return _getBackupStatus();
  });

  ipcMain.handle(BACKUP_IPC_CHANNELS.getStatePath, async () => {
    return {
      ok: true,
      statePath: _backupStateFilePath(),
    };
  });

  ipcMain.handle(BACKUP_IPC_CHANNELS.updateTargets, async (_event, payload) => {
    return _updateBackupTargetsProcess(payload);
  });

  ipcMain.handle(BACKUP_IPC_CHANNELS.uploadFolderFromDialog, async (_event, payload) => {
    return _uploadBackupFolderFromDialogProcess(payload);
  });

  ipcMain.handle(OFFLINE_IPC_CHANNELS.enable, async (_event, payload) => {
    return _enableOfflineFileProcess(payload);
  });

  ipcMain.handle(OFFLINE_IPC_CHANNELS.disable, async (_event, payload) => {
    try {
      return await _disableOfflineUseProcess(payload);
    } catch (err) {
      return { ok: false, error: String(err?.message || err || 'オフライン利用の停止に失敗しました') };
    }
  });

  ipcMain.handle(DOWNLOAD_IPC_CHANNELS.saveToDownloads, async (_event, payload) => {
    try {
      return await _downloadFileToDownloadsProcess(payload);
    } catch (err) {
      return { ok: false, error: String(err?.message || err || 'ダウンロードに失敗しました') };
    }
  });

  ipcMain.handle(DOWNLOAD_IPC_CHANNELS.openFile, async (_event, payload) => {
    try {
      return await _openCloudFileWithDefaultAppProcess(payload);
    } catch (err) {
      return { ok: false, error: String(err?.message || err || 'ファイルを開けませんでした') };
    }
  });

  app.whenReady().then(() => {
    _resumeNodeOnAppReady().catch(() => {});
    _resumeBackupIfConfigured().catch(() => {});
  });

  app.on('before-quit', () => {
    _stopBackupTimer();
    _backupConfig = null;
    _backupTokenCache = null;
  });
}

module.exports = {
  IPC_CHANNELS,
  BACKUP_IPC_CHANNELS,
  OFFLINE_IPC_CHANNELS,
  DOWNLOAD_IPC_CHANNELS,
  registerPhase1NodeBridgeIpc,
};
