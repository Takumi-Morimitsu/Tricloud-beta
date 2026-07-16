// electron_app_main.cjs
// Vite renderer + Phase1 desktop backend IPC をつなぐ Electron 起動ファイル。
// v2: packaged build の白画面診断と本番/開発分岐の安全化を追加。

const { app, BrowserWindow } = require('electron');
const path = require('node:path');
const fs = require('node:fs');

let logFile = null;

function now() {
  return new Date().toISOString();
}

function safeStringify(value) {
  try {
    if (value instanceof Error) {
      return `${value.stack || value.message || value}`;
    }
    if (typeof value === 'string') return value;
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function log(...parts) {
  const line = `[${now()}] ${parts.map(safeStringify).join(' ')}\n`;
  try {
    process.stderr.write(line);
  } catch {
    // ignore
  }
  if (logFile) {
    try {
      fs.mkdirSync(path.dirname(logFile), { recursive: true });
      fs.appendFileSync(logFile, line, 'utf8');
    } catch {
      // ignore
    }
  }
}

process.on('uncaughtException', (err) => {
  log('[main uncaughtException]', err);
});

process.on('unhandledRejection', (reason) => {
  log('[main unhandledRejection]', reason);
});

function fileExists(filePath) {
  try {
    return fs.existsSync(filePath);
  } catch {
    return false;
  }
}

function createFallbackHtml(title, details) {
  const escapedTitle = String(title).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));

  const escapedDetails = String(details).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[ch]));

  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>${escapedTitle}</title>
  <style>
    body {
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 40px;
      line-height: 1.6;
      color: #111827;
      background: #ffffff;
    }
    pre {
      white-space: pre-wrap;
      background: #f3f4f6;
      border: 1px solid #e5e7eb;
      border-radius: 8px;
      padding: 16px;
    }
  </style>
</head>
<body>
  <h1>${escapedTitle}</h1>
  <p>Electron は起動しましたが、画面の読み込みに失敗しました。</p>
  <pre>${escapedDetails}</pre>
</body>
</html>`;
}

function attachWindowDiagnostics(win) {
  win.webContents.on('did-finish-load', () => {
    log('[renderer did-finish-load]', win.webContents.getURL());
  });

  win.webContents.on('did-fail-load', (event, errorCode, errorDescription, validatedURL, isMainFrame) => {
    log('[renderer did-fail-load]', {
      errorCode,
      errorDescription,
      validatedURL,
      isMainFrame,
    });
  });

  win.webContents.on('did-fail-provisional-load', (event, errorCode, errorDescription, validatedURL, isMainFrame) => {
    log('[renderer did-fail-provisional-load]', {
      errorCode,
      errorDescription,
      validatedURL,
      isMainFrame,
    });
  });

  win.webContents.on('console-message', (event, level, message, line, sourceId) => {
    log('[renderer console-message]', {
      level,
      message,
      line,
      sourceId,
    });
  });

  win.webContents.on('render-process-gone', (event, details) => {
    log('[renderer render-process-gone]', details);
  });

  win.webContents.on('preload-error', (event, preloadPath, error) => {
    log('[renderer preload-error]', {
      preloadPath,
      error: safeStringify(error),
    });
  });
}

function createWindow() {
  const preloadPath = path.join(
    __dirname,
    'electron',
    'electron_desktop_backend_preload_node_AutoBackup_OfflineUse.cjs'
  );

  log('[createWindow]', {
    isPackaged: app.isPackaged,
    appPath: app.getAppPath(),
    dirname: __dirname,
    execPath: process.execPath,
    preloadPath,
    preloadExists: fileExists(preloadPath),
    ELECTRON_RENDERER_MODE: process.env.ELECTRON_RENDERER_MODE || '',
    VITE_DEV_SERVER_URL: process.env.VITE_DEV_SERVER_URL || '',
  });

  const win = new BrowserWindow({
    width: 1280,
    height: 840,
    show: true,
    backgroundColor: '#ffffff',
    webPreferences: {
      preload: preloadPath,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  attachWindowDiagnostics(win);

  if (process.env.TRICLOUD_OPEN_DEVTOOLS === '1') {
    win.webContents.openDevTools({ mode: 'detach' });
  }

  // 重要:
  // 本番版で偶然 ELECTRON_RENDERER_MODE=dev が残っていても dev server へ行かないようにする。
  const shouldUseDevServer =
    !app.isPackaged && process.env.ELECTRON_RENDERER_MODE === 'dev';

  if (shouldUseDevServer) {
    const devUrl = process.env.VITE_DEV_SERVER_URL || 'http://127.0.0.1:5173';
    log('[loadURL]', devUrl);
    win.loadURL(devUrl).catch((err) => {
      log('[loadURL failed]', err);
      win.loadURL(
        'data:text/html;charset=utf-8,' +
          encodeURIComponent(createFallbackHtml('Tricloud loadURL failed', safeStringify(err)))
      );
    });
    return;
  }

  const candidates = [
    path.join(__dirname, 'dist', 'index.html'),
    path.join(app.getAppPath(), 'dist', 'index.html'),
  ];

  const indexHtml = candidates.find(fileExists);

  log('[loadFile candidates]', candidates.map((candidate) => ({
    path: candidate,
    exists: fileExists(candidate),
  })));

  if (!indexHtml) {
    const details = [
      'dist/index.html が見つかりません。',
      '',
      `app.isPackaged: ${app.isPackaged}`,
      `__dirname: ${__dirname}`,
      `app.getAppPath(): ${app.getAppPath()}`,
      '',
      'checked:',
      ...candidates.map((candidate) => `- ${candidate}`),
      '',
      `logFile: ${logFile || '(not initialized)'}`,
    ].join('\n');

    log('[loadFile missing dist/index.html]', details);
    win.loadURL(
      'data:text/html;charset=utf-8,' +
        encodeURIComponent(createFallbackHtml('Tricloud renderer file not found', details))
    );
    return;
  }

  log('[loadFile]', indexHtml);
  win.loadFile(indexHtml).catch((err) => {
    log('[loadFile failed]', err);
    win.loadURL(
      'data:text/html;charset=utf-8,' +
        encodeURIComponent(createFallbackHtml('Tricloud loadFile failed', safeStringify(err)))
    );
  });
}

app.whenReady().then(() => {
  try {
    logFile = path.join(app.getPath('userData'), 'electron-main.log');
    log('[startup]', {
      name: app.name,
      version: app.getVersion(),
      userData: app.getPath('userData'),
      logFile,
    });
  } catch (err) {
    log('[startup log init failed]', err);
  }

  try {
    const bridgeModule = require('./electron/electron_desktop_backend_main_node_AutoBackup_OfflineUse.cjs');
    if (bridgeModule && typeof bridgeModule.registerPhase1NodeBridgeIpc === 'function') {
      bridgeModule.registerPhase1NodeBridgeIpc();
      log('[registerPhase1NodeBridgeIpc ok]');
    } else {
      log('[registerPhase1NodeBridgeIpc missing export]', Object.keys(bridgeModule || {}));
    }
  } catch (err) {
    // ここでアプリ全体を落とすと原因が画面に出ないため、まずログに残して画面は起動する。
    log('[registerPhase1NodeBridgeIpc failed]', err);
  }

  createWindow();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
