const CONFIG = {
  SYNC_URL: 'http://boll.aqcloud.ltd/api/trade-sync',
  SYNC_TOKEN: 'sd_iwillbetherichestmanintheworld',
  SHEET_NAME: 'main',
  ACCOUNT_SHEET_NAME: 'account',
  SIGNAL_TARGETS_SHEET_NAME: 'signal_targets',
  HEADER_ROW: 1,
  TIMEZONE: 'Asia/Shanghai',

  AUTO_SYNC_ENABLED: true,
  AUTO_SYNC_INTERVAL_MINUTES: 15,

  AUTO_SYNC_START_HOUR: 21,
  AUTO_SYNC_END_HOUR: 24
};

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Trade Sync')
    .addItem('Sync Now', 'syncTradesToServer')
    .addItem('Install Auto Trigger', 'installSyncTrigger')
    .addItem('Delete Auto Triggers', 'deleteSyncTriggers')
    .addItem('Sync Status', 'showSyncStatus')
    .addToUi();
}

function syncTradesToServer() {
  return runSync_({ manual: true });
}

function autoSyncTradesToServer() {
  return runSync_({ manual: false });
}

function runSync_(options) {
  const manual = Boolean(options && options.manual);

  if (!manual && !shouldAutoSyncNow_()) {
    Logger.log('Skip auto sync: outside active trading window');
    return {
      success: true,
      skipped: true,
      reason: 'outside_active_window'
    };
  }

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const mainSheet = ss.getSheetByName(CONFIG.SHEET_NAME);
  if (!mainSheet) {
    throw new Error('Sheet not found: ' + CONFIG.SHEET_NAME);
  }

  const payload = buildPayload_(ss, mainSheet);

  const response = UrlFetchApp.fetch(CONFIG.SYNC_URL, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + CONFIG.SYNC_TOKEN
    },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  });

  const code = response.getResponseCode();
  const text = response.getContentText();

  Logger.log('trade sync status=' + code);
  Logger.log(text);

  if (code < 200 || code >= 300) {
    throw new Error('Sync failed: HTTP ' + code + ' ' + text);
  }

  const mode = manual ? 'Manual Sync' : 'Auto Sync';
  SpreadsheetApp.getActiveSpreadsheet().toast(mode + ' success', 'Trade Sync', 5);

  return {
    success: true,
    skipped: false,
    statusCode: code,
    responseText: text
  };
}

function buildPayload_(spreadsheet, sheet) {
  const mainRows = readSheetRows_(sheet);
  const accountSheet = spreadsheet.getSheetByName(CONFIG.ACCOUNT_SHEET_NAME);
  const targetSheet = spreadsheet.getSheetByName(CONFIG.SIGNAL_TARGETS_SHEET_NAME);
  const accountRows = accountSheet ? readSheetRows_(accountSheet) : [];
  const signalTargetRows = targetSheet ? readSheetRows_(targetSheet) : [];

  return {
    schema_version: 2,
    spreadsheet_id: spreadsheet.getId(),
    spreadsheet_name: spreadsheet.getName(),
    sheet_name: sheet.getName(),
    exported_at: Utilities.formatDate(
      new Date(),
      CONFIG.TIMEZONE,
      "yyyy-MM-dd'T'HH:mm:ssXXX"
    ),
    rows: mainRows,
    sheets: {
      main: {
        sheet_name: sheet.getName(),
        rows: mainRows
      },
      account: {
        sheet_name: CONFIG.ACCOUNT_SHEET_NAME,
        rows: accountRows
      },
      signal_targets: {
        sheet_name: CONFIG.SIGNAL_TARGETS_SHEET_NAME,
        rows: signalTargetRows
      }
    },
    account: accountRows,
    signal_targets: signalTargetRows
  };
}

function readSheetRows_(sheet) {
  const values = sheet.getDataRange().getValues();
  if (values.length < 2) {
    return [];
  }

  const headers = values[CONFIG.HEADER_ROW - 1].map(normalizeHeader_);
  const rows = [];

  for (let i = CONFIG.HEADER_ROW; i < values.length; i++) {
    const row = values[i];
    if (isEmptyRow_(row)) {
      continue;
    }

    const obj = {};
    for (let j = 0; j < headers.length; j++) {
      const key = headers[j];
      if (!key) {
        continue;
      }
      obj[key] = normalizeCellValue_(row[j]);
    }

    obj._sheet_row = i + 1;
    rows.push(obj);
  }

  return rows;
}

function installSyncTrigger() {
  deleteSyncTriggers_();

  ScriptApp.newTrigger('autoSyncTradesToServer')
    .timeBased()
    .everyMinutes(CONFIG.AUTO_SYNC_INTERVAL_MINUTES)
    .create();

  SpreadsheetApp.getActiveSpreadsheet().toast(
    'Auto sync trigger installed: every ' + CONFIG.AUTO_SYNC_INTERVAL_MINUTES + ' minutes',
    'Trade Sync',
    5
  );
}

function deleteSyncTriggers() {
  deleteSyncTriggers_();
  SpreadsheetApp.getActiveSpreadsheet().toast('Auto sync triggers deleted', 'Trade Sync', 5);
}

function deleteSyncTriggers_() {
  const triggers = ScriptApp.getProjectTriggers();
  for (const trigger of triggers) {
    const fn = trigger.getHandlerFunction();
    if (fn === 'autoSyncTradesToServer' || fn === 'syncTradesToServer') {
      ScriptApp.deleteTrigger(trigger);
    }
  }
}

function showSyncStatus() {
  const now = new Date();
  const nowText = Utilities.formatDate(now, CONFIG.TIMEZONE, 'yyyy-MM-dd HH:mm:ss');
  const inWindow = shouldAutoSyncNow_();

  const message = [
    'Sheet: ' + CONFIG.SHEET_NAME,
    'Account Sheet: ' + CONFIG.ACCOUNT_SHEET_NAME,
    'Signal Targets Sheet: ' + CONFIG.SIGNAL_TARGETS_SHEET_NAME,
    'URL: ' + CONFIG.SYNC_URL,
    'Auto Sync: ' + (CONFIG.AUTO_SYNC_ENABLED ? 'ON' : 'OFF'),
    'Interval: every ' + CONFIG.AUTO_SYNC_INTERVAL_MINUTES + ' minutes',
    'Window: ' + pad2_(CONFIG.AUTO_SYNC_START_HOUR) + ':00 - ' + pad2_(CONFIG.AUTO_SYNC_END_HOUR) + ':00',
    'Now: ' + nowText,
    'In Active Window: ' + (inWindow ? 'YES' : 'NO')
  ].join('\n');

  SpreadsheetApp.getUi().alert('Trade Sync Status', message, SpreadsheetApp.getUi().ButtonSet.OK);
}

function shouldAutoSyncNow_() {
  if (!CONFIG.AUTO_SYNC_ENABLED) {
    return false;
  }

  const now = new Date();
  const hour = Number(Utilities.formatDate(now, CONFIG.TIMEZONE, 'H'));

  return hour >= CONFIG.AUTO_SYNC_START_HOUR && hour < CONFIG.AUTO_SYNC_END_HOUR;
}

function normalizeHeader_(value) {
  return String(value || '').trim();
}

function normalizeCellValue_(value) {
  if (value instanceof Date) {
    return Utilities.formatDate(value, CONFIG.TIMEZONE, 'yyyy-MM-dd');
  }
  return value;
}

function isEmptyRow_(row) {
  for (const cell of row) {
    if (cell !== '' && cell !== null) {
      return false;
    }
  }
  return true;
}

function pad2_(n) {
  return n < 10 ? '0' + n : String(n);
}
