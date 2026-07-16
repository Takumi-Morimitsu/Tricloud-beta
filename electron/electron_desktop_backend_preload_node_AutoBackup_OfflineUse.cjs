// electron_desktop_backend_preload_node_AutoBackup_OfflineUse.cjs
// -*- coding: utf-8 -*-
/**
 * Phase1 デスクトップアプリ用 preload
 *
 * 目的:
 * - Renderer(TSX) から直接 Node/Electron API を触らせず、
 *   node / backup / offline API を正式な単一入口として安全に公開する
 */

const { contextBridge, ipcRenderer, webUtils } = require('electron');

function _toFileArray(filesLike) {
  try {
    return Array.from(filesLike || []);
  } catch {
    return [];
  }
}

const BRIDGE = {
  /**
   * ノード起動
   */
  startNode: async (payload) => {
    return ipcRenderer.invoke('phase1-node:start', payload);
  },

  /**
   * ノード停止
   */
  stopNode: async () => {
    return ipcRenderer.invoke('phase1-node:stop');
  },

  /**
   * ノード状態確認
   */
  getNodeStatus: async () => {
    return ipcRenderer.invoke('phase1-node:status');
  },

  /**
   * ローカル保存先の確認（デバッグ用）
   */
  getNodeStatePath: async () => {
    return ipcRenderer.invoke('phase1-node:state-path');
  },

  /**
   * このPCのストレージ提供用ローカル容量を取得する。
   */
  getLocalCapacity: async (payload = {}) => {
    return ipcRenderer.invoke('phase1-node:local-capacity', payload);
  },

  /**
   * File オブジェクトからローカル絶対パスを取得する。
   * Electron 29+ では webUtils.getPathForFile() が推奨。
   */
  getPathForFiles: (filesLike) => {
    return _toFileArray(filesLike).map((file) => {
      try {
        return webUtils.getPathForFile(file) || '';
      } catch {
        return '';
      }
    });
  },

  /**
   * 自動バックアップ開始
   */
  startBackup: async (payload) => {
    return ipcRenderer.invoke('phase1-backup:start', payload);
  },

  /**
   * 自動バックアップ停止
   */
  stopBackup: async () => {
    return ipcRenderer.invoke('phase1-backup:stop');
  },

  /**
   * 自動バックアップ状態確認
   */
  getBackupStatus: async () => {
    return ipcRenderer.invoke('phase1-backup:status');
  },

  /**
   * 自動バックアップ対象の更新
   */
  updateBackupTargets: async (payload) => {
    return ipcRenderer.invoke('phase1-backup:update-targets', payload);
  },

  /**
   * 自動バックアップ用フォルダ選択とアップロード
   */
  uploadBackupFolderFromDialog: async (payload) => {
    return ipcRenderer.invoke('phase1-backup:upload-folder-from-dialog', payload);
  },

  /**
   * 自動バックアップ状態ファイルの確認（デバッグ用）
   */
  getBackupStatePath: async () => {
    return ipcRenderer.invoke('phase1-backup:state-path');
  },

  /**
   * ファイル・フォルダをオフライン利用用としてローカルに保存する
   */
  enableOfflineFile: async (payload) => {
    return ipcRenderer.invoke('phase1-offline:enable', payload);
  },

  /**
   * オフライン利用を停止し、ローカルコピーを削除する
   */
  disableOfflineUse: async (payload) => {
    return ipcRenderer.invoke('phase1-offline:disable', payload);
  },

  /**
   * 復号済みファイルをOS標準のダウンロードフォルダへ保存する
   */
  downloadFileToDownloads: async (payload) => {
    return ipcRenderer.invoke('phase1-download:to-downloads', payload);
  },

  /**
   * 復号済みファイルを一時保存して、OS既定のアプリで開く
   */
  openCloudFile: async (payload) => {
    return ipcRenderer.invoke('phase1-file:open', payload);
  },
};

contextBridge.exposeInMainWorld('electronAPI', BRIDGE);
// 以前の命名に合わせた別名も公開しておく
contextBridge.exposeInMainWorld('phase1Desktop', BRIDGE);
