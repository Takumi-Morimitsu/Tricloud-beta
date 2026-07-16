import React, { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  Activity,
  Zap,
  ZapOff,
  ChevronRight,
  Clock3,
  Coins,
  Copy,
  Download,
  Globe,
  MapPinned,
  Gem,
  Folder, FolderOpen,
  FolderPlus,
  FileArchive,
  FileImage,
  FileSpreadsheet,
  FileText,
  FileVideo,
  HardDrive,
  History,
  Home,
  Laptop,
  LaptopMinimalCheck,
  CloudDownload,
  UserRoundPlus,
  Link as LinkIcon,
  LogOut,
  MoreHorizontal,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Server,
  Settings,
  ShieldCheck,
  Star,
  Trash,
  Trash2,
  Upload,
  UserCircle2,
  Users,
  Wallet,
} from "lucide-react";
import StripeConnect from "./StripeConnect";
import tricloudAppIconLogin from "./assets/tricloud-app-icon-login.png";
import tricloudAppIconApp from "./assets/tricloud-app-icon-app.png";

type AuthMode = "login" | "signup";
type ViewMode = "home" | "folders" | "shared" | "recent" | "search" | "provider" | "trash" | "sync";
type SortKey = "name" | "updated_at" | "size_bytes" | "trashed_at";
type SortDir = "asc" | "desc";

const TRI_CLOUD_ITEM_DRAG_MIME = "application/x-tri-cloud-item-ids";
const TRI_CLOUD_ITEM_TEXT_PREFIX = "tri-cloud-item-ids:";

type AuthResponse = { user_id: string; access_token: string };
type UserProfile = {
  user_id: string;
  email: string;
  last_name?: string | null;
  first_name?: string | null;
  country_code?: string | null;
};
type SignupPayload = {
  last_name: string;
  first_name: string;
  email: string;
  password: string;
  country_code: string;
  accepted_terms: boolean;
  accepted_privacy_policy: boolean;
};

type Item = {
  item_id: string;
  type: "file" | "folder";
  parent_id: string | null;
  name: string;
  size_bytes?: number | null;
  updated_at?: number | null;
  created_at?: number | null;
  owner_user_id?: string;
  file_object_id?: string | null;
  trashed_at?: number | null;
  trash_batch_id?: string | null;
  version_count?: number | null;
  path?: string;
};

type ShareResponse = { share_id: string; expires_at?: number | null };
type ShareSendResponse = {
  ok: boolean;
  share_id?: string;
  recipient_user_id?: string;
  recipient_email?: string;
  item_id?: string;
  name?: string;
  message?: string | null;
};
type DownloadTokenResponse = {
  download_token: string;
  file_object_id: string;
  expires_at: number;
  charge_user_id: string;
  is_shared: boolean;
};

type ListResponse = { items: Item[]; parent?: Item | null; breadcrumbs?: Item[] };
type SearchResponse = { items: Item[]; q: string; total?: number };

type VersionEntry = {
  version_id: string;
  version_no?: number | null;
  file_object_id?: string | null;
  name: string;
  size_bytes: number;
  created_at?: number | null;
  created_by_user_id?: string;
  source?: string;
  restore_from_version_id?: string | null;
  part_count?: number;
  is_current?: boolean;
};

type VersionResponse = { item: Item; versions: VersionEntry[] };

type SyncProfile = {
  user_id?: string;
  local_root_display: string;
  sync_mode: string;
  polling_interval_sec: number;
  ignore_hidden: boolean;
  created_at?: number | null;
  updated_at?: number | null;
};

type SyncClientStatus = {
  client_id: string;
  user_id?: string;
  local_root_display: string;
  status: string;
  sync_mode: string;
  pending_changes: number;
  app_version?: string | null;
  last_seen?: number | null;
  last_sync_at?: number | null;
};

type SyncProfileResponse = { profile: SyncProfile; clients: SyncClientStatus[] };

type TreeResponse = { items: Item[]; profile?: SyncProfile | null };
type UploadCandidate = { file: File; relativePath: string; absolutePath?: string | null };

type TextPromptConfig = {
  title: string;
  message: string;
  defaultValue?: string;
  placeholder?: string;
  inputType?: React.HTMLInputTypeAttribute;
  confirmLabel?: string;
  cancelLabel?: string;
};

type TextPromptState = TextPromptConfig & {
  resolve: (value: string | null) => void;
};

type AppDialogVariant = "info" | "warning" | "danger";

type AppDialogConfig = {
  title: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: AppDialogVariant;
};

type AppDialogState = AppDialogConfig & {
  resolve: (value: boolean) => void;
};

type UploadConflictDecision = "cancel" | "copy" | "replace";

type UploadConflictDialogConfig = {
  title: string;
  message: string;
  cancelLabel?: string;
  copyLabel?: string;
  replaceLabel?: string;
};

type UploadConflictDialogState = UploadConflictDialogConfig & {
  resolve: (value: UploadConflictDecision) => void;
};


type BackupTarget = {
  local_path: string;
  remote_path: string;
  item_type: "file" | "folder";
  display_name: string;
  source_device_label?: string;
  remote_item_id?: string | null;
  // オフライン利用では、ダウンロード直後の状態を基準値として保持する。
  // 初回監視前に編集された場合でも、ここから差分を検出してクラウドへ反映するため。
  baseline_snapshot?: any;
  // 通常の「バックアップ設定」と、右クリックの「オフライン利用」を分離する。
  // 未指定は既存互換のため通常バックアップとして扱う。
  target_kind?: "backup" | "offline";
};

type BackupTargetsResponse = {
  targets: BackupTarget[];
  total?: number;
};

type OfflineUseFileRequest = {
  item_id: string;
  download_token: string;
  remote_path: string;
  display_name: string;
  size_bytes?: number | null;
};

type OfflineUseFolderRequest = {
  item_id: string;
  remote_path: string;
  display_name: string;
};

type BackupFolderUploadResponse = {
  ok: boolean;
  cancelled?: boolean;
  target?: BackupTarget;
  uploaded_count?: number;
  folder_count?: number;
  message?: string;
  error?: string;
};

type BackupBridgeResponse = {
  ok: boolean;
  alreadyRunning?: boolean;
  alreadyStopped?: boolean;
  message?: string;
  error?: string;
  state?: {
    is_running: boolean;
    started_at?: string | null;
    stopped_at?: string | null;
    last_scan_at?: string | null;
    last_sync_at?: string | null;
    status?: string;
    pending_changes?: number;
    error?: string | null;
    targets?: BackupTarget[];
    current_device_label?: string | null;
    resume_enabled?: boolean;
    open_at_login_enabled?: boolean;
  };
};

type OfflineUseResponse = {
  ok: boolean;
  local_path?: string;
  remote_path?: string;
  display_name?: string;
  offline_root_path?: string;
  source_device_label?: string;
  file_count?: number;
  folder_count?: number;
  baseline_snapshot?: any;
  message?: string;
  error?: string;
};

type OfflineDisableResponse = {
  ok: boolean;
  removed_count?: number;
  deleted_local_count?: number;
  state?: BackupBridgeResponse["state"];
  message?: string;
  error?: string;
};

type NodeProfile = {
  node_id: string;
  owner_user_id: string;
  node_name: string;
  desired_capacity_bytes: number;
  desired_capacity_gb: number;
  node_api_key_preview?: string;
  stripe_connected_account_id?: string | null;
  payout_enabled?: boolean;
  payouts_paused?: boolean;
  created_at?: number | null;
  updated_at?: number | null;
};

type NodeRuntime = {
  online: boolean;
  last_seen?: number | null;
  capacity_bytes: number;
  reserved_bytes: number;
  free_bytes: number;
  source: string;
};

type EarningsSummary = {
  history_count: number;
  total_net_amount_yen: number;
  total_gb_month: number;
  avg_yen_per_gb_month: number;
  latest_period_net_yen: number;
  latest_period_end?: number | null;
};

type RewardProjection = {
  desired_capacity_gb: number;
  scenarios: {
    utilization_ratio: number;
    estimated_gb_month: number;
    estimated_reward_yen: number;
  }[];
};

type StripeSummary = {
  configured: boolean;
  connected: boolean;
  payout_enabled: boolean;
  payouts_paused: boolean;
};

type LaunchSummary = {
  runner_file: string;
  command: string;
  node_id: string;
  node_api_key: string;
  server: string;
  storage_dir: string;
  capacity_gb: number;
};

type NodeProviderSummary = {
  profile: NodeProfile | null;
  runtime: NodeRuntime;
  earnings_summary: EarningsSummary & { avg_utilization_ratio?: number | null };
  recent_earnings: Array<Record<string, any>>;
  recent_payouts: Array<Record<string, any>>;
  reward_projection: RewardProjection;
  stripe: StripeSummary;
  launch: LaunchSummary | null;
  defaults: {
    node_name: string;
    desired_capacity_gb: number;
    suggested_slider_max_gb: number;
  };
  local_capacity?: {
    total_bytes?: number;
    free_bytes?: number;
    offerable_bytes?: number;
    offerable_gb?: number;
    path?: string;
    source?: string;
  };
  uptime_summary?: {
    period_start?: number | null;
    period_end?: number | null;
    avg_used_bytes?: number | null;
    avg_used_gb?: number | null;
    online_ratio?: number | null;
    sample_count?: number;
    expected_samples?: number;
  };
};

type LocalCapacityHint = NonNullable<NodeProviderSummary["local_capacity"]> & {
  ok?: boolean;
  error?: string | null;
};

const API_BASE = ((import.meta as any)?.env?.VITE_API_BASE || "https://api.trytricloud.com").replace(/\/$/, "");
const SERVER_BASE = ((import.meta as any)?.env?.VITE_SERVER_BASE || "tcp://api.trytricloud.com:8888").replace(/\/$/, "");
const UPLOAD_CANCELLED_BY_USER = "__TRICLOUD_UPLOAD_CANCELLED_BY_USER__";

const COUNTRY_OPTIONS = [
  { code: "JP", label: "日本" },
  { code: "US", label: "アメリカ合衆国" },
  { code: "CA", label: "カナダ" },
  { code: "GB", label: "イギリス" },
  { code: "DE", label: "ドイツ" },
  { code: "FR", label: "フランス" },
  { code: "IT", label: "イタリア" },
  { code: "ES", label: "スペイン" },
  { code: "NL", label: "オランダ" },
  { code: "BE", label: "ベルギー" },
  { code: "CH", label: "スイス" },
  { code: "SE", label: "スウェーデン" },
  { code: "NO", label: "ノルウェー" },
  { code: "DK", label: "デンマーク" },
  { code: "FI", label: "フィンランド" },
  { code: "IE", label: "アイルランド" },
  { code: "PT", label: "ポルトガル" },
  { code: "AT", label: "オーストリア" },
  { code: "PL", label: "ポーランド" },
  { code: "CZ", label: "チェコ" },
  { code: "HU", label: "ハンガリー" },
  { code: "RO", label: "ルーマニア" },
  { code: "GR", label: "ギリシャ" },
  { code: "TR", label: "トルコ" },
  { code: "UA", label: "ウクライナ" },
  { code: "RU", label: "ロシア" },
  { code: "AU", label: "オーストラリア" },
  { code: "NZ", label: "ニュージーランド" },
  { code: "SG", label: "シンガポール" },
  { code: "KR", label: "韓国" },
  { code: "TW", label: "台湾" },
  { code: "HK", label: "香港" },
  { code: "CN", label: "中国" },
  { code: "IN", label: "インド" },
  { code: "TH", label: "タイ" },
  { code: "VN", label: "ベトナム" },
  { code: "MY", label: "マレーシア" },
  { code: "ID", label: "インドネシア" },
  { code: "PH", label: "フィリピン" },
  { code: "AE", label: "アラブ首長国連邦" },
  { code: "SA", label: "サウジアラビア" },
  { code: "IL", label: "イスラエル" },
  { code: "ZA", label: "南アフリカ" },
  { code: "EG", label: "エジプト" },
  { code: "BR", label: "ブラジル" },
  { code: "MX", label: "メキシコ" },
  { code: "AR", label: "アルゼンチン" },
  { code: "CL", label: "チリ" },
  { code: "CO", label: "コロンビア" },
 ];

const LANGUAGE_OPTIONS = [
  { code: "ja", label: "日本語" },
  { code: "en", label: "English" },
  { code: "es", label: "Español" },
];

const TERMS_VERSION = "2026-04";
const PRIVACY_POLICY_VERSION = "2026-04";
const TERMS_AND_PRIVACY_TEXT = `利用規約（抜粋）

1. このサービスは、利用者のファイル保存・同期・共有、および提供者によるストレージ提供機能を含みます。
2. 利用者は、自身がアップロード・保存・共有するデータについて責任を負います。
3. 違法なデータ、権利侵害データ、またはサービス運営を妨げるデータの保存は禁止します。
4. サービス品質の維持、セキュリティ対策、課金・報酬処理のため、必要な範囲で利用情報を記録します。
5. ストレージ提供者には、報酬支払いのため外部決済サービスとの連携を求める場合があります。
6. 国別ノード選定や不正検知のため、登録国情報を利用します。
7. 本サービスは継続的に改善されるため、規約やポリシーは改定されることがあります。

プライバシーポリシー（抜粋）

1. 取得する主な情報は、氏名、メールアドレス、登録国、認証情報、サービス利用履歴です。
2. 取得情報は、本人確認、認証、サポート、課金、報酬支払い、障害調査、セキュリティ対策のために利用します。
3. ファイル本体や暗号鍵は、サービス仕様に基づいて適切に取り扱います。
4. 法令上必要な場合や、決済・認証などサービス運営に必要な委託先を除き、個人情報を第三者へ不当に提供しません。
5. 利用者は、法令や規約に反しない範囲で登録情報の確認・更新を求めることができます。

上記内容を読み、利用規約 ${TERMS_VERSION} とプライバシーポリシー ${PRIVACY_POLICY_VERSION} に同意したうえで登録を進めてください。`;



type UILanguageCode = "ja" | "en" | "es";

type UiTranslationMap = Record<string, Partial<Record<UILanguageCode, string>>>;

const UI_TRANSLATIONS: UiTranslationMap = {
  "ユーザー": {
    "en": "User",
    "es": "Usuario"
  },
  "メールアドレス未設定": {
    "en": "Email not set",
    "es": "Correo no configurado"
  },
  "共有アイテム": {
    "en": "Shared items",
    "es": "Elementos compartidos"
  },
  "検索結果": {
    "en": "Search results",
    "es": "Resultados de búsqueda"
  },
  "ストレージ提供": {
    "en": "Storage contribution",
    "es": "Aportar almacenamiento"
  },
  "ごみ箱": {
    "en": "Trash",
    "es": "Papelera"
  },
  "バックアップ設定": {
    "en": "Backup settings",
    "es": "Configuración de copia de seguridad"
  },
  "フォルダ": {
    "en": "Folder",
    "es": "Carpeta"
  },
  "ファイル": {
    "en": "File",
    "es": "Archivo"
  },
  "同じ{targetLabel}があります": {
    "en": "A {targetLabel} already exists",
    "es": "Ya existe este tipo de elemento: {targetLabel}"
  },
  "同じ{targetLabel}「{name}」が既にあります。\n\nアップロード自体をやめる場合は「キャンセル」、別名で新規アップロードする場合は「新規にアップロード」、今の同じ{targetLabel}と置き換える場合は「置き換える」を選んでください。": {
    "en": "A {targetLabel} named \"{name}\" already exists.\n\nChoose \"Cancel\" to stop the upload, \"Upload as new\" to upload it with a different name, or \"Replace\" to replace the existing {targetLabel}.",
    "es": "Ya existe un elemento de tipo {targetLabel} llamado \"{name}\".\n\nElige \"Cancelar\" para detener la subida, \"Subir como nuevo\" para subirlo con otro nombre, o \"Reemplazar\" para reemplazar el {targetLabel} existente."
  },
  "新規にアップロード": {
    "en": "Upload as new",
    "es": "Subir como nuevo"
  },
  "置き換える": {
    "en": "Replace",
    "es": "Reemplazar"
  },
  "ホーム": {
    "en": "Home",
    "es": "Inicio"
  },
  "クラウド上のファイル・フォルダを管理します。": {
    "en": "Manage files and folders in the cloud.",
    "es": "Gestiona archivos y carpetas en la nube."
  },
  "他のユーザーから共有されたファイル・フォルダを表示します。": {
    "en": "View files and folders shared by other users.",
    "es": "Consulta archivos y carpetas compartidos por otros usuarios."
  },
  "このPCから自動バックアップするファイル・フォルダを設定、管理します。バックアップを解除する際は、そのままごみ箱へ移動させて下さい。": {
    "en": "Set and manage files and folders automatically backed up from this PC. To remove a backup setting, move it to the trash.",
    "es": "Configura y gestiona los archivos y carpetas que se respaldan automáticamente desde este PC. Para quitar una copia de seguridad, muévela a la papelera."
  },
  "このPCの空き容量を他ユーザーへ提供し、報酬を受け取る設定を行います。": {
    "en": "Offer unused space on this PC to other users and configure rewards.",
    "es": "Ofrece espacio libre de este PC a otros usuarios y configura las recompensas."
  },
  "復元しなかったファイル・フォルダは30 日後には完全に削除されます。": {
    "en": "Files and folders that are not restored will be permanently deleted after 30 days.",
    "es": "Los archivos y carpetas que no se restauren se eliminarán permanentemente después de 30 días."
  },
  "すべて削除": {
    "en": "Delete all",
    "es": "Eliminar todo"
  },
  "{count}件を選択中": {
    "en": "{count} selected",
    "es": "{count} seleccionados"
  },
  "合計 {size}": {
    "en": "Total {size}",
    "es": "Total {size}"
  },
  "復元": {
    "en": "Restore",
    "es": "Restaurar"
  },
  "完全削除": {
    "en": "Delete permanently",
    "es": "Eliminar permanentemente"
  },
  "ダウンロード": {
    "en": "Download",
    "es": "Descargar"
  },
  "共有": {
    "en": "Share",
    "es": "Compartir"
  },
  "コピー": {
    "en": "Copy",
    "es": "Copiar"
  },
  "移動": {
    "en": "Move",
    "es": "Mover"
  },
  "削除": {
    "en": "Delete",
    "es": "Eliminar"
  },
  "選択解除": {
    "en": "Clear selection",
    "es": "Borrar selección"
  },
  "言語または地域": {
    "en": "Language or region",
    "es": "Idioma o región"
  },
  "保存・共有・提供": {
    "en": "Store, Share, Provide",
    "es": "Guarda, Comparte, Ofrecer"
  },
  "あなたのストレージをもっと自由にするクラウド。": {
    "en": "A cloud that gives your storage more freedom.",
    "es": "Una nube que da más libertad a tu almacenamiento."
  },
  "ファイルの保存、検索、共有、同期、ストレージ提供まで。": {
    "en": "Store, search, share, sync, and contribute storage.",
    "es": "Guarda, busca, comparte, sincroniza y aporta almacenamiento."
  },
  "日常的に使う操作を、ひとつの画面にまとめました。": {
    "en": "Everyday actions are gathered into one screen.",
    "es": "Las acciones cotidianas se reúnen en una sola pantalla."
  },
  "ログイン": {
    "en": "Log in",
    "es": "Iniciar sesión"
  },
  "登録": {
    "en": "Log in",
    "es": "Iniciar sesión"
  },
  "利用規約・プライバシーポリシーへの同意": {
    "en": "Agree to the Terms and Privacy Policy",
    "es": "Aceptar los Términos y la Política de privacidad"
  },
  "新規登録": {
    "en": "Create account",
    "es": "Crear cuenta"
  },
  "メールアドレスとパスワードでログインできます。": {
    "en": "Log in with your email address and password.",
    "es": "Inicia sesión con tu correo electrónico y contraseña."
  },
  "登録前に内容を確認し、同意した場合のみ次へ進めます。": {
    "en": "Review the details before creating an account. You can continue only after agreeing.",
    "es": "Revisa el contenido antes de crear la cuenta. Solo puedes continuar si aceptas."
  },
  "姓・名、メールアドレス、パスワード、登録地域を入力してください。": {
    "en": "Enter your name, email address, password, and country/region.",
    "es": "Introduce tu nombre, correo electrónico, contraseña y país/región."
  },
  "利用規約 / プライバシーポリシー": {
    "en": "Terms of Use / Privacy Policy",
    "es": "Términos de uso / Política de privacidad"
  },
  "上記の利用規約 {termsVersion} とプライバシーポリシー {privacyVersion} を確認し、同意します。": {
    "en": "I have reviewed and agree to the Terms of Use {termsVersion} and Privacy Policy {privacyVersion} above.",
    "es": "He revisado y acepto los Términos de uso {termsVersion} y la Política de privacidad {privacyVersion} anteriores."
  },
  "ログインへ戻る": {
    "en": "Back to log in",
    "es": "Volver al inicio de sesión"
  },
  "同意して続行": {
    "en": "Agree and continue",
    "es": "Aceptar y continuar"
  },
  "姓": {
    "en": "Last name",
    "es": "Apellido"
  },
  "名": {
    "en": "First name",
    "es": "Nombre"
  },
  "メールアドレス": {
    "en": "Email address",
    "es": "Correo electrónico"
  },
  "パスワード": {
    "en": "Password",
    "es": "Contraseña"
  },
  "アカウント作成": {
    "en": "Create account",
    "es": "Crear cuenta"
  },
  "同意画面へ戻る": {
    "en": "Back to agreement",
    "es": "Volver a la aceptación"
  },
  "新規登録へ切り替える": {
    "en": "Create a new account",
    "es": "Crear una cuenta nueva"
  },
  "新規またはアップロード": {
    "en": "New",
    "es": "Nuevo"
  },
  "新しいフォルダ": {
    "en": "New folder",
    "es": "Nueva carpeta"
  },
  "ファイルのアップロード": {
    "en": "Upload files",
    "es": "Subir archivos"
  },
  "フォルダのアップロード": {
    "en": "Upload folder",
    "es": "Subir carpeta"
  },
  "検索を実行": {
    "en": "Run search",
    "es": "Buscar"
  },
  "検索": {
    "en": "Search",
    "es": "Buscar"
  },
  "最近": {
    "en": "Recent",
    "es": "Recientes"
  },
  "最新": {
    "en": "Recent",
    "es": "Recientes"
  },
  "開始中...": {
    "en": "Starting...",
    "es": "Iniciando..."
  },
  "自動バックアップを開始": {
    "en": "Start auto backup",
    "es": "Iniciar copia automática"
  },
  "直近の報酬": {
    "en": "Recent rewards",
    "es": "Recompensas recientes"
  },
  "まだ報酬履歴がありません。ノードがオンラインで使われ始めるとここに月次記録が出ます。": {
    "en": "No reward history yet. Monthly records will appear here after your node is online and used.",
    "es": "Aún no hay historial de recompensas. Los registros mensuales aparecerán aquí cuando tu nodo esté en línea y empiece a usarse."
  },
  "ノード状態": {
    "en": "Node status",
    "es": "Estado del nodo"
  },
  "オンライン": {
    "en": "Online",
    "es": "En línea"
  },
  "オフライン": {
    "en": "Offline",
    "es": "Sin conexión"
  },
  "最終 heartbeat: {date}": {
    "en": "Last heartbeat: {date}",
    "es": "Último heartbeat: {date}"
  },
  "提供容量": {
    "en": "Offered capacity",
    "es": "Capacidad ofrecida"
  },
  "実行時容量: {value}": {
    "en": "Runtime capacity: {value}",
    "es": "Capacidad en ejecución: {value}"
  },
  "貸出し中": {
    "en": "In use",
    "es": "En préstamo"
  },
  "アップデート: {date}": {
    "en": "Updated: {date}",
    "es": "Actualizado: {date}"
  },
  "平均稼働率": {
    "en": "Average uptime",
    "es": "Tasa media de actividad"
  },
  "稼働率 {value}%": {
    "en": "Utilization {value}%",
    "es": "Utilización {value}%"
  },
  "直近の利用実績から見た平均値": {
    "en": "Average based on recent usage.",
    "es": "Promedio basado en el uso reciente."
  },
  "提供量の設定": {
    "en": "Contribution limit",
    "es": "Límite de aportación"
  },
  "このPCから提供するストレージ容量の上限を設定します。": {
    "en": "Set the maximum storage capacity this PC can contribute.",
    "es": "Configura la capacidad máxima de almacenamiento que este PC puede aportar."
  },
  "このパソコンが提供できる上限": {
    "en": "Maximum this PC can contribute",
    "es": "Máximo que este PC puede aportar"
  },
  "このPCの空き容量をもとに、安全に提供できる上限を表示しています。": {
    "en": "This shows the safe contribution limit based on free space on this PC.",
    "es": "Muestra el límite seguro de aportación según el espacio libre de este PC."
  },
  "細かく入力したい場合": {
    "en": "For precise input",
    "es": "Para introducir un valor preciso"
  },
  "もしストレージの提供をやめる場合は、0を入力して『設定を保存』をクリックしてください。": {
    "en": "To stop contributing storage, enter 0 and click 'Save settings'.",
    "es": "Para dejar de aportar almacenamiento, introduce 0 y haz clic en 'Guardar configuración'."
  },
  "ストレージを提供するパソコンを変更する場合も、0を入力して『設定を保存』をクリックして提供を停止してください。": {
    "en": "To switch the PC that contributes storage, enter 0 and click 'Save settings' to stop this PC first.",
    "es": "Para cambiar el PC que aporta almacenamiento, introduce 0 y haz clic en 'Guardar configuración' para detener primero este PC."
  },
  "保存中...": {
    "en": "Saving...",
    "es": "Guardando..."
  },
  "設定を保存": {
    "en": "Save settings",
    "es": "Guardar configuración"
  },
  "想定月次報酬": {
    "en": "Estimated monthly rewards",
    "es": "Recompensas mensuales estimadas"
  },
  "この見積りは、最近の利用率ごとの想定に沿って月次報酬を並べたものです。": {
    "en": "This estimate shows monthly rewards based on recent utilization assumptions.",
    "es": "Esta estimación muestra recompensas mensuales según supuestos recientes de utilización."
  },
  "報酬の受け取り設定": {
    "en": "Reward payout settings",
    "es": "Configuración de cobro de recompensas"
  },
  "口座連携: {value}": {
    "en": "Account connection: {value}",
    "es": "Conexión de cuenta: {value}"
  },
  "支払い有効: {value}": {
    "en": "Payouts enabled: {value}",
    "es": "Pagos habilitados: {value}"
  },
  "支払い一時停止: {value}": {
    "en": "Payouts paused: {value}",
    "es": "Pagos pausados: {value}"
  },
  "利用可能": {
    "en": "Available",
    "es": "Disponible"
  },
  "未設定": {
    "en": "Not configured",
    "es": "No configurado"
  },
  "連携済み": {
    "en": "Connected",
    "es": "Conectado"
  },
  "未連携": {
    "en": "Not connected",
    "es": "No conectado"
  },
  "有効": {
    "en": "Enabled",
    "es": "Activo"
  },
  "未有効": {
    "en": "Not enabled",
    "es": "No activo"
  },
  "あり": {
    "en": "Yes",
    "es": "Sí"
  },
  "なし": {
    "en": "No",
    "es": "No"
  },
  "起動中...": {
    "en": "Starting...",
    "es": "Iniciando..."
  },
  "ストレージの提供を開始する": {
    "en": "Start contributing storage",
    "es": "Empezar a aportar almacenamiento"
  },
  "名前": {
    "en": "Name",
    "es": "Nombre"
  },
  "バックアップ元": {
    "en": "Backup source",
    "es": "Origen de copia"
  },
  "サイズ": {
    "en": "Size",
    "es": "Tamaño"
  },
  "更新日時": {
    "en": "Updated",
    "es": "Actualizado"
  },
  "削除日時": {
    "en": "Deleted",
    "es": "Eliminado"
  },
  "読み込み中...": {
    "en": "Loading...",
    "es": "Cargando..."
  },
  "バックアップ設定に追加されたファイル・フォルダはまだありません。": {
    "en": "No files or folders have been added to backup settings yet.",
    "es": "Aún no se han agregado archivos o carpetas a la configuración de copia."
  },
  "該当するファイルがありません。": {
    "en": "No matching files.",
    "es": "No hay archivos coincidentes."
  },
  "{count}件の履歴": {
    "en": "{count} versions",
    "es": "{count} versiones"
  },
  "削除期限超過": {
    "en": "Deletion deadline passed",
    "es": "Plazo de eliminación vencido"
  },
  "あと{days}日で削除": {
    "en": "Deleted in {days} days",
    "es": "Se eliminará en {days} días"
  },
  "共有・コピー・移動・削除は自分の項目だけに使えます。": {
    "en": "Share, copy, move, and delete are available only for your own items.",
    "es": "Compartir, copiar, mover y eliminar solo están disponibles para tus propios elementos."
  },
  "開く": {
    "en": "Open",
    "es": "Abrir"
  },
  "オフライン利用": {
    "en": "Offline access",
    "es": "Uso sin conexión"
  },
  "オフライン利用を停止": {
    "en": "Stop offline access",
    "es": "Detener uso sin conexión"
  },
  "リンクをコピー": {
    "en": "Copy link",
    "es": "Copiar enlace"
  },
  "名前を変更": {
    "en": "Rename",
    "es": "Cambiar nombre"
  },
  "アクティビティ": {
    "en": "Activity",
    "es": "Actividad"
  },
  "ごみ箱へ移動": {
    "en": "Move to trash",
    "es": "Mover a la papelera"
  },
  "{count}件を復元": {
    "en": "Restore {count} items",
    "es": "Restaurar {count} elementos"
  },
  "{count}件を完全削除": {
    "en": "Delete {count} permanently",
    "es": "Eliminar permanentemente {count}"
  },
  "{count}件をダウンロード": {
    "en": "Download {count} items",
    "es": "Descargar {count} elementos"
  },
  "{count}件を共有": {
    "en": "Share {count} items",
    "es": "Compartir {count} elementos"
  },
  "{count}件分のリンクをコピー": {
    "en": "Copy links for {count} items",
    "es": "Copiar enlaces de {count} elementos"
  },
  "{count}件をコピー": {
    "en": "Copy {count} items",
    "es": "Copiar {count} elementos"
  },
  "{count}件を移動": {
    "en": "Move {count} items",
    "es": "Mover {count} elementos"
  },
  "{count}件をごみ箱へ移動": {
    "en": "Move {count} items to trash",
    "es": "Mover {count} elementos a la papelera"
  },
  "キャンセル": {
    "en": "Cancel",
    "es": "Cancelar"
  },
  "追加": {
    "en": "Add",
    "es": "Agregar"
  },
  "送信中...": {
    "en": "Sending...",
    "es": "Enviando..."
  },
  "送信": {
    "en": "Send",
    "es": "Enviar"
  },
  "{name} を共有": {
    "en": "Share {name}",
    "es": "Compartir {name}"
  },
  "{count}件を共有タイトル": {
    "en": "Share {count} items",
    "es": "Compartir {count} elementos"
  },
  "対象: {names}": {
    "en": "Target: {names}",
    "es": "Destino: {names}"
  },
  "メールアドレスでユーザーを追加": {
    "en": "Add users by email address",
    "es": "Agrega usuarios por correo electrónico"
  },
  "メッセージを追加": {
    "en": "Add a message",
    "es": "Agregar un mensaje"
  },
  "移動先を選択": {
    "en": "Choose destination",
    "es": "Elegir destino"
  },
  "選択中の{count}件の移動先フォルダを選択してください。": {
    "en": "Choose the destination folder for the {count} selected items.",
    "es": "Elige la carpeta de destino para los {count} elementos seleccionados."
  },
  "「{name}」の移動先フォルダを選択してください。": {
    "en": "Choose the destination folder for \"{name}\".",
    "es": "Elige la carpeta de destino para \"{name}\"."
  },
  "移動先フォルダ": {
    "en": "Destination folder",
    "es": "Carpeta de destino"
  },
  "移動中...": {
    "en": "Moving...",
    "es": "Moviendo..."
  },
  "版履歴": {
    "en": "Version history",
    "es": "Historial de versiones"
  },
  "{name} の過去版と現在版を一覧表示する。": {
    "en": "View previous and current versions of {name}.",
    "es": "Ver versiones anteriores y actuales de {name}."
  },
  "現在の版": {
    "en": "Current version",
    "es": "Versión actual"
  },
  "版 {number}": {
    "en": "Version {number}",
    "es": "Versión {number}"
  },
  "作成日時: {date} ・ サイズ: {size} ・ {part}": {
    "en": "Created: {date} ・ Size: {size} ・ {part}",
    "es": "Creado: {date} ・ Tamaño: {size} ・ {part}"
  },
  "この版に戻す": {
    "en": "Restore this version",
    "es": "Restaurar esta versión"
  },
  "履歴はまだありません。同名アップロードや同期クライアントからの更新があると、ここに版が追加されます。": {
    "en": "No history yet. Versions will appear here when files are updated by uploads with the same name or sync clients.",
    "es": "Aún no hay historial. Las versiones aparecerán aquí cuando haya actualizaciones por subidas con el mismo nombre o clientes de sincronización."
  },
  "閉じる": {
    "en": "Close",
    "es": "Cerrar"
  },
  "設定": {
    "en": "Settings",
    "es": "Configuración"
  },
  "テスト版": {
    "en": "Test version",
    "es": "Versión de prueba"
  },
  "ログアウト": {
    "en": "Log out",
    "es": "Cerrar sesión"
  },
  "アカウント": {
    "en": "Account",
    "es": "Cuenta"
  },
  "プロフィール画像を変更": {
    "en": "Change profile image",
    "es": "Cambiar imagen de perfil"
  },
  "国/地域を選択": {
    "en": "Choose country/region",
    "es": "Elegir país/región"
  },
  "言語を選択": {
    "en": "Choose language",
    "es": "Elegir idioma"
  },
  "言語": {
    "en": "Language",
    "es": "Idioma"
  },
  "国/地域": {
    "en": "Country/region",
    "es": "País/región"
  },
  "作成": {
    "en": "Create",
    "es": "Crear"
  },
  "変更": {
    "en": "Change",
    "es": "Cambiar"
  },
  "OK": {
    "en": "OK",
    "es": "Aceptar"
  }
,
  "新しいフォルダ名を入力してください": {
    "en": "Enter a new folder name.",
    "es": "Introduce el nombre de la nueva carpeta."
  },
  "フォルダ名": {
    "en": "Folder name",
    "es": "Nombre de carpeta"
  },
  "フォルダ作成に失敗しました。": {
    "en": "Failed to create the folder.",
    "es": "No se pudo crear la carpeta."
  },
  "お知らせ": {
    "en": "Notice",
    "es": "Aviso"
  },
  "削除の確認": {
    "en": "Confirm move to trash",
    "es": "Confirmar mover a la papelera"
  },
  "完全削除の確認": {
    "en": "Confirm permanent deletion",
    "es": "Confirmar eliminación permanente"
  },
  "すべて削除の確認": {
    "en": "Confirm delete all",
    "es": "Confirmar eliminar todo"
  },
  "{count}件をごみ箱へ移動しますか？": {
    "en": "Move {count} items to trash?",
    "es": "¿Mover {count} elementos a la papelera?"
  },
  "「{name}」をごみ箱へ移動しますか？": {
    "en": "Move \"{name}\" to trash?",
    "es": "¿Mover \"{name}\" a la papelera?"
  },
  "{count}件を完全削除しますか？この操作は元に戻せません。": {
    "en": "Permanently delete {count} items? This action can't be undone.",
    "es": "¿Eliminar permanentemente {count} elementos? Esta acción no se puede deshacer."
  },
  "「{name}」を完全削除しますか？この操作は元に戻せません。": {
    "en": "Permanently delete \"{name}\"? This action can't be undone.",
    "es": "¿Eliminar permanentemente \"{name}\"? Esta acción no se puede deshacer."
  },
  "本当にごみ箱内のファイルやフォルダを全て削除しますか？この操作は元に戻せません。": {
    "en": "Permanently delete all files and folders in Trash? This action can't be undone.",
    "es": "¿Eliminar permanentemente todos los archivos y carpetas de la Papelera? Esta acción no se puede deshacer."
  },
  "ごみ箱は空です。": {
    "en": "Trash is empty.",
    "es": "La papelera está vacía."
  },
  "新しい名前を入力してください": {
    "en": "Enter a new name.",
    "es": "Introduce un nuevo nombre."
  },
  "新しい名前": {
    "en": "New name",
    "es": "Nuevo nombre"
  },
  "リンクをコピーしました: {shareId}": {
    "en": "Link copied: {shareId}",
    "es": "Enlace copiado: {shareId}"
  },
  "{count}件分のリンクを改行区切りでコピーしました。": {
    "en": "Copied links for {count} items, separated by line breaks.",
    "es": "Se copiaron los enlaces de {count} elementos, separados por saltos de línea."
  },
  "停止して削除": {
    "en": "Stop and delete",
    "es": "Detener y eliminar"
  },
  "「{name}」の監視を停止し、ローカルに保存されたオフライン利用用コピーを削除します。クラウド上のファイル・フォルダは削除されません。": {
    "en": "Stop monitoring \"{name}\" and delete the local offline copy. Files and folders in the cloud will not be deleted.",
    "es": "Detén la supervisión de \"{name}\" y elimina la copia local sin conexión. Los archivos y carpetas en la nube no se eliminarán."
  },
  "「{name}」のオフライン利用を停止しました。\nローカルコピーは削除されました。": {
    "en": "Stopped offline access for \"{name}\".\nThe local copy was deleted.",
    "es": "Se detuvo el uso sin conexión de \"{name}\".\nLa copia local se eliminó."
  },
  "「{name}」をオフライン利用に追加しました。\n種類: {type}\n保存先: {path}{savedFileLine}": {
    "en": "Added \"{name}\" to offline access.\nType: {type}\nSaved to: {path}{savedFileLine}",
    "es": "Se agregó \"{name}\" al uso sin conexión.\nTipo: {type}\nGuardado en: {path}{savedFileLine}"
  },
  "保存ファイル数: {count}件": {
    "en": "Saved files: {count}",
    "es": "Archivos guardados: {count}"
  },
  "このアイテムは現在オフライン利用中ではありません。": {
    "en": "This item is not currently available offline.",
    "es": "Este elemento no está disponible sin conexión actualmente."
  },
  "デスクトップアプリ側のオフライン利用停止ブリッジが未接続です。": {
    "en": "The desktop app bridge for stopping offline access is not connected.",
    "es": "El puente de la app de escritorio para detener el uso sin conexión no está conectado."
  },
  "デスクトップアプリ側のオフライン利用ブリッジが未接続です。": {
    "en": "The desktop app bridge for offline access is not connected.",
    "es": "El puente de la app de escritorio para uso sin conexión no está conectado."
  },
  "オフライン利用は自分のファイル・フォルダでのみ利用できます。": {
    "en": "Offline access is available only for your own files and folders.",
    "es": "El uso sin conexión solo está disponible para tus propios archivos y carpetas."
  },
  "オフライン利用の停止に失敗しました。": {
    "en": "Failed to stop offline access.",
    "es": "No se pudo detener el uso sin conexión."
  },
  "オフライン利用ファイルの保存に失敗しました。": {
    "en": "Failed to save the offline access file.",
    "es": "No se pudo guardar el archivo para uso sin conexión."
  },
  "オフライン利用の設定に失敗しました。": {
    "en": "Failed to set up offline access.",
    "es": "No se pudo configurar el uso sin conexión."
  },
  "リンクのコピーに失敗しました。": {
    "en": "Failed to copy the link.",
    "es": "No se pudo copiar el enlace."
  },
  "名前変更に失敗しました。": {
    "en": "Failed to rename the item.",
    "es": "No se pudo cambiar el nombre del elemento."
  },
  "ごみ箱移動に失敗しました。": {
    "en": "Failed to move the item to trash.",
    "es": "No se pudo mover el elemento a la papelera."
  },
  "完全削除に失敗しました。": {
    "en": "Failed to delete permanently.",
    "es": "No se pudo eliminar permanentemente."
  },
  "ごみ箱内のすべて削除に失敗しました。": {
    "en": "Failed to delete all items in Trash.",
    "es": "No se pudieron eliminar todos los elementos de la Papelera."
  }

,
  "クラウド上のパスを特定できないため、オフライン利用にできませんでした。": {
    "en": "Offline access could not be enabled because the cloud path could not be identified.",
    "es": "No se pudo activar el uso sin conexión porque no se pudo identificar la ruta en la nube."
  },
  "オフライン利用を開始します": {
    "en": "Starting offline access",
    "es": "Iniciando uso sin conexión"
  },
  "移動に失敗しました。": {
    "en": "Failed to move the item.",
    "es": "No se pudo mover el elemento."
  },
  "復元に失敗しました。": {
    "en": "Failed to restore the item.",
    "es": "No se pudo restaurar el elemento."
  }

,
  "先にバックアップ対象のファイルまたはフォルダをアップロードしてください。": {
    "en": "Upload a file or folder to back up first.",
    "es": "Primero sube un archivo o una carpeta para respaldar."
  },
  "自動バックアップを開始します": {
    "en": "Starting auto backup",
    "es": "Iniciando copia automática"
  },
  "バックアップ設定を保存し、自動バックアップを開始しました。": {
    "en": "Saved the backup settings and started auto backup.",
    "es": "Se guardó la configuración de copia de seguridad y se inició la copia automática."
  },
  "バックアップ設定の保存に失敗しました。": {
    "en": "Failed to save the backup settings.",
    "es": "No se pudo guardar la configuración de copia de seguridad."
  },
  "デスクトップアプリ側のバックアップブリッジが未接続です。": {
    "en": "The desktop app backup bridge is not connected.",
    "es": "El puente de copia de seguridad de la app de escritorio no está conectado."
  },
  "バックアップ開始にはログイン用メールアドレスが必要です。再ログイン後にお試しください。": {
    "en": "An email address for login is required to start backup. Please log in again and try again.",
    "es": "Se necesita el correo electrónico de inicio de sesión para iniciar la copia de seguridad. Vuelve a iniciar sesión e inténtalo de nuevo."
  },
  "監視対象がありません。": {
    "en": "There are no items to monitor.",
    "es": "No hay elementos para supervisar."
  },
  "バックアップ対象の更新に失敗しました。": {
    "en": "Failed to update the backup targets.",
    "es": "No se pudieron actualizar los elementos de copia de seguridad."
  },
  "バックアップ開始に失敗しました。": {
    "en": "Failed to start backup.",
    "es": "No se pudo iniciar la copia de seguridad."
  },
  "自動バックアップの停止に失敗しました。": {
    "en": "Failed to stop auto backup.",
    "es": "No se pudo detener la copia automática."
  },
  "Control API に接続できません": {
    "en": "Cannot connect to the Control API",
    "es": "No se puede conectar con la API de control"
  },
  "バックエンドが起動しているか確認してください。": {
    "en": "Check that the backend is running.",
    "es": "Comprueba que el backend esté en ejecución."
  },
  "エラーが発生しました": {
    "en": "An error occurred",
    "es": "Se produjo un error"
  },
  "詳細": {
    "en": "Details",
    "es": "Detalles"
  },
  "再読み込み": {
    "en": "Reload",
    "es": "Recargar"
  },
  "広告掲載予定エリア": {
    "en": "Planned ad placement",
    "es": "Espacio previsto para anuncios"
  },
  "無料プランでは、この場所に広告が表示される予定です。": {
    "en": "In the free plan, ads are planned to appear here.",
    "es": "En el plan gratuito, está previsto que los anuncios aparezcan aquí."
  }

};

const COUNTRY_LABELS_BY_LANGUAGE: Record<string, Record<UILanguageCode, string>> = {
  "JP": {
    "ja": "日本",
    "en": "Japan",
    "es": "Japón"
  },
  "US": {
    "ja": "アメリカ合衆国",
    "en": "United States",
    "es": "Estados Unidos"
  },
  "CA": {
    "ja": "カナダ",
    "en": "Canada",
    "es": "Canadá"
  },
  "GB": {
    "ja": "イギリス",
    "en": "United Kingdom",
    "es": "Reino Unido"
  },
  "DE": {
    "ja": "ドイツ",
    "en": "Germany",
    "es": "Alemania"
  },
  "FR": {
    "ja": "フランス",
    "en": "France",
    "es": "Francia"
  },
  "IT": {
    "ja": "イタリア",
    "en": "Italy",
    "es": "Italia"
  },
  "ES": {
    "ja": "スペイン",
    "en": "Spain",
    "es": "España"
  },
  "NL": {
    "ja": "オランダ",
    "en": "Netherlands",
    "es": "Países Bajos"
  },
  "BE": {
    "ja": "ベルギー",
    "en": "Belgium",
    "es": "Bélgica"
  },
  "CH": {
    "ja": "スイス",
    "en": "Switzerland",
    "es": "Suiza"
  },
  "SE": {
    "ja": "スウェーデン",
    "en": "Sweden",
    "es": "Suecia"
  },
  "NO": {
    "ja": "ノルウェー",
    "en": "Norway",
    "es": "Noruega"
  },
  "DK": {
    "ja": "デンマーク",
    "en": "Denmark",
    "es": "Dinamarca"
  },
  "FI": {
    "ja": "フィンランド",
    "en": "Finland",
    "es": "Finlandia"
  },
  "IE": {
    "ja": "アイルランド",
    "en": "Ireland",
    "es": "Irlanda"
  },
  "PT": {
    "ja": "ポルトガル",
    "en": "Portugal",
    "es": "Portugal"
  },
  "AT": {
    "ja": "オーストリア",
    "en": "Austria",
    "es": "Austria"
  },
  "PL": {
    "ja": "ポーランド",
    "en": "Poland",
    "es": "Polonia"
  },
  "CZ": {
    "ja": "チェコ",
    "en": "Czechia",
    "es": "Chequia"
  },
  "HU": {
    "ja": "ハンガリー",
    "en": "Hungary",
    "es": "Hungría"
  },
  "RO": {
    "ja": "ルーマニア",
    "en": "Romania",
    "es": "Rumanía"
  },
  "GR": {
    "ja": "ギリシャ",
    "en": "Greece",
    "es": "Grecia"
  },
  "TR": {
    "ja": "トルコ",
    "en": "Turkey",
    "es": "Turquía"
  },
  "UA": {
    "ja": "ウクライナ",
    "en": "Ukraine",
    "es": "Ucrania"
  },
  "RU": {
    "ja": "ロシア",
    "en": "Russia",
    "es": "Rusia"
  },
  "AU": {
    "ja": "オーストラリア",
    "en": "Australia",
    "es": "Australia"
  },
  "NZ": {
    "ja": "ニュージーランド",
    "en": "New Zealand",
    "es": "Nueva Zelanda"
  },
  "SG": {
    "ja": "シンガポール",
    "en": "Singapore",
    "es": "Singapur"
  },
  "KR": {
    "ja": "韓国",
    "en": "South Korea",
    "es": "Corea del Sur"
  },
  "TW": {
    "ja": "台湾",
    "en": "Taiwan",
    "es": "Taiwán"
  },
  "HK": {
    "ja": "香港",
    "en": "Hong Kong",
    "es": "Hong Kong"
  },
  "CN": {
    "ja": "中国",
    "en": "China",
    "es": "China"
  },
  "IN": {
    "ja": "インド",
    "en": "India",
    "es": "India"
  },
  "TH": {
    "ja": "タイ",
    "en": "Thailand",
    "es": "Tailandia"
  },
  "VN": {
    "ja": "ベトナム",
    "en": "Vietnam",
    "es": "Vietnam"
  },
  "MY": {
    "ja": "マレーシア",
    "en": "Malaysia",
    "es": "Malasia"
  },
  "ID": {
    "ja": "インドネシア",
    "en": "Indonesia",
    "es": "Indonesia"
  },
  "PH": {
    "ja": "フィリピン",
    "en": "Philippines",
    "es": "Filipinas"
  },
  "AE": {
    "ja": "アラブ首長国連邦",
    "en": "United Arab Emirates",
    "es": "Emiratos Árabes Unidos"
  },
  "SA": {
    "ja": "サウジアラビア",
    "en": "Saudi Arabia",
    "es": "Arabia Saudita"
  },
  "IL": {
    "ja": "イスラエル",
    "en": "Israel",
    "es": "Israel"
  },
  "ZA": {
    "ja": "南アフリカ",
    "en": "South Africa",
    "es": "Sudáfrica"
  },
  "EG": {
    "ja": "エジプト",
    "en": "Egypt",
    "es": "Egipto"
  },
  "BR": {
    "ja": "ブラジル",
    "en": "Brazil",
    "es": "Brasil"
  },
  "MX": {
    "ja": "メキシコ",
    "en": "Mexico",
    "es": "México"
  },
  "AR": {
    "ja": "アルゼンチン",
    "en": "Argentina",
    "es": "Argentina"
  },
  "CL": {
    "ja": "チリ",
    "en": "Chile",
    "es": "Chile"
  },
  "CO": {
    "ja": "コロンビア",
    "en": "Colombia",
    "es": "Colombia"
  }
};

const LANGUAGE_LABELS_BY_LANGUAGE: Record<string, Record<UILanguageCode, string>> = {
  ja: {
    ja: "日本語",
    en: "日本語",
    es: "日本語",
  },
  en: {
    ja: "English",
    en: "English",
    es: "English",
  },
  es: {
    ja: "Español",
    en: "Español",
    es: "Español",
  },
};

const TERMS_AND_PRIVACY_TEXT_BY_LANGUAGE: Record<UILanguageCode, string> = {
  ja: TERMS_AND_PRIVACY_TEXT,
  en: "Terms of Use (excerpt)\n\n1. This service includes file storage, synchronization, sharing, and storage contribution features.\n2. Users are responsible for the data they upload, store, and share.\n3. Illegal data, infringing data, or data that interferes with service operations is prohibited.\n4. Usage information may be recorded as necessary for service quality, security, billing, and reward processing.\n5. Storage contributors may be asked to connect an external payment service to receive rewards.\n6. Country/region information is used for country-based node selection and fraud prevention.\n7. These terms and policies may be updated as the service improves.\n\nPrivacy Policy (excerpt)\n\n1. The main information collected includes name, email address, country/region, authentication information, and service usage history.\n2. Collected information is used for identity verification, authentication, support, billing, reward payments, troubleshooting, and security.\n3. File data and encryption keys are handled appropriately according to the service design.\n4. Personal information is not improperly provided to third parties except where required by law or necessary for operations such as payments and authentication.\n5. Users may request confirmation or updates to their registered information within the scope permitted by law and the terms.\n\nPlease review the above and continue registration only if you agree to the Terms of Use ${TERMS_VERSION} and Privacy Policy ${PRIVACY_POLICY_VERSION}.",
  es: "Términos de uso (extracto)\n\n1. Este servicio incluye almacenamiento, sincronización, uso compartido de archivos y funciones para aportar almacenamiento.\n2. Los usuarios son responsables de los datos que suben, almacenan y comparten.\n3. Está prohibido almacenar datos ilegales, datos que infrinjan derechos o datos que interfieran con el funcionamiento del servicio.\n4. La información de uso puede registrarse según sea necesario para calidad del servicio, seguridad, facturación y procesamiento de recompensas.\n5. A quienes aporten almacenamiento se les puede solicitar conectar un servicio de pago externo para recibir recompensas.\n6. La información de país/región se usa para la selección de nodos por país y la prevención de fraude.\n7. Estos términos y políticas pueden actualizarse a medida que el servicio mejore.\n\nPolítica de privacidad (extracto)\n\n1. La información principal recopilada incluye nombre, correo electrónico, país/región, datos de autenticación e historial de uso del servicio.\n2. La información recopilada se utiliza para verificación de identidad, autenticación, soporte, facturación, pagos de recompensas, investigación de fallos y seguridad.\n3. Los archivos y las claves de cifrado se gestionan adecuadamente según el diseño del servicio.\n4. La información personal no se proporciona indebidamente a terceros, salvo cuando lo exija la ley o sea necesario para operaciones como pagos y autenticación.\n5. Los usuarios pueden solicitar la confirmación o actualización de su información registrada dentro de lo permitido por la ley y los términos.\n\nRevisa lo anterior y continúa el registro solo si aceptas los Términos de uso ${TERMS_VERSION} y la Política de privacidad ${PRIVACY_POLICY_VERSION}.",
};

function normalizeUiLanguageCode(value?: string | null): UILanguageCode {
  return value === "en" || value === "es" ? value : "ja";
}

function translateUiText(languageCode: string, jaText: string, params: Record<string, string | number> = {}) {
  const language = normalizeUiLanguageCode(languageCode);
  const template = language === "ja" ? jaText : UI_TRANSLATIONS[jaText]?.[language] || jaText;
  return template.replace(/\{(\w+)\}/g, (_, key) => String(params[key] ?? `\{${key}\}`));
}

function getCountryLabelByLanguage(code: string, languageCode: string) {
  const safeCode = String(code || "").toUpperCase();
  const language = normalizeUiLanguageCode(languageCode);
  const fallback = COUNTRY_OPTIONS.find((entry) => entry.code === safeCode)?.label || safeCode;
  return COUNTRY_LABELS_BY_LANGUAGE[safeCode]?.[language] || fallback;
}

function getLanguageLabelByLanguage(code: string, languageCode: string) {
  const safeCode = String(code || "").trim().toLowerCase();
  const language = normalizeUiLanguageCode(languageCode);
  const fallback = LANGUAGE_OPTIONS.find((entry) => entry.code === safeCode)?.label || safeCode;
  return LANGUAGE_LABELS_BY_LANGUAGE[safeCode]?.[language] || fallback;
}

function getTermsAndPrivacyText(languageCode: string) {
  return TERMS_AND_PRIVACY_TEXT_BY_LANGUAGE[normalizeUiLanguageCode(languageCode)];
}

function formatBytes(input?: number | null): string {
  const bytes = Number(input ?? 0);
  if (!bytes) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`;
}

function formatDate(ts?: number | null): string {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("ja-JP", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatYen(value?: number | null): string {
  const safe = Number(value ?? 0);
  return new Intl.NumberFormat("ja-JP", { style: "currency", currency: "JPY", maximumFractionDigits: 0 }).format(safe);
}

function formatGb(value?: number | null): string {
  const gb = Number(value ?? 0) / (1024 ** 3);
  if (!Number.isFinite(gb)) return "—";
  if (gb === 0) return "0 GB";
  return `${gb >= 100 ? gb.toFixed(0) : gb.toFixed(1)} GB`;
}

function formatPercent(value?: number | null): string {
  const safe = Number(value);
  if (!Number.isFinite(safe)) return "—";
  return `${Math.round(safe * 100)}%`;
}

function getAverageUtilizationRatio(summary?: NodeProviderSummary | null): number | null {
  if (!summary) return null;
  const summaryRatio = Number(summary.earnings_summary?.avg_utilization_ratio);
  if (Number.isFinite(summaryRatio)) return Math.max(0, Math.min(1, summaryRatio));

  const desiredCapacityGb = Number(summary.profile?.desired_capacity_bytes || 0) / (1024 ** 3);
  const earnings = summary.recent_earnings || [];
  if (desiredCapacityGb > 0 && earnings.length) {
    const ratios = earnings.flatMap((earning) => {
      const gbMonth = Number(earning.gb_month || 0);
      const start = Number(earning.period_start || 0);
      const end = Number(earning.period_end || 0);
      const periodDays = start > 0 && end > start ? Math.max(1, (end - start) / 86400) : 30;
      const periodMonths = periodDays / 30;
      if (periodMonths <= 0) return [];
      return [Math.max(0, Math.min(1, gbMonth / (desiredCapacityGb * periodMonths)))];
    });
    if (ratios.length) return ratios.reduce((sum, value) => sum + value, 0) / ratios.length;
  }

  const capacityBytes = Number(summary.runtime?.capacity_bytes || 0);
  const reservedBytes = Number(summary.runtime?.reserved_bytes || 0);
  if (capacityBytes > 0) return Math.max(0, Math.min(1, reservedBytes / capacityBytes));
  return null;
}

function getOfferableCapacityGb(summary?: NodeProviderSummary | null): number | null {
  if (!summary) return null;
  const explicit = Number(summary.local_capacity?.offerable_gb);
  if (Number.isFinite(explicit)) return explicit;
  const fallback = Number(summary.defaults?.suggested_slider_max_gb);
  return Number.isFinite(fallback) ? fallback : null;
}


function getDesktopBridgeForCapacity() {
  if (typeof window === "undefined") return null;
  return (
    (window as any).phase1Desktop ||
    (window as any).electronAPI ||
    (window as any).__PHASE1_DESKTOP__ ||
    null
  );
}

async function loadDesktopLocalCapacity(): Promise<LocalCapacityHint | null> {
  const bridge = getDesktopBridgeForCapacity();
  try {
    if (bridge?.getLocalCapacity) {
      return await bridge.getLocalCapacity({});
    }
    if (bridge?.invoke) {
      return await bridge.invoke("phase1-node:local-capacity", {});
    }
  } catch (err) {
    console.warn("[Tricloud] local capacity bridge failed", err);
  }
  return null;
}

function applyDesktopLocalCapacity(
  summary: NodeProviderSummary,
  localCapacity: LocalCapacityHint | null
): NodeProviderSummary {
  const offerableGb = Number(localCapacity?.offerable_gb);
  if (!localCapacity || !Number.isFinite(offerableGb)) {
    return summary;
  }

  const safeOfferableGb = Math.max(0, Math.floor(offerableGb));
  const mergedLocalCapacity = {
    ...summary.local_capacity,
    ...localCapacity,
    offerable_gb: safeOfferableGb,
    source: localCapacity.source || "electron_local_disk_90pct",
  };

  return {
    ...summary,
    local_capacity: mergedLocalCapacity,
    defaults: {
      ...summary.defaults,
      suggested_slider_max_gb: safeOfferableGb,
    },
  };
}

function getLocalOfferableGb(localCapacity: LocalCapacityHint | null): number | null {
  const offerableGb = Number(localCapacity?.offerable_gb);
  return Number.isFinite(offerableGb) ? Math.max(0, Math.floor(offerableGb)) : null;
}


function getDesktopNodeServerEndpoint(launchServer?: string | null): string {
  const raw = String(launchServer || "").trim();
  const lower = raw.toLowerCase();
  const isLoopback =
    !raw ||
    lower === "tcp://127.0.0.1:9999" ||
    lower === "tcp://localhost:9999" ||
    lower === "tcp://0.0.0.0:9999" ||
    lower === "tcp://*:9999" ||
    lower.includes("127.0.0.1") ||
    lower.includes("localhost") ||
    lower.includes("0.0.0.0") ||
    lower.includes("*:9999");

  if (!isLoopback) return raw.replace(/\/$/, "");

  try {
    const apiUrl = new URL(API_BASE);
    return `tcp://${apiUrl.hostname}:9999`;
  } catch {
    return "tcp://api.trytricloud.com:9999";
  }
}

function getAverageOnlineRatio(summary?: NodeProviderSummary | null): number | null {
  if (!summary) return null;
  const explicit = Number(summary.uptime_summary?.online_ratio);
  if (Number.isFinite(explicit)) return Math.max(0, Math.min(1, explicit));
  if (summary.runtime?.online === true) return 1;
  if (summary.runtime?.online === false) return 0;
  return null;
}

function getItemIcon(item: Item) {
  if (item.type === "folder") return <Folder className="h-4 w-4 text-sky-500" />;
  const ext = item.name.split(".").pop()?.toLowerCase() || "";
  if (["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(ext)) return <FileImage className="h-4 w-4 text-emerald-500" />;
  if (["mp4", "mov", "avi", "mkv", "webm"].includes(ext)) return <FileVideo className="h-4 w-4 text-violet-500" />;
  if (["zip", "rar", "7z", "tar", "gz"].includes(ext)) return <FileArchive className="h-4 w-4 text-amber-500" />;
  if (["csv", "xlsx", "xls"].includes(ext)) return <FileSpreadsheet className="h-4 w-4 text-green-600" />;
  return <FileText className="h-4 w-4 text-slate-500" />;
}

async function api<T>(path: string, options: RequestInit = {}, token?: string): Promise<T> {
  const headers = new Headers(options.headers || {});
  if (!(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      detail = data?.detail || JSON.stringify(data);
    } catch {
      // noop
    }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function safeUploadFileName(file: File): string {
  const rawName = String(file?.name || "upload.bin").replace(/\\/g, "/").replace(/^\/+/, "").replace(/\/+$/, "");
  return rawName.split("/").filter(Boolean).pop() || "upload.bin";
}

async function uploadViaExistingClientApi(
  file: File,
  token: string,
  parentId: string | null,
  targetItemId?: string | null,
  options: { uploadContext?: "normal" | "backup"; replaceExisting?: boolean } = {},
) {
  const fd = new FormData();
  const uploadFileName = safeUploadFileName(file);
  fd.append("file", file, uploadFileName);
  const uploadContext = options.uploadContext || "normal";
  const shouldReplace = options.replaceExisting !== undefined ? options.replaceExisting : uploadContext !== "backup" || Boolean(targetItemId);
  fd.append("replace_existing", shouldReplace ? "true" : "false");
  if (uploadContext) fd.append("upload_context", uploadContext);
  if (parentId) fd.append("parent_id", parentId);
  if (targetItemId) fd.append("target_item_id", targetItemId);
  return api<{ item_id: string; versioned?: boolean }>("/ui/upload", { method: "POST", body: fd }, token);
}

function HeaderButton(props: React.ButtonHTMLAttributes<HTMLButtonElement> & { active?: boolean }) {
  const { active, className = "", ...rest } = props;
  return <button {...rest} className={`inline-flex items-center gap-2 rounded-2xl px-4 py-2.5 text-sm ${active ? "bg-sky-50 text-sky-700 border border-sky-200" : "bg-white border border-slate-200 hover:bg-slate-50"} ${className}`} />;
}

function ShredderIcon({ className = "" }: { className?: string }) {
  return (
    <svg
      aria-hidden="true"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="2"
      viewBox="0 0 24 24"
    >
      <path d="M8 3h8l1 4H7l1-4Z" />
      <path d="M5 7h14" />
      <path d="M6 11h12v4H6z" />
      <path d="M8 15v6" />
      <path d="M12 15v6" />
      <path d="M16 15v6" />
      <path d="M10 18h4" />
    </svg>
  );
}

const ROOT_ID = "root";
const TRASH_RETENTION_DAYS = 30;
const TRASH_RETENTION_SECONDS = TRASH_RETENTION_DAYS * 24 * 60 * 60;
const ROW_SELECTION_BG = "#c2e7ff";
const ROW_SELECTION_ACCENT = "#0b57d0";
const CONTEXT_MENU_MARGIN = 12;
const CONTEXT_MENU_WIDTH = 260;
const CONTEXT_MENU_HEIGHT_TRASH = 120;
const CONTEXT_MENU_HEIGHT_MULTI = 300;
const CONTEXT_MENU_HEIGHT_FOLDER = 400;
const CONTEXT_MENU_HEIGHT_FILE = 460;
const DRAG_SELECT_AUTO_SCROLL_EDGE_PX = 96;
const DRAG_SELECT_AUTO_SCROLL_MAX_STEP_PX = 30;

function clampContextMenuPosition(x: number, y: number, estimatedHeight: number) {
  if (typeof window === "undefined") return { x, y };
  const safeWidth = Math.max(CONTEXT_MENU_WIDTH, 240);
  const safeHeight = Math.max(estimatedHeight, 120);
  const maxX = Math.max(CONTEXT_MENU_MARGIN, window.innerWidth - safeWidth - CONTEXT_MENU_MARGIN);
  const maxY = Math.max(CONTEXT_MENU_MARGIN, window.innerHeight - safeHeight - CONTEXT_MENU_MARGIN);
  return {
    x: Math.max(CONTEXT_MENU_MARGIN, Math.min(x, maxX)),
    y: Math.max(CONTEXT_MENU_MARGIN, Math.min(y - 12, maxY)),
  };
}

function getTrashExpiryMeta(trashedAt?: number | null) {
  if (!trashedAt) return null;
  const expiresAt = Number(trashedAt) + TRASH_RETENTION_SECONDS;
  const now = Math.floor(Date.now() / 1000);
  const remainingSeconds = expiresAt - now;
  const remainingDays = Math.ceil(remainingSeconds / (24 * 60 * 60));
  return {
    expiresAt,
    remainingDays,
    expired: remainingSeconds <= 0,
  };
}

function filePathParts(relativePath: string) {
  const normalized = relativePath.replace(/^\/+/, "").replace(/\\/g, "/");
  const parts = normalized.split("/").filter(Boolean);
  const fileName = parts.pop() || normalized;
  return { fileName, directoryPath: parts.join("/") };
}

function splitNameForCopy(name: string, isFolder: boolean): [string, string] {
  if (isFolder) return [name, ""];
  if (name.startsWith(".") && !name.slice(1).includes(".")) return [name, ""];
  const firstDot = name.indexOf(".");
  if (firstDot <= 0) return [name, ""];
  return [name.slice(0, firstDot), name.slice(firstDot)];
}

function dedupeCopyName(originalName: string, isFolder: boolean, exists: (candidate: string) => boolean): string {
  const [base, suffix] = splitNameForCopy(originalName, isFolder);
  let candidate = `${base} コピー${suffix}`;
  if (!exists(candidate)) return candidate;
  let idx = 2;
  while (true) {
    candidate = `${base} コピー (${idx})${suffix}`;
    if (!exists(candidate)) return candidate;
    idx += 1;
  }
}

async function readDirectoryEntries(reader: any): Promise<any[]> {
  const all: any[] = [];
  while (true) {
    const batch: any[] = await new Promise((resolve, reject) => {
      reader.readEntries(resolve, reject);
    });
    if (!batch.length) break;
    all.push(...batch);
  }
  return all;
}

async function walkDroppedEntry(entry: any, prefix = ""): Promise<UploadCandidate[]> {
  if (!entry) return [];
  if (entry.isFile) {
    const file = await new Promise<File>((resolve, reject) => entry.file(resolve, reject));
    const relativePath = prefix ? `${prefix}/${file.name}` : file.name;
    return [{ file, relativePath }];
  }
  if (entry.isDirectory) {
    const nextPrefix = prefix ? `${prefix}/${entry.name}` : entry.name;
    const reader = entry.createReader();
    const children = await readDirectoryEntries(reader);
    const nested = await Promise.all(children.map((child: any) => walkDroppedEntry(child, nextPrefix)));
    return nested.flat();
  }
  return [];
}

async function extractDroppedCandidates(dt: DataTransfer, absolutePaths: string[] = []): Promise<UploadCandidate[]> {
  const files = Array.from(dt.files || []);
  const items = Array.from(dt.items || []);
  if (items.length) {
    const nested = await Promise.all(items.map(async (item) => {
      const entry = (item as any).webkitGetAsEntry?.();
      if (entry) return walkDroppedEntry(entry);
      const file = item.getAsFile?.();
      return file ? [{ file, relativePath: (file as any).webkitRelativePath || file.name }] : [];
    }));
    const flattened = nested.flat().filter((entry) => entry.file);
    if (flattened.length) {
      return flattened.map((entry, index) => ({ ...entry, absolutePath: absolutePaths[index] || "" }));
    }
  }
  return files.map((file, index) => ({ file, relativePath: (file as any).webkitRelativePath || file.name, absolutePath: absolutePaths[index] || "" }));
}


function normalizeBackupTargetsForSignature(targets: BackupTarget[]) {
  return [...(targets || [])].map((target) => ({
    local_path: String(target.local_path || ""),
    remote_path: String(target.remote_path || ""),
    item_type: String(target.item_type || ""),
    display_name: String(target.display_name || ""),
    source_device_label: String(target.source_device_label || ""),
    remote_item_id: String(target.remote_item_id || ""),
  })).sort((a, b) => `${a.item_type}:${a.local_path}:${a.remote_path}`.localeCompare(`${b.item_type}:${b.local_path}:${b.remote_path}`));
}

function backupTargetListSignature(targets: BackupTarget[]) {
  return JSON.stringify(normalizeBackupTargetsForSignature(targets));
}

function itemListSignature(sourceItems: Item[]) {
  return JSON.stringify((sourceItems || []).map((item) => ({
    item_id: item.item_id,
    type: item.type,
    parent_id: item.parent_id || null,
    name: item.name,
    size_bytes: Number(item.size_bytes || 0),
    updated_at: Number(item.updated_at || 0),
    created_at: Number(item.created_at || 0),
    owner_user_id: item.owner_user_id || "",
    file_object_id: item.file_object_id || "",
    trashed_at: item.trashed_at || null,
    trash_batch_id: item.trash_batch_id || null,
    version_count: Number(item.version_count || 0),
    path: item.path || "",
  })));
}

function syncSummarySignature(value: SyncProfileResponse | null) {
  if (!value) return "";
  return JSON.stringify({
    profile: value.profile || null,
    clients: (value.clients || []).map((client) => ({
      client_id: client.client_id,
      status: client.status,
      pending_changes: Number(client.pending_changes || 0),
      local_root_display: client.local_root_display || "",
      last_seen: client.last_seen || null,
      last_sync_at: client.last_sync_at || null,
    })),
  });
}

export default function DriveLikeApp() {
  const [token, setToken] = useState<string>(() => localStorage.getItem("phase1_token") || "");
  const [userId, setUserId] = useState<string>(() => localStorage.getItem("phase1_user_id") || "");
  const [accountProfile, setAccountProfile] = useState<UserProfile | null>(null);
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const [accountCountrySelectOpen, setAccountCountrySelectOpen] = useState(false);
  const [accountLanguageSelectOpen, setAccountLanguageSelectOpen] = useState(false);
  const [loginLanguageMenuOpen, setLoginLanguageMenuOpen] = useState(false);
  const [accountSelectMenuPosition, setAccountSelectMenuPosition] = useState<{ left: number; top: number; width: number; maxHeight: number } | null>(null);
  const [accountMenuPosition, setAccountMenuPosition] = useState<{ left: number; top: number; width: number; maxHeight: number } | null>(null);
  const [avatarDataUrl, setAvatarDataUrl] = useState<string>("");
  const [authMode, setAuthMode] = useState<AuthMode>("login");
  const [signupStep, setSignupStep] = useState<"consent" | "form">("consent");
  const [email, setEmail] = useState<string>(() => localStorage.getItem("phase1_email") || "");
  const [password, setPassword] = useState("");
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [countryCode, setCountryCode] = useState("JP");
  const [languageCode, setLanguageCode] = useState<string>(() => localStorage.getItem("phase1_language_code") || "ja");
  const [acceptedPolicies, setAcceptedPolicies] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>("home");
  const [breadcrumbRootView, setBreadcrumbRootView] = useState<ViewMode>("home");
  const [items, setItems] = useState<Item[]>([]);
  const [breadcrumbs, setBreadcrumbs] = useState<Item[]>([]);
  const [parentId, setParentId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [activeQuery, setActiveQuery] = useState("");
  const [searchScope, setSearchScope] = useState<"home" | "owned" | "shared" | "recent">("home");
  const [sortKey, setSortKey] = useState<SortKey>("updated_at");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<Item | null>(null);
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [providerSummary, setProviderSummary] = useState<NodeProviderSummary | null>(null);
  const [providerSummaryFetchedAt, setProviderSummaryFetchedAt] = useState<number | null>(null);
  const [providerLoading, setProviderLoading] = useState(false);
  const [providerSaving, setProviderSaving] = useState(false);
  const [providerStarting, setProviderStarting] = useState(false);
  const [providerName, setProviderName] = useState("");
  const [desiredCapacityGb, setDesiredCapacityGb] = useState(0);
  const [desiredCapacityInput, setDesiredCapacityInput] = useState("0");
  const [copiedLabel, setCopiedLabel] = useState("");
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [versionData, setVersionData] = useState<VersionResponse | null>(null);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [syncSummary, setSyncSummary] = useState<SyncProfileResponse | null>(null);
  const [syncLoading, setSyncLoading] = useState(false);
  const [syncSaving, setSyncSaving] = useState(false);
  const [syncRootDisplay, setSyncRootDisplay] = useState("~/Phase1 Drive");
  const [syncIntervalSec, setSyncIntervalSec] = useState(5);
  const [syncIgnoreHidden, setSyncIgnoreHidden] = useState(true);
  const [backupTargets, setBackupTargets] = useState<BackupTarget[]>(() => {
    try {
      const raw = localStorage.getItem("phase1_backup_targets");
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  });
  const [offlineTargets, setOfflineTargets] = useState<BackupTarget[]>(() => {
    try {
      const raw = localStorage.getItem("phase1_offline_targets");
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return parsed.map((entry) => ({ ...entry, target_kind: "offline" as const }));
    } catch {
      return [];
    }
  });
  const backupTargetsSignatureRef = useRef(backupTargetListSignature(backupTargets));
  const offlineTargetsSignatureRef = useRef(backupTargetListSignature(offlineTargets));
  const syncItemsSignatureRef = useRef("");
  const syncBreadcrumbsSignatureRef = useRef("");
  const syncSummarySignatureRef = useRef("");
  const syncRefreshInFlightRef = useRef(false);
  const [currentDeviceLabel, setCurrentDeviceLabel] = useState<string>("");
  const [backupAutoRefreshEnabled, setBackupAutoRefreshEnabled] = useState(false);
  const [uploadDragging, setUploadDragging] = useState(false);
  const [uploadMenuOpen, setUploadMenuOpen] = useState(false);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; item: Item } | null>(null);
  const [shareDialogItems, setShareDialogItems] = useState<Item[]>([]);
  const [shareRecipientInput, setShareRecipientInput] = useState("");
  const [shareRecipientEmails, setShareRecipientEmails] = useState<string[]>([]);
  const [shareMessage, setShareMessage] = useState("");
  const [shareSending, setShareSending] = useState(false);
  const [shareDialogPanelWidth, setShareDialogPanelWidth] = useState<number | null>(null);
  const [moveDialogItem, setMoveDialogItem] = useState<Item | null>(null);
  const [moveTargetParentId, setMoveTargetParentId] = useState<string>(ROOT_ID);
  const [moveTargets, setMoveTargets] = useState<Item[]>([]);
  const [moveLoading, setMoveLoading] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [selectionAnchorId, setSelectionAnchorId] = useState<string | null>(null);
  const [draggingItemIds, setDraggingItemIds] = useState<string[]>([]);
  const [hoverFolderId, setHoverFolderId] = useState<string | null>(null);
  const [dragSelectActive, setDragSelectActive] = useState(false);
  const [recentButtonActive, setRecentButtonActive] = useState(false);
  const [hashRoute, setHashRoute] = useState<string>(() => (typeof window !== "undefined" ? window.location.hash || "" : ""));
  const fileUploadRef = useRef<HTMLInputElement | null>(null);
  const folderUploadRef = useRef<HTMLInputElement | null>(null);
  const avatarUploadRef = useRef<HTMLInputElement | null>(null);
  const accountButtonRef = useRef<HTMLDivElement | null>(null);
  const accountCountrySelectButtonRef = useRef<HTMLButtonElement | null>(null);
  const accountLanguageSelectButtonRef = useRef<HTMLButtonElement | null>(null);
  const uploadIntentRef = useRef<"normal" | "backup" | null>(null);
  const mainContentRef = useRef<HTMLElement | null>(null);
  const dragHoverTimerRef = useRef<number | null>(null);
  const dragSelectAnchorRef = useRef<string | null>(null);
  const dragSelectMovedRef = useRef(false);
  const dragSelectActiveRef = useRef(false);
  const dragSelectPointerRef = useRef<{ x: number; y: number } | null>(null);
  const dragSelectAutoScrollFrameRef = useRef<number | null>(null);
  const sortedItemsRef = useRef<Item[]>([]);
  const backupTargetsHydratedRef = useRef(false);

  const [textPrompt, setTextPrompt] = useState<TextPromptState | null>(null);
  const [textPromptValue, setTextPromptValue] = useState("");
  const [appDialog, setAppDialog] = useState<AppDialogState | null>(null);
  const [uploadConflictDialog, setUploadConflictDialog] = useState<UploadConflictDialogState | null>(null);

  const currentLanguageCode = normalizeUiLanguageCode(languageCode);
  const tx = (text: string, params: Record<string, string | number> = {}) => translateUiText(currentLanguageCode, text, params);
  const countryLabel = (code: string) => getCountryLabelByLanguage(code, currentLanguageCode);
  const localizedCountryOptions = useMemo(() => COUNTRY_OPTIONS.map((option) => ({ ...option, label: getCountryLabelByLanguage(option.code, currentLanguageCode) })), [currentLanguageCode]);
  const localizedLanguageOptions = useMemo(() => LANGUAGE_OPTIONS.map((option) => ({ ...option, label: getLanguageLabelByLanguage(option.code, currentLanguageCode) })), [currentLanguageCode]);

  const breadcrumbRootCandidates: ViewMode[] = ["home", "folders", "shared", "sync", "trash"];
  const isBreadcrumbRootCandidate = (value: ViewMode) => breadcrumbRootCandidates.includes(value);

  const getBreadcrumbRootLabel = (rootView: ViewMode) => {
    if (rootView === "folders") return tx("フォルダ");
    if (rootView === "shared") return tx("共有アイテム");
    if (rootView === "sync") return tx("バックアップ設定");
    if (rootView === "trash") return tx("ごみ箱");
    return tx("ホーム");
  };

  const getFolderApiViewForBreadcrumbRoot = (rootView: ViewMode): ViewMode => {
    if (rootView === "shared") return "shared";
    if (rootView === "sync") return "sync";
    return "folders";
  };


  const pageTitle = useMemo(() => {
    if (breadcrumbs.length && (viewMode === "folders" || viewMode === "shared" || viewMode === "sync")) {
      return breadcrumbs[breadcrumbs.length - 1].name;
    }
    if (viewMode === "shared") return tx("共有アイテム");
    if (viewMode === "search") return tx("検索結果");
    if (viewMode === "provider") return tx("ストレージ提供");
    if (viewMode === "trash") return tx("ごみ箱");
    if (viewMode === "sync") return tx("バックアップ設定");
    if (viewMode === "folders") return tx("フォルダ");
    if (viewMode === "home") return tx("ホーム");
    return tx("ホーム");
  }, [viewMode, breadcrumbs, currentLanguageCode]);

  const pageDescriptionLines = useMemo(() => {
    if (viewMode === "folders" && !breadcrumbs.length) {
      return [tx("クラウド上のファイル・フォルダを管理します。")];
    }
    if (viewMode === "shared" && !breadcrumbs.length) {
      return [tx("他のユーザーから共有されたファイル・フォルダを表示します。")];
    }
    if (viewMode === "sync" && !breadcrumbs.length) {
      return [tx("このPCから自動バックアップするファイル・フォルダを設定、管理します。バックアップを解除する際は、そのままごみ箱へ移動させて下さい。")];
    }
    if (viewMode === "provider") {
      return [tx("このPCの空き容量を他ユーザーへ提供し、報酬を受け取る設定を行います。")];
    }
    if (viewMode === "trash" && !breadcrumbs.length) {
      return [tx("復元しなかったファイル・フォルダは30 日後には完全に削除されます。")];
    }
    return [];
  }, [viewMode, breadcrumbs.length, currentLanguageCode]);

  const sortedItems = useMemo(() => {
    const cloned = [...items];
    cloned.sort((a, b) => {
      if (a.type !== b.type && viewMode !== "trash") return a.type === "folder" ? -1 : 1;
      if (sortKey === "name") {
        const result = a.name.localeCompare(b.name, "ja");
        return sortDir === "asc" ? result : -result;
      }
      if (sortKey === "size_bytes") {
        const result = Number(a.size_bytes || 0) - Number(b.size_bytes || 0);
        return sortDir === "asc" ? result : -result;
      }
      if (sortKey === "trashed_at") {
        const result = Number(a.trashed_at || 0) - Number(b.trashed_at || 0);
        return sortDir === "asc" ? result : -result;
      }
      const result = Number(a.updated_at || 0) - Number(b.updated_at || 0);
      return sortDir === "asc" ? result : -result;
    });
    return cloned;
  }, [items, sortDir, sortKey, viewMode]);

  useEffect(() => {
    sortedItemsRef.current = sortedItems;
  }, [sortedItems]);

  const selectedItems = useMemo(() => {
    const idSet = new Set(selectedIds);
    return sortedItems.filter((item) => idSet.has(item.item_id));
  }, [sortedItems, selectedIds]);

  const detailItem = useMemo(() => {
    if (selectedItems.length === 1) return selectedItems[0];
    if (selected && selectedIds.length === 0) return selected;
    return null;
  }, [selected, selectedIds.length, selectedItems]);

  const selectedTotalSize = useMemo(() => {
    return selectedItems.reduce((sum, item) => sum + Number(item.size_bytes || 0), 0);
  }, [selectedItems]);

  const allSelectedOwned = useMemo(() => {
    return selectedItems.length > 0 && selectedItems.every((item) => String(item.owner_user_id || "") === userId);
  }, [selectedItems, userId]);

  const shareDialogOpen = shareDialogItems.length > 0;
  const measureHalfWidthChars = (value: string) => Array.from(value || "").reduce((total, char) => {
    const code = char.codePointAt(0) || 0;
    return total + (code <= 0x7f ? 1 : 2);
  }, 0);

  const shareRecipientInputWidthCh = useMemo(() => {
    const inputLengthCh = measureHalfWidthChars(shareRecipientInput);
    if (!shareRecipientInput.trim()) {
      return shareRecipientEmails.length ? 4 : 40;
    }
    if (shareRecipientEmails.length) {
      return Math.max(4, inputLengthCh + 2);
    }
    return Math.max(40, inputLengthCh + 2);
  }, [shareRecipientEmails.length, shareRecipientInput]);

  const backupListGridStyle = useMemo(() => ({
    gridTemplateColumns: "2fr 20px 1.3fr 80px 130px 80px",
  }), []);

  useEffect(() => {
    if (!shareDialogOpen) {
      setShareDialogPanelWidth(null);
      return;
    }

    const updateShareDialogPanelWidth = () => {
      const halfWindowWidth = Math.round(window.innerWidth * 0.4);
      const nextWidth = Math.min(window.innerWidth - 32, Math.max(560, halfWindowWidth));
      setShareDialogPanelWidth(nextWidth);
    };

    updateShareDialogPanelWidth();
    window.addEventListener("resize", updateShareDialogPanelWidth);
    return () => window.removeEventListener("resize", updateShareDialogPanelWidth);
  }, [shareDialogOpen]);

  const canUploadHere = viewMode === "home" || viewMode === "folders" || viewMode === "sync";

  const syncCommand = useMemo(() => {
    return `python desktop_sync/phase2_desktop_sync.py --control "${API_BASE}" --server "${SERVER_BASE}" --email "<YOUR_EMAIL>" --password "<LOGIN_PASSWORD>" --sync-dir "${syncRootDisplay}" --interval ${syncIntervalSec}${syncIgnoreHidden ? " --ignore-hidden" : ""}`;
  }, [syncRootDisplay, syncIntervalSec, syncIgnoreHidden]);

  const desktopBridge = useMemo(() => {
    return (window as any).phase1Desktop || (window as any).electronAPI || null;
  }, []);

  const accountAvatarStorageKey = useMemo(() => userId ? `phase1_account_avatar_${userId}` : "phase1_account_avatar", [userId]);

  useEffect(() => {
    try {
      setAvatarDataUrl(localStorage.getItem(accountAvatarStorageKey) || "");
    } catch {
      setAvatarDataUrl("");
    }
  }, [accountAvatarStorageKey]);

  const requestTextInput = (config: TextPromptConfig): Promise<string | null> => {
    return new Promise((resolve) => {
      setTextPromptValue(config.defaultValue ?? "");
      setTextPrompt({
        ...config,
        resolve,
      });
    });
  };

  const finishTextPrompt = (value: string | null) => {
    const resolver = textPrompt?.resolve;
    setTextPrompt(null);
    setTextPromptValue("");
    resolver?.(value);
  };

  const requestAppConfirm = (config: AppDialogConfig): Promise<boolean> => {
    setUploadMenuOpen(false);
    setContextMenu(null);
    return new Promise((resolve) => {
      setAppDialog({
        ...config,
        resolve,
      });
    });
  };

  const requestAppAlert = async (message: string, title = tx("お知らせ")) => {
    await requestAppConfirm({
      title: tx(title),
      message,
      confirmLabel: tx("OK"),
      cancelLabel: "",
      variant: "info",
    });
  };

  const finishAppDialog = (value: boolean) => {
    const resolver = appDialog?.resolve;
    setAppDialog(null);
    resolver?.(value);
  };

  const requestUploadConflictDecision = (config: UploadConflictDialogConfig): Promise<UploadConflictDecision> => {
    setUploadMenuOpen(false);
    setContextMenu(null);
    return new Promise((resolve) => {
      setUploadConflictDialog({
        ...config,
        resolve,
      });
    });
  };

  const finishUploadConflictDialog = (value: UploadConflictDecision) => {
    const resolver = uploadConflictDialog?.resolve;
    setUploadConflictDialog(null);
    resolver?.(value);
  };

  const verifyCurrentPassword = async (currentPassword: string, purpose = "この操作") => {
    const trimmedEmail = email.trim();
    if (!trimmedEmail) {
      throw new Error(`${purpose}にはログイン用メールアドレスが必要です。再ログイン後にお試しください。`);
    }
    try {
      await api<AuthResponse>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email: trimmedEmail, password: currentPassword }),
      });
    } catch {
      throw new Error("ログイン時パスワードが正しくありません。もう一度入力してください。");
    }
  };


  const getCreateUploadTarget = (baseParentId?: string | null) => {
    if (viewMode === "sync") {
      return {
        targetView: "sync" as ViewMode,
        targetParentId: baseParentId !== undefined ? baseParentId : parentId,
      };
    }
    if (viewMode === "folders") {
      return {
        targetView: "folders" as ViewMode,
        targetParentId: baseParentId !== undefined ? baseParentId : parentId,
      };
    }
    return {
      targetView: "folders" as ViewMode,
      targetParentId: baseParentId ?? null,
    };
  };

  const refreshAfterCreateUpload = async (targetView: ViewMode, targetParentId: string | null) => {
    if (targetView === "sync") {
      clearSelection();
      setRecentButtonActive(false);
      setBreadcrumbRootView("sync");
      setViewMode("sync");
      setParentId(targetParentId);
      await refresh("sync", targetParentId, "");
      return;
    }
    clearSelection();
    setRecentButtonActive(false);
    setBreadcrumbRootView("folders");
    setViewMode("folders");
    setParentId(targetParentId);
    await refresh("folders", targetParentId, "");
  };

  const persistBackupTargetsToBackend = (nextTargets: BackupTarget[]) => {
    if (!token) return;
    void api<BackupTargetsResponse>("/backup/targets", {
      method: "PUT",
      body: JSON.stringify({ targets: nextTargets }),
    }, token).catch((err: any) => {
      console.warn("backup targets persistence failed", err);
    });
  };

  const replaceBackupTargets = (nextTargets: BackupTarget[], persist = true) => {
    const safeTargets = sanitizeBackupSettingTargets(Array.isArray(nextTargets) ? nextTargets : []);
    const nextSignature = backupTargetListSignature(safeTargets);
    const changed = backupTargetsSignatureRef.current !== nextSignature;

    if (changed) {
      backupTargetsSignatureRef.current = nextSignature;
      setBackupTargets(safeTargets);
      localStorage.setItem("phase1_backup_targets", JSON.stringify(safeTargets));
      if (viewMode === "sync") {
        setSyncRootDisplay(summarizeBackupTargets(safeTargets));
      }
    }

    if (persist) persistBackupTargetsToBackend(safeTargets);
  };


  const isOfflineTarget = (target?: BackupTarget | null) => {
    const kind = String(target?.target_kind || "backup").toLowerCase();
    const localPath = String(target?.local_path || "").replace(/\\/g, "/");
    // 旧版で target_kind が付かずに保存されたオフライン対象も、表示・永続化上は通常バックアップから分離する。
    return kind === "offline" || localPath.includes("/Phase1 Offline/") || localPath.endsWith("/Phase1 Offline");
  };

  const normalizeBackupSettingTarget = (target: BackupTarget): BackupTarget => ({
    ...target,
    target_kind: "backup",
  });

  const isValidBackupSettingTarget = (target?: BackupTarget | null) => {
    if (!target || isOfflineTarget(target)) return false;
    // フォルダバックアップは、クラウド上のルートフォルダ item_id と紐付いたものだけを有効扱いにする。
    // 失敗した過去の登録で remote_item_id が空のフォルダ target が残ると、
    // 通常表示まで隠したり、Electron 側で再アップロードの原因になる。
    if (target.item_type === "folder") {
      return Boolean(String(target.remote_item_id || "").trim());
    }
    return Boolean(String(target.local_path || "").trim() && String(target.remote_path || "").trim());
  };

  const sanitizeBackupSettingTargets = (targets: BackupTarget[]) => {
    return (Array.isArray(targets) ? targets : [])
      .map(normalizeBackupSettingTarget)
      .filter(isValidBackupSettingTarget);
  };

  const normalizeOfflineTarget = (target: BackupTarget): BackupTarget => ({
    ...target,
    target_kind: "offline",
  });

  const splitRuntimeTargets = (targets: BackupTarget[]) => {
    const backup: BackupTarget[] = [];
    const offline: BackupTarget[] = [];
    for (const target of Array.isArray(targets) ? targets : []) {
      if (isOfflineTarget(target)) {
        offline.push(normalizeOfflineTarget(target));
      } else {
        const normalized = normalizeBackupSettingTarget(target);
        if (isValidBackupSettingTarget(normalized)) backup.push(normalized);
      }
    }
    return { backup, offline };
  };

  const combineRuntimeTargets = (backupList = backupTargets, offlineList = offlineTargets) => {
    const merged = [
      ...sanitizeBackupSettingTargets(backupList),
      ...offlineList.map(normalizeOfflineTarget),
    ];
    const seen = new Set<string>();
    return merged.filter((target) => {
      const key = [target.target_kind || "backup", target.item_type, target.remote_item_id || "", target.local_path, target.remote_path].join("::");
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  };

  const replaceOfflineTargets = (nextTargets: BackupTarget[]) => {
    const safeTargets = (Array.isArray(nextTargets) ? nextTargets : []).map(normalizeOfflineTarget);
    const nextSignature = backupTargetListSignature(safeTargets);
    const changed = offlineTargetsSignatureRef.current !== nextSignature;
    if (!changed) return;
    offlineTargetsSignatureRef.current = nextSignature;
    setOfflineTargets(safeTargets);
    localStorage.setItem("phase1_offline_targets", JSON.stringify(safeTargets));
  };

  const getItemRemotePath = (item: Item) => normalizeRemotePath(item.path || item.name || "");

  const buildActiveItemPathMap = (treeItems: Item[]) => {
    const activeItems = (treeItems || []).filter((item) => item && item.trashed_at == null);
    const byId = new Map(activeItems.map((item) => [String(item.item_id || ""), item]));
    const memo = new Map<string, string>();

    const buildPath = (item: Item): string => {
      const itemId = String(item.item_id || "");
      if (!itemId) return "";
      if (memo.has(itemId)) return memo.get(itemId) || "";
      const explicitPath = normalizeRemotePath(item.path || "");
      if (explicitPath) {
        memo.set(itemId, explicitPath);
        return explicitPath;
      }
      const name = normalizeRemotePath(item.name || "");
      const parentId = String(item.parent_id || ROOT_ID);
      const parent = parentId && parentId !== ROOT_ID ? byId.get(parentId) : null;
      const parentPath = parent ? buildPath(parent) : "";
      const pathValue = normalizeRemotePath(parentPath ? `${parentPath}/${name}` : name);
      memo.set(itemId, pathValue);
      return pathValue;
    };

    for (const item of activeItems) {
      buildPath(item);
    }
    return { activeItems, byId, pathById: memo };
  };

  const targetExistsInActiveTree = (target: BackupTarget, treeItems: Item[]) => {
    const { activeItems, byId, pathById } = buildActiveItemPathMap(treeItems);
    const targetRemoteItemId = String(target.remote_item_id || "").trim();
    if (targetRemoteItemId) return byId.has(targetRemoteItemId);

    const targetPath = normalizeRemotePath(target.remote_path || target.display_name || "");
    if (!targetPath) return false;
    return activeItems.some((item) => {
      const itemPath = normalizeRemotePath(pathById.get(String(item.item_id || "")) || item.path || item.name || "");
      if (!itemPath) return false;
      if (target.item_type === "folder") {
        return item.type === "folder" && itemPath === targetPath;
      }
      return item.type === "file" && itemPath === targetPath;
    });
  };

  const pruneTargetsForActiveTree = (targets: BackupTarget[], treeItems: Item[]) => {
    if (!Array.isArray(targets) || !targets.length) return [];
    return targets.filter((target) => targetExistsInActiveTree(target, treeItems));
  };

  const targetMatchesDeletedItem = (target: BackupTarget, item: Item) => {
    if (target.remote_item_id && target.remote_item_id === item.item_id) return true;
    const itemRemotePath = getItemRemotePath(item);
    const targetRemotePath = normalizeRemotePath(target.remote_path || target.display_name || "");
    if (!itemRemotePath || !targetRemotePath) return false;
    if (item.type === "folder") {
      return targetRemotePath === itemRemotePath || targetRemotePath.startsWith(`${itemRemotePath}/`);
    }
    return targetRemotePath === itemRemotePath;
  };

  const getBackupSourceTargetForItem = (item: Item) => {
    const itemRemotePath = getItemRemotePath(item);
    const itemNamePath = normalizeRemotePath(item.name || "");
    if (!itemRemotePath && !itemNamePath) return null;

    return backupTargets.find((target) => {
      if (target.remote_item_id && target.remote_item_id === item.item_id) return true;
      const targetRemotePath = normalizeRemotePath(target.remote_path || target.display_name || "");
      if (!targetRemotePath) return false;
      if (targetRemotePath === itemRemotePath || targetRemotePath === itemNamePath) return true;
      if (item.type === "folder") {
        return Boolean(
          (itemRemotePath && targetRemotePath.startsWith(`${itemRemotePath}/`)) ||
          (itemNamePath && targetRemotePath.startsWith(`${itemNamePath}/`))
        );
      }
      return false;
    }) || null;
  };

  const normalizeDeviceLabel = (value?: string | null) => String(value || "").trim();

  const getBackupSourceDeviceDisplayLabel = (deviceLabel?: string | null) => {
    const safeLabel = normalizeDeviceLabel(deviceLabel);
    const safeCurrentLabel = normalizeDeviceLabel(currentDeviceLabel);
    if (!safeLabel || safeLabel === "このデバイス" || safeLabel === "このPC") return "このPC";
    if (safeCurrentLabel && safeLabel === safeCurrentLabel) return "このPC";
    return safeLabel;
  };

  const getBackupSourceForItem = (item: Item) => {
    const target = getBackupSourceTargetForItem(item);
    if (!target) return null;
    return {
      deviceLabel: getBackupSourceDeviceDisplayLabel(target.source_device_label),
      localPath: String(target.local_path || ""),
      remoteItemId: target.remote_item_id || null,
    };
  };

  const targetCoversItem = (target: BackupTarget, item: Item) => {
    if (target.remote_item_id && target.remote_item_id === item.item_id) return true;
    const targetPath = normalizeRemotePath(target.remote_path || target.display_name || "");
    const itemPath = getItemRemotePath(item);
    if (!targetPath || !itemPath) return false;
    if (target.item_type === "folder") {
      return itemPath === targetPath || itemPath.startsWith(`${targetPath}/`);
    }
    return itemPath === targetPath;
  };

  const isOfflineAvailableItem = (item: Item) => {
    return offlineTargets.some((target) => targetCoversItem(target, item));
  };

  const isSharedDisplayItem = (item: Item) => {
    if (viewMode === "shared") return true;
    const ownerId = String(item.owner_user_id || "");
    return viewMode === "home" && Boolean(ownerId) && ownerId !== userId;
  };

  const renderItemStatusIcon = (item: Item, context: "sync" | "normal") => {
    if (context === "sync") {
      if (!backupAutoRefreshEnabled || !getBackupSourceForItem(item)) return null;
      return <LaptopMinimalCheck className="h-4 w-4 text-emerald-600" aria-label="自動バックアップ中" />;
    }

    if (isOfflineAvailableItem(item)) {
      return <CloudDownload className="h-4 w-4 text-emerald-500" aria-label="オフライン利用中" />;
    }

    if (isSharedDisplayItem(item)) {
      return <Users className="h-4 w-4 text-violet-600" aria-label="共有アイテム" />;
    }

    return null;
  };

  const syncBackupTargetsToDesktop = async (nextTargets: BackupTarget[], nextOfflineTargets = offlineTargets) => {
    const runtimeTargets = combineRuntimeTargets(nextTargets, nextOfflineTargets);
    const localRootSummary = summarizeBackupTargets(nextTargets);
    if (desktopBridge?.updateBackupTargets && runtimeTargets.length) {
      const response = await desktopBridge.updateBackupTargets({
        api_base: API_BASE,
        access_token: token,
        email: email.trim(),
        polling_interval_sec: syncIntervalSec,
        ignore_hidden: syncIgnoreHidden,
        local_root_display: localRootSummary,
        targets: runtimeTargets,
      }) as BackupBridgeResponse;
      if (!response?.ok) {
        throw new Error(response?.error || response?.message || tx("バックアップ対象の更新に失敗しました。"));
      }
      return response;
    }
    if (!runtimeTargets.length && desktopBridge?.stopBackup) {
      const response = await desktopBridge.stopBackup() as BackupBridgeResponse;
      if (!response?.ok) {
        throw new Error(response?.error || response?.message || tx("自動バックアップの停止に失敗しました。"));
      }
      return response;
    }
    return { ok: true } as BackupBridgeResponse;
  };

  const removeBackupTargetsForItems = async (itemsToRemove: Item[]) => {
    const removedItems = Array.isArray(itemsToRemove) ? itemsToRemove.filter(Boolean) : [];
    if (!removedItems.length) return false;

    // state だけではなく localStorage に残っている過去 target も含めて削除する。
    // これをしないと、画面上では消したつもりでも、次回「自動バックアップを開始」時に
    // 古い localStorage / backend の target が Electron へ再注入されることがある。
    const currentBackupTargets = sanitizeBackupSettingTargets(
      mergeBackupTargetLists(backupTargets, readStoredBackupTargets()),
    );
    const currentOfflineTargets = (Array.isArray(offlineTargets) ? offlineTargets : []).map(normalizeOfflineTarget);

    const nextTargets = currentBackupTargets.filter((target) => !removedItems.some((item) => targetMatchesDeletedItem(target, item)));
    const nextOfflineTargets = currentOfflineTargets.filter((target) => !removedItems.some((item) => targetMatchesDeletedItem(target, item)));
    const backupChanged = nextTargets.length !== currentBackupTargets.length;
    const offlineChanged = nextOfflineTargets.length !== currentOfflineTargets.length;

    if (!backupChanged && !offlineChanged) {
      // state 上では一致しなくても、backend 側に古い対象が残っている可能性があるため、
      // 削除後の正しい一覧を PUT して active target を明示的に置き換える。
      persistBackupTargetsToBackend(nextTargets);
      return false;
    }

    if (backupChanged) replaceBackupTargets(nextTargets);
    if (offlineChanged) replaceOfflineTargets(nextOfflineTargets);
    try {
      await syncBackupTargetsToDesktop(nextTargets, nextOfflineTargets);
      return true;
    } catch (err: any) {
      setError(err?.message || tx("バックアップ対象の更新に失敗しました。"));
      return false;
    }
  };

  const getLocalFilePaths = async (files: File[]): Promise<string[]> => {
    try {
      if (desktopBridge?.getPathForFiles) {
        const values = desktopBridge.getPathForFiles(files);
        return Array.isArray(values) ? values.map((value: any) => String(value || "")) : files.map(() => "");
      }
    } catch {
      // noop
    }
    return files.map((file) => String((file as any)?.path || ""));
  };

  const normalizeRemotePath = (value: string) => String(value || "").replace(/\\/g, "/").replace(/^\/+/, "").replace(/\/+$/, "");

  const joinRemotePath = (...parts: Array<string | null | undefined>) => parts
    .map((part) => normalizeRemotePath(String(part || "")))
    .filter(Boolean)
    .join("/");

  const summarizeBackupTargets = (targets: BackupTarget[]) => {
    if (!targets.length) return "バックアップ対象なし";
    if (targets.length === 1) return targets[0].local_path;
    return `${targets[0].local_path} ほか ${targets.length - 1}件`;
  };

  const upsertBackupTarget = (target: BackupTarget) => {
    const normalizedTarget = normalizeBackupSettingTarget(target);
    if (!isValidBackupSettingTarget(normalizedTarget)) {
      console.warn("invalid backup target ignored", normalizedTarget);
      setError("バックアップ対象フォルダのクラウドIDを取得できなかったため、登録を中止しました。もう一度アップロードしてください。");
      return;
    }
    setBackupTargets((prev) => {
      const next = [...prev];
      const index = next.findIndex((entry) => entry.local_path === normalizedTarget.local_path && entry.item_type === normalizedTarget.item_type);
      if (index >= 0) {
        next[index] = normalizedTarget;
      } else {
        next.push(normalizedTarget);
      }
      localStorage.setItem("phase1_backup_targets", JSON.stringify(next));
      persistBackupTargetsToBackend(next);
      return next;
    });
  };

  const mergeBackupTargets = (currentTargets: BackupTarget[], target: BackupTarget) => {
    const filtered = currentTargets.filter((entry) => !(entry.item_type === target.item_type && (entry.local_path === target.local_path || entry.remote_path === target.remote_path)));
    return [...filtered, target];
  };


  const readStoredBackupTargets = (): BackupTarget[] => {
    try {
      const raw = localStorage.getItem("phase1_backup_targets");
      if (!raw) return sanitizeBackupSettingTargets(backupTargets);
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? sanitizeBackupSettingTargets(parsed) : sanitizeBackupSettingTargets(backupTargets);
    } catch {
      return sanitizeBackupSettingTargets(backupTargets);
    }
  };

  const readStoredOfflineTargets = (): BackupTarget[] => {
    try {
      const raw = localStorage.getItem("phase1_offline_targets");
      if (!raw) return offlineTargets;
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return offlineTargets;
      return parsed.map((entry) => normalizeOfflineTarget(entry));
    } catch {
      return offlineTargets;
    }
  };

  const mergeBackupTargetLists = (primaryTargets: BackupTarget[], secondaryTargets: BackupTarget[]) => {
    const merged: BackupTarget[] = [];
    const seen = new Set<string>();
    for (const target of [...primaryTargets, ...secondaryTargets]) {
      const normalized = normalizeBackupSettingTarget(target);
      const key = [
        normalized.item_type,
        normalized.remote_item_id || "",
        normalizeRemotePath(normalized.remote_path || normalized.display_name || ""),
        String(normalized.local_path || ""),
      ].join("::");
      if (seen.has(key)) continue;
      seen.add(key);
      merged.push(normalized);
    }
    return merged;
  };

  const loadBackupTargetsFromBackend = async (): Promise<BackupTarget[]> => {
    if (!token) return readStoredBackupTargets();
    try {
      const res = await api<BackupTargetsResponse>("/backup/targets", {}, token);
      const nextTargets = Array.isArray(res.targets) ? res.targets : [];
      let activeTreeItems: Item[] = [];
      try {
        const tree = await api<TreeResponse>("/sync/tree", {}, token);
        activeTreeItems = Array.isArray(tree.items) ? tree.items : [];
      } catch (treeErr) {
        console.warn("backup target prune tree load failed", treeErr);
      }

      const split = splitRuntimeTargets(nextTargets);
      const prunedBackendBackupTargets = activeTreeItems.length
        ? pruneTargetsForActiveTree(split.backup, activeTreeItems)
        : split.backup;
      const prunedBackendOfflineTargets = activeTreeItems.length
        ? pruneTargetsForActiveTree(split.offline, activeTreeItems)
        : split.offline;
      const backendPruned = prunedBackendBackupTargets.length !== split.backup.length || prunedBackendOfflineTargets.length !== split.offline.length;

      const pendingLocalBackupTargets = splitRuntimeTargets(readStoredBackupTargets()).backup;
      const prunedPendingLocalBackupTargets = activeTreeItems.length
        ? pruneTargetsForActiveTree(pendingLocalBackupTargets, activeTreeItems)
        : pendingLocalBackupTargets;
      // 「バックアップ設定」にアップロードした直後、まだ「自動バックアップを開始」を押す前は
      // /backup/targets には未保存なので、backendの空一覧だけでローカルの表示用targetsを消さない。
      // ただし remote_item_id のないフォルダ target は、失敗したアップロード試行の残骸として
      // 通常表示を隠したり、監視側で無限アップロードの原因になりやすいので表示用には採用しない。
      const safePendingLocalBackupTargets = prunedPendingLocalBackupTargets.filter((target) => {
        if (target.item_type !== "folder") return true;
        return Boolean(String(target.remote_item_id || "").trim());
      });
      const mergedBackupTargets = sanitizeBackupSettingTargets(mergeBackupTargetLists(prunedBackendBackupTargets, safePendingLocalBackupTargets));
      replaceBackupTargets(mergedBackupTargets, backendPruned);
      if (split.offline.length) {
        replaceOfflineTargets(prunedBackendOfflineTargets);
      }
      backupTargetsHydratedRef.current = true;
      return mergedBackupTargets;
    } catch (err: any) {
      console.warn("backup targets load failed", err);
      backupTargetsHydratedRef.current = true;
      return sanitizeBackupSettingTargets(readStoredBackupTargets());
    }
  };

  const getBackupTargetPath = (target: BackupTarget) => normalizeRemotePath(target.remote_path || target.display_name || "");

  const getItemPathForSeparation = (item: Item) => normalizeRemotePath(item.path || item.name || "");

  const isItemInBackupNamespace = (item: Item, targets: BackupTarget[], contextParentId?: string | null) => {
    const itemPath = getItemPathForSeparation(item);
    if (!itemPath || !targets.length) return false;
    const currentParentKey = contextParentId || null;
    return targets.some((target) => {
      const targetRemoteItemId = String(target.remote_item_id || "").trim();
      const targetPath = getBackupTargetPath(target);
      if (!targetPath) return false;

      // 失敗したバックアップ設定フォルダが localStorage / runtime に残ると、
      // remote_path だけで通常アップロードのフォルダまで隠してしまう。
      // そのため、通常表示から隠すのは「クラウド上の item_id と結び付いた確定済み対象」に限定する。
      if (!targetRemoteItemId) return false;
      if (targetRemoteItemId === item.item_id) return true;

      // フォルダ対象は、直下だけではなく配下すべてをバックアップ名前空間として扱う。
      // 子フォルダを開いたときも、その中のフォルダ・ファイルを表示/非表示判定できるようにするため。
      if (target.item_type === "folder") {
        if (targetRemoteItemId && targetRemoteItemId === item.item_id) return true;
        return itemPath.startsWith(`${targetPath}/`);
      }
      return false;
    });
  };

  const hideBackupNamespaceItems = (sourceItems: Item[], explicitTargets?: BackupTarget[], contextParentId?: string | null) => {
    const targets = explicitTargets || backupTargets || readStoredBackupTargets();
    if (!targets.length) return sourceItems;
    return sourceItems.filter((item) => !isItemInBackupNamespace(item, targets, contextParentId));
  };

  const buildSyncBreadcrumbs = (treeItems: Item[], currentParentId: string | null) => {
    if (!currentParentId) return [];
    const byId = new Map(treeItems.map((item) => [item.item_id, item]));
    const trail: Item[] = [];
    let cursor: Item | undefined = byId.get(currentParentId);
    const seen = new Set<string>();
    while (cursor && !seen.has(cursor.item_id)) {
      seen.add(cursor.item_id);
      trail.unshift(cursor);
      cursor = cursor.parent_id ? byId.get(cursor.parent_id) : undefined;
    }
    return trail;
  };

  const normalizeTreeParentId = (value?: string | null) => {
    const safe = String(value || ROOT_ID).trim();
    return safe || ROOT_ID;
  };

  const buildBackupTargetIdSet = (treeItems: Item[], targets: BackupTarget[]) => {
    const safeTargets = sanitizeBackupSettingTargets(targets || []);
    const rootTargetIds = new Set(
      safeTargets
        .map((target) => String(target.remote_item_id || "").trim())
        .filter(Boolean),
    );
    const byParent = new Map<string, Item[]>();
    for (const item of treeItems || []) {
      if (!item || item.trashed_at != null) continue;
      if (String(item.owner_user_id || "") !== userId) continue;
      const parentKey = normalizeTreeParentId(item.parent_id);
      if (!byParent.has(parentKey)) byParent.set(parentKey, []);
      byParent.get(parentKey)!.push(item);
    }

    const allowed = new Set<string>();
    const stack = [...rootTargetIds];
    while (stack.length) {
      const currentId = stack.pop()!;
      if (!currentId || allowed.has(currentId)) continue;
      allowed.add(currentId);
      for (const child of byParent.get(currentId) || []) {
        stack.push(String(child.item_id || ""));
      }
    }
    return { allowed, rootTargetIds };
  };

  const buildSyncVisibleItems = (treeItems: Item[], targets: BackupTarget[], currentParentId: string | null) => {
    const safeTargets = sanitizeBackupSettingTargets(targets || []);
    if (!safeTargets.length) return [];
    const parentKey = normalizeTreeParentId(currentParentId);
    const { allowed, rootTargetIds } = buildBackupTargetIdSet(treeItems || [], safeTargets);

    return (treeItems || []).filter((item) => {
      if (item.trashed_at != null) return false;
      if (String(item.owner_user_id || "") !== userId) return false;

      // バックアップ設定のルートでは、登録済み target のルートだけを表示する。
      if (parentKey === ROOT_ID) {
        return rootTargetIds.has(String(item.item_id || ""));
      }

      // 子フォルダを開いた場合は、その親ID直下の項目を、
      // バックアップ対象ルートから辿れる範囲に限って表示する。
      // これにより「フォルダの中のフォルダの中...」も全階層で表示できる。
      if (normalizeTreeParentId(item.parent_id) !== parentKey) return false;
      return allowed.has(String(item.item_id || ""));
    });
  };

  const deriveBackupTarget = (absolutePath: string, relativePath: string, remoteFullPath: string, remoteItemId?: string | null): BackupTarget | null => {
    const safeAbsolutePath = String(absolutePath || "").trim();
    const safeRelativePath = String(relativePath || "").replace(/\\/g, "/").replace(/^\/+/, "");
    const safeRemotePath = normalizeRemotePath(remoteFullPath);
    if (!safeAbsolutePath || !safeRemotePath) return null;

    if (!safeRelativePath.includes("/")) {
      return {
        local_path: safeAbsolutePath,
        remote_path: safeRemotePath,
        item_type: "file",
        display_name: safeRemotePath.split("/").pop() || safeRemotePath,
        source_device_label: currentDeviceLabel || "このPC",
        remote_item_id: remoteItemId || null,
      };
    }

    const relParts = safeRelativePath.split("/").filter(Boolean);
    const absParts = safeAbsolutePath.split(/[/\\]+/).filter(Boolean);
    if (relParts.length < 2 || absParts.length < relParts.length) return null;
    const rootLocalParts = absParts.slice(0, absParts.length - relParts.length + 1);
    if (!rootLocalParts.length) return null;
    const rootRemotePath = safeRemotePath.split("/").filter(Boolean)[0] || safeRemotePath;
    const localRootPath = (safeAbsolutePath.startsWith('\\') ? '\\' : safeAbsolutePath.startsWith('/') ? '/' : '') + rootLocalParts.join('/');
    return {
      local_path: localRootPath,
      remote_path: rootRemotePath,
      item_type: "folder",
      display_name: rootRemotePath,
      source_device_label: currentDeviceLabel || "このPC",
      remote_item_id: remoteItemId || null,
      target_kind: "backup",
    };
  };


  const deriveFolderBackupRootTarget = (
    absolutePath: string,
    relativePath: string,
    actualDirectoryPath: string,
    rootRemoteItemId?: string | null,
  ): BackupTarget | null => {
    const safeAbsolutePath = String(absolutePath || "").trim();
    const safeRelativePath = String(relativePath || "").replace(/\\/g, "/").replace(/^\/+/, "");
    const actualRootRemotePath = normalizeRemotePath(actualDirectoryPath).split("/").filter(Boolean)[0] || "";
    if (!safeAbsolutePath || !safeRelativePath.includes("/") || !actualRootRemotePath) return null;

    const relParts = safeRelativePath.split("/").filter(Boolean);
    const absParts = safeAbsolutePath.split(/[/\\]+/).filter(Boolean);
    if (relParts.length < 2 || absParts.length < relParts.length) return null;

    const rootLocalParts = absParts.slice(0, absParts.length - relParts.length + 1);
    if (!rootLocalParts.length) return null;
    const localRootPath = (safeAbsolutePath.startsWith("\\") ? "\\" : safeAbsolutePath.startsWith("/") ? "/" : "") + rootLocalParts.join("/");

    return {
      local_path: localRootPath,
      remote_path: actualRootRemotePath,
      item_type: "folder",
      display_name: actualRootRemotePath,
      source_device_label: currentDeviceLabel || "このPC",
      remote_item_id: rootRemoteItemId || null,
      target_kind: "backup",
    };
  };

  const openRecentItemsView = async () => {
    clearSelection();
    const nextActive = !recentButtonActive;
    setRecentButtonActive(nextActive);
    setSearchScope(nextActive ? "recent" : (viewMode === "shared" ? "shared" : viewMode === "folders" ? "owned" : "home"));
    setParentId(null);
    await refresh(viewMode, null, "");
  };

  const toggleSort = (nextKey: SortKey) => {
    if (sortKey === nextKey) {
      setSortDir((prev) => (prev === "asc" ? "desc" : "asc"));
      return;
    }
    setSortKey(nextKey);
    if (nextKey === "updated_at" || nextKey === "trashed_at") {
      setSortDir("desc");
    } else {
      setSortDir("asc");
    }
  };

  const getSortArrow = (key: SortKey) => {
    if (sortKey !== key) return "↕";
    return sortDir === "asc" ? "↑" : "↓";
  };

  const loadProviderSummary = async () => {
    if (!token) return;
    setProviderLoading(true);
    setError("");
    try {
      const apiSummary = await api<NodeProviderSummary>("/node/provider/summary", {}, token);
      const localCapacity = await loadDesktopLocalCapacity();
      const res = applyDesktopLocalCapacity(apiSummary, localCapacity);
      setProviderSummary(res);
      setProviderSummaryFetchedAt(Math.floor(Date.now() / 1000));
      setProviderName(res.profile?.node_name || res.defaults.node_name || "マイノード");
      const nextCapacityGb = res.profile?.desired_capacity_gb ?? 0;
      setDesiredCapacityGb(nextCapacityGb);
      setDesiredCapacityInput(String(nextCapacityGb));
      setSelected(null);
    } catch (err: any) {
      setError(err.message || "ノード情報の読み込みに失敗しました。");
    } finally {
      setProviderLoading(false);
    }
  };

  const loadSyncSummary = async (options: { silent?: boolean; preserveStoredOfflineTargets?: boolean } | boolean = {}): Promise<BackupTarget[]> => {
    const silent = typeof options === "object" && Boolean(options.silent);
    const preserveStoredOfflineTargets = typeof options === "boolean"
      ? options
      : Boolean(options.preserveStoredOfflineTargets);
    if (!token) return readStoredBackupTargets();
    if (!silent) {
      setSyncLoading(true);
      setError("");
    }
    let runtimeTargets = await loadBackupTargetsFromBackend();
    try {
      const res = await api<SyncProfileResponse>("/sync/profile", {}, token);
      const nextSummarySignature = syncSummarySignature(res);
      if (syncSummarySignatureRef.current !== nextSummarySignature) {
        syncSummarySignatureRef.current = nextSummarySignature;
        setSyncSummary(res);
      }
      try {
        if (desktopBridge?.getBackupStatus) {
          const backupStatus = await desktopBridge.getBackupStatus() as BackupBridgeResponse;
          const nextDeviceLabel = String(backupStatus?.state?.current_device_label || "");
          if (nextDeviceLabel) {
            setCurrentDeviceLabel((prev) => (prev === nextDeviceLabel ? prev : nextDeviceLabel));
          }
          const runtimeState = backupStatus?.state || null;
          const runtimeIsRunning = Boolean(runtimeState?.is_running);
          let runtimeTargetCount = runtimeTargets.length;
          if (Array.isArray(runtimeState?.targets) && runtimeState.targets.length > 0) {
            const split = splitRuntimeTargets(runtimeState.targets);
            runtimeTargets = split.backup;
            runtimeTargetCount = split.backup.length + split.offline.length;
            // 状態確認だけで /backup/targets へ PUT しない。
            // 実行中は Electron runtime state を正とする。
            // localStorage に古い削除済み target が残っていても再混入させないため。
            if (runtimeIsRunning) {
              runtimeTargets = sanitizeBackupSettingTargets(split.backup);
            } else {
              // 停止中だけ、「開始前にバックアップ設定へアップロードしただけ」の対象を表示用に保持する。
              const pendingLocalBackupTargets = splitRuntimeTargets(readStoredBackupTargets()).backup;
              runtimeTargets = sanitizeBackupSettingTargets(mergeBackupTargetLists(split.backup, pendingLocalBackupTargets));
            }
            replaceBackupTargets(runtimeTargets, false);
            const nextOfflineRuntimeTargets = preserveStoredOfflineTargets && !split.offline.length
              ? readStoredOfflineTargets()
              : split.offline;
            replaceOfflineTargets(nextOfflineRuntimeTargets);
          }
          setBackupAutoRefreshEnabled((prev) => {
            const next = runtimeIsRunning && runtimeTargetCount > 0;
            return prev === next ? prev : next;
          });
        }
      } catch {
        // noop
      }
      const nextRootDisplay = res.profile.local_root_display || summarizeBackupTargets(runtimeTargets) || "~/Phase1 Drive";
      setSyncRootDisplay((prev) => (prev === nextRootDisplay ? prev : nextRootDisplay));
      const nextInterval = res.profile.polling_interval_sec || 5;
      setSyncIntervalSec((prev) => (prev === nextInterval ? prev : nextInterval));
      const nextIgnoreHidden = Boolean(res.profile.ignore_hidden);
      setSyncIgnoreHidden((prev) => (prev === nextIgnoreHidden ? prev : nextIgnoreHidden));
      if (!silent) setSelected(null);
      return runtimeTargets;
    } catch (err: any) {
      if (!silent) setError(err.message || "同期設定の読み込みに失敗しました。");
      else console.warn("silent sync summary refresh failed", err);
      return runtimeTargets;
    } finally {
      if (!silent) setSyncLoading(false);
    }
  };

  const refresh = async (nextView?: ViewMode, nextParent?: string | null, q?: string, options: { silent?: boolean } = {}) => {
    if (!token) return;
    const silent = Boolean(options.silent);
    const finalView = nextView || viewMode;
    const finalParent = nextParent !== undefined ? nextParent : parentId;
    const finalQuery = q !== undefined ? q : activeQuery;
    let normalViewBackupTargets: BackupTarget[] | null = null;

    if (finalView !== "sync" && finalView !== "provider") {
      normalViewBackupTargets = await loadBackupTargetsFromBackend();
    }

    if (finalView === "provider") {
      await loadProviderSummary();
      return;
    }
    if (finalView === "sync") {
      if (!silent) {
        setLoading(true);
        setError("");
        // 別画面から戻った直後に、前画面の items が残って見えることを防ぐ。
        // ちらつき抑制用の署名もリセットし、同じバックアップ一覧でも必ず再反映する。
        syncItemsSignatureRef.current = "";
        syncBreadcrumbsSignatureRef.current = "";
        setItems([]);
        setBreadcrumbs([]);
      }
      const runtimeTargets = await loadSyncSummary({ silent });
      try {
        const tree = await api<TreeResponse>("/sync/tree", {}, token);
        const targetSnapshot = runtimeTargets.length ? runtimeTargets : readStoredBackupTargets();
        const nextItems = buildSyncVisibleItems(tree.items || [], targetSnapshot, finalParent);
        const nextBreadcrumbs = buildSyncBreadcrumbs(tree.items || [], finalParent);
        const nextItemsSignature = itemListSignature(nextItems);
        const nextBreadcrumbsSignature = itemListSignature(nextBreadcrumbs);

        if (syncItemsSignatureRef.current !== nextItemsSignature) {
          syncItemsSignatureRef.current = nextItemsSignature;
          setItems(nextItems);
        }
        if (syncBreadcrumbsSignatureRef.current !== nextBreadcrumbsSignature) {
          syncBreadcrumbsSignatureRef.current = nextBreadcrumbsSignature;
          setBreadcrumbs(nextBreadcrumbs);
        }
        setParentId((prev) => (prev === finalParent ? prev : finalParent));
        if (!silent) {
          setSelected(null);
          setSelectedIds([]);
        }
      } catch (err: any) {
        if (!silent) setError(err.message || "バックアップ対象の読み込みに失敗しました。");
        else console.warn("silent sync tree refresh failed", err);
      } finally {
        if (!silent) setLoading(false);
      }
      return;
    }

    setLoading(true);
    setError("");
    try {
      if (recentButtonActive && (finalView === "home" || finalView === "folders" || finalView === "shared")) {
        const res = await api<ListResponse>("/library/recent_opened", {}, token);
        setItems(hideBackupNamespaceItems(res.items || [], normalViewBackupTargets || undefined, finalParent));
        setBreadcrumbs([]);
        setSelected(null);
        setSelectedIds([]);
        return;
      }
      if (finalView === "home") {
        const res = await api<ListResponse>(`/library/home?sort=${sortKey}:${sortDir}`, {}, token);
        setItems(hideBackupNamespaceItems(res.items || [], normalViewBackupTargets || undefined, finalParent));
        setBreadcrumbs([]);
        setSelected(null);
        setSelectedIds([]);
        return;
      }
      if (finalView === "folders") {
        if (finalParent) {
          const params = new URLSearchParams();
          params.set("parent_id", finalParent);
          params.set("sort", `${sortKey}:${sortDir}`);
          const res = await api<ListResponse>(`/items?${params.toString()}`, {}, token);
          setItems(hideBackupNamespaceItems(res.items || [], normalViewBackupTargets || undefined, finalParent));
          setBreadcrumbs(res.breadcrumbs || []);
          setSelected(null);
          setSelectedIds([]);
          return;
        }
        const res = await api<ListResponse>(`/library/owned?sort=${sortKey}:${sortDir}`, {}, token);
        setItems(hideBackupNamespaceItems(res.items || [], normalViewBackupTargets || undefined, finalParent));
        setBreadcrumbs([]);
        setSelected(null);
        setSelectedIds([]);
        return;
      }
      if (finalView === "shared") {
        if (finalParent) {
          const params = new URLSearchParams();
          params.set("parent_id", finalParent);
          params.set("sort", `${sortKey}:${sortDir}`);
          const res = await api<ListResponse>(`/library/shared_children?${params.toString()}`, {}, token);
          setItems(res.items || []);
          setBreadcrumbs(res.breadcrumbs || []);
          setSelected(null);
          setSelectedIds([]);
          return;
        }
        const res = await api<ListResponse>(`/library/shared_received?sort=${sortKey}:${sortDir}`, {}, token);
        setItems(res.items || []);
        setBreadcrumbs([]);
        setSelected(null);
        setSelectedIds([]);
        return;
      }
      if (finalView === "recent") {
        const res = await api<ListResponse>("/library/recent_opened", {}, token);
        setItems(hideBackupNamespaceItems(res.items || [], normalViewBackupTargets || undefined, finalParent));
        setBreadcrumbs([]);
        setSelected(null);
        setSelectedIds([]);
        return;
      }
      if (finalView === "trash") {
        try {
          await api("/trash/purge_expired", { method: "POST" }, token);
        } catch {
          // 30日経過アイテムの整理 API が未導入でも一覧表示は続ける
        }
        const res = await api<ListResponse>(`/trash/items?sort=${sortKey}:${sortDir}`, {}, token);
        setItems(res.items || []);
        setBreadcrumbs([]);
        setSelected(null);
        setSelectedIds([]);
        return;
      }
      if (finalView === "search") {
        const params = new URLSearchParams();
        if (finalQuery) params.set("q", finalQuery);
        params.set("scope", searchScope);
        const res = await api<SearchResponse>(`/library/search?${params.toString()}`, {}, token);
        setItems(res.items || []);
        setBreadcrumbs([]);
        setSelected(null);
        setSelectedIds([]);
        return;
      }
    } catch (err: any) {
      setError(err.message || "読み込みに失敗しました。");
    } finally {
      setLoading(false);
    }
  };


  useEffect(() => {
    if (!token) {
      setAccountProfile(null);
      return;
    }
    let cancelled = false;
    api<UserProfile>("/auth/profile", {}, token).then((profile) => {
      if (cancelled) return;
      setAccountProfile(profile);
      if (profile.email) {
        setEmail(profile.email);
        localStorage.setItem("phase1_email", profile.email);
      }
      if (profile.country_code) setCountryCode(String(profile.country_code).toUpperCase());
    }).catch((err: any) => {
      console.warn("auth profile load failed", err);
    });
    return () => { cancelled = true; };
  }, [token]);

  useEffect(() => {
    if (!token) return;
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, sortKey, sortDir, recentButtonActive]);

  useEffect(() => {
    const onHashChange = () => setHashRoute(window.location.hash || "");
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  useEffect(() => {
    const close = () => {
      setContextMenu(null);
      setUploadMenuOpen(false);
      setAccountMenuOpen(false);
      setAccountCountrySelectOpen(false);
      setAccountLanguageSelectOpen(false);
      setAccountSelectMenuPosition(null);
      setLoginLanguageMenuOpen(false);
    };
    window.addEventListener("click", close);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("resize", close);
    };
  }, []);

  useEffect(() => {
    if (selectedIds.length === 1) {
      const found = items.find((item) => item.item_id === selectedIds[0]) || null;
      setSelected(found);
      return;
    }
    if (selectedIds.length !== 1) {
      setSelected(null);
    }
  }, [items, selectedIds]);

  useEffect(() => {
    backupTargetsSignatureRef.current = backupTargetListSignature(backupTargets);
    localStorage.setItem("phase1_backup_targets", JSON.stringify(backupTargets));
    if (viewMode === "sync" && backupTargets.length) {
      setSyncRootDisplay((prev) => {
        const next = summarizeBackupTargets(backupTargets);
        return prev === next ? prev : next;
      });
    }
  }, [backupTargets, viewMode]);

  useEffect(() => {
    offlineTargetsSignatureRef.current = backupTargetListSignature(offlineTargets);
    localStorage.setItem("phase1_offline_targets", JSON.stringify(offlineTargets));
  }, [offlineTargets]);

  useEffect(() => {
    if (!token) return;
    backupTargetsHydratedRef.current = false;
    void loadBackupTargetsFromBackend();
  }, [token, userId]);

  useEffect(() => {
    if (!token || viewMode !== "sync") return;
    if (!backupAutoRefreshEnabled || (backupTargets.length + offlineTargets.length) === 0) return;
    const timer = window.setInterval(() => {
      if (syncRefreshInFlightRef.current) return;
      syncRefreshInFlightRef.current = true;
      void refresh("sync", parentId, activeQuery, { silent: true }).finally(() => {
        syncRefreshInFlightRef.current = false;
      });
    }, Math.max(5, Number(syncIntervalSec || 5)) * 1000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, viewMode, parentId, activeQuery, syncIntervalSec, backupAutoRefreshEnabled, backupTargets.length, offlineTargets.length]);

  const stopDragSelectAutoScroll = () => {
    if (dragSelectAutoScrollFrameRef.current !== null) {
      window.cancelAnimationFrame(dragSelectAutoScrollFrameRef.current);
      dragSelectAutoScrollFrameRef.current = null;
    }
  };

  const getDragSelectScrollElement = (): HTMLElement | null => {
    const main = mainContentRef.current;
    if (main && main.scrollHeight > main.clientHeight + 4) return main;
    return (document.scrollingElement as HTMLElement | null) || document.documentElement;
  };

  const isDocumentScrollElement = (scrollElement: HTMLElement) => (
    scrollElement === document.scrollingElement ||
    scrollElement === document.documentElement ||
    scrollElement === document.body
  );

  const getDragSelectScrollRect = (scrollElement: HTMLElement) => {
    if (isDocumentScrollElement(scrollElement)) return { top: 0, bottom: window.innerHeight };
    const rect = scrollElement.getBoundingClientRect();
    return { top: rect.top, bottom: rect.bottom };
  };

  const getVisibleSelectableRows = (scrollElement: HTMLElement) => {
    const { top, bottom } = getDragSelectScrollRect(scrollElement);
    return (Array.from(document.querySelectorAll('[data-selectable-row="true"]')) as HTMLElement[])
      .filter((row) => {
        const rect = row.getBoundingClientRect();
        return rect.bottom >= top && rect.top <= bottom;
      });
  };

  const getDragSelectHeaderBottom = (scrollElement: HTMLElement) => {
    const { top } = getDragSelectScrollRect(scrollElement);
    const header = document.querySelector('[data-select-list-header="true"]') as HTMLElement | null;
    if (!header) return top + DRAG_SELECT_AUTO_SCROLL_EDGE_PX;
    const rect = header.getBoundingClientRect();
    if (rect.bottom <= 0) return top + DRAG_SELECT_AUTO_SCROLL_EDGE_PX;
    return Math.max(top, rect.bottom);
  };

  const updateDragSelectionFromClientPoint = (clientX: number, clientY: number) => {
    if (!dragSelectActiveRef.current || !dragSelectAnchorRef.current) return;

    const element = document.elementFromPoint(clientX, clientY) as HTMLElement | null;
    let row = element?.closest?.('[data-selectable-row="true"]') as HTMLElement | null;

    if (!row) {
      const scrollElement = getDragSelectScrollElement();
      if (scrollElement) {
        const { bottom } = getDragSelectScrollRect(scrollElement);
        const headerBottom = getDragSelectHeaderBottom(scrollElement);
        const visibleRows = getVisibleSelectableRows(scrollElement);
        if (clientY <= headerBottom) {
          row = visibleRows[0] || null;
        } else if (clientY >= bottom - DRAG_SELECT_AUTO_SCROLL_EDGE_PX) {
          row = visibleRows[visibleRows.length - 1] || null;
        }
      }
    }

    const itemId = row?.dataset?.itemId;
    if (!itemId) return;
    const sourceItems = sortedItemsRef.current.length ? sortedItemsRef.current : sortedItems;
    const item = sourceItems.find((entry) => entry.item_id === itemId);
    if (item) updateDragSelectedRange(item);
  };

  const stepDragSelectAutoScroll = () => {
    dragSelectAutoScrollFrameRef.current = null;
    if (!dragSelectActiveRef.current) return;

    const pointer = dragSelectPointerRef.current;
    const scrollElement = getDragSelectScrollElement();
    if (!pointer || !scrollElement) return;

    const { top, bottom } = getDragSelectScrollRect(scrollElement);
    const headerBottom = getDragSelectHeaderBottom(scrollElement);
    let scrollDelta = 0;

    if (pointer.y <= headerBottom) {
      const ratio = Math.min(1, Math.max(0.15, (headerBottom - pointer.y + 1) / DRAG_SELECT_AUTO_SCROLL_EDGE_PX));
      scrollDelta = -Math.max(4, Math.ceil(DRAG_SELECT_AUTO_SCROLL_MAX_STEP_PX * ratio));
    } else if (pointer.y > bottom - DRAG_SELECT_AUTO_SCROLL_EDGE_PX) {
      const ratio = Math.min(1, Math.max(0, (pointer.y - (bottom - DRAG_SELECT_AUTO_SCROLL_EDGE_PX)) / DRAG_SELECT_AUTO_SCROLL_EDGE_PX));
      scrollDelta = Math.max(4, Math.ceil(DRAG_SELECT_AUTO_SCROLL_MAX_STEP_PX * ratio));
    }

    if (scrollDelta !== 0) {
      const maxScrollTop = Math.max(0, scrollElement.scrollHeight - scrollElement.clientHeight);
      const nextScrollTop = Math.max(0, Math.min(maxScrollTop, scrollElement.scrollTop + scrollDelta));
      const actualDelta = nextScrollTop - scrollElement.scrollTop;

      if (actualDelta !== 0) {
        scrollElement.scrollBy({ top: actualDelta, behavior: "auto" });
        window.requestAnimationFrame(() => {
          const latestPointer = dragSelectPointerRef.current;
          if (latestPointer) updateDragSelectionFromClientPoint(latestPointer.x, latestPointer.y);
        });
      }

      dragSelectAutoScrollFrameRef.current = window.requestAnimationFrame(stepDragSelectAutoScroll);
    }
  };

  const startDragSelectAutoScroll = (clientX?: number, clientY?: number) => {
    if (typeof clientX === "number" && typeof clientY === "number") {
      dragSelectPointerRef.current = { x: clientX, y: clientY };
    }
    if (dragSelectAutoScrollFrameRef.current === null) {
      dragSelectAutoScrollFrameRef.current = window.requestAnimationFrame(stepDragSelectAutoScroll);
    }
  };

  useEffect(() => {
    const stopDragSelect = () => {
      dragSelectActiveRef.current = false;
      setDragSelectActive(false);
      dragSelectAnchorRef.current = null;
      dragSelectPointerRef.current = null;
      stopDragSelectAutoScroll();
    };
    const handleDragSelectMouseMove = (event: MouseEvent) => {
      if (!dragSelectActiveRef.current) return;
      dragSelectPointerRef.current = { x: event.clientX, y: event.clientY };
      updateDragSelectionFromClientPoint(event.clientX, event.clientY);
      startDragSelectAutoScroll();
    };
    window.addEventListener("mousemove", handleDragSelectMouseMove);
    window.addEventListener("mouseup", stopDragSelect);
    window.addEventListener("blur", stopDragSelect);
    return () => {
      window.removeEventListener("mousemove", handleDragSelectMouseMove);
      window.removeEventListener("mouseup", stopDragSelect);
      window.removeEventListener("blur", stopDragSelect);
      stopDragSelectAutoScroll();
      if (dragHoverTimerRef.current) {
        window.clearTimeout(dragHoverTimerRef.current);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);


  const isLoginAuthReady = email.trim().length > 0 && password.trim().length > 0;
  const isSignupAuthReady =
    lastName.trim().length > 0 &&
    firstName.trim().length > 0 &&
    email.trim().length > 0 &&
    password.trim().length > 0 &&
    Boolean(countryCode) &&
    acceptedPolicies;
  const canSubmitAuth = authMode === "login" ? isLoginAuthReady : isSignupAuthReady;

  const submitAuth = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (!canSubmitAuth) return;
    try {
      const path = authMode === "login" ? "/auth/login" : "/auth/signup";
      const body = authMode === "login"
        ? { email: email.trim(), password }
        : ({
            last_name: lastName.trim(),
            first_name: firstName.trim(),
            email: email.trim(),
            password,
            country_code: countryCode,
            accepted_terms: acceptedPolicies,
            accepted_privacy_policy: acceptedPolicies,
          } satisfies SignupPayload);
      const res = await api<AuthResponse>(path, { method: "POST", body: JSON.stringify(body) });
      setToken(res.access_token);
      setUserId(res.user_id);
      localStorage.setItem("phase1_token", res.access_token);
      localStorage.setItem("phase1_user_id", res.user_id);
      localStorage.setItem("phase1_email", email.trim());
      if (authMode === "signup") {
        localStorage.setItem("phase1_signup_display_name", `${lastName.trim()} ${firstName.trim()}`.trim());
        localStorage.setItem("phase1_country_code", countryCode);
      }
      setBreadcrumbRootView("home");
      setViewMode("home");
      setParentId(null);
    } catch (err: any) {
      setError(err.message || "認証に失敗しました。");
    }
  };

  const logout = () => {
    try {
      void desktopBridge?.stopBackup?.();
    } catch {
      // noop
    }
    localStorage.removeItem("phase1_token");
    localStorage.removeItem("phase1_user_id");
    localStorage.removeItem("phase1_email");
    localStorage.removeItem("phase1_backup_targets");
    localStorage.removeItem("phase1_offline_targets");
    setBackupTargets([]);
    setOfflineTargets([]);
    setAccountProfile(null);
    setAccountMenuOpen(false);
    setToken("");
    setUserId("");
    setItems([]);
    setBreadcrumbs([]);
    setSelected(null);
    setProviderSummary(null);
    setSyncSummary(null);
  };

  const accountDisplayName = useMemo(() => {
    const profileName = `${accountProfile?.last_name || ""} ${accountProfile?.first_name || ""}`.trim();
    if (profileName) return profileName;
    const storedName = localStorage.getItem("phase1_signup_display_name") || "";
    if (storedName.trim()) return storedName.trim();
    const safeEmail = (accountProfile?.email || email || "").trim();
    return safeEmail ? safeEmail.split("@")[0] : "ユーザー";
  }, [accountProfile?.last_name, accountProfile?.first_name, accountProfile?.email, email]);

  const accountEmail = (accountProfile?.email || email || "メールアドレス未設定").trim();
  const accountCountryCode = String(accountProfile?.country_code || countryCode || localStorage.getItem("phase1_country_code") || "JP").toUpperCase();
  const accountCountryName = countryLabel(accountCountryCode);
  const accountLanguageCode = currentLanguageCode;
  const accountLanguageName = getLanguageLabelByLanguage(accountLanguageCode, currentLanguageCode);

  const handleAccountLanguageChange = (nextLanguageCode: string) => {
    const normalizedCode = String(nextLanguageCode || "").trim().toLowerCase();
    if (!LANGUAGE_OPTIONS.some((entry) => entry.code === normalizedCode)) return;
    setLanguageCode(normalizedCode);
    localStorage.setItem("phase1_language_code", normalizedCode);
  };

  const handleAccountCountryChange = async (nextCountryCode: string) => {
    if (!token) return;
    const normalizedCode = String(nextCountryCode || "").trim().toUpperCase();
    if (!COUNTRY_OPTIONS.some((entry) => entry.code === normalizedCode)) {
      setError("選択できない国/地域です。");
      return;
    }
    if (normalizedCode === accountCountryCode) return;

    const previousCountryCode = accountCountryCode;
    setError("");
    setCountryCode(normalizedCode);
    setAccountProfile((prev) => prev ? { ...prev, country_code: normalizedCode } : prev);
    localStorage.setItem("phase1_country_code", normalizedCode);

    try {
      const updatedProfile = await api<UserProfile>("/auth/profile", {
        method: "PATCH",
        body: JSON.stringify({ country_code: normalizedCode }),
      }, token);
      setAccountProfile(updatedProfile);
      const savedCountryCode = String(updatedProfile.country_code || normalizedCode).toUpperCase();
      setCountryCode(savedCountryCode);
      localStorage.setItem("phase1_country_code", savedCountryCode);
    } catch (err: any) {
      setCountryCode(previousCountryCode);
      setAccountProfile((prev) => prev ? { ...prev, country_code: previousCountryCode } : prev);
      localStorage.setItem("phase1_country_code", previousCountryCode);
      setError(err?.message || "国/地域の更新に失敗しました。");
      void requestAppAlert(err?.message || "国/地域の更新に失敗しました。", "国/地域の更新");
    }
  };

  const calculateAccountMenuPosition = () => {
    if (typeof window === "undefined") return null;
    const anchor = accountButtonRef.current;
    if (!anchor) return null;

    const rect = anchor.getBoundingClientRect();
    const margin = 12;
    const gap = 12;
    const visualViewport = window.visualViewport;
    const viewportLeft = visualViewport?.offsetLeft ?? 0;
    const viewportTop = visualViewport?.offsetTop ?? 0;
    const viewportWidth = visualViewport?.width ?? document.documentElement.clientWidth ?? window.innerWidth;
    const viewportHeight = visualViewport?.height ?? document.documentElement.clientHeight ?? window.innerHeight;
    const viewportRight = viewportLeft + viewportWidth;
    const viewportBottom = viewportTop + viewportHeight;
    const preferredMenuWidth = 320;
    const menuWidth = Math.min(preferredMenuWidth, Math.max(240, viewportWidth - margin * 2));
    const estimatedMenuHeight = 440;

    // まずはアイコンの右端にメニュー右端を合わせる。
    // 右にはみ出す場合は左へ、左にはみ出す場合は画面端の余白まで戻す。
    let left = rect.right - menuWidth;
    left = Math.min(left, viewportRight - menuWidth - margin);
    left = Math.max(viewportLeft + margin, left);

    // まずはアイコンの下に出す。下にはみ出す場合だけ上側へ逃がす。
    const belowTop = rect.bottom + gap;
    const aboveTop = rect.top - estimatedMenuHeight - gap;
    const belowMaxHeight = viewportBottom - belowTop - margin;
    const aboveMaxHeight = rect.top - viewportTop - margin - gap;
    let top = belowTop;
    let maxHeight = belowMaxHeight;

    if (belowMaxHeight < estimatedMenuHeight && aboveMaxHeight > belowMaxHeight) {
      top = Math.max(viewportTop + margin, aboveTop);
      maxHeight = Math.min(estimatedMenuHeight, rect.top - top - gap);
    }

    top = Math.max(viewportTop + margin, Math.min(top, viewportBottom - margin - 160));
    maxHeight = Math.max(160, Math.min(maxHeight, viewportBottom - top - margin));

    return { left, top, width: menuWidth, maxHeight };
  };

  const updateAccountMenuPosition = () => {
    const nextPosition = calculateAccountMenuPosition();
    if (nextPosition) setAccountMenuPosition(nextPosition);
  };

  const calculateAccountSelectMenuPosition = (anchor: HTMLElement | null) => {
    if (typeof window === "undefined" || !anchor) return null;
    const rect = anchor.getBoundingClientRect();
    const margin = 12;
    const gap = 6;
    const desiredHeight = 224;
    const width = Math.max(220, Math.min(320, rect.width - 24));
    const left = Math.max(margin, Math.min(rect.left + 12, window.innerWidth - width - margin));
    const spaceBelow = window.innerHeight - rect.bottom - margin;
    const spaceAbove = rect.top - margin;

    let maxHeight = Math.min(desiredHeight, Math.max(120, spaceBelow - gap));
    let top = rect.bottom + gap;

    if (spaceBelow < 140 && spaceAbove > spaceBelow) {
      maxHeight = Math.min(desiredHeight, Math.max(120, spaceAbove - gap));
      top = Math.max(margin, rect.top - gap - maxHeight);
    }

    return { left, top, width, maxHeight };
  };

  const updateAccountSelectMenuPosition = () => {
    const anchor = accountCountrySelectOpen
      ? accountCountrySelectButtonRef.current
      : accountLanguageSelectOpen
        ? accountLanguageSelectButtonRef.current
        : null;
    const nextPosition = calculateAccountSelectMenuPosition(anchor);
    if (nextPosition) {
      setAccountSelectMenuPosition(nextPosition);
    } else {
      setAccountSelectMenuPosition(null);
    }
  };

  useLayoutEffect(() => {
    if (!accountMenuOpen) {
      setAccountMenuPosition(null);
      setAccountCountrySelectOpen(false);
      setAccountLanguageSelectOpen(false);
      setAccountSelectMenuPosition(null);
      return;
    }
    updateAccountMenuPosition();
  }, [accountMenuOpen]);

  useLayoutEffect(() => {
    if (!accountMenuOpen || (!accountCountrySelectOpen && !accountLanguageSelectOpen)) {
      setAccountSelectMenuPosition(null);
      return;
    }
    updateAccountSelectMenuPosition();
  }, [accountMenuOpen, accountCountrySelectOpen, accountLanguageSelectOpen, accountMenuPosition]);

  useEffect(() => {
    if (!accountMenuOpen) return;
    window.addEventListener("resize", updateAccountMenuPosition);
    window.addEventListener("scroll", updateAccountMenuPosition, true);
    return () => {
      window.removeEventListener("resize", updateAccountMenuPosition);
      window.removeEventListener("scroll", updateAccountMenuPosition, true);
    };
  }, [accountMenuOpen]);

  useEffect(() => {
    if (!accountMenuOpen || (!accountCountrySelectOpen && !accountLanguageSelectOpen)) return;
    window.addEventListener("resize", updateAccountSelectMenuPosition);
    window.addEventListener("scroll", updateAccountSelectMenuPosition, true);
    return () => {
      window.removeEventListener("resize", updateAccountSelectMenuPosition);
      window.removeEventListener("scroll", updateAccountSelectMenuPosition, true);
    };
  }, [accountMenuOpen, accountCountrySelectOpen, accountLanguageSelectOpen]);

  const handleAvatarImageChange = (files: FileList | null) => {
    const file = files?.[0];
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      void requestAppAlert("画像ファイルを選択してください。", "アカウント画像");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const value = String(reader.result || "");
      setAvatarDataUrl(value);
      try {
        localStorage.setItem(accountAvatarStorageKey, value);
      } catch {
        // noop
      }
    };
    reader.readAsDataURL(file);
  };

  const renderAccountAvatar = (sizeClass: string, iconClass: string) => (
    avatarDataUrl
      ? <img src={avatarDataUrl} alt="アカウント画像" className={`${sizeClass} rounded-full object-cover`} />
      : <UserCircle2 className={iconClass} />
  );

  const renderAccountMenu = () => {
    if (!accountMenuOpen || typeof document === "undefined" || !accountMenuPosition) return null;
    return createPortal(
      <div
        className="fixed isolate overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-2xl"
        style={{
          left: accountMenuPosition.left,
          top: accountMenuPosition.top,
          width: accountMenuPosition.width,
          maxHeight: accountMenuPosition.maxHeight,
          overflowY: "auto",
          overflowX: "hidden",
          zIndex: 2147483647,
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="border-b border-slate-100 px-5 py-5 text-center">
          <button
            type="button"
            onClick={() => avatarUploadRef.current?.click()}
            className="mx-auto grid h-20 w-20 place-items-center overflow-hidden rounded-full bg-slate-100 text-slate-500 ring-4 ring-slate-50 hover:bg-slate-200"
            title={tx("プロフィール画像を変更")}
          >
            {renderAccountAvatar("h-20 w-20", "h-16 w-16 text-slate-400")}
          </button>
          <div className="mt-3 truncate text-base font-semibold text-slate-900">{accountDisplayName}</div>
          <div className="mt-1 truncate text-xs text-slate-500">{accountEmail}</div>
        </div>
        <div className="p-2 text-sm">
          <button
            type="button"
            onClick={() => void requestAppAlert("設定画面はテスト版ではアカウントメニューに統合しています。", "設定")}
            className="flex w-full items-center justify-between rounded-2xl px-3 py-2.5 text-left hover:bg-slate-50"
          >
            <span className="inline-flex items-center gap-3 text-slate-500">
              <Settings className="h-4 w-4" />
              {tx("設定")}
            </span>
            <span className="text-xs text-slate-400">{tx("テスト版")}</span>
          </button>
          <div
            className="rounded-2xl text-slate-500 hover:bg-slate-50"
            title={tx("国/地域")}
            aria-label={`${tx("国/地域")}: ${accountCountryName}`}
          >
            <button
              ref={accountCountrySelectButtonRef}
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                const shouldOpen = !accountCountrySelectOpen;
                setAccountLanguageSelectOpen(false);
                setAccountCountrySelectOpen(shouldOpen);
                const nextPosition = shouldOpen ? calculateAccountSelectMenuPosition(e.currentTarget) : null;
                setAccountSelectMenuPosition(nextPosition);
              }}
              className="flex w-full items-center gap-3 rounded-2xl px-3 py-2.5 text-left"
              aria-expanded={accountCountrySelectOpen}
              aria-haspopup="listbox"
            >
              <MapPinned className="h-4 w-4 shrink-0" />
              <span className="min-w-0 flex-1 truncate text-sm text-slate-500">{accountCountryName}</span>
              <ChevronRight className={`h-4 w-4 shrink-0 text-slate-400 transition-transform ${accountCountrySelectOpen ? "rotate-90" : ""}`} />
            </button>
          </div>
          <div
            className="rounded-2xl text-slate-500 hover:bg-slate-50"
            title={tx("言語")}
            aria-label={`${tx("言語")}: ${accountLanguageName}`}
          >
            <button
              ref={accountLanguageSelectButtonRef}
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                const shouldOpen = !accountLanguageSelectOpen;
                setAccountCountrySelectOpen(false);
                setAccountLanguageSelectOpen(shouldOpen);
                const nextPosition = shouldOpen ? calculateAccountSelectMenuPosition(e.currentTarget) : null;
                setAccountSelectMenuPosition(nextPosition);
              }}
              className="flex w-full items-center gap-3 rounded-2xl px-3 py-2.5 text-left"
              aria-expanded={accountLanguageSelectOpen}
              aria-haspopup="listbox"
            >
              <Globe className="h-4 w-4 shrink-0" />
              <span className="min-w-0 flex-1 truncate text-sm text-slate-500">{accountLanguageName}</span>
              <ChevronRight className={`h-4 w-4 shrink-0 text-slate-400 transition-transform ${accountLanguageSelectOpen ? "rotate-90" : ""}`} />
            </button>
          </div>
          <button
            type="button"
            onClick={logout}
            className="mt-1 flex w-full items-center gap-3 rounded-2xl px-3 py-2.5 text-left text-rose-600 hover:bg-rose-50"
          >
            <LogOut className="h-4 w-4" />
            <span className="font-medium">{tx("ログアウト")}</span>
          </button>
        </div>
      </div>,
      document.body,
    );
  };

  const renderAccountSelectMenu = () => {
    if (!accountMenuOpen || typeof document === "undefined" || !accountSelectMenuPosition) return null;
    if (!accountCountrySelectOpen && !accountLanguageSelectOpen) return null;

    const isCountryMenu = accountCountrySelectOpen;
    const options = isCountryMenu ? localizedCountryOptions : localizedLanguageOptions;
    const selectedCode = isCountryMenu ? accountCountryCode : accountLanguageCode;
    const label = isCountryMenu ? tx("国/地域を選択") : tx("言語を選択");

    return createPortal(
      <div
        className="fixed overflow-y-auto rounded-2xl border border-slate-100 bg-white py-1 shadow-2xl"
        role="listbox"
        aria-label={label}
        style={{
          left: accountSelectMenuPosition.left,
          top: accountSelectMenuPosition.top,
          width: accountSelectMenuPosition.width,
          maxHeight: accountSelectMenuPosition.maxHeight,
          zIndex: 2147483647,
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {options.map((option) => {
          const isSelected = option.code === selectedCode;
          return (
            <button
              key={option.code}
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                setAccountCountrySelectOpen(false);
                setAccountLanguageSelectOpen(false);
                setAccountSelectMenuPosition(null);
                if (isCountryMenu) {
                  void handleAccountCountryChange(option.code);
                } else {
                  handleAccountLanguageChange(option.code);
                }
              }}
              className={`flex w-full items-center px-3 py-2 text-left text-sm ${isSelected ? "bg-sky-50 text-sky-700" : "text-slate-600 hover:bg-slate-50"}`}
              role="option"
              aria-selected={isSelected}
            >
              <span className="truncate">{option.label}</span>
            </button>
          );
        })}
      </div>,
      document.body,
    );
  };

  const renderAccountButton = () => (
    <div ref={accountButtonRef} className="relative">
      <button
        type="button"
        aria-label={tx("アカウント")}
        onClick={(e) => {
          e.stopPropagation();
          setUploadMenuOpen(false);
          setContextMenu(null);
          const nextOpen = !accountMenuOpen;
          if (nextOpen) {
            const nextPosition = calculateAccountMenuPosition();
            if (nextPosition) setAccountMenuPosition(nextPosition);
          } else {
            setAccountMenuPosition(null);
            setAccountCountrySelectOpen(false);
            setAccountLanguageSelectOpen(false);
            setAccountSelectMenuPosition(null);
          }
          setAccountMenuOpen(nextOpen);
        }}
        className="grid h-11 w-11 place-items-center overflow-hidden rounded-full bg-transparent text-slate-600 hover:bg-transparent hover:text-slate-700"
      >
        {renderAccountAvatar("h-8 w-8", "h-8 w-8 text-slate-600")}
      </button>
      {renderAccountMenu()}
      {renderAccountSelectMenu()}
    </div>
  );

  const clearSelection = () => {
    setSelectedIds([]);
    setSelectionAnchorId(null);
    setSelected(null);
  };

  const handleWorkspaceBlankClick = (event: React.MouseEvent) => {
    const target = event.target as HTMLElement;
    if (!selectedIds.length) return;
    if (target.closest('[data-selectable-row="true"]')) return;
    if (target.closest('[data-preserve-selection="true"]')) return;
    clearSelection();
  };

  const selectSingleItem = (item: Item) => {
    setSelectedIds([item.item_id]);
    setSelectionAnchorId(item.item_id);
    setSelected(item);
  };

  const handleRowSelection = (item: Item, event: React.MouseEvent) => {
    const isToggle = event.metaKey || event.ctrlKey;
    const isRange = event.shiftKey;
    const currentIndex = sortedItems.findIndex((entry) => entry.item_id === item.item_id);

    if (isRange && selectionAnchorId) {
      const anchorIndex = sortedItems.findIndex((entry) => entry.item_id === selectionAnchorId);
      if (anchorIndex >= 0 && currentIndex >= 0) {
        const [start, end] = anchorIndex < currentIndex ? [anchorIndex, currentIndex] : [currentIndex, anchorIndex];
        setSelectedIds(sortedItems.slice(start, end + 1).map((entry) => entry.item_id));
        setSelected(item);
        return;
      }
    }

    if (isToggle) {
      setSelectedIds((prev) => {
        if (prev.includes(item.item_id)) return prev.filter((id) => id !== item.item_id);
        return [...prev, item.item_id];
      });
      setSelectionAnchorId(item.item_id);
      setSelected(item);
      return;
    }

    selectSingleItem(item);
  };

  const updateDragSelectedRange = (item: Item) => {
    if (!dragSelectActiveRef.current || !dragSelectAnchorRef.current) return;
    const sourceItems = sortedItemsRef.current.length ? sortedItemsRef.current : sortedItems;
    const anchorIndex = sourceItems.findIndex((entry) => entry.item_id === dragSelectAnchorRef.current);
    const currentIndex = sourceItems.findIndex((entry) => entry.item_id === item.item_id);
    if (anchorIndex < 0 || currentIndex < 0) return;
    const [start, end] = anchorIndex < currentIndex ? [anchorIndex, currentIndex] : [currentIndex, anchorIndex];
    const nextIds = sourceItems.slice(start, end + 1).map((entry) => entry.item_id);
    dragSelectMovedRef.current = currentIndex !== anchorIndex;
    setSelectedIds(nextIds);
    setSelected(item);
  };

  const clearDragHover = () => {
    if (dragHoverTimerRef.current) {
      window.clearTimeout(dragHoverTimerRef.current);
      dragHoverTimerRef.current = null;
    }
    setHoverFolderId(null);
  };

  const beginFolderHover = (folder: Item) => {
    if (dragHoverTimerRef.current) {
      window.clearTimeout(dragHoverTimerRef.current);
      dragHoverTimerRef.current = null;
    }
    setHoverFolderId(folder.item_id);
  };

  const getActiveItemIds = (itemId?: string) => {
    if (itemId && selectedIds.includes(itemId) && selectedIds.length > 0) return [...selectedIds];
    if (itemId) return [itemId];
    return [...selectedIds];
  };

  const readDraggedItemIds = (dataTransfer?: DataTransfer | null) => {
    const fromState = draggingItemIds.filter(Boolean);
    if (fromState.length) return fromState;

    if (!dataTransfer) return [];

    const rawCustom = dataTransfer.getData(TRI_CLOUD_ITEM_DRAG_MIME);
    if (rawCustom) {
      try {
        const parsed = JSON.parse(rawCustom);
        if (Array.isArray(parsed)) return parsed.map((id) => String(id)).filter(Boolean);
      } catch {
        // text/plain fallback below
      }
    }

    const rawPlain = dataTransfer.getData("text/plain");
    if (!rawPlain || !rawPlain.startsWith(TRI_CLOUD_ITEM_TEXT_PREFIX)) return [];
    return rawPlain.slice(TRI_CLOUD_ITEM_TEXT_PREFIX.length).split(",").map((id) => id.trim()).filter(Boolean);
  };

  const moveDraggedItemsToFolder = async (dataTransfer: DataTransfer, destinationFolder: Item) => {
    const activeIds = readDraggedItemIds(dataTransfer)
      .filter((id, index, arr) => id && arr.indexOf(id) === index)
      .filter((id) => id !== destinationFolder.item_id);

    if (!activeIds.length) {
      setDraggingItemIds([]);
      clearDragHover();
      return;
    }

    await moveItemIdsToParent(activeIds, destinationFolder.item_id);
  };

  const moveItemIdsToParent = async (itemIds: string[], destinationParentId: string | null) => {
    if (!itemIds.length) return;
    setLoading(true);
    setError("");
    try {
      for (const itemId of itemIds) {
        await api(`/items/${itemId}/move`, {
          method: "POST",
          body: JSON.stringify({ parent_id: destinationParentId }),
        }, token);
      }
      clearSelection();
      await refresh(viewMode, parentId, activeQuery);
    } catch (err: any) {
      setError(err.message || tx("移動に失敗しました。"));
    } finally {
      setLoading(false);
      setDraggingItemIds([]);
      clearDragHover();
    }
  };

  const trashMultipleItems = async (itemIds: string[]) => {
    if (!itemIds.length) return;
    const confirmed = await requestAppConfirm({
      title: tx("削除の確認"),
      message: tx("{count}件をごみ箱へ移動しますか？", { count: itemIds.length }),
      confirmLabel: tx("ごみ箱へ移動"),
      cancelLabel: tx("キャンセル"),
      variant: "danger",
    });
    if (!confirmed) return;
    const targetItems = sortedItems.filter((item) => itemIds.includes(item.item_id));
    setLoading(true);
    setError("");
    try {
      for (const itemId of itemIds) {
        await api(`/items/${itemId}/trash`, { method: "POST" }, token);
      }
      const ownedTargetItems = targetItems.filter((item) => String(item.owner_user_id || "") === userId);
      if (ownedTargetItems.length) {
        await removeBackupTargetsForItems(ownedTargetItems);
      }
      clearSelection();
      await refresh(viewMode, parentId, activeQuery);
    } catch (err: any) {
      setError(err.message || tx("ごみ箱移動に失敗しました。"));
    } finally {
      setLoading(false);
    }
  };

  const restoreMultipleItems = async (itemIds: string[]) => {
    if (!itemIds.length) return;
    setLoading(true);
    setError("");
    try {
      for (const itemId of itemIds) {
        await api(`/items/${itemId}/restore`, { method: "POST" }, token);
      }
      clearSelection();
      await refresh("trash", null, "");
    } catch (err: any) {
      setError(err.message || tx("復元に失敗しました。"));
    } finally {
      setLoading(false);
    }
  };

  const purgeMultipleItems = async (itemIds: string[]) => {
    if (!itemIds.length) return;
    const confirmed = await requestAppConfirm({
      title: tx("完全削除の確認"),
      message: tx("{count}件を完全削除しますか？この操作は元に戻せません。", { count: itemIds.length }),
      confirmLabel: tx("完全削除"),
      cancelLabel: tx("キャンセル"),
      variant: "danger",
    });
    if (!confirmed) return;
    const targetItems = sortedItems.filter((item) => itemIds.includes(item.item_id));
    setLoading(true);
    setError("");
    try {
      for (const itemId of itemIds) {
        await api(`/items/${itemId}/purge_permanent`, { method: "DELETE" }, token);
      }
      const ownedTargetItems = targetItems.filter((item) => String(item.owner_user_id || "") === userId);
      if (ownedTargetItems.length) {
        await removeBackupTargetsForItems(ownedTargetItems);
      }
      clearSelection();
      await refresh("trash", null, "");
    } catch (err: any) {
      setError(err.message || tx("完全削除に失敗しました。"));
    } finally {
      setLoading(false);
    }
  };

  const purgeAllTrashItems = async () => {
    const trashItems = sortedItems.filter((item) => item?.item_id);
    if (!trashItems.length) {
      await requestAppAlert(tx("ごみ箱は空です。"));
      return;
    }

    const confirmed = await requestAppConfirm({
      title: tx("すべて削除の確認"),
      message: tx("本当にごみ箱内のファイルやフォルダを全て削除しますか？この操作は元に戻せません。"),
      confirmLabel: tx("すべて削除"),
      cancelLabel: tx("キャンセル"),
      variant: "danger",
    });
    if (!confirmed) return;

    const itemIds = trashItems.map((item) => item.item_id);
    setLoading(true);
    setError("");
    try {
      for (const itemId of itemIds) {
        await api(`/items/${itemId}/purge_permanent`, { method: "DELETE" }, token);
      }
      const ownedTrashItems = trashItems.filter((item) => String(item.owner_user_id || "") === userId);
      if (ownedTrashItems.length) {
        await removeBackupTargetsForItems(ownedTrashItems);
      }
      clearSelection();
      await refresh("trash", null, "");
    } catch (err: any) {
      setError(err.message || tx("ごみ箱内のすべて削除に失敗しました。"));
    } finally {
      setLoading(false);
    }
  };

  const markItemOpened = async (itemId: string) => {
    try {
      await api(`/items/${itemId}/open`, { method: "POST" }, token);
    } catch {
      // 最近記録の失敗は操作を止めない
    }
  };

  const openFolder = async (item: Item | null) => {
    const nextParent = item?.item_id ?? null;
    if (item) {
      await markItemOpened(item.item_id);
    }
    clearSelection();
    setRecentButtonActive(false);

    const isAlreadyInBreadcrumbContext = Boolean(parentId) || breadcrumbs.length > 0;
    const currentRootView = isAlreadyInBreadcrumbContext
      ? breadcrumbRootView
      : isBreadcrumbRootCandidate(viewMode)
        ? viewMode
        : breadcrumbRootView;
    const itemOwnerUserId = item ? String(item.owner_user_id || "") : "";
    const itemLooksShared = Boolean(itemOwnerUserId) && itemOwnerUserId !== userId;
    const nextView: ViewMode =
      currentRootView === "sync" ? "sync" :
      currentRootView === "shared" || itemLooksShared ? "shared" :
      "folders";
    const nextRootView: ViewMode =
      nextView === "shared" ? "shared" :
      currentRootView === "home" ? "home" :
      nextView === "sync" ? "sync" :
      "folders";

    setBreadcrumbRootView(nextRootView);
    setViewMode(nextView);
    setParentId(nextParent);
    await refresh(nextView, nextParent, "");
  };

  const openBreadcrumbRoot = async () => {
    const rootView = breadcrumbRootView;
    clearSelection();
    setRecentButtonActive(false);

    if (rootView === "trash") setSortKey("trashed_at");
    if (rootView !== "trash" && sortKey === "trashed_at") setSortKey("updated_at");
    if (rootView === "home") setSearchScope("home");
    if (rootView === "folders") setSearchScope("owned");
    if (rootView === "shared") setSearchScope("shared");

    setViewMode(rootView);
    setParentId(null);
    setBreadcrumbRootView(rootView);
    await refresh(rootView, null, "");
  };

  const openBreadcrumbFolder = async (item: Item | null) => {
    const nextParent = item?.item_id ?? null;
    if (item) {
      await markItemOpened(item.item_id);
    }
    clearSelection();
    setRecentButtonActive(false);

    // パンくずリストの項目には owner_user_id が含まれない場合があるため、
    // openFolder と同じ所有者判定を使わず、パンくずの先頭に表示している
    // ルート機能（ホーム / フォルダ / 共有アイテム / バックアップ設定）を基準に API を選ぶ。
    const nextView = getFolderApiViewForBreadcrumbRoot(breadcrumbRootView);

    setViewMode(nextView);
    setParentId(nextParent);
    await refresh(nextView, nextParent, "");
  };

  const openProviderView = async () => {
    clearSelection();
    setRecentButtonActive(false);
    setViewMode("provider");
    await refresh("provider", null, "");
  };

  const openSyncView = async () => {
    clearSelection();
    setRecentButtonActive(false);
    setBreadcrumbRootView("sync");
    setViewMode("sync");
    setParentId(null);
    await refresh("sync", null, "");
  };

  const openTrashView = async () => {
    clearSelection();
    setRecentButtonActive(false);
    setBreadcrumbRootView("trash");
    setViewMode("trash");
    setParentId(null);
    setSortKey("trashed_at");
    setSortDir("desc");
    await refresh("trash", null, "");
  };

  const runSearch = async (e?: React.FormEvent) => {
    e?.preventDefault();
    const trimmedQuery = query.trim();
    if (!trimmedQuery) return;

    clearSelection();
    setRecentButtonActive(false);
    const scope = viewMode === "shared" ? "shared" : viewMode === "recent" ? "recent" : viewMode === "folders" ? "owned" : searchScope;
    setSearchScope(scope);
    setActiveQuery(trimmedQuery);
    setViewMode("search");
    await refresh("search", null, trimmedQuery);
  };

  const createFolder = async () => {
    const name = await requestTextInput({
      title: tx("新しいフォルダ"),
      message: tx("新しいフォルダ名を入力してください"),
      placeholder: tx("フォルダ名"),
      confirmLabel: tx("作成"),
    });
    if (!name?.trim()) return;
    const { targetView, targetParentId } = getCreateUploadTarget();
    setCreatingFolder(true);
    setError("");
    try {
      await api("/items/folder", { method: "POST", body: JSON.stringify({ name: name.trim(), parent_id: targetParentId }) }, token);
      await refreshAfterCreateUpload(targetView, targetParentId);
    } catch (err: any) {
      setError(err.message || tx("フォルダ作成に失敗しました。"));
    } finally {
      setCreatingFolder(false);
    }
  };

  const renameItem = async (item: Item) => {
    const name = await requestTextInput({
      title: tx("名前を変更"),
      message: tx("新しい名前を入力してください"),
      defaultValue: item.name,
      placeholder: tx("新しい名前"),
      confirmLabel: tx("変更"),
    });
    if (!name || name === item.name) return;
    try {
      await api(`/items/${item.item_id}`, { method: "PATCH", body: JSON.stringify({ name }) }, token);
      await refresh();
    } catch (err: any) {
      setError(err.message || tx("名前変更に失敗しました。"));
    }
  };

  const trashItem = async (item: Item) => {
    const confirmed = await requestAppConfirm({
      title: tx("削除の確認"),
      message: tx("「{name}」をごみ箱へ移動しますか？", { name: item.name }),
      confirmLabel: tx("ごみ箱へ移動"),
      cancelLabel: tx("キャンセル"),
      variant: "danger",
    });
    if (!confirmed) return;
    try {
      await api(`/items/${item.item_id}/trash`, { method: "POST" }, token);
      if (String(item.owner_user_id || "") === userId) {
        await removeBackupTargetsForItems([item]);
      }
      await refresh();
    } catch (err: any) {
      setError(err.message || tx("ごみ箱移動に失敗しました。"));
    }
  };

  const restoreItem = async (item: Item) => {
    try {
      await api(`/items/${item.item_id}/restore`, { method: "POST" }, token);
      await refresh("trash", null, "");
    } catch (err: any) {
      setError(err.message || tx("復元に失敗しました。"));
    }
  };

  const purgeItem = async (item: Item) => {
    const confirmed = await requestAppConfirm({
      title: tx("完全削除の確認"),
      message: tx("「{name}」を完全削除しますか？この操作は元に戻せません。", { name: item.name }),
      confirmLabel: tx("完全削除"),
      cancelLabel: tx("キャンセル"),
      variant: "danger",
    });
    if (!confirmed) return;
    try {
      await api(`/items/${item.item_id}/purge_permanent`, { method: "DELETE" }, token);
      if (String(item.owner_user_id || "") === userId) {
        await removeBackupTargetsForItems([item]);
      }
      await refresh("trash", null, "");
    } catch (err: any) {
      setError(err.message || tx("完全削除に失敗しました。"));
    }
  };

  const copyShareLink = async (item: Item) => {
    try {
      const res = await api<ShareResponse>("/share/create", { method: "POST", body: JSON.stringify({ item_id: item.item_id, role: "viewer" }) }, token);
      await navigator.clipboard.writeText(res.share_id);
      await requestAppAlert(tx("リンクをコピーしました: {shareId}", { shareId: res.share_id }));
    } catch (err: any) {
      setError(err.message || tx("リンクのコピーに失敗しました。"));
    }
  };

  const copyShareLinks = async (itemIds: string[]) => {
    if (!itemIds.length) return;
    setLoading(true);
    setError("");
    try {
      const shareIds: string[] = [];
      for (const itemId of itemIds) {
        const res = await api<ShareResponse>("/share/create", { method: "POST", body: JSON.stringify({ item_id: itemId, role: "viewer" }) }, token);
        shareIds.push(res.share_id);
      }
      await navigator.clipboard.writeText(shareIds.join("\n"));
      await requestAppAlert(tx("{count}件分のリンクを改行区切りでコピーしました。", { count: itemIds.length }));
    } catch (err: any) {
      setError(err.message || tx("リンクのコピーに失敗しました。"));
    } finally {
      setLoading(false);
    }
  };

  const normalizeShareRecipientEmail = (value: string) => value.trim().toLowerCase();

  const isValidShareRecipientEmail = (value: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);

  const splitShareRecipientCandidates = (value: string) => value
    .split(/[\n,;]+/)
    .map((entry) => normalizeShareRecipientEmail(entry))
    .filter(Boolean);

  const addShareRecipients = (rawValue?: string) => {
    const candidates = splitShareRecipientCandidates(rawValue ?? shareRecipientInput);
    if (!candidates.length) return false;

    const invalid = candidates.find((candidate) => !isValidShareRecipientEmail(candidate));
    if (invalid) {
      setError("メールアドレスの形式が正しくありません。");
      return false;
    }

    setShareRecipientEmails((prev) => {
      const next = [...prev];
      for (const candidate of candidates) {
        if (!next.includes(candidate)) next.push(candidate);
      }
      return next;
    });
    setShareRecipientInput("");
    setError("");
    return true;
  };

  const removeShareRecipient = (emailToRemove: string) => {
    setShareRecipientEmails((prev) => prev.filter((email) => email !== emailToRemove));
  };

  const handleShareRecipientInputKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter" || event.key === "," || event.key === ";") {
      event.preventDefault();
      addShareRecipients();
      return;
    }
    if (event.key === "Backspace" && !shareRecipientInput.trim() && shareRecipientEmails.length) {
      event.preventDefault();
      setShareRecipientEmails((prev) => prev.slice(0, -1));
    }
  };

  const openShareDialogForItems = (itemsToShare: Item[]) => {
    const owned = itemsToShare.filter((item) => String(item.owner_user_id || "") === userId);
    if (!owned.length) return;
    setShareDialogItems(owned);
    setShareRecipientInput("");
    setShareRecipientEmails([]);
    setShareMessage("");
    setContextMenu(null);
    setError("");
  };

  const closeShareDialog = () => {
    if (shareSending) return;
    setShareDialogItems([]);
    setShareRecipientInput("");
    setShareRecipientEmails([]);
    setShareMessage("");
  };

  const sendShareToRecipient = async () => {
    if (!shareDialogItems.length) return;

    const pendingEmail = shareRecipientInput.trim();
    if (pendingEmail) {
      const added = addShareRecipients(pendingEmail);
      if (!added) return;
    }

    const recipientEmails = Array.from(new Set(shareRecipientEmails.concat(splitShareRecipientCandidates(pendingEmail))));
    if (!recipientEmails.length) {
      setError("共有先のメールアドレスを入力してください。");
      return;
    }

    const message = shareMessage.trim();

    setShareSending(true);
    setError("");
    try {
      for (const item of shareDialogItems) {
        for (const recipientEmail of recipientEmails) {
          await api<ShareSendResponse>("/share/send_by_email", {
            method: "POST",
            body: JSON.stringify({ item_id: item.item_id, recipient_email: recipientEmail, role: "viewer", message }),
          }, token);
        }
      }
      await requestAppAlert(`${shareDialogItems.length}件を ${recipientEmails.join("、")} の共有アイテムへ送信しました。`);
      setShareDialogItems([]);
      setShareRecipientInput("");
      setShareRecipientEmails([]);
      setShareMessage("");
    } catch (err: any) {
      setError(err.message || "共有の送信に失敗しました。");
    } finally {
      setShareSending(false);
    }
  };

  const saveBlobToBrowserDownloads = async (blob: Blob, fileName: string) => {
    const objectUrl = URL.createObjectURL(blob);
    try {
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = fileName || "download.bin";
      a.style.display = "none";
      document.body.appendChild(a);
      a.click();
      a.remove();
    } finally {
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 10_000);
    }
  };

  const downloadItem = async (item: Item) => {
    try {
      if (item.type !== "file") {
        setError("フォルダのダウンロードはまだ未対応です。");
        return;
      }

      await markItemOpened(item.item_id);
      const tokenPath = String(item.owner_user_id || "") === userId
        ? `/items/${item.item_id}/download_token`
        : `/library/items/${item.item_id}/download_token`;
      const tokenRes = await api<DownloadTokenResponse>(tokenPath, { method: "POST" }, token);

      if (desktopBridge?.downloadFileToDownloads) {
        const result = await desktopBridge.downloadFileToDownloads({
          api_base: API_BASE,
          download_token: tokenRes.download_token,
          access_token: token,
          file_name: item.name || "download.bin",
        });
        if (!result?.ok || !result.local_path) {
          throw new Error(result?.error || result?.message || "ダウンロード保存に失敗しました。");
        }
        await requestAppAlert(`ダウンロードしました: ${result.local_path}`);
        return;
      }

      const response = await fetch(`${API_BASE}/ui/download`, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          download_token: tokenRes.download_token,
          file_name: item.name || "download.bin",
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
        throw new Error(detail || "ダウンロードに失敗しました。");
      }
      const blob = await response.blob();
      await saveBlobToBrowserDownloads(blob, item.name || "download.bin");
    } catch (err: any) {
      setError(err.message || "ダウンロードに失敗しました。");
    }
  };


  const openItem = async (item: Item) => {
    try {
      if (viewMode === "trash") {
        setError("ごみ箱内のファイルは開けません。復元してから開いてください。");
        return;
      }
      if (item.type === "folder") {
        openFolder(item);
        return;
      }
      if (!desktopBridge?.openCloudFile) {
        setError("デスクトップアプリ側のファイルを開くブリッジが未接続です。");
        return;
      }

      setLoading(true);
      setError("");
      await markItemOpened(item.item_id);
      const tokenPath = String(item.owner_user_id || "") === userId
        ? `/items/${item.item_id}/download_token`
        : `/library/items/${item.item_id}/download_token`;
      const tokenRes = await api<DownloadTokenResponse>(tokenPath, { method: "POST" }, token);
      const result = await desktopBridge.openCloudFile({
        api_base: API_BASE,
        download_token: tokenRes.download_token,
        access_token: token,
        file_name: item.name || "download.bin",
      });
      if (!result?.ok) {
        throw new Error(result?.error || result?.message || "ファイルを開けませんでした。");
      }
    } catch (err: any) {
      setError(err?.message || String(err) || "ファイルを開けませんでした。");
    } finally {
      setLoading(false);
    }
  };

  const downloadMultipleItems = async (itemIds: string[]) => {
    if (!itemIds.length) return;
    const targetItems = sortedItems.filter((item) => itemIds.includes(item.item_id));
    const fileItems = targetItems.filter((item) => item.type === "file");
    const folderCount = targetItems.length - fileItems.length;
    if (!fileItems.length && folderCount > 0) {
      await requestAppAlert("フォルダの一括ダウンロードはまだ未対応です。今回はファイルのみダウンロードできます。");
      return;
    }
    for (const item of fileItems) {
      await downloadItem(item);
    }
    if (folderCount > 0) {
      await requestAppAlert(`ファイル ${fileItems.length}件をダウンロードしました。フォルダ ${folderCount}件は一括ダウンロード未対応のためスキップしました。`);
    }
  };

  const copyItems = async (itemIds: string[]) => {
    if (!itemIds.length) return;
    setLoading(true);
    setError("");
    try {
      await api<{ items: Item[]; count: number }>("/items/copy_batch", {
        method: "POST",
        body: JSON.stringify({ item_ids: itemIds }),
      }, token);
      clearSelection();
      await refresh(viewMode, parentId, activeQuery);
    } catch (err: any) {
      setError(err.message || "コピーに失敗しました。");
    } finally {
      setLoading(false);
    }
  };

  const openVersions = async (item: Item) => {
    if (item.type !== "file") return;
    setVersionsOpen(true);
    setVersionsLoading(true);
    setError("");
    try {
      const res = await api<VersionResponse>(`/items/${item.item_id}/versions`, {}, token);
      setVersionData(res);
    } catch (err: any) {
      setError(err.message || "版履歴の読み込みに失敗しました。");
    } finally {
      setVersionsLoading(false);
    }
  };

  const restoreVersion = async (itemId: string, versionId: string) => {
    try {
      await api(`/items/${itemId}/versions/${versionId}/restore`, { method: "POST" }, token);
      await refresh();
      const res = await api<VersionResponse>(`/items/${itemId}/versions`, {}, token);
      setVersionData(res);
    } catch (err: any) {
      setError(err.message || "版の復元に失敗しました。");
    }
  };

  const takeUploadIntent = (): "normal" | "backup" => {
    const explicit = uploadIntentRef.current;
    uploadIntentRef.current = null;
    if (explicit) return explicit;
    return viewMode === "sync" ? "backup" : "normal";
  };

  const handleFileUpload = async (files: FileList | null) => {
    const intent = takeUploadIntent();
    if (!files?.length) return;
    const fileArray = Array.from(files);
    const localPaths = await getLocalFilePaths(fileArray);
    const candidates: UploadCandidate[] = fileArray.map((file, index) => ({
      file,
      relativePath: file.name,
      absolutePath: localPaths[index] || String((file as any)?.path || ""),
    }));
    await uploadDroppedCandidates(candidates, undefined, { intent });
    if (fileUploadRef.current) fileUploadRef.current.value = "";
  };

  const handleFolderUpload = async (files: FileList | null) => {
    const intent = takeUploadIntent();
    if (!files?.length) return;
    const fileArray = Array.from(files);
    const localPaths = await getLocalFilePaths(fileArray);
    const candidates: UploadCandidate[] = fileArray.map((file, index) => ({
      file,
      relativePath: (file as any).webkitRelativePath || file.name,
      absolutePath: localPaths[index] || String((file as any)?.path || ""),
    }));
    await uploadDroppedCandidates(candidates, undefined, { intent });
    if (folderUploadRef.current) folderUploadRef.current.value = "";
  };

  const openUploadFiles = () => {
    setUploadMenuOpen(false);
    uploadIntentRef.current = viewMode === "sync" ? "backup" : "normal";
    fileUploadRef.current?.click();
  };

  const uploadBackupFolderFromDialog = async () => {
    if (!token) return;
    if (!desktopBridge?.uploadBackupFolderFromDialog) {
      folderUploadRef.current?.click();
      return;
    }

    setLoading(true);
    setError("");
    try {
      const result = await desktopBridge.uploadBackupFolderFromDialog({
        api_base: API_BASE,
        access_token: token,
        email: email.trim(),
        polling_interval_sec: syncIntervalSec,
        ignore_hidden: syncIgnoreHidden,
        local_root_display: summarizeBackupTargets(backupTargets),
      }) as BackupFolderUploadResponse;

      if (!result?.ok) {
        if (!result?.cancelled) {
          throw new Error(result?.error || result?.message || "フォルダのアップロードに失敗しました。");
        }
        return;
      }
      if (!result.target) {
        throw new Error("バックアップ対象フォルダの情報を取得できませんでした。");
      }

      const nextTargets = mergeBackupTargets(readStoredBackupTargets(), normalizeBackupSettingTarget(result.target));
      replaceBackupTargets(nextTargets, false);
      setRecentButtonActive(false);
      setBreadcrumbRootView("sync");
      setViewMode("sync");
      setParentId(null);
      clearSelection();
      await refresh("sync", null, "");
    } catch (err: any) {
      setError(err?.message || "フォルダのアップロードに失敗しました。");
    } finally {
      setLoading(false);
      if (folderUploadRef.current) folderUploadRef.current.value = "";
    }
  };

  const openUploadFolder = () => {
    setUploadMenuOpen(false);
    // クリック時点の用途を明示的に保持する。
    // ファイル選択ダイアログの後に viewMode や state が更新されても、
    // 通常アップロードとバックアップ設定アップロードが混ざらないようにする。
    uploadIntentRef.current = viewMode === "sync" ? "backup" : "normal";
    folderUploadRef.current?.click();
  };

  const buildDescendantSet = (allItems: Item[], sourceId: string) => {
    const byParent = new Map<string, Item[]>();
    for (const item of allItems) {
      const pid = String(item.parent_id || ROOT_ID);
      if (!byParent.has(pid)) byParent.set(pid, []);
      byParent.get(pid)!.push(item);
    }
    const blocked = new Set<string>([sourceId]);
    const stack = [sourceId];
    while (stack.length) {
      const current = stack.pop()!;
      const children = byParent.get(current) || [];
      for (const child of children) {
        if (!blocked.has(child.item_id)) {
          blocked.add(child.item_id);
          stack.push(child.item_id);
        }
      }
    }
    return blocked;
  };

  const openMoveDialog = async (item: Item) => {
    setContextMenu(null);
    setMoveDialogItem(item);
    setMoveLoading(true);
    setError("");
    try {
      const tree = await api<TreeResponse>("/sync/tree", {}, token);
      const folders = (tree.items || []).filter((entry) => entry.type === "folder" && entry.trashed_at == null);
      const blocked = item.type === "folder" ? buildDescendantSet(tree.items || [], item.item_id) : new Set<string>([item.item_id]);
      setMoveTargets(folders.filter((folder) => !blocked.has(folder.item_id)));
      setMoveTargetParentId(item.parent_id || ROOT_ID);
    } catch (err: any) {
      setError(err.message || "移動先の読み込みに失敗しました。");
      setMoveDialogItem(null);
    } finally {
      setMoveLoading(false);
    }
  };

  const submitMoveItem = async () => {
    if (!moveDialogItem) return;
    const itemIds = getActiveItemIds(moveDialogItem.item_id);
    setMoveLoading(true);
    setError("");
    try {
      for (const itemId of itemIds) {
        await api(`/items/${itemId}/move`, {
          method: "POST",
          body: JSON.stringify({ parent_id: moveTargetParentId === ROOT_ID ? null : moveTargetParentId }),
        }, token);
      }
      setMoveDialogItem(null);
      clearSelection();
      await refresh();
    } catch (err: any) {
      setError(err.message || tx("移動に失敗しました。"));
    } finally {
      setMoveLoading(false);
    }
  };

  const uploadDroppedCandidates = async (
    candidates: UploadCandidate[],
    baseParentId?: string | null,
    options: { intent?: "normal" | "backup" } = {},
  ) => {
    if (!candidates.length || !token) return;
    const intent = options.intent || (viewMode === "sync" ? "backup" : "normal");
    const uploadTarget = intent === "backup"
      ? { targetView: "sync" as ViewMode, targetParentId: null }
      : getCreateUploadTarget(baseParentId);
    const { targetView, targetParentId: resolvedBaseParentId } = uploadTarget;

    const includesFolder = candidates.some((candidate) => String(candidate.relativePath || "").replace(/\\/g, "/").includes("/"));
    if (targetView === "sync" && includesFolder) {
      const missingLocalPath = candidates.some((candidate) => !String(candidate.absolutePath || "").trim());
      if (missingLocalPath) {
        setError("フォルダのローカルパスを取得できませんでした。Electron の preload が最新か確認してから再度お試しください。アップロードだけ先に進めるとバックアップ設定欄に表示できないため、処理を中止しました。");
        if (folderUploadRef.current) folderUploadRef.current.value = "";
        return;
      }
    }

    setLoading(true);
    setError("");
    try {
      const tree = await api<TreeResponse>("/sync/tree", {}, token);

      // フォルダアップロード中の重複判定は「現在の親フォルダに存在する通常アイテム」だけを見る。
      // ごみ箱・削除済み・バックアップ設定の残骸・壊れたフォルダ target は重複扱いしない。
      const readRawLocalBackupTargets = (): BackupTarget[] => {
        try {
          const raw = localStorage.getItem("phase1_backup_targets");
          const parsed = raw ? JSON.parse(raw) : [];
          return Array.isArray(parsed) ? parsed : [];
        } catch {
          return [];
        }
      };
      const brokenBackupFolderRootPaths = new Set<string>();
      for (const target of [...readRawLocalBackupTargets(), ...backupTargets]) {
        if (!target || isOfflineTarget(target)) continue;
        if (target.item_type !== "folder") continue;
        if (String(target.remote_item_id || "").trim()) continue;
        const rootPath = normalizeRemotePath(target.remote_path || target.display_name || "").split("/").filter(Boolean)[0] || "";
        if (rootPath) brokenBackupFolderRootPaths.add(rootPath);
      }

      const isBrokenBackupFolderResidue = (entry: Item) => {
        if (entry.type !== "folder") return false;
        const entryPath = normalizeRemotePath(entry.path || entry.name || "");
        const entryRoot = entryPath.split("/").filter(Boolean)[0] || entry.name || "";
        return brokenBackupFolderRootPaths.has(entryRoot);
      };

      const folderIndex = new Map<string, string>();
      const fileIndex = new Map<string, string>();
      for (const entry of tree.items || []) {
        if (entry.trashed_at != null) continue;
        if (String(entry.owner_user_id || "") !== userId) continue;
        if (isBrokenBackupFolderResidue(entry)) continue;
        const parentKey = String(entry.parent_id || ROOT_ID);
        if (entry.type === "folder") folderIndex.set(`${parentKey}::${entry.name}`, entry.item_id);
        if (entry.type === "file") fileIndex.set(`${parentKey}::${entry.name}`, entry.item_id);
      }

      const folderDecisionCache = new Map<string, UploadConflictDecision>();
      const fileDecisionCache = new Map<string, UploadConflictDecision>();
      const foldersCreatedInThisUpload = new Set<string>();
      let nextBackupTargetsSnapshot = readStoredBackupTargets();
      const pendingFolderBackupTargets = new Map<string, BackupTarget>();

      const askDuplicateDecision = async (kind: "file" | "folder", name: string): Promise<UploadConflictDecision> => {
        const targetLabel = tx(kind === "file" ? "ファイル" : "フォルダ");
        return requestUploadConflictDecision({
          title: tx("同じ{targetLabel}があります", { targetLabel }),
          message: tx("同じ{targetLabel}「{name}」が既にあります。\n\nアップロード自体をやめる場合は「キャンセル」、別名で新規アップロードする場合は「新規にアップロード」、今の同じ{targetLabel}と置き換える場合は「置き換える」を選んでください。", {
            targetLabel,
            name,
          }),
          cancelLabel: tx("キャンセル"),
          copyLabel: tx("新規にアップロード"),
          replaceLabel: tx("置き換える"),
        });
      };

      const ensureFolderPath = async (relativeDirectory: string) => {
        let currentParent = resolvedBaseParentId || ROOT_ID;
        const actualSegments: string[] = [];
        for (const segment of relativeDirectory.split("/").filter(Boolean)) {
          const folderKey = `${currentParent}::${segment}`;
          const existingFolderId = folderIndex.get(folderKey);
          if (existingFolderId) {
            // 同じフォルダアップロード処理の先行ファイルで作成したフォルダは、
            // 後続ファイルから見れば既存に見える。これはユーザーに確認すべき重複ではないため、
            // そのまま再利用する。
            if (foldersCreatedInThisUpload.has(folderKey)) {
              actualSegments.push(segment);
              currentParent = existingFolderId;
              continue;
            }

            let decision = folderDecisionCache.get(folderKey);
            if (!decision) {
              // バックアップ設定では、同名フォルダはバックアップルートとして再利用する。
              // 失敗した過去の試行でクラウド側だけに残ったフォルダがある場合でも、
              // ここで新規コピーを増やさず同じフォルダへ紐付け直す。
              decision = targetView === "sync" ? "replace" : await askDuplicateDecision("folder", segment);
              folderDecisionCache.set(folderKey, decision);
            }
            if (decision === "cancel") {
              throw new Error(UPLOAD_CANCELLED_BY_USER);
            }
            if (decision === "replace") {
              actualSegments.push(segment);
              currentParent = existingFolderId;
              continue;
            }
            const newName = dedupeCopyName(segment, true, (candidate) => folderIndex.has(`${currentParent}::${candidate}`));
            const created = await api<Item>("/items/folder", {
              method: "POST",
              body: JSON.stringify({ name: newName, parent_id: currentParent === ROOT_ID ? null : currentParent }),
            }, token);
            const createdKey = `${currentParent}::${newName}`;
            folderIndex.set(createdKey, created.item_id);
            foldersCreatedInThisUpload.add(createdKey);
            folderDecisionCache.set(createdKey, "replace");
            actualSegments.push(newName);
            currentParent = created.item_id;
            continue;
          }

          const created = await api<Item>("/items/folder", {
            method: "POST",
            body: JSON.stringify({ name: segment, parent_id: currentParent === ROOT_ID ? null : currentParent }),
          }, token);
          folderIndex.set(folderKey, created.item_id);
          foldersCreatedInThisUpload.add(folderKey);
          folderDecisionCache.set(folderKey, "replace");
          actualSegments.push(segment);
          currentParent = created.item_id;
        }
        return {
          parentId: currentParent === ROOT_ID ? null : currentParent,
          actualDirectoryPath: actualSegments.join("/"),
        };
      };

      for (const candidate of candidates) {
        const { fileName, directoryPath } = filePathParts(candidate.relativePath);
        const folderResult = await ensureFolderPath(directoryPath);
        const targetParent = folderResult.parentId;
        const actualDirectoryPath = folderResult.actualDirectoryPath;
        const parentKey = String(targetParent || ROOT_ID);
        const fileKey = `${parentKey}::${fileName}`;
        const existingItemId = fileIndex.get(fileKey) || null;

        let targetItemId = existingItemId;
        let finalFileName = fileName;
        if (existingItemId) {
          let decision = fileDecisionCache.get(fileKey);
          if (!decision) {
            // バックアップ設定でフォルダを再登録する場合は、配下ファイルも同名なら置き換える。
            // ここでコピーを増やすと、フォルダバックアップ開始前から重複・無限アップロードの原因になる。
            decision = targetView === "sync" ? "replace" : await askDuplicateDecision("file", fileName);
            fileDecisionCache.set(fileKey, decision);
          }
          if (decision === "cancel") {
            throw new Error(UPLOAD_CANCELLED_BY_USER);
          }
          if (decision === "copy") {
            targetItemId = null;
            finalFileName = dedupeCopyName(fileName, false, (candidateName) => fileIndex.has(`${parentKey}::${candidateName}`));
          }
        }

        const uploadFile = candidate.file.name === finalFileName
          ? candidate.file
          : new File([candidate.file], finalFileName, { type: candidate.file.type, lastModified: candidate.file.lastModified });

        const result = await uploadViaExistingClientApi(uploadFile, token, targetParent, targetItemId, {
          uploadContext: targetView === "sync" ? "backup" : "normal",
          replaceExisting: targetView === "sync" ? Boolean(targetItemId) : true,
        });
        fileIndex.set(`${parentKey}::${finalFileName}`, result.item_id);

        if (targetView === "sync" && candidate.absolutePath) {
          const remoteFullPath = joinRemotePath(actualDirectoryPath, finalFileName);
          const backupTarget = deriveBackupTarget(candidate.absolutePath, candidate.relativePath, remoteFullPath, result.item_id);
          if (backupTarget) {
            if (backupTarget.item_type === "folder") {
              const rootRemotePath = normalizeRemotePath(actualDirectoryPath).split("/").filter(Boolean)[0] || backupTarget.remote_path;
              const rootParentKey = String(resolvedBaseParentId || ROOT_ID);
              const rootFolderItemId = folderIndex.get(`${rootParentKey}::${rootRemotePath}`) || backupTarget.remote_item_id || null;
              const folderRootTarget = deriveFolderBackupRootTarget(
                candidate.absolutePath,
                candidate.relativePath,
                actualDirectoryPath,
                rootFolderItemId,
              );
              if (folderRootTarget) {
                const folderKey = `${folderRootTarget.local_path}::${folderRootTarget.remote_path}`;
                pendingFolderBackupTargets.set(folderKey, folderRootTarget);
              }
            } else {
              nextBackupTargetsSnapshot = mergeBackupTargets(nextBackupTargetsSnapshot, normalizeBackupSettingTarget(backupTarget));
              // ここでは画面・localStorageだけ更新する。
              // /backup/targets への保存は「自動バックアップを開始」完了後に行う。
              replaceBackupTargets(nextBackupTargetsSnapshot, false);
            }
          }
        }
      }

      if (targetView === "sync" && includesFolder && !pendingFolderBackupTargets.size) {
        const rootGroups = new Map<string, UploadCandidate[]>();
        for (const candidate of candidates) {
          const rel = String(candidate.relativePath || "").replace(/\\/g, "/").replace(/^\/+/, "");
          const rootName = rel.split("/").filter(Boolean)[0] || "";
          if (!rootName) continue;
          if (!rootGroups.has(rootName)) rootGroups.set(rootName, []);
          rootGroups.get(rootName)!.push(candidate);
        }
        for (const [rootName, group] of rootGroups) {
          const first = group[0];
          if (!first) continue;
          const rootParentKey = String(resolvedBaseParentId || ROOT_ID);
          const rootFolderId = folderIndex.get(`${rootParentKey}::${rootName}`) || null;
          if (!rootFolderId) continue;

          const firstWithAbsolutePath = group.find((entry) => String(entry.absolutePath || "").trim()) || first;
          let rootTarget: BackupTarget | null = null;
          if (String(firstWithAbsolutePath.absolutePath || "").trim()) {
            rootTarget = deriveFolderBackupRootTarget(
              firstWithAbsolutePath.absolutePath,
              firstWithAbsolutePath.relativePath,
              rootName,
              rootFolderId,
            );
          }

          // Electron の file input / webkitdirectory では、環境によってフォルダ内ファイルの
          // 絶対パスを取得できない場合がある。この場合でも「バックアップ設定への表示」だけは
          // 通常アップロードと同じ作成済みルートフォルダ item_id を使って確実に行う。
          if (!rootTarget) {
            rootTarget = {
              local_path: rootName,
              remote_path: rootName,
              item_type: "folder",
              display_name: rootName,
              source_device_label: currentDeviceLabel || "このPC",
              remote_item_id: rootFolderId,
              target_kind: "backup",
            };
          }

          const key = `${rootTarget.local_path}::${rootTarget.remote_path}`;
          pendingFolderBackupTargets.set(key, rootTarget);
        }
      }

      if (targetView === "sync" && pendingFolderBackupTargets.size) {
        for (const folderTarget of pendingFolderBackupTargets.values()) {
          const normalizedFolderTarget = normalizeBackupSettingTarget(folderTarget);
          if (!isValidBackupSettingTarget(normalizedFolderTarget)) {
            console.warn("folder backup target without remote_item_id skipped", normalizedFolderTarget);
            continue;
          }
          nextBackupTargetsSnapshot = mergeBackupTargets(nextBackupTargetsSnapshot, normalizedFolderTarget);
        }
        // フォルダアップロードでは、配下ファイル単位ではなくルートフォルダ単位の BackupTarget を登録する。
        // これを行わないと「バックアップ設定」欄には出ず、ホーム/フォルダ側で通常ファイルとして見えてしまう。
        // フォルダをバックアップ設定に追加した時点で /backup/targets にも保存する。
        // これにより、直後の refresh や再起動で backend の空一覧に上書きされても表示が消えない。
        replaceBackupTargets(nextBackupTargetsSnapshot, true);
      }

      await refreshAfterCreateUpload(targetView, resolvedBaseParentId);
    } catch (err: any) {
      if (err?.message === UPLOAD_CANCELLED_BY_USER) {
        setError("");
        return;
      }
      setError(err.message || "ドラッグ&ドロップアップロードに失敗しました。");
    } finally {
      setLoading(false);
      setUploadDragging(false);
      clearDragHover();
    }
  };

  const handleDroppedDataTransfer = async (dt: DataTransfer, baseParentId?: string | null) => {
    if (!canUploadHere) return;
    const localPaths = await getLocalFilePaths(Array.from(dt.files || []));
    const candidates = await extractDroppedCandidates(dt, localPaths);
    await uploadDroppedCandidates(candidates, baseParentId);
  };

  const openContextMenu = (event: React.MouseEvent, item: Item) => {
    event.preventDefault();
    event.stopPropagation();
    const activeSelectionCount = selectedIds.includes(item.item_id) ? selectedIds.length : 1;
    const estimatedHeight = viewMode === "trash"
      ? CONTEXT_MENU_HEIGHT_TRASH
      : activeSelectionCount > 1
        ? CONTEXT_MENU_HEIGHT_MULTI
        : item.type === "file"
          ? CONTEXT_MENU_HEIGHT_FILE
          : CONTEXT_MENU_HEIGHT_FOLDER;
    const position = clampContextMenuPosition(event.clientX, event.clientY, estimatedHeight);
    if (!selectedIds.includes(item.item_id)) {
      selectSingleItem(item);
    }
    setContextMenu({ x: position.x, y: position.y, item });
  };

  const stopProviderStorage = async () => {
    setProviderSaving(true);
    setError("");
    try {
      try {
        await stopNodeWithDesktopBridge();
      } catch (bridgeErr) {
        console.warn("[Tricloud] local node stop bridge failed", bridgeErr);
      }

      const stoppedProfile = await api<NodeProviderSummary>("/node/provider/profile", {
        method: "POST",
        body: JSON.stringify({ node_name: providerName, desired_capacity_gb: 0 }),
      }, token);

      // 旧キーの無効化は API キー再生成 API 側で行う前提にする。
      await api("/node/provider/rotate_api_key", { method: "POST" }, token);

      // 現在保存されているクライアント関連データの削除は専用 API に委ねる。
      await api("/node/provider/client_data", { method: "DELETE" }, token);

      setProviderSummary(stoppedProfile);
      setProviderSummaryFetchedAt(Math.floor(Date.now() / 1000));
      setDesiredCapacityGb(0);
      setDesiredCapacityInput("0");
      setCopiedLabel("");
      setSyncSummary((prev) => (prev ? { ...prev, clients: [] } : prev));
      await loadProviderSummary();
      await requestAppAlert("ストレージの提供を終了しました");
    } catch (err: any) {
      setError(err.message || "ストレージの提供を終了に失敗しました。");
      await requestAppAlert("ストレージの提供を終了に失敗しました。再度「設定を保存」をクリックしてください");
    } finally {
      setProviderSaving(false);
    }
  };

  const saveProviderProfile = async () => {
    const rawCapacity = desiredCapacityInput.trim();
    if (rawCapacity === "") {
      setError("提供容量を入力してください。提供をやめる場合は 0 を入力してください。");
      return;
    }
    const normalizedCapacity = Number(rawCapacity);
    if (!Number.isFinite(normalizedCapacity) || normalizedCapacity < 0) {
      setError("提供容量は 0 以上の整数で入力してください。");
      return;
    }

    if (normalizedCapacity === 0) {
      await stopProviderStorage();
      return;
    }

    setProviderSaving(true);
    setError("");
    try {
      const localCapacity = await loadDesktopLocalCapacity();
      const localOfferableGb = getLocalOfferableGb(localCapacity);
      if (localOfferableGb != null && normalizedCapacity > localOfferableGb) {
        throw new Error(`このPCで安全に提供できる上限は ${localOfferableGb} GB です。`);
      }

      const apiSummary = await api<NodeProviderSummary>("/node/provider/profile", {
        method: "POST",
        body: JSON.stringify({ node_name: providerName, desired_capacity_gb: normalizedCapacity }),
      }, token);
      const res = applyDesktopLocalCapacity(apiSummary, localCapacity);

      setProviderSummary(res);
      setProviderSummaryFetchedAt(Math.floor(Date.now() / 1000));
      setDesiredCapacityGb(normalizedCapacity);
      setDesiredCapacityInput(String(normalizedCapacity));
      setCopiedLabel("");

      if (!res.launch) {
        throw new Error("node_id と node_api_key の発行に失敗しました。");
      }

      await requestAppAlert("設定を保存しました。ストレージの提供を開始するには「ストレージの提供を開始する」をクリックしてください。");
    } catch (err: any) {
      setError(err.message || "ノード設定の保存に失敗しました。");
      await requestAppAlert("設定の保存に失敗しました。デスクトップアプリの起動設定を確認してください。");
    } finally {
      setProviderSaving(false);
    }
  };

  const openStripeOnboarding = async () => {
    setError("");
    if (typeof window !== "undefined") {
      window.location.hash = "#/provider/stripe-connect";
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  };

  const copyText = async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedLabel(label);
      window.setTimeout(() => setCopiedLabel(""), 1600);
    } catch {
      setError("コピーに失敗しました。");
    }
  };

  const getDesktopBridgeForNode = () => {
    return (window as any).phase1Desktop || (window as any).electronAPI || (window as any).__PHASE1_DESKTOP__;
  };

  const ensureDesktopBridgeResponseOk = (response: any, fallbackMessage: string) => {
    if (response && response.ok === false) {
      const state = response.state || {};
      const logTail = String(state.log_tail || "").trim();
      const detail = response.error || response.message || state.error || fallbackMessage;
      throw new Error(logTail ? `${detail}

--- node log ---
${logTail.slice(-3000)}` : detail);
    }
    return response;
  };

  const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

  const getNodeStatusWithDesktopBridge = async () => {
    const desktopBridge = getDesktopBridgeForNode();
    if (desktopBridge?.getNodeStatus) {
      return ensureDesktopBridgeResponseOk(
        await desktopBridge.getNodeStatus(),
        "デスクトップアプリ側のノード状態確認に失敗しました。"
      );
    }
    if (desktopBridge?.invoke) {
      return ensureDesktopBridgeResponseOk(
        await desktopBridge.invoke("phase1-node:status"),
        "デスクトップアプリ側のノード状態確認に失敗しました。"
      );
    }
    return null;
  };

  const getNodeStatusFailureText = (status: any) => {
    const state = status?.state || {};
    const errorText = String(state.error || "").trim();
    const logTail = String(state.log_tail || "").trim();
    if (errorText && logTail) return `${errorText}

--- node log ---
${logTail.slice(-3000)}`;
    return errorText || (logTail ? logTail.slice(-3000) : "");
  };

  const waitForLocalNodeProcessAfterStart = async () => {
    // Python/pyzmq不足や同梱ファイル不足の場合、起動直後にプロセスが落ちる。
    // ここでローカル状態とログを拾い、バックエンド側の「オフライン」だけでは原因不明にならないようにする。
    for (let i = 0; i < 5; i += 1) {
      const status = await getNodeStatusWithDesktopBridge();
      const state = status?.state || {};
      const failureText = getNodeStatusFailureText(status);
      if (failureText && state.is_running === false) {
        throw new Error(failureText);
      }
      if (state.is_running === true) return status;
      await sleep(1000);
    }
    const status = await getNodeStatusWithDesktopBridge();
    const failureText = getNodeStatusFailureText(status);
    if (failureText) throw new Error(failureText);
    throw new Error("ローカルのストレージノードプロセスが起動状態になりませんでした。");
  };

  const stopNodeWithDesktopBridge = async () => {
    const desktopBridge = getDesktopBridgeForNode();

    if (desktopBridge?.stopNode) {
      return ensureDesktopBridgeResponseOk(
        await desktopBridge.stopNode(),
        "デスクトップアプリ側のノード停止に失敗しました。"
      );
    }

    if (desktopBridge?.invoke) {
      return ensureDesktopBridgeResponseOk(
        await desktopBridge.invoke("phase1-node:stop"),
        "デスクトップアプリ側のノード停止に失敗しました。"
      );
    }

    return null;
  };

  const startNodeWithDesktopBridge = async (launch: LaunchSummary, capacityGbOverride?: number) => {
    const desktopBridge = getDesktopBridgeForNode();
    const localCapacity = await loadDesktopLocalCapacity();
    const normalizedCapacityOverride = Number(capacityGbOverride);
    const launchCapacityGb = Number.isFinite(normalizedCapacityOverride) && normalizedCapacityOverride > 0
      ? normalizedCapacityOverride
      : (desiredCapacityGb || launch.capacity_gb);

    // Control API はGCP上の実行パス（例: C:\opt\tricloud\backend\node_phase1_runner.py）を
    // launch に含めることがある。これはユーザーPC上には存在しないため、
    // デスクトップ側ではローカルに同梱された node_phase1_runner.py を自動探索させる。
    const { runner_file: _serverRunnerFile, runner_path: _serverRunnerPath, runnerPath: _serverRunnerPathCamel, python_path: _serverPythonPath, pythonPath: _serverPythonPathCamel, ...desktopLaunchBase } = launch as any;

    const launchForDesktop = {
      ...desktopLaunchBase,
      server: getDesktopNodeServerEndpoint((desktopLaunchBase as any).server || launch.server),
      storage_dir: localCapacity?.path || launch.storage_dir,
      capacity_gb: launchCapacityGb,
    };

    if (desktopBridge?.startNode) {
      return ensureDesktopBridgeResponseOk(
        await desktopBridge.startNode(launchForDesktop),
        "デスクトップアプリ側のノード起動に失敗しました。"
      );
    }

    if (desktopBridge?.invoke) {
      return ensureDesktopBridgeResponseOk(
        await desktopBridge.invoke("phase1-node:start", launchForDesktop),
        "デスクトップアプリ側のノード起動に失敗しました。"
      );
    }

    throw new Error("デスクトップアプリ側のノード起動ブリッジが未接続です。");
  };

  const startProviderNode = async () => {
    const rawCapacity = desiredCapacityInput.trim();
    if (rawCapacity === "") {
      setError("提供容量を入力してください。起動するには 1 以上の値が必要です。");
      return;
    }

    const normalizedCapacity = Number(rawCapacity);
    if (!Number.isFinite(normalizedCapacity) || normalizedCapacity <= 0) {
      setError("ストレージの提供を開始するには、提供容量を 1 以上の整数で入力してください。");
      return;
    }

    if (!providerSummary?.launch) {
      setError("先に「設定を保存」をクリックして、ノード起動情報を発行してください。");
      return;
    }

    const savedCapacityGb = Number(
      providerSummary?.profile?.desired_capacity_gb ??
      providerSummary?.launch?.capacity_gb ??
      0
    );
    if (!Number.isFinite(savedCapacityGb) || savedCapacityGb <= 0) {
      setError("先に 1GB 以上の提供量を保存してください。");
      return;
    }
    if (savedCapacityGb !== normalizedCapacity) {
      setError("提供量を変更した場合は、先に「設定を保存」をクリックしてください。");
      return;
    }

    setProviderStarting(true);
    setError("");
    try {
      const localCapacity = await loadDesktopLocalCapacity();
      const localOfferableGb = getLocalOfferableGb(localCapacity);
      if (localOfferableGb != null && normalizedCapacity > localOfferableGb) {
        throw new Error(`このPCで安全に提供できる上限は ${localOfferableGb} GB です。`);
      }

      await startNodeWithDesktopBridge(providerSummary.launch, normalizedCapacity);
      await waitForLocalNodeProcessAfterStart();
      await sleep(4000);
      await loadProviderSummary();
      await requestAppAlert("ストレージの提供を開始しました");
    } catch (err: any) {
      setError(err.message || "ストレージ提供の開始に失敗しました。");
      await requestAppAlert("ストレージの提供を開始できませんでした。デスクトップアプリの起動設定を確認してください。");
    } finally {
      setProviderStarting(false);
    }
  };

  const ensureBackupRuntimeWithTargets = async (nextTargets: BackupTarget[], _promptMessage: string, nextOfflineTargets = offlineTargets) => {
    if (!desktopBridge?.startBackup) {
      throw new Error(tx("デスクトップアプリ側のバックアップブリッジが未接続です。"));
    }
    if (!email.trim()) {
      throw new Error(tx("バックアップ開始にはログイン用メールアドレスが必要です。再ログイン後にお試しください。"));
    }

    const runtimeTargets = combineRuntimeTargets(nextTargets, nextOfflineTargets);
    if (!runtimeTargets.length) {
      throw new Error(tx("監視対象がありません。"));
    }
    const localRootSummary = summarizeBackupTargets(nextTargets);
    const profile = await api<SyncProfile>("/sync/profile", {
      method: "POST",
      body: JSON.stringify({
        local_root_display: localRootSummary,
        sync_mode: "mirror",
        polling_interval_sec: syncIntervalSec,
        ignore_hidden: syncIgnoreHidden,
      }),
    }, token);
    setSyncSummary((prev) => ({ profile, clients: prev?.clients || [] }));

    let running = false;
    let statusState: any = null;
    try {
      if (desktopBridge?.getBackupStatus) {
        const status = await desktopBridge.getBackupStatus() as BackupBridgeResponse;
        statusState = status?.state || null;
        if (statusState?.current_device_label) {
          setCurrentDeviceLabel(String(statusState.current_device_label));
        }
        running = Boolean(statusState?.is_running);
      }
    } catch {
      running = false;
    }

    const backupStatus = String(statusState?.status || "");
    const backupError = String(statusState?.error || "");
    const needsCredentialRestart = running && (
      backupStatus === "error" ||
      backupError.includes("invalid credentials") ||
      backupError.includes("バックアップ用ログインに失敗") ||
      backupError.includes("401")
    );
    if (needsCredentialRestart && desktopBridge?.stopBackup) {
      await desktopBridge.stopBackup();
      running = false;
    }

    let bridgeResponse: BackupBridgeResponse | null = null;
    if (running && desktopBridge?.updateBackupTargets) {
      const updateResponse = await desktopBridge.updateBackupTargets({
        api_base: API_BASE,
        access_token: token,
        email: email.trim(),
        polling_interval_sec: syncIntervalSec,
        ignore_hidden: syncIgnoreHidden,
        local_root_display: localRootSummary,
        targets: runtimeTargets,
      }) as BackupBridgeResponse;
      if (!updateResponse?.ok) {
        throw new Error(updateResponse?.error || updateResponse?.message || tx("バックアップ対象の更新に失敗しました。"));
      }
      bridgeResponse = updateResponse;
    } else {
      const backupResponse = await desktopBridge.startBackup({
        api_base: API_BASE,
        access_token: token,
        email: email.trim(),
        polling_interval_sec: syncIntervalSec,
        ignore_hidden: syncIgnoreHidden,
        local_root_display: localRootSummary,
        targets: runtimeTargets,
      }) as BackupBridgeResponse;

      if (!backupResponse?.ok) {
        throw new Error(backupResponse?.error || backupResponse?.message || tx("バックアップ開始に失敗しました。"));
      }
      bridgeResponse = backupResponse;
    }

    const responseTargets = Array.isArray(bridgeResponse?.state?.targets) ? bridgeResponse.state.targets : runtimeTargets;
    const responseSplit = splitRuntimeTargets(responseTargets);
    const effectiveBackupTargets = sanitizeBackupSettingTargets(responseSplit.backup);
    const effectiveOfflineTargets = responseSplit.offline.length
      ? responseSplit.offline.map(normalizeOfflineTarget)
      : nextOfflineTargets;
    const effectiveRootSummary = summarizeBackupTargets(effectiveBackupTargets);

    replaceBackupTargets(effectiveBackupTargets, true);
    replaceOfflineTargets(effectiveOfflineTargets);
    setBackupAutoRefreshEnabled(effectiveBackupTargets.length + effectiveOfflineTargets.length > 0);
    setSyncRootDisplay(effectiveRootSummary);
    // オフライン利用を追加した直後は、Electron側の状態反映が一瞬遅れても
    // phase1_offline_targets を消さないように、保存済みオフライン対象を保持して読み直す。
    await loadSyncSummary({ preserveStoredOfflineTargets: nextOfflineTargets.length > 0 });
    if (viewMode === "sync") {
      await refresh("sync", parentId, activeQuery);
    }
    return true;
  };

  const buildOwnedOfflinePayload = async (item: Item) => {
    if (String(item.owner_user_id || "") !== userId) {
      throw new Error(tx("オフライン利用は自分のファイル・フォルダでのみ利用できます。"));
    }

    const tree = await api<TreeResponse>("/sync/tree", {}, token);
    const treeItems = Array.isArray(tree.items) ? tree.items : [];
    const byId = new Map(treeItems.map((entry) => [entry.item_id, entry]));
    const treeItem = byId.get(item.item_id) || item;
    const rootRemotePath = normalizeRemotePath(treeItem.path || getItemRemotePath(item));
    if (!rootRemotePath) {
      throw new Error(tx("クラウド上のパスを特定できないため、オフライン利用にできませんでした。"));
    }

    const displayName = treeItem.name || item.name || rootRemotePath.split("/").pop() || "offline-item";

    if (item.type === "file") {
      const tokenRes = await api<DownloadTokenResponse>(`/items/${item.item_id}/download_token`, { method: "POST" }, token);
      const files: OfflineUseFileRequest[] = [{
        item_id: item.item_id,
        download_token: tokenRes.download_token,
        remote_path: rootRemotePath,
        display_name: displayName,
        size_bytes: item.size_bytes ?? treeItem.size_bytes ?? null,
      }];
      return {
        itemType: "file" as const,
        rootRemotePath,
        displayName,
        files,
        folders: [] as OfflineUseFolderRequest[],
      };
    }

    const subtree = treeItems.filter((entry) => {
      if (String(entry.owner_user_id || "") !== userId) return false;
      const entryPath = normalizeRemotePath(entry.path || entry.name || "");
      return entryPath === rootRemotePath || entryPath.startsWith(`${rootRemotePath}/`);
    });

    const folders: OfflineUseFolderRequest[] = subtree
      .filter((entry) => entry.type === "folder")
      .map((entry) => ({
        item_id: entry.item_id,
        remote_path: normalizeRemotePath(entry.path || entry.name || ""),
        display_name: entry.name,
      }))
      .filter((entry) => Boolean(entry.remote_path));

    const fileEntries = subtree.filter((entry) => entry.type === "file");
    const files: OfflineUseFileRequest[] = [];
    for (const fileEntry of fileEntries) {
      const fileRemotePath = normalizeRemotePath(fileEntry.path || fileEntry.name || "");
      if (!fileRemotePath) continue;
      const tokenRes = await api<DownloadTokenResponse>(`/items/${fileEntry.item_id}/download_token`, { method: "POST" }, token);
      files.push({
        item_id: fileEntry.item_id,
        download_token: tokenRes.download_token,
        remote_path: fileRemotePath,
        display_name: fileEntry.name,
        size_bytes: fileEntry.size_bytes ?? null,
      });
    }

    return {
      itemType: "folder" as const,
      rootRemotePath,
      displayName,
      files,
      folders,
    };
  };

  const offlineTargetMatchesItem = (target: BackupTarget, item: Item) => {
    if (!target || !isOfflineTarget(target)) return false;

    // メニューの「オフライン利用を停止」は、その item 自身が
    // offline target として登録されている場合だけ表示する。
    // 子ファイルだけをオフライン利用したときに、親フォルダまで
    // 停止対象扱いにしないため、配下判定や前方一致は使わない。
    const targetType = String(target.item_type || "").trim();
    const itemType = String(item.type || "").trim();
    if (targetType && itemType && targetType !== itemType) return false;

    const targetItemId = String(target.remote_item_id || "").trim();
    const itemId = String(item.item_id || "").trim();
    if (targetItemId && itemId) return targetItemId === itemId;

    // 旧データ互換用の補助判定。remote_item_id が無い target だけ、
    // 同じ種別かつ remote_path が完全一致する場合に限って同一 item とみなす。
    const targetRemotePath = normalizeRemotePath(target.remote_path || target.display_name || "");
    const itemRemotePath = normalizeRemotePath(item.path || getItemRemotePath(item) || item.name || "");
    return Boolean(!targetItemId && targetRemotePath && itemRemotePath && targetRemotePath === itemRemotePath);
  };
                                                             
  const getOfflineTargetForItem = (item: Item) => {
    return offlineTargets.find((target) => offlineTargetMatchesItem(target, item)) || null;
  };

  const isOfflineUseEnabledForItem = (item: Item) => {
    return Boolean(getOfflineTargetForItem(item));
  };

  const disableOfflineUse = async (item: Item) => {
    const target = getOfflineTargetForItem(item);
    if (!target) {
      setError(tx("このアイテムは現在オフライン利用中ではありません。"));
      return;
    }
    if (!desktopBridge?.disableOfflineUse) {
      setError(tx("デスクトップアプリ側のオフライン利用停止ブリッジが未接続です。"));
      return;
    }

    const confirmed = await requestAppConfirm({
      title: tx("オフライン利用を停止"),
      message: tx("「{name}」の監視を停止し、ローカルに保存されたオフライン利用用コピーを削除します。クラウド上のファイル・フォルダは削除されません。", { name: item.name }),
      confirmLabel: tx("停止して削除"),
      cancelLabel: tx("キャンセル"),
      variant: "danger",
    });
    if (!confirmed) return;

    setLoading(true);
    setError("");
    try {
      const disableResponse = await desktopBridge.disableOfflineUse({
        local_path: target.local_path,
        remote_path: target.remote_path,
        remote_item_id: target.remote_item_id || item.item_id,
        item_id: item.item_id,
        item_type: target.item_type,
        display_name: target.display_name || item.name,
        delete_local: true,
      }) as OfflineDisableResponse;

      if (!disableResponse?.ok) {
        throw new Error(disableResponse?.error || disableResponse?.message || tx("オフライン利用の停止に失敗しました。"));
      }

      const nextOfflineTargets = offlineTargets
        .filter((entry) => !offlineTargetMatchesItem(entry, item))
        .map(normalizeOfflineTarget);
      replaceOfflineTargets(nextOfflineTargets);

      try {
        await syncBackupTargetsToDesktop(backupTargets, nextOfflineTargets);
      } catch (syncErr: any) {
        console.warn("offline stop target sync failed", syncErr);
      }

      setBackupAutoRefreshEnabled(backupTargets.length + nextOfflineTargets.length > 0);
      await loadSyncSummary({ preserveStoredOfflineTargets: nextOfflineTargets.length > 0 });
      if (viewMode === "sync") {
        await refresh("sync", parentId, activeQuery);
      }
      await requestAppAlert(tx("「{name}」のオフライン利用を停止しました。\nローカルコピーは削除されました。", { name: item.name }));
    } catch (err: any) {
      setError(err.message || tx("オフライン利用の停止に失敗しました。"));
    } finally {
      setLoading(false);
    }
  };

  const enableOfflineUse = async (item: Item) => {
    if (String(item.owner_user_id || "") !== userId) {
      setError(tx("オフライン利用は自分のファイル・フォルダでのみ利用できます。"));
      return;
    }
    if (!desktopBridge?.enableOfflineFile) {
      setError(tx("デスクトップアプリ側のオフライン利用ブリッジが未接続です。"));
      return;
    }

    setLoading(true);
    setError("");
    try {
      const offlinePayload = await buildOwnedOfflinePayload(item);
      const offlineResponse = await desktopBridge.enableOfflineFile({
        api_base: API_BASE,
        access_token: token,
        item_id: item.item_id,
        remote_item_id: item.item_id,
        item_type: offlinePayload.itemType,
        remote_path: offlinePayload.rootRemotePath,
        display_name: offlinePayload.displayName,
        files: offlinePayload.files,
        folders: offlinePayload.folders,
      }) as OfflineUseResponse;

      if (!offlineResponse?.ok || !offlineResponse?.local_path) {
        throw new Error(offlineResponse?.error || offlineResponse?.message || tx("オフライン利用ファイルの保存に失敗しました。"));
      }

      const nextTarget: BackupTarget = normalizeOfflineTarget({
        local_path: String(offlineResponse.local_path),
        remote_path: offlinePayload.rootRemotePath,
        item_type: offlinePayload.itemType,
        display_name: offlinePayload.displayName,
        source_device_label: offlineResponse.source_device_label,
        remote_item_id: item.item_id,
        baseline_snapshot: offlineResponse.baseline_snapshot || null,
      });
      const nextOfflineTargets = mergeBackupTargets(offlineTargets, nextTarget).map(normalizeOfflineTarget);
      const started = await ensureBackupRuntimeWithTargets(backupTargets, tx("オフライン利用を開始します"), nextOfflineTargets);
      if (!started) {
        return;
      }
      const savedCount = Number(offlineResponse.file_count ?? offlinePayload.files.length ?? 0);
      const targetLabel = tx(offlinePayload.itemType === "folder" ? "フォルダ" : "ファイル");
      const savedFileLine = offlinePayload.itemType === "folder" ? `\n${tx("保存ファイル数: {count}件", { count: savedCount })}` : "";
      await requestAppAlert(tx("「{name}」をオフライン利用に追加しました。\n種類: {type}\n保存先: {path}{savedFileLine}", {
        name: offlinePayload.displayName,
        type: targetLabel,
        path: offlineResponse.local_path,
        savedFileLine,
      }));
    } catch (err: any) {
      setError(err.message || tx("オフライン利用の設定に失敗しました。"));
    } finally {
      setLoading(false);
    }
  };

  const saveSyncProfile = async () => {
    if (!backupTargets.length) {
      setBackupAutoRefreshEnabled(false);
      setError(tx("先にバックアップ対象のファイルまたはフォルダをアップロードしてください。"));
      return;
    }
    setSyncSaving(true);
    setError("");
    try {
      const started = await ensureBackupRuntimeWithTargets(backupTargets, tx("自動バックアップを開始します"));
      if (!started) return;
      await requestAppAlert(tx("バックアップ設定を保存し、自動バックアップを開始しました。"));
    } catch (err: any) {
      setError(err.message || tx("バックアップ設定の保存に失敗しました。"));
    } finally {
      setSyncSaving(false);
    }
  };


  const hasActiveSelection = selectedIds.length > 0;
  const showHeaderSelectionActions = hasActiveSelection && ["home", "folders", "shared", "sync"].includes(viewMode);
  const showHeaderUtilityActions = showHeaderSelectionActions || ["home", "folders", "shared", "sync"].includes(viewMode);
  const showTrashSelectionActions = viewMode === "trash" && hasActiveSelection;

  const selectionIconButtonClass = "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-2xl border border-slate-200 bg-white p-0 text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50";
  const selectionTextButtonClass = "inline-flex h-8 items-center gap-2 rounded-2xl border border-slate-200 bg-white px-3 py-0 text-sm text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50";
  const selectionClearButtonClass = "inline-flex h-8 items-center rounded-2xl border border-slate-200 bg-white px-3 py-0 text-sm text-slate-700 hover:bg-slate-50";

  const renderTrashClearAllButton = () => (
    <button
      type="button"
      className="mt-3 inline-flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 shadow-sm hover:bg-slate-50"
      title={tx("すべて削除")}
      aria-label={tx("すべて削除")}
      onClick={purgeAllTrashItems}
    >
      <Trash className="h-4 w-4 shrink-0" />
      <span>{tx("すべて削除")}</span>
    </button>
  );

  const renderSelectionActionBar = (placement: "header" | "trash") => (
    <div
      data-preserve-selection="true"
      className={`${placement === "header" ? "" : "mt-3"} flex min-h-[38px] w-[calc(100%-200px)] flex-wrap items-center justify-between gap-2 rounded-2xl border border-slate-200 bg-white px-3 py-0 text-sm shadow-sm`}
    >
      <div className="min-w-0 text-sm text-slate-700">
        <span className="font-medium leading-5">{tx("{count}件を選択中", { count: selectedIds.length })}</span>
        {selectedTotalSize > 0 ? <span className="ml-2 text-xs text-slate-500">{tx("合計 {size}", { size: formatBytes(selectedTotalSize) })}</span> : null}
      </div>
      <div className="flex flex-wrap items-center gap-6">
        {viewMode === "trash" ? (
          <>
            <button
              type="button"
              data-preserve-selection="true"
              onClick={() => restoreMultipleItems(selectedIds)}
              disabled={!hasActiveSelection}
              title={tx("復元")}
              aria-label={tx("復元")}
              className={selectionIconButtonClass}
            >
              <RefreshCw className="h-4 w-4 shrink-0" />
            </button>
            <button
              type="button"
              data-preserve-selection="true"
              onClick={() => purgeMultipleItems(selectedIds)}
              disabled={!hasActiveSelection}
              title={tx("完全削除")}
              aria-label={tx("完全削除")}
              className={selectionIconButtonClass}
            >
              <ShredderIcon className="h-4 w-4 shrink-0" />
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              data-preserve-selection="true"
              onClick={() => downloadMultipleItems(selectedIds)}
              title={tx("ダウンロード")}
              aria-label={tx("ダウンロード")}
              className={selectionIconButtonClass}
            >
              <Download className="h-4 w-4 shrink-0" />
            </button>
            <button
              type="button"
              data-preserve-selection="true"
              onClick={() => openShareDialogForItems(selectedItems)}
              disabled={!allSelectedOwned}
              title={tx("共有")}
              aria-label={tx("共有")}
              className={selectionIconButtonClass}
            >
              <UserRoundPlus className="h-4 w-4 shrink-0" />
            </button>
            <button
              type="button"
              data-preserve-selection="true"
              onClick={() => copyItems(selectedIds)}
              disabled={!allSelectedOwned}
              title={tx("コピー")}
              aria-label={tx("コピー")}
              className={selectionIconButtonClass}
            >
              <Copy className="h-4 w-4 shrink-0" />
            </button>
            <button
              type="button"
              data-preserve-selection="true"
              onClick={() => detailItem && openMoveDialog(detailItem)}
              disabled={selectedIds.length !== 1 || !allSelectedOwned}
              title={tx("移動")}
              aria-label={tx("移動")}
              className={selectionIconButtonClass}
            >
              <Folder className="h-4 w-4 shrink-0" />
            </button>
            <button
              type="button"
              data-preserve-selection="true"
              onClick={() => trashMultipleItems(selectedIds)}
              disabled={!allSelectedOwned}
              title={tx("削除")}
              aria-label={tx("削除")}
              className={selectionIconButtonClass}
            >
              <Trash2 className="h-4 w-4 shrink-0" />
            </button>
          </>
        )}
        <button
          type="button"
          data-preserve-selection="true"
          onClick={clearSelection}
          className={selectionClearButtonClass}
        >
          {tx("選択解除")}
        </button>
      </div>
    </div>
  );

  if (hashRoute === "#/provider/stripe-connect") {
    return <StripeConnect />;
  }

  if (!token) {
    return (
      <div className="min-h-screen bg-slate-100 text-slate-900">
        <header className="flex h-20 items-center justify-between border-b border-slate-200 bg-white px-7 lg:px-10">
          <div className="flex items-center gap-2">
            <img
              src={tricloudAppIconLogin}
              alt="Tricloud"
              draggable={false}
              onDragStart={(event) => event.preventDefault()}
              className="h-11 w-11 shrink-0 object-contain select-none"
            />
            <div className="text-2xl font-bold leading-none tracking-tight text-slate-900">{tx("Tricloud")}</div>
          </div>
          <div className="relative" onClick={(e) => e.stopPropagation()}>
            <button
              type="button"
              onClick={() => setLoginLanguageMenuOpen((value) => !value)}
              className="grid h-9 w-9 place-items-center rounded-full text-slate-800 hover:bg-slate-100"
              aria-label={`${tx("言語")}: ${accountLanguageName}`}
              aria-expanded={loginLanguageMenuOpen}
              aria-haspopup="listbox"
              title={`${tx("言語")}: ${accountLanguageName}`}
            >
              <Globe className="h-5 w-5" />
            </button>
            {loginLanguageMenuOpen ? (
              <div
                className="absolute right-0 top-full z-50 mt-2 w-44 overflow-hidden rounded-2xl border border-slate-100 bg-white py-1 shadow-2xl"
                role="listbox"
                aria-label={tx("言語を選択")}
              >
                {localizedLanguageOptions.map((option) => {
                  const isSelected = option.code === accountLanguageCode;
                  return (
                    <button
                      key={option.code}
                      type="button"
                      onClick={() => {
                        handleAccountLanguageChange(option.code);
                        setLoginLanguageMenuOpen(false);
                      }}
                      className={`flex w-full items-center px-3 py-2 text-left text-sm ${isSelected ? "bg-sky-50 text-sky-700" : "text-slate-600 hover:bg-slate-50"}`}
                      role="option"
                      aria-selected={isSelected}
                    >
                      <span className="truncate">{option.label}</span>
                    </button>
                  );
                })}
              </div>
            ) : null}
          </div>
        </header>

        <main className="flex min-h-[calc(100vh-80px)] items-center justify-center p-6">
          <div className="w-full max-w-5xl lg:h-[500px] grid lg:grid-cols-2 overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-2xl">
            <div className="hidden lg:flex flex-col justify-center bg-gradient-to-br from-sky-600 via-blue-600 to-indigo-700 p-10 text-white">
              <div>
                <h1 className="text-4xl font-semibold leading-tight">
                  {tx("保存・共有・提供")}
                  <br />
                  {tx("あなたのストレージをもっと自由にするクラウド。")}
                </h1>
                <p className="mt-5 leading-7 text-white/80">
                  {tx("ファイルの保存、検索、共有、同期、ストレージ提供まで。")}
                  <br />
                  {tx("日常的に使う操作を、ひとつの画面にまとめました。")}
                </p>
              </div>
            </div>
            <div className="flex min-h-0 flex-col p-8 lg:p-10">
              <div className="mb-8 shrink-0">
                <h2 className="text-2xl font-semibold text-slate-900">{authMode === "login" ? tx("ログイン") : signupStep === "consent" ? tx("利用規約・プライバシーポリシーへの同意") : tx("新規登録")}</h2>
                <p className="mt-2 text-sm text-slate-500">{authMode === "login" ? tx("メールアドレスとパスワードでログインできます。") : signupStep === "consent" ? tx("登録前に内容を確認し、同意した場合のみ次へ進めます。") : tx("姓・名、メールアドレス、パスワード、登録地域を入力してください。")}</p>
              </div>
              {authMode === "signup" && signupStep === "consent" ? (
                <div className="space-y-4">
                  <div className="rounded-3xl border border-slate-200 bg-slate-50 p-4">
                    <div className="text-sm font-medium text-slate-800">{tx("利用規約 / プライバシーポリシー")}</div>
                    <textarea
                      readOnly
                      value={getTermsAndPrivacyText(currentLanguageCode)}
                      className="mt-3 h-[118px] w-full resize-none overflow-auto rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm leading-6 text-slate-700 outline-none"
                    />
                    <label className="mt-4 inline-flex shrink-0 items-start gap-3 rounded-2xl border border-slate-200 bg-white px-4 py-3 text-sm text-slate-700">
                      <input
                        type="checkbox"
                        checked={acceptedPolicies}
                        onChange={(e) => setAcceptedPolicies(e.target.checked)}
                        className="mt-0.5 h-4 w-4 rounded border-slate-300 text-sky-600 focus:ring-sky-500 focus:ring-offset-0"
                      />
                      <span>{tx("上記の利用規約 {termsVersion} とプライバシーポリシー {privacyVersion} を確認し、同意します。", { termsVersion: TERMS_VERSION, privacyVersion: PRIVACY_POLICY_VERSION })}</span>
                    </label>
                  </div>
                  {error ? <div className="shrink-0 rounded-2xl bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}
                  <div className="flex shrink-0 gap-3">
                    <button
                      type="button"
                      onClick={() => { setAuthMode("login"); setError(""); }}
                      className="flex-1 rounded-2xl border border-slate-200 bg-white py-3 text-sm font-medium text-slate-700 hover:bg-slate-50"
                    >
                      {tx("ログインへ戻る")}
                    </button>
                    <button
                      type="button"
                      disabled={!acceptedPolicies}
                      onClick={() => {
                        if (!acceptedPolicies) return;
                        setSignupStep("form");
                        setError("");
                      }}
                      className="flex-1 rounded-2xl bg-sky-600 py-3 text-sm font-medium text-white transition hover:bg-sky-700 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      {tx("同意して続行")}
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  <form className="space-y-4" onSubmit={submitAuth}>
                    {authMode === "signup" ? (
                      <>
                        <div className="grid gap-4 sm:grid-cols-2">
                          <input className="w-full rounded-2xl border border-slate-200 px-4 py-3 outline-none focus:ring-2 focus:ring-sky-500" placeholder={tx("姓")} value={lastName} onChange={(e) => setLastName(e.target.value)} />
                          <input className="w-full rounded-2xl border border-slate-200 px-4 py-3 outline-none focus:ring-2 focus:ring-sky-500" placeholder={tx("名")} value={firstName} onChange={(e) => setFirstName(e.target.value)} />
                        </div>
                        <select className="w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 outline-none focus:ring-2 focus:ring-sky-500" value={countryCode} onChange={(e) => setCountryCode(e.target.value)}>
                          {localizedCountryOptions.map((option) => (
                            <option key={option.code} value={option.code}>{option.label}</option>
                          ))}
                        </select>
                      </>
                    ) : null}
                    <input className="w-full rounded-2xl border border-slate-200 px-4 py-3 outline-none focus:ring-2 focus:ring-sky-500" placeholder={tx("メールアドレス")} value={email} onChange={(e) => setEmail(e.target.value)} />
                    <input className="w-full rounded-2xl border border-slate-200 px-4 py-3 outline-none focus:ring-2 focus:ring-sky-500" placeholder={tx("パスワード")} type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
                    {error ? <div className="rounded-2xl bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</div> : null}
                    <div className={authMode === "login" ? "pt-20" : ""}>
                      <button
                        disabled={!canSubmitAuth}
                        className="w-full rounded-2xl bg-sky-600 py-3 font-medium text-white transition hover:bg-sky-700 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {authMode === "login" ? tx("ログイン") : tx("アカウント作成")}
                      </button>
                    </div>
                  </form>
                  <div className="mt-5 flex flex-wrap items-center gap-3">
                    {authMode === "signup" ? (
                      <button className="text-sm text-slate-600 hover:text-slate-800" onClick={() => { setSignupStep("consent"); setError(""); }}>{tx("同意画面へ戻る")}</button>
                    ) : null}
                    {authMode === "login" ? (
                      <button className="text-sm text-sky-700 hover:text-sky-800" onClick={() => {
                        setAuthMode("signup");
                        setSignupStep("consent");
                        setAcceptedPolicies(false);
                        setError("");
                      }}>{tx("新規登録へ切り替える")}</button>
                    ) : null}
                  </div>
                </>
              )}
            </div>
          </div>
        </main>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-slate-100 text-slate-900" onDragOver={(e) => { e.preventDefault(); if (canUploadHere && !readDraggedItemIds(e.dataTransfer).length) setUploadDragging(true); }} onDragLeave={() => { if (!draggingItemIds.length) setUploadDragging(false); clearDragHover(); }} onDrop={async (e) => { e.preventDefault(); if (readDraggedItemIds(e.dataTransfer).length) { setDraggingItemIds([]); clearDragHover(); return; } setUploadDragging(false); await handleDroppedDataTransfer(e.dataTransfer); }}>
      <div className="flex min-h-screen items-start">
        <aside className="hidden md:flex sticky top-0 h-screen w-72 shrink-0 flex-col overflow-hidden border-r border-slate-200 bg-white">
          <div className="relative z-20 shrink-0 bg-white px-4 pb-4 pt-4">
          <div className="flex items-center gap-1 px-2 py-3">
            <img
              src={tricloudAppIconApp}
              alt="Tricloud"
              draggable={false}
              onDragStart={(event) => event.preventDefault()}
              className="h-12 w-12 shrink-0 object-contain drop-shadow-sm select-none"
            />
            <div className="flex min-h-12 items-center">
              <div className="text-3xl font-bold leading-none tracking-tight text-slate-900">Tricloud</div>
            </div>
          </div>
          <div className="relative mt-4">
            <button onClick={(e) => { e.stopPropagation(); setUploadMenuOpen((v) => !v); }} className="inline-flex w-full items-center justify-center gap-2 rounded-2xl bg-sky-600 px-4 py-3 text-white shadow-sm hover:bg-sky-700">
              <Plus className="h-4 w-4" /> {tx("新規またはアップロード")}
            </button>
            {uploadMenuOpen ? (
              <div className="absolute left-0 top-full z-30 mt-2 w-full rounded-2xl border border-slate-200 bg-white p-2 shadow-2xl">
                <button onClick={() => { setUploadMenuOpen(false); createFolder(); }} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50">
                  <FolderPlus className="h-4 w-4 text-slate-500" />
                  <span>{tx("新しいフォルダ")}</span>
                </button>
                <button onClick={openUploadFiles} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50">
                  <Upload className="h-4 w-4 text-sky-600" />
                  <span>{tx("ファイルのアップロード")}</span>
                </button>
                <button onClick={openUploadFolder} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50">
                  <Folder className="h-4 w-4 text-sky-600" />
                  <span>{tx("フォルダのアップロード")}</span>
                </button>
              </div>
            ) : null}
          </div>
            </div>
          <div
            className="min-h-0 flex-1 overflow-y-auto px-4 pb-4 [scrollbar-width:thin] [&::-webkit-scrollbar]:w-1.5 [&::-webkit-scrollbar-track]:bg-transparent [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-slate-200/60 [&::-webkit-scrollbar-thumb:hover]:bg-slate-300/70"
            style={{ scrollbarColor: "#e2e8f0 transparent" }}
          >
          <input ref={fileUploadRef} type="file" className="hidden" multiple onChange={(e) => handleFileUpload(e.target.files)} />
          <input ref={folderUploadRef} type="file" className="hidden" multiple onChange={(e) => handleFolderUpload(e.target.files)} {...({ webkitdirectory: "", directory: "" } as any)} />
          <input ref={avatarUploadRef} type="file" accept="image/*" className="hidden" onChange={(e) => handleAvatarImageChange(e.target.files)} />
          <nav className="mt-6 space-y-1 text-sm">
            {[
              { key: "home", label: tx("ホーム"), icon: <Home className="h-4 w-4" /> },
              { key: "folders", label: tx("フォルダ"), icon: <Folder className="h-4 w-4" /> },
                            { key: "shared", label: tx("共有アイテム"), icon: <Users className="h-4 w-4" /> },
              { key: "sync", label: currentLanguageCode === "es" ? "Configuración\nde copia de seguridad" : tx("バックアップ設定"), icon: <Laptop className="h-4 w-4" /> },
              { key: "trash", label: tx("ごみ箱"), icon: <Trash2 className="h-4 w-4" /> },
            ].map((entry) => (
              <button
                key={entry.key}
                onClick={async () => {
                  clearSelection();
                  setRecentButtonActive(false);
                  setBreadcrumbRootView(entry.key as ViewMode);
                  setViewMode(entry.key as ViewMode);
                  if (entry.key === "trash") setSortKey("trashed_at");
                  if (entry.key !== "trash" && sortKey === "trashed_at") setSortKey("updated_at");
                  const nextParent = entry.key === "home" || entry.key === "folders" || entry.key === "shared" || entry.key === "sync" || entry.key === "trash" ? null : parentId;
                  if (entry.key === "home") setSearchScope("home");
                  if (entry.key === "folders") setSearchScope("owned");
                  if (entry.key === "shared") setSearchScope("shared");
                  setParentId(nextParent);
                  await refresh(entry.key as ViewMode, nextParent, activeQuery);
                }}
                className={`grid w-full grid-cols-[1rem_minmax(0,1fr)] items-start gap-2 rounded-2xl px-3 py-2.5 text-left ${viewMode === entry.key ? "bg-sky-50 text-sky-700" : "text-slate-600 hover:bg-slate-100"}`}
              >
                <span className="flex h-5 w-4 items-center justify-center">{entry.icon}</span>
                <span className="min-w-0 whitespace-pre-line leading-5">{entry.label}</span>
              </button>
            ))}
          </nav>

          <section
            aria-label={tx("広告掲載予定エリア")}
            className="mt-8 flex min-h-[220px] items-center justify-center rounded-3xl border border-dashed border-slate-300 bg-slate-50/90 px-4 py-4 text-center shadow-sm"
            onClick={(event) => event.stopPropagation()}
          >
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-[0.2em] text-slate-500">
                {tx("広告掲載予定エリア")}
              </div>
              <div className="mt-2 text-xs leading-relaxed text-slate-600">
                {tx("無料プランでは、この場所に広告が表示される予定です。")}
              </div>
            </div>
          </section>

          <div className="h-2 shrink-0" />
          </div>
        </aside>

        <main ref={mainContentRef} className="flex-1 flex flex-col">
          <header className="sticky top-0 z-10 border-b border-slate-200 bg-white/90 backdrop-blur px-4 md:px-8 py-4">
            {viewMode === "provider" ? (
              <div>
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <h1 className="text-xl font-semibold">{pageTitle}</h1>
                    {pageDescriptionLines.map((line, index) => (
                      <div key={line} className={`${index === 0 ? "mt-2" : "mt-0.5"} text-sm text-slate-500`}>{line}</div>
                    ))}
                  </div>

                  <div className="flex shrink-0 items-center gap-2 self-start">
                    {renderAccountButton()}
                  </div>
                </div>
              </div>
            ) : (
              <>
                <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
                  <form onSubmit={runSearch} className="flex-1 max-w-2xl">
                    <div className="flex items-center gap-3 rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 focus-within:ring-2 focus-within:ring-sky-500">
                      <button type="submit" aria-label={tx("検索を実行")} className="grid h-4 w-4 shrink-0 place-items-center text-slate-400 hover:text-slate-600">
                        <Search className="h-4 w-4" />
                      </button>
                      <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={tx("検索")} className="w-full bg-transparent outline-none text-sm placeholder:text-slate-600" />
                    </div>
                  </form>
                  <div className="flex flex-wrap items-center gap-2">
                    <button onClick={openProviderView} className="inline-flex h-11 items-center gap-2 rounded-2xl bg-white border border-slate-200 px-4 text-sm hover:bg-slate-50"><Gem className="h-4 w-4" /> {tx("ストレージ提供")}</button>
                    {renderAccountButton()}
                  </div>
                </div>
                {viewMode === "trash" ? (
                  <div className="mt-4">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <h1 className="text-xl font-semibold">{pageTitle}</h1>
                        {pageDescriptionLines.map((line, index) => (
                          <div key={line} className={`${index === 0 ? "mt-2" : "mt-0.5"} text-sm text-slate-500`}>{line}</div>
                        ))}
                        {showTrashSelectionActions ? renderSelectionActionBar("trash") : renderTrashClearAllButton()}
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="mt-4">
                    <div className="min-w-0">
                      <h1 className="text-xl font-semibold">{pageTitle}</h1>
                      {pageDescriptionLines.length ? (
                        <div className="mt-2 space-y-0.5 text-sm text-slate-500">
                          {pageDescriptionLines.map((line) => <div key={line}>{line}</div>)}
                        </div>
                      ) : (viewMode === "folders" || viewMode === "shared" || viewMode === "sync") && breadcrumbs.length ? (
                        <div className="mt-2 flex items-center gap-1 text-sm text-slate-500 flex-wrap">
                          <button onClick={openBreadcrumbRoot} className="hover:text-slate-700">
                            {getBreadcrumbRootLabel(breadcrumbRootView)}
                          </button>
                          {breadcrumbs.map((crumb) => (
                            <React.Fragment key={crumb.item_id}>
                              <ChevronRight className="h-3.5 w-3.5" />
                              <button onClick={() => openBreadcrumbFolder(crumb)} className="hover:text-slate-700">{crumb.name}</button>
                            </React.Fragment>
                          ))}
                        </div>
                      ) : null}
                      {showHeaderUtilityActions ? (
                        <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
                          {showHeaderSelectionActions ? (
                            renderSelectionActionBar("header")
                          ) : (
                            <>
                              {(viewMode === "home" || viewMode === "folders" || viewMode === "shared") ? (
                                <button
                                  onClick={openRecentItemsView}
                                  className={`inline-flex items-center gap-2 rounded-2xl border px-3 py-2 ${
                                    recentButtonActive ? "border-sky-200 bg-sky-50 text-sky-700" : "border-slate-200 hover:bg-slate-50"
                                  }`}
                                >
                                  <Clock3 className="h-4 w-4" /> {tx("最近")}
                                </button>
                              ) : null}
                              {viewMode === "sync" ? (
                                <>
                                  <button
                                    onClick={async () => {
                                      setRecentButtonActive(false);
                                      setSortKey("updated_at");
                                      setSortDir("desc");
                                      await refresh("sync", null, activeQuery);
                                    }}
                                    className="inline-flex items-center gap-2 rounded-2xl border border-slate-200 px-3 py-2 hover:bg-slate-50"
                                  >
                                    <Clock3 className="h-4 w-4" /> {tx("最新")}
                                  </button>
                                  <button
                                    type="button"
                                    onClick={saveSyncProfile}
                                    disabled={syncSaving}
                                    className="inline-flex items-center gap-2 rounded-2xl bg-sky-600 px-3 py-2 text-white shadow-sm hover:bg-sky-700 disabled:cursor-not-allowed disabled:opacity-50"
                                  >
                                    <ShieldCheck className="h-4 w-4" />
                                    {syncSaving ? tx("開始中...") : tx("自動バックアップを開始")}
                                  </button>
                                </>
                              ) : null}
                            </>
                          )}
                        </div>
                      ) : null}
                    </div>
                  </div>
                )}
              </>
            )}
          </header>

          <div className="flex-1 px-4 md:px-8 py-6" onClick={handleWorkspaceBlankClick}>
            {error ? (
              <div className="mb-3 rounded-2xl border border-rose-100 bg-rose-50 px-4 py-3 text-sm text-rose-700">
                <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
                  <div>
                    <div className="font-medium text-rose-800">
                      {/Failed to fetch|NetworkError|Load failed/i.test(error) ? tx("Control API に接続できません") : tx("エラーが発生しました")}
                    </div>
                    <div className="mt-0.5 text-xs text-rose-700">
                      {/Failed to fetch|NetworkError|Load failed/i.test(error) ? tx("バックエンドが起動しているか確認してください。") : error}
                    </div>
                    {/Failed to fetch|NetworkError|Load failed/i.test(error) ? (
                      <div className="mt-1 text-[11px] text-rose-500">{tx("詳細")}: {error}</div>
                    ) : null}
                  </div>
                  <button
                    type="button"
                    onClick={(event) => {
                      event.stopPropagation();
                      void refresh();
                    }}
                    className="self-start md:self-center rounded-xl border border-rose-200 bg-white px-3 py-1.5 text-xs font-medium text-rose-700 hover:bg-rose-50"
                  >
                    {tx("再読み込み")}
                  </button>
                </div>
              </div>
            ) : null}

            <section
              aria-label={tx("広告掲載予定エリア")}
              className="mb-4 flex min-h-[88px] items-center justify-center rounded-3xl border border-dashed border-slate-200 bg-white/80 px-6 py-4 text-center shadow-sm"
              onClick={(event) => event.stopPropagation()}
            >
              <div>
                <div className="text-[11px] font-semibold uppercase tracking-[0.22em] text-slate-400">
                  {tx("広告掲載予定エリア")}
                </div>
                <div className="mt-1 text-sm text-slate-500">
                  {tx("無料プランでは、この場所に広告が表示される予定です。")}
                </div>
              </div>
            </section>

            {viewMode === "provider" ? (
              <div className="space-y-6">
                <section className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
                  <div className="flex items-center gap-2 text-lg font-semibold"><Coins className="h-5 w-5 text-amber-500" /> {tx("直近の報酬")}</div>
                  {providerSummary?.recent_earnings?.length ? (
                    <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                      {providerSummary.recent_earnings.map((earning) => (
                        <div key={earning.earning_id} className="rounded-2xl border border-slate-200 p-4">
                          <div className="flex items-center justify-between text-sm"><span className="font-medium text-slate-700">{formatDate(earning.period_end)}</span><span className="rounded-full bg-amber-50 px-2.5 py-1 text-xs text-amber-700">{earning.status}</span></div>
                          <div className="mt-2 text-lg font-semibold">{formatYen(earning.net_amount_yen)}</div>
                          <div className="mt-1 text-xs text-slate-500">{Number(earning.gb_month || 0).toFixed(2)} GB-month</div>
                        </div>
                      ))}
                    </div>
                  ) : <div className="mt-4 rounded-2xl bg-slate-50 p-4 text-sm text-slate-600">{tx("まだ報酬履歴がありません。ノードがオンラインで使われ始めるとここに月次記録が出ます。")}</div>}
                </section>

                <div className="space-y-6">
                  <section className="space-y-6">
                    <div className="grid gap-4 md:grid-cols-4">
                      <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
                        <div className="flex items-center justify-between text-sm text-slate-500"><span>{tx("ノード状態")}</span>{providerSummary?.runtime.online ? <Zap className="h-4 w-4 text-amber-500" /> : <ZapOff className="h-4 w-4 text-amber-500" />}</div>
                        <div className="mt-4 text-2xl font-semibold">{providerSummary?.runtime.online ? tx("オンライン") : tx("オフライン")}</div>
                        <div className="mt-2 text-xs text-slate-500">{tx("最終 heartbeat: {date}", { date: formatDate(providerSummary?.runtime.last_seen) })}</div>
                      </div>
                      <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
                        <div className="flex items-center justify-between text-sm text-slate-500"><span>{tx("提供容量")}</span><HardDrive className="h-4 w-4 text-sky-500" /></div>
                        <div className="mt-4 text-2xl font-semibold">{providerSummary ? formatBytes(providerSummary.profile?.desired_capacity_bytes || providerSummary.runtime.capacity_bytes) : "—"}</div>
                        <div className="mt-2 text-xs text-slate-500">{tx("実行時容量: {value}", { value: formatBytes(providerSummary?.runtime.capacity_bytes) })}</div>
                      </div>
                      <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
                        <div className="flex items-center justify-between text-sm text-slate-500"><span>{tx("貸出し中")}</span><Server className="h-4 w-4 text-violet-500" /></div>
                        <div className="mt-4">
                          <div className="text-2xl font-semibold leading-none">
                            {providerSummary ? formatGb(providerSummary.runtime.reserved_bytes) : "—"}
                          </div>
                          <div className="mt-2 text-xs text-slate-500">
                            {tx("アップデート: {date}", { date: formatDate(providerSummaryFetchedAt) })}
                          </div>
                        </div>
                      </div>
                      <div className="rounded-3xl border border-slate-200 bg-white p-5 shadow-sm">
                        <div className="flex items-center justify-between text-sm text-slate-500"><span>{tx("平均稼働率")}</span><Activity className={`h-4 w-4 ${providerSummary?.runtime.online ? "text-emerald-500" : "text-slate-400"}`} /></div>
                        <div className="mt-4 text-2xl font-semibold">{formatPercent(getAverageOnlineRatio(providerSummary))}</div>
                        <div className="mt-2 text-xs text-slate-500">{tx("直近の利用実績から見た平均値")}</div>
                      </div>
                    </div>

                    <div className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
                      <div className="flex items-start justify-between gap-4">
                        <div>
                          <h2 className="text-lg font-semibold">{tx("提供量の設定")}</h2>
                          <p className="mt-2 text-sm leading-6 text-slate-500">{tx("このPCから提供するストレージ容量の上限を設定します。")}</p>
                        </div>
                      </div>
                      <div className="mt-8 grid gap-6 lg:grid-cols-[1.2fr_0.8fr]">
                        <div>
                          <label className="block text-sm font-medium text-slate-700">{tx("このパソコンが提供できる上限")}</label>
                          <div className="mt-2 rounded-2xl border border-slate-200 bg-slate-50 px-4 py-4">
                            <div className="text-2xl font-semibold">{getOfferableCapacityGb(providerSummary) != null ? `${getOfferableCapacityGb(providerSummary)} GB` : "—"}</div>
                            <div className="mt-2 text-xs leading-5 text-slate-500">{tx("このPCの空き容量をもとに、安全に提供できる上限を表示しています。")}</div>
                          </div>
                          <div className="mt-6 flex items-center justify-between text-sm"><label className="font-medium text-slate-700">{tx("提供容量")}</label><span className="rounded-full bg-sky-50 px-3 py-1 font-medium text-sky-700">{desiredCapacityInput.trim() === "" ? "—" : `${desiredCapacityInput} GB`}</span></div>
                          <input type="range" min={0} max={providerSummary?.local_capacity?.offerable_gb || providerSummary?.defaults.suggested_slider_max_gb || 2000} step={1} value={desiredCapacityGb} onChange={(e) => { const next = Number(e.target.value); setDesiredCapacityGb(next); setDesiredCapacityInput(String(next)); }} className="mt-4 w-full accent-sky-600" aria-label="提供容量GB" />
                          <div className="mt-4 grid gap-3 sm:grid-cols-[180px_1fr] sm:items-center">
                            <div className="text-sm text-slate-500">{tx("細かく入力したい場合")}</div>
                            <div className="flex items-center gap-3"><input type="number" min={0} max={providerSummary?.local_capacity?.offerable_gb || 999999} value={desiredCapacityInput} onChange={(e) => { const raw = e.target.value; if (raw === "") { setDesiredCapacityInput(""); return; } if (!/^\d+$/.test(raw)) return; const hardMax = providerSummary?.local_capacity?.offerable_gb || 999999; const next = Math.min(hardMax, Number(raw)); setDesiredCapacityGb(next); setDesiredCapacityInput(String(next)); }} className="w-40 rounded-2xl border border-slate-200 px-4 py-3 outline-none focus:ring-2 focus:ring-sky-500" /><span className="text-sm text-slate-500">GB</span></div>
                          </div>
                          <div className="mt-2 space-y-1 text-xs leading-5 text-slate-500">
                            <div>{tx("もしストレージの提供をやめる場合は、0を入力して『設定を保存』をクリックしてください。")}</div>
                            <div>{tx("ストレージを提供するパソコンを変更する場合も、0を入力して『設定を保存』をクリックして提供を停止してください。")}</div>
                          </div>
                          <div className="mt-6 flex flex-wrap items-center gap-3">
                            <button onClick={saveProviderProfile} disabled={providerSaving || providerStarting || desiredCapacityInput.trim() === ""} className="inline-flex items-center gap-2 rounded-2xl bg-sky-600 px-5 py-3 text-white hover:bg-sky-700 disabled:opacity-50 disabled:cursor-not-allowed"><ShieldCheck className="h-4 w-4" /> {providerSaving ? tx("保存中...") : tx("設定を保存")}</button>
                          </div>
                        </div>
                        <div className="rounded-3xl border border-slate-200 bg-slate-50 p-5">
                          <div className="flex items-center gap-2 text-sm font-medium text-slate-700"><Coins className="h-4 w-4 text-amber-500" /> {tx("想定月次報酬")}</div>
                          <div className="mt-4 space-y-3">
                            {(providerSummary?.reward_projection.scenarios || []).map((scenario) => (
                              <div key={scenario.utilization_ratio} className="rounded-2xl bg-white p-4 border border-slate-200">
                                <div className="flex items-center justify-between text-sm"><span className="font-medium text-slate-700">{tx("稼働率 {value}%", { value: Math.round(scenario.utilization_ratio * 100) })}</span><span className="text-slate-500">{scenario.estimated_gb_month.toFixed(1)} GB-month</span></div>
                                <div className="mt-2 text-xl font-semibold">{formatYen(scenario.estimated_reward_yen)}</div>
                              </div>
                            ))}
                          </div>
                          <p className="mt-4 text-xs leading-5 text-slate-500">{tx("この見積りは、最近の利用率ごとの想定に沿って月次報酬を並べたものです。")}</p>
                        </div>
                      </div>
                    </div>

                    <div className="rounded-3xl border border-slate-200 bg-white p-6 shadow-sm">
                      <div className="flex items-center gap-2 text-lg font-semibold"><Wallet className="h-5 w-5 text-emerald-500" /> {tx("報酬の受け取り設定")}</div>
                      <div className="mt-4 rounded-2xl bg-slate-50 p-4 text-sm text-slate-600 space-y-2">
                        <div>Stripe Connect: {providerSummary?.stripe.configured ? tx("利用可能") : tx("未設定")}</div>
                        <div>{tx("口座連携: {value}", { value: providerSummary?.stripe.connected ? tx("連携済み") : tx("未連携") })}</div>
                        <div>{tx("支払い有効: {value}", { value: providerSummary?.stripe.payout_enabled ? tx("有効") : tx("未有効") })}</div>
                        <div>{tx("支払い一時停止: {value}", { value: providerSummary?.stripe.payouts_paused ? tx("あり") : tx("なし") })}</div>
                      </div>
                      <button onClick={openStripeOnboarding} disabled={!providerSummary?.profile || !providerSummary?.stripe.configured} className="mt-4 inline-flex w-full items-center justify-center gap-2 rounded-2xl bg-slate-900 px-4 py-3 text-white hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"><ShieldCheck className="h-4 w-4" /> Stripe Connect を連携</button>
                    </div>

                    <button
                      onClick={startProviderNode}
                      disabled={providerStarting || providerSaving || !providerSummary?.launch || desiredCapacityInput.trim() === "" || Number(desiredCapacityInput || 0) <= 0 || Number(providerSummary?.profile?.desired_capacity_gb ?? providerSummary?.launch?.capacity_gb ?? 0) !== Number(desiredCapacityInput || 0)}
                      className="inline-flex w-full items-center justify-center gap-2 rounded-3xl bg-sky-600 px-6 py-4 text-base font-medium text-white shadow-sm hover:bg-sky-700 disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      <HardDrive className="h-5 w-5" />
                      {providerStarting ? tx("起動中...") : tx("ストレージの提供を開始する")}
                    </button>
                  </section>
                </div>
              </div>
            ) : viewMode === "sync" ? (
              <div className="space-y-6">
                <section data-preserve-selection="true" className="overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm">
                  <div data-select-list-header="true" className="grid gap-4 border-b border-slate-200 bg-slate-50 px-5 py-3 text-xs font-medium uppercase tracking-wide text-slate-500" style={backupListGridStyle}>
                    <button onClick={() => toggleSort("name")} className="inline-flex items-center gap-1 text-left hover:text-slate-700">
                      <span>{tx("名前")}</span>
                      <span className="text-[11px]">{getSortArrow("name")}</span>
                    </button>
                    <div></div>
                    <div className="text-left">{tx("バックアップ元")}</div>
                    <button onClick={() => toggleSort("size_bytes")} className="inline-flex items-center gap-1 text-left hover:text-slate-700">
                      <span>{tx("サイズ")}</span>
                      <span className="text-[11px]">{getSortArrow("size_bytes")}</span>
                    </button>
                    <button onClick={() => toggleSort("updated_at")} className="inline-flex items-center gap-1 text-left hover:text-slate-700">
                      <span>{tx("更新日時")}</span>
                      <span className="text-[11px]">{getSortArrow("updated_at")}</span>
                    </button>
                    <div></div>
                  </div>
                  {loading ? <div className="p-10 text-center text-slate-500">{tx("読み込み中...")}</div> : sortedItems.length === 0 ? <div className="p-10 text-center text-slate-500">{tx("バックアップ設定に追加されたファイル・フォルダはまだありません。")}</div> : (
                    <div className="divide-y divide-slate-100">
                      {sortedItems.map((item) => {
                        const isSelected = selectedIds.includes(item.item_id);
                        const backupSource = getBackupSourceForItem(item);
                        return (
                          <div
                            key={item.item_id}
                            data-selectable-row="true"
                            data-item-id={item.item_id}
                            onContextMenu={(event) => openContextMenu(event, item)}
                            onMouseDown={(event) => {
                              const target = event.target as HTMLElement;
                              if (event.button !== 0) return;
                              if (event.metaKey || event.ctrlKey || event.shiftKey) return;
                              if (target.closest('[data-row-action="true"]')) return;
                              dragSelectAnchorRef.current = item.item_id;
                              dragSelectMovedRef.current = false;
                              dragSelectActiveRef.current = true;
                              dragSelectPointerRef.current = { x: event.clientX, y: event.clientY };
                              setDragSelectActive(true);
                              startDragSelectAutoScroll(event.clientX, event.clientY);
                              setSelectionAnchorId(item.item_id);
                              setSelectedIds([item.item_id]);
                              setSelected(item);
                              event.preventDefault();
                            }}
                            onMouseEnter={() => updateDragSelectedRange(item)}
                            onClick={(event) => {
                              const target = event.target as HTMLElement;
                              if (target.closest('[data-row-action="true"]')) return;
                              if (dragSelectMovedRef.current) {
                                dragSelectMovedRef.current = false;
                                return;
                              }
                              handleRowSelection(item, event);
                            }}
                            onDoubleClick={(event) => {
                              const target = event.target as HTMLElement;
                              if (target.closest('[data-row-action="true"]')) return;
                              if (item.type === "folder") {
                                openFolder(item);
                              } else if (item.type === "file") {
                                openItem(item);
                              }
                            }}
                            className={`relative grid gap-4 px-5 py-3 items-center transition select-none ${isSelected ? "" : "hover:bg-slate-50"}`}
                            style={isSelected ? { ...backupListGridStyle, backgroundColor: ROW_SELECTION_BG } : backupListGridStyle}
                          >
                            <div className="absolute left-0 top-0 h-full w-[3px]" style={{ backgroundColor: isSelected ? ROW_SELECTION_ACCENT : "transparent" }} />
                            <div data-primary-cell="true" className="flex min-w-0 items-center gap-3 text-left">
                              <span className="grid h-9 w-9 place-items-center rounded-xl bg-slate-100">{getItemIcon(item)}</span>
                              <span className="truncate">
                                <span className="block truncate font-medium text-slate-800">{item.name}</span>
                                <span className="block truncate text-xs text-slate-500">
                                  {item.type === "folder" ? tx("フォルダ") : tx("ファイル")}
                                  {item.version_count ? ` ・ ${tx("{count}件の履歴", { count: item.version_count })}` : ""}
                                </span>
                              </span>
                            </div>
                            <div className="flex h-5 w-5 items-center justify-center" title={renderItemStatusIcon(item, "sync") ? "自動バックアップ中" : undefined}>
                              {renderItemStatusIcon(item, "sync")}
                            </div>
                            <div className="min-w-0 text-sm text-slate-600">
                              {backupSource ? (
                                <div className="min-w-0">
                                  <div className="truncate font-medium text-slate-700" title={backupSource.deviceLabel}>{backupSource.deviceLabel}</div>
                                  <div className="truncate text-xs text-slate-500" title={backupSource.localPath}>{backupSource.localPath || "—"}</div>
                                </div>
                              ) : (
                                <span className="text-slate-400">—</span>
                              )}
                            </div>
                            <div className="text-sm text-slate-600">{item.type === "folder" ? "—" : formatBytes(item.size_bytes)}</div>
                            <div className="text-sm text-slate-600">{formatDate(item.updated_at)}</div>
                            <div className="flex justify-end">
                              <button data-row-action="true" onClick={(event) => openContextMenu(event as unknown as React.MouseEvent, item)} className="grid h-9 w-9 place-items-center rounded-xl hover:bg-slate-100">
                                <MoreHorizontal className="h-4 w-4 text-slate-500" />
                              </button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </section>

              </div>
            ) : (
              <div>
                <section data-preserve-selection="true" className="overflow-hidden rounded-3xl border border-slate-200 bg-white shadow-sm">
                  <div data-select-list-header="true" className={`grid ${viewMode === "trash" ? "grid-cols-[minmax(0,1fr)_140px_190px_80px]" : "grid-cols-[minmax(0,1fr)_20px_140px_190px_80px]"} gap-4 border-b border-slate-200 bg-slate-50 px-5 py-3 text-xs font-medium uppercase tracking-wide text-slate-500`}>
                    <button onClick={() => toggleSort("name")} className="inline-flex items-center gap-1 text-left hover:text-slate-700">
                      <span>{tx("名前")}</span>
                      <span className="text-[11px]">{getSortArrow("name")}</span>
                    </button>
                    {viewMode !== "trash" ? <div></div> : null}
                    <button onClick={() => toggleSort("size_bytes")} className="inline-flex items-center gap-1 text-left hover:text-slate-700">
                      <span>{tx("サイズ")}</span>
                      <span className="text-[11px]">{getSortArrow("size_bytes")}</span>
                    </button>
                    <button onClick={() => toggleSort(viewMode === "trash" ? "trashed_at" : "updated_at")} className="inline-flex items-center gap-1 text-left hover:text-slate-700">
                      <span>{viewMode === "trash" ? tx("削除日時") : tx("更新日時")}</span>
                      <span className="text-[11px]">{getSortArrow(viewMode === "trash" ? "trashed_at" : "updated_at")}</span>
                    </button>
                    <div></div>
                  </div>
                  {loading ? <div className="p-10 text-center text-slate-500">{tx("読み込み中...")}</div> : sortedItems.length === 0 ? <div className="p-10 text-center text-slate-500">{tx("該当するファイルがありません。")}</div> : (
                    <div className="divide-y divide-slate-100">
                      {sortedItems.map((item) => {
                        const trashMeta = getTrashExpiryMeta(item.trashed_at);
                        const isSelected = selectedIds.includes(item.item_id);
                        const isDragSource = draggingItemIds.includes(item.item_id);
                        const isFolderDropTarget = hoverFolderId === item.item_id && item.type === "folder";
                        return (
                        <div
                          key={item.item_id}
                          data-selectable-row="true"
                          data-item-id={item.item_id}
                          onContextMenu={(event) => openContextMenu(event, item)}
                          draggable={canUploadHere && String(item.owner_user_id || "") === userId && isSelected}
                          onMouseDown={(event) => {
                            const target = event.target as HTMLElement;
                            if (event.button !== 0) return;
                            if (event.metaKey || event.ctrlKey || event.shiftKey) return;
                            if (target.closest('[data-row-action="true"]')) return;
                            const clickedPrimary = Boolean(target.closest('[data-primary-cell="true"]'));
                            if (canUploadHere && String(item.owner_user_id || "") === userId && isSelected && clickedPrimary) return;
                            dragSelectAnchorRef.current = item.item_id;
                            dragSelectMovedRef.current = false;
                            dragSelectActiveRef.current = true;
                            dragSelectPointerRef.current = { x: event.clientX, y: event.clientY };
                            setDragSelectActive(true);
                            startDragSelectAutoScroll(event.clientX, event.clientY);
                            setSelectionAnchorId(item.item_id);
                            setSelectedIds([item.item_id]);
                            setSelected(item);
                            event.preventDefault();
                          }}
                          onMouseEnter={() => updateDragSelectedRange(item)}
                          onClick={(event) => {
                            const target = event.target as HTMLElement;
                            if (target.closest('[data-row-action="true"]')) return;
                            if (dragSelectMovedRef.current) {
                              dragSelectMovedRef.current = false;
                              return;
                            }
                            handleRowSelection(item, event);
                          }}
                          onDoubleClick={(event) => {
                            const target = event.target as HTMLElement;
                            if (target.closest('[data-row-action="true"]')) return;
                            if (item.type === "folder" && viewMode !== "trash") {
                              openFolder(item);
                            } else if (item.type === "file" && viewMode !== "trash") {
                              openItem(item);
                            }
                          }}
                          onDragStart={(event) => {
                            const activeIds = getActiveItemIds(item.item_id);
                            setDraggingItemIds(activeIds);
                            event.dataTransfer.effectAllowed = "move";
                            event.dataTransfer.setData(TRI_CLOUD_ITEM_DRAG_MIME, JSON.stringify(activeIds));
                            event.dataTransfer.setData("text/plain", `${TRI_CLOUD_ITEM_TEXT_PREFIX}${activeIds.join(",")}`);
                          }}
                          onDragEnd={() => {
                            setDraggingItemIds([]);
                            clearDragHover();
                          }}
                          onDragOver={(event) => {
                            if (!canUploadHere || item.type !== "folder" || String(item.owner_user_id || "") !== userId) return;
                            const hasInternalDrag = draggingItemIds.length > 0 || event.dataTransfer.types.includes(TRI_CLOUD_ITEM_DRAG_MIME);
                            if (hasInternalDrag || event.dataTransfer.types.includes("Files")) {
                              event.preventDefault();
                              event.dataTransfer.dropEffect = hasInternalDrag ? "move" : "copy";
                              if (hoverFolderId !== item.item_id) beginFolderHover(item);
                            }
                          }}
                          onDragEnter={(event) => {
                            if (!canUploadHere || item.type !== "folder" || String(item.owner_user_id || "") !== userId) return;
                            const hasInternalDrag = draggingItemIds.length > 0 || event.dataTransfer.types.includes(TRI_CLOUD_ITEM_DRAG_MIME);
                            if (hasInternalDrag || event.dataTransfer.types.includes("Files")) {
                              event.preventDefault();
                              beginFolderHover(item);
                            }
                          }}
                          onDragLeave={() => {
                            if (hoverFolderId === item.item_id) clearDragHover();
                          }}
                          onDrop={async (event) => {
                            if (!canUploadHere || item.type !== "folder" || String(item.owner_user_id || "") !== userId) return;
                            event.preventDefault();
                            event.stopPropagation();
                            const internalIds = readDraggedItemIds(event.dataTransfer);
                            if (internalIds.length) {
                              await moveDraggedItemsToFolder(event.dataTransfer, item);
                              return;
                            }
                            await handleDroppedDataTransfer(event.dataTransfer, item.item_id);
                          }}
                          className={`relative grid ${viewMode === "trash" ? "grid-cols-[minmax(0,1fr)_140px_190px_80px]" : "grid-cols-[minmax(0,1fr)_20px_140px_190px_80px]"} gap-4 px-5 py-3 items-center transition select-none ${isSelected ? "" : "hover:bg-slate-50"} ${isDragSource ? "opacity-60" : ""} ${isFolderDropTarget ? "bg-sky-50 ring-2 ring-inset ring-sky-300 rounded-xl" : ""}`}
                          style={isSelected ? { backgroundColor: ROW_SELECTION_BG } : undefined}
                        >
                          <div className="absolute left-0 top-0 h-full w-[3px]" style={{ backgroundColor: isSelected ? ROW_SELECTION_ACCENT : "transparent" }} />
                          <div data-primary-cell="true" className="flex min-w-0 items-center gap-3 text-left">
                            <span className="grid h-9 w-9 place-items-center rounded-xl bg-slate-100">{getItemIcon(item)}</span>
                            <span className="truncate">
                              <span className="block truncate font-medium text-slate-800">{item.name}</span>
                              <span className="block truncate text-xs text-slate-500">
                                {item.type === "folder" ? tx("フォルダ") : tx("ファイル")}
                                {item.version_count ? ` ・ ${tx("{count}件の履歴", { count: item.version_count })}` : ""}
                                {viewMode === "trash" && trashMeta ? ` ・ ${trashMeta.expired ? tx("削除期限超過") : tx("あと{days}日で削除", { days: trashMeta.remainingDays })}` : ""}
                              </span>
                            </span>
                          </div>
                          {viewMode !== "trash" ? (
                            <div className="flex h-5 w-5 items-center justify-center" title={renderItemStatusIcon(item, "normal") ? (isOfflineAvailableItem(item) ? tx("オフライン利用中") : tx("共有アイテム")) : undefined}>
                              {renderItemStatusIcon(item, "normal")}
                            </div>
                          ) : null}
                          <div className="text-sm text-slate-600">{item.type === "folder" ? "—" : formatBytes(item.size_bytes)}</div>
                          <div className="text-sm text-slate-600">{viewMode === "trash" ? formatDate(item.trashed_at) : formatDate(item.updated_at)}</div>
                          <div className="flex justify-end">
                            <button data-row-action="true" onClick={(event) => openContextMenu(event as unknown as React.MouseEvent, item)} className="grid h-9 w-9 place-items-center rounded-xl hover:bg-slate-100">
                              <MoreHorizontal className="h-4 w-4 text-slate-500" />
                            </button>
                          </div>
                        </div>
                      )})}
                    </div>
                  )}
                </section>

              </div>
            )}
          </div>
        </main>
      </div>

      {contextMenu ? (
        <div className="fixed inset-0 z-40" onClick={() => setContextMenu(null)}>
          <div
            className="absolute z-50 min-w-[240px] rounded-2xl border border-slate-200 bg-white p-2 shadow-2xl"
            style={{
              left: contextMenu.x,
              top: contextMenu.y,
              maxHeight: `calc(100vh - ${CONTEXT_MENU_MARGIN * 2}px)`,
              overflowY: "auto",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            {viewMode === "trash" ? (
              <>
                <button onClick={() => { restoreMultipleItems(getActiveItemIds(contextMenu.item.item_id)); setContextMenu(null); }} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50"><RefreshCw className="h-4 w-4 text-sky-600" /> {selectedIds.length > 1 && selectedIds.includes(contextMenu.item.item_id) ? tx("{count}件を復元", { count: selectedIds.length }) : tx("復元")}</button>
                <button onClick={() => { purgeMultipleItems(getActiveItemIds(contextMenu.item.item_id)); setContextMenu(null); }} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm text-rose-700 hover:bg-rose-50"><Trash2 className="h-4 w-4" /> {selectedIds.length > 1 && selectedIds.includes(contextMenu.item.item_id) ? tx("{count}件を完全削除", { count: selectedIds.length }) : tx("完全削除")}</button>
              </>
            ) : (
              <>
                {selectedIds.length <= 1 ? (
                  <>
                    {contextMenu.item.type === "folder" ? <>
                      <button onClick={() => { openFolder(contextMenu.item); setContextMenu(null); }} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50"><Folder className="h-4 w-4 text-sky-600" /> {tx("開く")}</button>
                      {String(contextMenu.item.owner_user_id || "") === userId ? (isOfflineUseEnabledForItem(contextMenu.item) ? <button onClick={() => { disableOfflineUse(contextMenu.item); setContextMenu(null); }} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50"><CloudDownload className="h-4 w-4 text-slate-500" /> {tx("オフライン利用を停止")}</button> : <button onClick={() => { enableOfflineUse(contextMenu.item); setContextMenu(null); }} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50"><CloudDownload className="h-4 w-4 text-slate-500" /> {tx("オフライン利用")}</button>) : null}
                    </> : <>
                      <button onClick={() => { openItem(contextMenu.item); setContextMenu(null); }} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50"><FolderOpen className="h-4 w-4 text-sky-600" /> {tx("開く")}</button>
                      <button onClick={() => { downloadItem(contextMenu.item); setContextMenu(null); }} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50"><Download className="h-4 w-4 text-sky-600" /> {tx("ダウンロード")}</button>
                      {String(contextMenu.item.owner_user_id || "") === userId ? (isOfflineUseEnabledForItem(contextMenu.item) ? <button onClick={() => { disableOfflineUse(contextMenu.item); setContextMenu(null); }} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50"><CloudDownload className="h-4 w-4 text-slate-500" /> {tx("オフライン利用を停止")}</button> : <button onClick={() => { enableOfflineUse(contextMenu.item); setContextMenu(null); }} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50"><CloudDownload className="h-4 w-4 text-slate-500" /> {tx("オフライン利用")}</button>) : null}
                    </>}
                    {String(contextMenu.item.owner_user_id || "") === userId ? (
                      <>
                        <button onClick={() => { openShareDialogForItems([contextMenu.item]); }} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50"><UserRoundPlus className="h-4 w-4 text-slate-500" /> {tx("共有")}</button>
                        <button onClick={() => { copyShareLink(contextMenu.item); setContextMenu(null); }} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50"><LinkIcon className="h-4 w-4 text-slate-500" /> {tx("リンクをコピー")}</button>
                        <button onClick={() => { copyItems([contextMenu.item.item_id]); setContextMenu(null); }} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50"><Copy className="h-4 w-4 text-slate-500" /> {tx("コピー")}</button>
                        <button onClick={() => { renameItem(contextMenu.item); setContextMenu(null); }} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50"><Pencil className="h-4 w-4 text-slate-500" /> {tx("名前を変更")}</button>
                        {contextMenu.item.type === "file" ? <button onClick={() => { openVersions(contextMenu.item); setContextMenu(null); }} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50"><History className="h-4 w-4 text-slate-500" /> {tx("アクティビティ")}</button> : null}
                      </>
                    ) : null}
                  </>
                ) : (
                  <>
                    <button onClick={() => { downloadMultipleItems(getActiveItemIds(contextMenu.item.item_id)); setContextMenu(null); }} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50"><Download className="h-4 w-4 text-sky-600" /> {selectedIds.length}件をダウンロード</button>
                    <button onClick={() => { openShareDialogForItems(sortedItems.filter((item) => getActiveItemIds(contextMenu.item.item_id).includes(item.item_id))); }} disabled={!allSelectedOwned} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"><UserRoundPlus className="h-4 w-4 text-slate-500" /> {selectedIds.length}件を共有</button>
                    <button onClick={() => { copyShareLinks(getActiveItemIds(contextMenu.item.item_id)); setContextMenu(null); }} disabled={!allSelectedOwned} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"><LinkIcon className="h-4 w-4 text-slate-500" /> {selectedIds.length}件分のリンクをコピー</button>
                    <button onClick={() => { copyItems(getActiveItemIds(contextMenu.item.item_id)); setContextMenu(null); }} disabled={!allSelectedOwned} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"><Copy className="h-4 w-4 text-slate-500" /> {selectedIds.length}件をコピー</button>
                  </>
                )}
                {selectedIds.length > 1 && !allSelectedOwned ? <div className="px-3 py-2 text-xs text-slate-500">{tx("共有・コピー・移動・削除は自分の項目だけに使えます。")}</div> : null}
                <button onClick={() => { openMoveDialog(contextMenu.item); setContextMenu(null); }} disabled={!allSelectedOwned} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"><Folder className="h-4 w-4 text-slate-500" /> {selectedIds.length > 1 && selectedIds.includes(contextMenu.item.item_id) ? tx("{count}件を移動", { count: selectedIds.length }) : tx("移動")}</button>
                <button onClick={() => { trashMultipleItems(getActiveItemIds(contextMenu.item.item_id)); setContextMenu(null); }} disabled={!allSelectedOwned} className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm text-rose-700 hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-50"><Trash2 className="h-4 w-4" /> {selectedIds.length > 1 && selectedIds.includes(contextMenu.item.item_id) ? tx("{count}件をごみ箱へ移動", { count: selectedIds.length }) : tx("ごみ箱へ移動")}</button>
              </>
            )}
          </div>
        </div>
      ) : null}

      {uploadConflictDialog ? (
        <div
          className="fixed inset-0 z-[80] bg-slate-950/45 backdrop-blur-sm flex items-center justify-center p-4"
          onClick={() => finishUploadConflictDialog("cancel")}
        >
          <div
            className="w-full max-w-md rounded-3xl bg-white shadow-2xl border border-slate-200 overflow-hidden"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="px-6 py-4 border-b border-slate-200 bg-slate-50">
              <div className="text-lg font-semibold text-slate-900">{uploadConflictDialog.title}</div>
            </div>
            <div className="bg-white px-6 py-5">
              {uploadConflictDialog.message ? <div className="mb-5 text-sm leading-6 text-slate-600 whitespace-pre-line">{uploadConflictDialog.message}</div> : null}
              <div className="flex items-center justify-between gap-3">
                <button
                  type="button"
                  onClick={() => finishUploadConflictDialog("cancel")}
                  className="rounded-2xl border border-slate-300 bg-white px-4 py-2.5 text-sm hover:bg-slate-50"
                >
                  {uploadConflictDialog.cancelLabel || tx("キャンセル")}
                </button>
                <div className="flex items-center justify-end gap-3">
                  <button
                    type="button"
                    onClick={() => finishUploadConflictDialog("copy")}
                    className="rounded-2xl border border-slate-300 bg-white px-4 py-2.5 text-sm hover:bg-slate-50"
                  >
                    {uploadConflictDialog.copyLabel || tx("新規にアップロード")}
                  </button>
                  <button
                    type="button"
                    autoFocus
                    onClick={() => finishUploadConflictDialog("replace")}
                    className="rounded-2xl bg-amber-600 px-4 py-2.5 text-sm text-white hover:bg-amber-700"
                  >
                    {uploadConflictDialog.replaceLabel || tx("置き換える")}
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>
      ) : null}

      {appDialog ? (
        <div
          className="fixed inset-0 z-[80] bg-slate-950/45 backdrop-blur-sm flex items-center justify-center p-4"
          onClick={() => finishAppDialog(false)}
        >
          <div
            className="w-full max-w-md rounded-3xl bg-white shadow-2xl border border-slate-200 overflow-hidden"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="px-6 py-4 border-b border-slate-200 bg-slate-50">
              <div className="text-lg font-semibold text-slate-900">{appDialog.title}</div>
            </div>
            <div className="bg-white px-6 py-5">
              {appDialog.message ? <div className="mb-5 text-sm leading-6 text-slate-600 whitespace-pre-line">{appDialog.message}</div> : null}
              <div className="flex items-center justify-end gap-3">
                {appDialog.cancelLabel !== "" ? (
                  <button
                    type="button"
                    onClick={() => finishAppDialog(false)}
                    className="rounded-2xl border border-slate-300 bg-white px-4 py-2.5 text-sm hover:bg-slate-50"
                  >
                    {appDialog.cancelLabel || tx("キャンセル")}
                  </button>
                ) : null}
                <button
                  type="button"
                  autoFocus
                  onClick={() => finishAppDialog(true)}
                  className={`rounded-2xl px-4 py-2.5 text-sm text-white ${appDialog.variant === "danger" ? "bg-rose-600 hover:bg-rose-700" : appDialog.variant === "warning" ? "bg-amber-600 hover:bg-amber-700" : "bg-sky-600 hover:bg-sky-700"}`}
                >
                  {appDialog.confirmLabel || "OK"}
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : null}

      {textPrompt ? (
        <div
          className="fixed inset-0 z-[70] bg-slate-950/45 backdrop-blur-sm flex items-center justify-center p-4"
          onClick={() => finishTextPrompt(null)}
        >
          <form
            className="w-full max-w-md rounded-3xl bg-white shadow-2xl border border-slate-200 overflow-hidden"
            onClick={(event) => event.stopPropagation()}
            onSubmit={(event) => {
              event.preventDefault();
              finishTextPrompt(textPromptValue);
            }}
          >
            <div className="px-6 py-4 border-b border-slate-200 bg-slate-50">
              <div className="text-lg font-semibold text-slate-900">{textPrompt.title}</div>
            </div>
            <div className="bg-white px-6 py-5">
              {textPrompt.message ? <div className="mb-4 text-sm leading-6 text-slate-600 whitespace-pre-line">{textPrompt.message}</div> : null}
              <input
                autoFocus
                type={textPrompt.inputType || "text"}
                value={textPromptValue}
                onChange={(event) => setTextPromptValue(event.target.value)}
                placeholder={textPrompt.placeholder || ""}
                className="w-full rounded-2xl border border-slate-300 bg-white px-4 py-3 text-base outline-none placeholder:text-slate-400 focus:border-sky-400 focus:ring-4 focus:ring-sky-100"
              />
              <div className="mt-5 flex items-center justify-end gap-3">
                <button
                  type="button"
                  onClick={() => finishTextPrompt(null)}
                  className="rounded-2xl border border-slate-300 bg-white px-4 py-2.5 text-sm hover:bg-slate-50"
                >
                  {textPrompt.cancelLabel || tx("キャンセル")}
                </button>
                <button
                  type="submit"
                  className="rounded-2xl bg-sky-600 px-4 py-2.5 text-sm text-white hover:bg-sky-700"
                >
                  {textPrompt.confirmLabel || "OK"}
                </button>
              </div>
            </div>
          </form>
        </div>
      ) : null}

      {shareDialogItems.length ? (
        <div
          className="fixed inset-0 z-50 bg-slate-950/45 backdrop-blur-sm flex items-center justify-center p-4"
          onClick={closeShareDialog}
        >
          <div
            className="min-w-0 max-w-none rounded-3xl bg-white shadow-2xl border border-slate-200 overflow-hidden"
            style={{ width: shareDialogPanelWidth ? `${shareDialogPanelWidth}px` : undefined }}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="flex items-center justify-between px-6 py-4 border-b border-slate-200 bg-slate-50">
              <div>
                <div className="text-lg font-semibold">{shareDialogItems.length === 1 ? tx("{name} を共有", { name: shareDialogItems[0].name }) : tx("{count}件を共有タイトル", { count: shareDialogItems.length })}</div>
              </div>
            </div>
            <div className="space-y-4 px-6 py-5">
              {shareDialogItems.length > 1 ? (
                <div className="rounded-2xl border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-600 break-words">
                  {tx("対象: {names}", { names: shareDialogItems.map((item) => item.name).join(", ") })}
                </div>
              ) : null}
              <div className="overflow-x-auto rounded-2xl border border-slate-300 px-3 py-2.5 focus-within:border-sky-400 focus-within:ring-4 focus-within:ring-sky-100">
                <div className="flex items-center gap-2">
                  <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
                    {shareRecipientEmails.map((recipientEmail) => (
                      <span key={recipientEmail} className="inline-flex max-w-none shrink-0 items-center gap-1 rounded-full bg-sky-50 px-3 py-1 text-sm text-sky-700">
                        <span className="whitespace-nowrap">{recipientEmail}</span>
                        <button
                          type="button"
                          onClick={() => removeShareRecipient(recipientEmail)}
                          disabled={shareSending}
                          className="rounded-full p-0.5 text-sky-500 hover:bg-sky-100 disabled:opacity-50"
                          aria-label={`${recipientEmail} ${tx("削除")}`}
                        >
                          ×
                        </button>
                      </span>
                    ))}
                    <input
                      autoFocus
                      type="email"
                      value={shareRecipientInput}
                      onChange={(e) => setShareRecipientInput(e.target.value)}
                      onKeyDown={handleShareRecipientInputKeyDown}
                      onBlur={() => {
                        if (shareRecipientInput.trim()) addShareRecipients();
                      }}
                      placeholder={shareRecipientEmails.length ? "" : tx("メールアドレスでユーザーを追加")}
                      style={{ width: `${shareRecipientInputWidthCh}ch` }}
                      className="min-w-0 flex-none border-0 bg-transparent px-1 py-1 text-sm outline-none placeholder:text-slate-400"
                    />
                  </div>
                  <button
                    type="button"
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => addShareRecipients()}
                    disabled={shareSending || !shareRecipientInput.trim()}
                    className="shrink-0 rounded-xl border border-slate-200 bg-slate-50 px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {tx("追加")}
                  </button>
                </div>
              </div>
              <textarea
                value={shareMessage}
                onChange={(e) => setShareMessage(e.target.value)}
                disabled={shareSending}
                rows={5}
                placeholder={tx("メッセージを追加")}
                className="w-full resize-none rounded-2xl border border-slate-300 bg-white px-4 py-3 text-base outline-none placeholder:text-slate-400 focus:border-sky-400 focus:ring-4 focus:ring-sky-100 disabled:bg-slate-50 disabled:text-slate-400"
              />
            </div>
            <div className="flex items-center justify-end gap-3 bg-white px-6 py-4">
              <button onClick={closeShareDialog} disabled={shareSending} className="rounded-2xl border border-slate-300 bg-white px-4 py-2.5 text-sm hover:bg-slate-50 disabled:opacity-50">{tx("キャンセル")}</button>
              <button onClick={sendShareToRecipient} disabled={shareSending || (!shareRecipientEmails.length && !shareRecipientInput.trim())} className="rounded-2xl bg-sky-600 px-4 py-2.5 text-sm text-white hover:bg-sky-700 disabled:opacity-50">{shareSending ? tx("送信中...") : tx("送信")}</button>
            </div>
          </div>
        </div>
      ) : null}

      {moveDialogItem ? (
        <div className="fixed inset-0 z-50 bg-slate-950/45 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="w-full max-w-2xl rounded-3xl bg-white shadow-2xl border border-slate-200 overflow-hidden">
            <div className="flex items-center justify-between px-6 py-4 border-b border-slate-200 bg-slate-50">
              <div>
                <div className="text-lg font-semibold">{tx("移動先を選択")}</div>
              </div>
            </div>
            <div className="p-6">
              {moveLoading ? <div className="text-sm text-slate-500">{tx("読み込み中...")}</div> : (
                <>
                  <div className="mb-4 text-sm leading-6 text-slate-600">
                    {selectedIds.length > 1 && selectedIds.includes(moveDialogItem.item_id)
                      ? tx("選択中の{count}件の移動先フォルダを選択してください。", { count: selectedIds.length })
                      : tx("「{name}」の移動先フォルダを選択してください。", { name: moveDialogItem.name })}
                  </div>
                  <div className="rounded-2xl border border-slate-200 bg-slate-50 p-3">
                    <label className="text-sm font-medium text-slate-700">{tx("移動先フォルダ")}</label>
                    <select value={moveTargetParentId} onChange={(e) => setMoveTargetParentId(e.target.value)} className="mt-3 w-full rounded-2xl border border-slate-200 bg-white px-4 py-3 outline-none focus:ring-2 focus:ring-sky-500">
                      <option value={ROOT_ID}>{tx("ホーム")}</option>
                      {moveTargets.map((folder) => (
                        <option key={folder.item_id} value={folder.item_id}>{folder.path || folder.name}</option>
                      ))}
                    </select>
                  </div>
                  <div className="mt-6 flex justify-end gap-3">
                    <button onClick={() => setMoveDialogItem(null)} className="rounded-2xl border border-slate-200 px-4 py-2.5 text-sm hover:bg-slate-50">{tx("キャンセル")}</button>
                    <button onClick={submitMoveItem} disabled={moveLoading} className="rounded-2xl bg-sky-600 px-4 py-2.5 text-sm text-white hover:bg-sky-700 disabled:opacity-50">{moveLoading ? tx("移動中...") : tx("移動")}</button>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      ) : null}

      {versionsOpen ? (
        <div className="fixed inset-0 z-50 bg-slate-950/45 backdrop-blur-sm flex items-center justify-center p-4">
          <div className="w-full max-w-4xl rounded-3xl bg-white shadow-2xl border border-slate-200 overflow-hidden">
            <div className="flex items-center justify-between px-6 py-4 border-b border-slate-200 bg-slate-50">
              <div>
                <div className="text-lg font-semibold">{tx("版履歴")}</div>
              </div>
            </div>
            <div className="p-6 max-h-[75vh] overflow-auto">
              <div className="mb-4 text-sm leading-6 text-slate-600">{tx("{name} の過去版と現在版を一覧表示する。", { name: versionData?.item.name || selected?.name || "" })}</div>
              {versionsLoading ? <div className="text-sm text-slate-500">{tx("読み込み中...")}</div> : versionData?.versions?.length ? (
                <div className="space-y-3">
                  {versionData.versions.map((version) => (
                    <div key={version.version_id} className={`rounded-3xl border p-5 ${version.is_current ? "border-sky-200 bg-sky-50/60" : "border-slate-200 bg-white"}`}>
                      <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
                        <div>
                          <div className="inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs font-medium bg-slate-100 text-slate-700">
                            {version.is_current ? tx("現在の版") : tx("版 {number}", { number: version.version_no ?? "" })}
                          </div>
                          <div className="mt-3 text-lg font-semibold">{version.name}</div>
                          <div className="mt-2 text-sm text-slate-500">{tx("作成日時: {date} ・ サイズ: {size} ・ {part}", { date: formatDate(version.created_at), size: formatBytes(version.size_bytes), part: version.part_count ? `part ${version.part_count}` : "single object" })}</div>
                          <div className="mt-1 text-xs text-slate-500">source: {version.source || "—"}{version.restore_from_version_id ? ` / restore-from: ${version.restore_from_version_id}` : ""}</div>
                        </div>
                        <div className="flex items-center gap-2">
                          {!version.is_current ? <button onClick={() => restoreVersion(versionData.item.item_id, version.version_id)} className="inline-flex items-center gap-2 rounded-2xl bg-sky-600 px-4 py-2.5 text-sm text-white hover:bg-sky-700"><RefreshCw className="h-4 w-4" /> {tx("この版に戻す")}</button> : null}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : <div className="text-sm text-slate-500">{tx("履歴はまだありません。同名アップロードや同期クライアントからの更新があると、ここに版が追加されます。")}</div>}
              <div className="mt-6 flex justify-end">
                <button onClick={() => setVersionsOpen(false)} className="rounded-2xl border border-slate-200 px-4 py-2.5 text-sm hover:bg-slate-50">{tx("閉じる")}</button>
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
