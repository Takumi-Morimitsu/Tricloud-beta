import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { api, getToken, JsonRecord, login, logout } from "./api";

type View =
  | "dashboard"
  | "integrity"
  | "repairs"
  | "nodes"
  | "users"
  | "billing"
  | "rewards"
  | "releases"
  | "audit";

type DangerRequest = {
  title: string;
  confirmation: string;
  path: string;
  method: "POST" | "PUT" | "PATCH";
  body?: JsonRecord;
};

const NAV: Array<[View, string]> = [
  ["dashboard", "概要"],
  ["users", "ユーザー"],
  ["nodes", "ノード"],
  ["integrity", "データ保全"],
  ["repairs", "修復ジョブ"],
  ["billing", "請求"],
  ["rewards", "報酬"],
  ["releases", "リリース"],
  ["audit", "操作監査ログ"],
];

const TITLE = Object.fromEntries(NAV) as Record<View, string>;

function asRows(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? (value as JsonRecord[]) : [];
}

function text(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "はい" : "いいえ";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function timestamp(value: unknown): string {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  return new Date(seconds * 1000).toLocaleString("ja-JP");
}

function bytes(value: unknown): string {
  let amount = Number(value || 0);
  if (!Number.isFinite(amount)) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${amount.toFixed(index ? 1 : 0)} ${units[index]}`;
}

function Badge({ value }: { value: unknown }) {
  const normalized = String(value ?? "unknown").toLowerCase();
  const tone = ["healthy", "online", "active", "approved", "paid", "completed", "ok"].includes(normalized)
    ? "good"
    : ["failed", "missing", "corrupt", "suspended", "held", "unpaid"].includes(normalized)
      ? "bad"
      : "plain";
  return <span className={`badge ${tone}`}>{text(value)}</span>;
}

function tableRowKey(row: JsonRecord, index: number): string {
  const identity = row.id
    || row.log_id
    || row.repair_job_id
    || row.audit_job_id
    || row.payout_id
    || row.earning_id
    || row.invoice_id
    || row.request_id
    || row.event_id
    || row.version
    || row.user_id
    || row.node_id
    || row.file_object_id;
  return identity === null || identity === undefined || identity === "" ? String(index) : String(identity);
}

function Table({ columns, rows }: { columns: Array<[string, string, ((row: JsonRecord) => ReactNode)?]>; rows: JsonRecord[] }) {
  return (
    <div className="table-wrap">
      <table>
        <thead><tr>{columns.map(([key, label]) => <th key={key}>{label}</th>)}</tr></thead>
        <tbody>
          {rows.length === 0 && <tr><td colSpan={columns.length} className="empty">データはありません</td></tr>}
          {rows.map((row, index) => (
            <tr key={tableRowKey(row, index)}>
              {columns.map(([key, , render]) => <td key={key}>{render ? render(row) : text(row[key])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Login({ onDone }: { onDone: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError("");
    try { await login(email, password); onDone(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  }
  return (
    <main className="login-page">
      <form className="login-card" onSubmit={submit}>
        <h1>Tricloud 管理</h1>
        <p>DBで admin ロールが付与されたアカウントのみ利用できます。</p>
        <label>メール / ログインID<input type="text" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required /></label>
        <label>パスワード<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
        {error && <div className="error">{error}</div>}
        <button className="primary" disabled={busy}>{busy ? "確認中…" : "ログイン"}</button>
      </form>
    </main>
  );
}

function ConfirmDialog({ request, busy, onCancel, onSubmit }: {
  request: DangerRequest; busy: boolean; onCancel: () => void;
  onSubmit: (password: string, confirmation: string) => void;
}) {
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  return (
    <div className="modal-backdrop" role="presentation">
      <form className="modal" onSubmit={(event) => { event.preventDefault(); onSubmit(password, confirmation); }}>
        <h2>{request.title}</h2>
        <p>この操作は監査ログに記録されます。管理者パスワードを再入力し、確認文を正確に入力してください。</p>
        <div className="confirm-copy">{request.confirmation}</div>
        <label>管理者パスワード<input autoFocus type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
        <label>確認文<input value={confirmation} onChange={(e) => setConfirmation(e.target.value)} required /></label>
        <div className="modal-actions">
          <button type="button" className="secondary" onClick={onCancel} disabled={busy}>中止</button>
          <button className="danger" disabled={busy || confirmation !== request.confirmation}>{busy ? "処理中…" : "実行"}</button>
        </div>
      </form>
    </div>
  );
}

export default function App() {
  const [authenticated, setAuthenticated] = useState(Boolean(getToken()));
  const [view, setView] = useState<View>("dashboard");
  const [data, setData] = useState<JsonRecord>({});
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [danger, setDanger] = useState<DangerRequest | null>(null);
  const [dangerBusy, setDangerBusy] = useState(false);
  const [detail, setDetail] = useState<{ title: string; data: unknown } | null>(null);
  const [releaseVersion, setReleaseVersion] = useState("");
  const [releaseRollout, setReleaseRollout] = useState(0);

  useEffect(() => {
    const handler = () => setAuthenticated(Boolean(getToken()));
    window.addEventListener("tricloud-admin-auth", handler);
    return () => window.removeEventListener("tricloud-admin-auth", handler);
  }, []);

  const endpoint = useMemo(() => {
    const query = encodeURIComponent(search.trim());
    const mapping: Record<View, string> = {
      dashboard: "/admin/v1/dashboard",
      integrity: `/admin/v1/integrity/objects?limit=100&q=${query}`,
      repairs: "/admin/v1/repairs?limit=200",
      nodes: `/admin/v1/nodes?limit=100&q=${query}`,
      users: `/admin/v1/users?limit=100&q=${query}`,
      billing: "/admin/v1/billing?limit=100",
      rewards: "/admin/v1/rewards?limit=100",
      releases: "/admin/v1/releases",
      audit: `/admin/v1/audit-logs?limit=200&q=${query}`,
    };
    return mapping[view];
  }, [view, search]);

  const load = useCallback(async () => {
    if (!authenticated) return;
    setLoading(true); setError("");
    try { setData(await api<JsonRecord>(endpoint)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setLoading(false); }
  }, [authenticated, endpoint]);

  useEffect(() => { void load(); }, [load]);

  async function runDanger(password: string, confirmation: string) {
    if (!danger) return;
    setDangerBusy(true); setError("");
    try {
      await api(danger.path, {
        method: danger.method,
        body: JSON.stringify({ ...(danger.body || {}), admin_password: password, confirmation }),
      });
      setDanger(null);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally { setDangerBusy(false); }
  }

  function requestAction(request: DangerRequest) { setDanger(request); }
  async function inspect(title: string, path: string) {
    setError("");
    try { setDetail({ title, data: await api(path) }); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  }
  const items = asRows(data.items);

  if (!authenticated) return <Login onDone={() => setAuthenticated(true)} />;

  return (
    <div className="shell">
      <aside>
        <div className="brand">Tricloud <small>管理</small></div>
        <div className="aside-scroll">
          <nav>{NAV.map(([key, label]) => <button key={key} className={view === key ? "active" : ""} onClick={() => { setView(key); setSearch(""); }}>{label}</button>)}</nav>
        </div>
        <div className="aside-footer">
          <div className="aside-note">ノード信頼性スコアは方針保留のため未実装です。</div>
          <button className="logout" onClick={() => void logout()}>ログアウト</button>
        </div>
      </aside>
      <main className="content">
        <header>
          <div><h1>{TITLE[view]}</h1><p>最終読込: {new Date().toLocaleTimeString("ja-JP")}</p></div>
          <div className="header-actions">
            {["integrity", "nodes", "users", "audit"].includes(view) && <input aria-label="検索" placeholder="ID・メール・名前で検索" value={search} onChange={(e) => setSearch(e.target.value)} />}
            <button className="secondary" onClick={() => void load()} disabled={loading}>{loading ? "読込中…" : "更新"}</button>
          </div>
        </header>
        {error && <div className="error banner">{error}</div>}
        {view === "dashboard" && <Dashboard data={data} />}
        {view === "integrity" && <Integrity rows={items} action={requestAction} inspect={inspect} />}
        {view === "repairs" && <Repairs rows={items} action={requestAction} inspect={inspect} />}
        {view === "nodes" && <Nodes rows={items} action={requestAction} inspect={inspect} />}
        {view === "users" && <Users rows={items} action={requestAction} inspect={inspect} />}
        {view === "billing" && <Billing data={data} action={requestAction} />}
        {view === "rewards" && <Rewards data={data} action={requestAction} />}
        {view === "releases" && <Releases rows={items} version={releaseVersion} setVersion={setReleaseVersion} rollout={releaseRollout} setRollout={setReleaseRollout} action={requestAction} />}
        {view === "audit" && <AuditLogs rows={items} />}
      </main>
      {danger && <ConfirmDialog request={danger} busy={dangerBusy} onCancel={() => setDanger(null)} onSubmit={(password, confirmation) => void runDanger(password, confirmation)} />}
      {detail && <div className="modal-backdrop" role="presentation"><section className="modal detail-modal"><div className="detail-head"><h2>{detail.title}</h2><button onClick={() => setDetail(null)}>閉じる</button></div><pre>{JSON.stringify(detail.data, null, 2)}</pre></section></div>}
    </div>
  );
}

function Dashboard({ data }: { data: JsonRecord }) {
  const users = (data.users || {}) as JsonRecord;
  const nodes = (data.nodes || {}) as JsonRecord;
  const integrity = (data.integrity || {}) as JsonRecord;
  const billing = (data.billing || {}) as JsonRecord;
  const rewards = (data.rewards || {}) as JsonRecord;
  const cards: Array<[string, unknown, string]> = [
    ["登録ユーザー", users.registered, `有料: ${text(users.paid)}`],
    ["オンラインノード", nodes.online, `全体: ${text(nodes.total)}`],
    ["提供容量", bytes(nodes.capacity_bytes), `使用・予約: ${bytes(nodes.reserved_bytes)}`],
    ["保全レプリカ", `${text(integrity.healthy_replica_percent)}%`, `不良: ${text(integrity.bad_replicas)}`],
    ["レプリカ不足", integrity.under_replicated_objects, `修復中: ${text(integrity.active_repairs)}`],
    ["監査失敗 (24h)", integrity.audit_failures_24h, `修復失敗: ${text(integrity.failed_repairs)}`],
    ["入金済み", `¥${Number(billing.paid_revenue_yen || 0).toLocaleString("ja-JP")}`, `決済問題: ${text(billing.payment_failures)}`],
    ["未承認報酬", rewards.unapproved_earnings, `送金失敗: ${text(rewards.payout_failures)}`],
  ];
  return <section className="cards">{cards.map(([label, value, sub]) => <article key={label}><span>{label}</span><strong>{text(value)}</strong><small>{sub}</small></article>)}</section>;
}

function Integrity({ rows, action, inspect }: { rows: JsonRecord[]; action: (r: DangerRequest) => void; inspect: (title: string, path: string) => Promise<void> }) {
  return <Table rows={rows} columns={[
    ["file_object_id", "オブジェクトID", (r) => <code>{text(r.file_object_id)}</code>],
    ["item_name", "名前", (r) => text(r.item_name)],
    ["size_bytes", "サイズ", (r) => bytes(r.size_bytes)],
    ["healthy_count", "正常/全体", (r) => `${text(r.healthy_count)} / ${text(r.replica_count)}`],
    ["bad_count", "不良", (r) => <Badge value={Number(r.bad_count || 0) ? "corrupt" : "healthy"} />],
    ["last_verified_at", "最終検証", (r) => timestamp(r.last_verified_at)],
    ["actions", "操作", (r) => <div className="row-actions"><button onClick={() => void inspect("レプリカ・監査・修復の詳細", `/admin/v1/integrity/objects/${encodeURIComponent(String(r.file_object_id))}`)}>詳細</button><button onClick={() => action({ title: "監査をキューへ追加", confirmation: "QUEUE AUDIT", path: `/admin/v1/integrity/objects/${encodeURIComponent(String(r.file_object_id))}/audits`, method: "POST", body: { limit: 100 } })}>監査</button><button onClick={() => action({ title: "手動修復を作成", confirmation: "CREATE REPAIR", path: "/admin/v1/repairs", method: "POST", body: { file_object_id: r.file_object_id, reason: "operator_requested" } })}>修復</button></div>],
  ]} />;
}

function Repairs({ rows, action, inspect }: { rows: JsonRecord[]; action: (r: DangerRequest) => void; inspect: (title: string, path: string) => Promise<void> }) {
  return <Table rows={rows} columns={[
    ["repair_job_id", "ジョブID", (r) => <code>{text(r.repair_job_id)}</code>],
    ["file_object_id", "オブジェクト", (r) => <code>{text(r.file_object_id)}</code>],
    ["status", "状態", (r) => <Badge value={r.status} />],
    ["attempt_count", "試行", (r) => text(r.attempt_count)],
    ["last_error", "エラー", (r) => text(r.last_error)],
    ["updated_at", "更新", (r) => timestamp(r.updated_at)],
    ["actions", "操作", (r) => <div className="row-actions row-actions-nowrap"><button onClick={() => void inspect("修復ジョブ履歴", `/admin/v1/repairs/${encodeURIComponent(String(r.repair_job_id))}/events`)}>履歴</button><button onClick={() => action({ title: "修復ジョブを再試行", confirmation: "RETRY REPAIR", path: `/admin/v1/repairs/${encodeURIComponent(String(r.repair_job_id))}/retry`, method: "POST", body: { reason: "operator_retry", reset_attempts: false } })}>再試行</button><button className="danger-text" onClick={() => action({ title: "修復ジョブを中止", confirmation: "CANCEL REPAIR", path: `/admin/v1/repairs/${encodeURIComponent(String(r.repair_job_id))}/cancel`, method: "POST", body: { reason: "operator_cancel" } })}>中止</button></div>],
  ]} />;
}

function Nodes({ rows, action, inspect }: { rows: JsonRecord[]; action: (r: DangerRequest) => void; inspect: (title: string, path: string) => Promise<void> }) {
  return <Table rows={rows} columns={[
    ["node_id", "ノード", (r) => <><code>{text(r.node_id)}</code><br /><small>{text(r.node_name)}</small></>],
    ["online", "接続", (r) => <Badge value={r.online ? "online" : "offline"} />],
    ["capacity", "予約/容量", (r) => `${bytes(r.reserved_bytes)} / ${bytes(r.capacity_bytes)}`],
    ["health", "正常レプリカ", (r) => `${text(r.healthy_replica_count)} / ${text(r.replica_count)}`],
    ["flags", "停止", (r) => `${r.placement_paused ? "配置 " : ""}${r.payouts_paused ? "支払" : ""}` || "—"],
    ["last_seen", "最終接続", (r) => timestamp(r.last_seen)],
    ["actions", "操作", (r) => <div className="row-actions"><button onClick={() => void inspect("ノードと保持レプリカ", `/admin/v1/nodes/${encodeURIComponent(String(r.node_id))}`)}>詳細</button><button onClick={() => action({ title: "ノード制御を変更", confirmation: "APPLY NODE CONTROLS", path: `/admin/v1/nodes/${encodeURIComponent(String(r.node_id))}/controls`, method: "PATCH", body: { placement_paused: !Boolean(r.placement_paused), payouts_paused: Boolean(r.payouts_paused), reason: "operator_control" } })}>{r.placement_paused ? "配置再開" : "配置停止"}</button><button onClick={() => action({ title: "ノード支払制御を変更", confirmation: "APPLY NODE CONTROLS", path: `/admin/v1/nodes/${encodeURIComponent(String(r.node_id))}/controls`, method: "PATCH", body: { placement_paused: Boolean(r.placement_paused), payouts_paused: !Boolean(r.payouts_paused), reason: "operator_control" } })}>{r.payouts_paused ? "支払再開" : "支払停止"}</button><button onClick={() => action({ title: "ノード監査をキューへ追加", confirmation: "QUEUE AUDIT", path: `/admin/v1/nodes/${encodeURIComponent(String(r.node_id))}/audits`, method: "POST", body: { limit: 100 } })}>監査</button></div>],
  ]} />;
}

function Users({ rows, action, inspect }: { rows: JsonRecord[]; action: (r: DangerRequest) => void; inspect: (title: string, path: string) => Promise<void> }) {
  const update = (r: JsonRecord, changes: JsonRecord, title: string) => action({ title, confirmation: "APPLY USER CONTROLS", path: `/admin/v1/users/${encodeURIComponent(String(r.user_id))}/controls`, method: "PATCH", body: { suspended: Boolean(r.suspended), abuse_flag: Boolean(r.abuse_flag), sharing_disabled: Boolean(r.sharing_disabled), downloads_disabled: Boolean(r.downloads_disabled), reason: "operator_control", ...changes } });
  return <Table rows={rows} columns={[
    ["email", "ユーザー", (r) => <>{text(r.email)}<br /><code>{text(r.user_id)}</code></>],
    ["subscription_status", "契約", (r) => <Badge value={r.subscription_status} />],
    ["storage_bytes", "保存量", (r) => bytes(r.storage_bytes)],
    ["flags", "制限", (r) => [r.suspended && "停止", r.abuse_flag && "要確認", r.sharing_disabled && "共有不可", r.downloads_disabled && "DL不可"].filter(Boolean).join(" / ") || "—"],
    ["created_at", "登録", (r) => timestamp(r.created_at)],
    ["actions", "操作", (r) => <div className="row-actions row-actions-nowrap"><button onClick={() => void inspect("ユーザーと管理操作履歴", `/admin/v1/users/${encodeURIComponent(String(r.user_id))}`)}>履歴</button><button onClick={() => update(r, { suspended: !Boolean(r.suspended) }, r.suspended ? "アカウント停止を解除" : "アカウントを停止")}>{r.suspended ? "停止解除" : "停止"}</button><button onClick={() => update(r, { sharing_disabled: !Boolean(r.sharing_disabled) }, "共有制限を変更")}>共有</button><button onClick={() => update(r, { downloads_disabled: !Boolean(r.downloads_disabled) }, "ダウンロード制限を変更")}>DL</button><button onClick={() => update(r, { abuse_flag: !Boolean(r.abuse_flag) }, "不正利用フラグを変更")}>要確認</button></div>],
  ]} />;
}

function Billing({ data, action }: { data: JsonRecord; action: (r: DangerRequest) => void }) {
  const subscriptions = asRows(data.subscriptions);
  const invoices = asRows(data.invoices);
  const events = asRows(data.webhook_events);
  const planPrices = asRows(data.plan_prices);
  const retryRequests = asRows(data.retry_requests);
  return <div className="stack"><h2>契約</h2><Table rows={subscriptions} columns={[
    ["email", "ユーザー", (r) => text(r.email)], ["stripe_customer_id", "Stripe Customer", (r) => <code>{text(r.stripe_customer_id)}</code>], ["plan_id", "プラン", (r) => text(r.plan_id)], ["status", "状態", (r) => <Badge value={r.status} />], ["current_period_end", "期限", (r) => timestamp(r.current_period_end)],
  ]} /><h2>請求書</h2><Table rows={invoices} columns={[
    ["invoice_id", "請求書ID", (r) => <code>{text(r.invoice_id)}</code>], ["user_id", "ユーザー", (r) => <code>{text(r.user_id)}</code>], ["total", "合計", (r) => `¥${Number(r.total || 0).toLocaleString("ja-JP")}`], ["status", "状態", (r) => <Badge value={r.status} />], ["created_at", "作成", (r) => timestamp(r.created_at)],
  ]} /><h2>プランマッピング</h2><Table rows={planPrices} columns={[
    ["plan_id", "プラン", (r) => text(r.plan_id)], ["stripe_price_id", "Stripe Price", (r) => <code>{text(r.stripe_price_id)}</code>], ["active", "有効", (r) => text(r.active)], ["created_at", "作成", (r) => timestamp(r.created_at)],
  ]} /><h2>Stripe Webhook受信履歴</h2><p className="note">再試行は実行せず、処理ワーカー向けの依頼だけを記録します。</p><Table rows={events} columns={[
    ["event_id", "イベント", (r) => <code>{text(r.event_id)}</code>], ["event_type", "種類", (r) => text(r.event_type)], ["status", "状態", (r) => <Badge value={r.status} />], ["detail", "詳細", (r) => text(r.detail)], ["processed_at", "処理時刻", (r) => timestamp(r.processed_at)], ["actions", "操作", (r) => <button onClick={() => action({ title: "Webhook再処理を依頼", confirmation: "REQUEST BILLING RETRY", path: `/admin/v1/billing/webhooks/${encodeURIComponent(String(r.event_id))}/retry-requests`, method: "POST", body: { reason: "operator_requested" } })}>再処理依頼</button>],
  ]} /><h2>再処理依頼</h2><Table rows={retryRequests} columns={[
    ["request_id", "依頼ID", (r) => <code>{text(r.request_id)}</code>], ["event_id", "イベント", (r) => <code>{text(r.event_id)}</code>], ["status", "状態", (r) => <Badge value={r.status} />], ["reason", "理由", (r) => text(r.reason)], ["created_at", "作成", (r) => timestamp(r.created_at)],
  ]} /></div>;
}

function Rewards({ data, action }: { data: JsonRecord; action: (r: DangerRequest) => void }) {
  const earnings = asRows(data.earnings);
  const payouts = asRows(data.payouts);
  const statusAction = (r: JsonRecord, status: string) => action({ title: `報酬を ${status} に変更`, confirmation: "UPDATE EARNING", path: `/admin/v1/rewards/earnings/${encodeURIComponent(String(r.earning_id))}`, method: "PATCH", body: { status, note: "operator_review" } });
  return <div className="stack"><h2>ノード報酬</h2><Table rows={earnings} columns={[
    ["earning_id", "報酬ID", (r) => <code>{text(r.earning_id)}</code>], ["node_id", "ノード", (r) => text(r.node_id)], ["net_amount_yen", "金額", (r) => <>{`¥${Number(r.net_amount_yen || 0).toLocaleString("ja-JP")}`}<br /><small>調整: ¥{Number(r.adjustments_yen || 0).toLocaleString("ja-JP")}</small></>], ["quality", "監査/稼働", (r) => `${text(r.audit_success_percent)}% / ${text(r.uptime_percent)}%`], ["status", "状態", (r) => <Badge value={r.status} />], ["actions", "操作", (r) => <div className="row-actions"><button onClick={() => statusAction(r, "approved")}>承認</button><button onClick={() => statusAction(r, "held")}>保留</button><button className="danger-text" onClick={() => statusAction(r, "void")}>無効</button></div>],
  ]} /><h2>支払履歴</h2><Table rows={payouts} columns={[
    ["payout_id", "支払ID", (r) => <code>{text(r.payout_id)}</code>], ["node_id", "ノード", (r) => text(r.node_id)], ["amount_yen", "金額", (r) => `¥${Number(r.amount_yen || 0).toLocaleString("ja-JP")}`], ["provider", "方式", (r) => text(r.provider)], ["provider_ref", "外部ID", (r) => <code>{text(r.provider_ref)}</code>], ["status", "状態", (r) => <Badge value={r.status} />], ["failure_reason", "失敗理由", (r) => text(r.failure_reason)], ["created_at", "作成", (r) => timestamp(r.created_at)],
  ]} /></div>;
}

function Releases({ rows, version, setVersion, rollout, setRollout, action }: { rows: JsonRecord[]; version: string; setVersion: (v: string) => void; rollout: number; setRollout: (v: number) => void; action: (r: DangerRequest) => void }) {
  const update = (r: JsonRecord, changes: JsonRecord, title: string) => action({ title, confirmation: "SAVE RELEASE", path: `/admin/v1/releases/${encodeURIComponent(String(r.version))}`, method: "PUT", body: { channel: r.channel || "stable", status: r.status || "draft", minimum_supported: Boolean(r.minimum_supported), force_update: Boolean(r.force_update), rollout_percent: Number(r.rollout_percent || 0), release_notes: r.release_notes || "", ...changes } });
  return <div className="stack"><form className="inline-form" onSubmit={(event) => { event.preventDefault(); if (version.trim()) action({ title: "リリース情報を保存", confirmation: "SAVE RELEASE", path: `/admin/v1/releases/${encodeURIComponent(version.trim())}`, method: "PUT", body: { channel: "stable", status: "draft", minimum_supported: false, force_update: false, rollout_percent: rollout, release_notes: "" } }); }}><label>バージョン<input value={version} onChange={(e) => setVersion(e.target.value)} placeholder="例: 0.2.0" required /></label><label>配信率<input type="number" min="0" max="100" value={rollout} onChange={(e) => setRollout(Number(e.target.value))} /></label><button className="primary">下書きを保存</button></form><p className="note">ここでは配信メタデータだけを管理します。実際のGitHub更新・ビルド・配布は行いません。</p><Table rows={rows} columns={[
    ["version", "バージョン", (r) => text(r.version)], ["channel", "チャンネル", (r) => text(r.channel)], ["status", "状態", (r) => <Badge value={r.status} />], ["rollout_percent", "配信率", (r) => `${text(r.rollout_percent)}%`], ["minimum_supported", "最低対応", (r) => text(r.minimum_supported)], ["force_update", "強制", (r) => text(r.force_update)], ["release_notes", "ノート", (r) => text(r.release_notes)], ["updated_at", "更新", (r) => timestamp(r.updated_at)], ["actions", "操作", (r) => <div className="row-actions"><button onClick={() => update(r, { status: r.status === "active" ? "paused" : "active" }, r.status === "active" ? "配信を一時停止" : "配信を有効化")}>{r.status === "active" ? "停止" : "有効化"}</button><button onClick={() => { const value = window.prompt("新しい配信率 (0～100)", String(r.rollout_percent || 0)); if (value === null) return; const number = Number(value); if (!Number.isInteger(number) || number < 0 || number > 100) { window.alert("0～100の整数を入力してください"); return; } update(r, { rollout_percent: number }, "段階配信率を変更"); }}>配信率</button><button onClick={() => { const value = window.prompt("リリースノート", String(r.release_notes || "")); if (value !== null) update(r, { release_notes: value }, "リリースノートを変更"); }}>ノート</button><button onClick={() => update(r, { minimum_supported: !Boolean(r.minimum_supported) }, "最低サポート設定を変更")}>最低対応</button><button onClick={() => update(r, { force_update: !Boolean(r.force_update) }, "強制更新設定を変更")}>強制更新</button><button className="danger-text" onClick={() => update(r, { status: "retired", rollout_percent: 0 }, "リリースを終了")}>終了</button></div>],
  ]} /></div>;
}

function AuditLogs({ rows }: { rows: JsonRecord[] }) {
  return <Table rows={rows} columns={[
    ["created_at", "時刻", (r) => timestamp(r.created_at)], ["admin_user_id", "管理者", (r) => <code>{text(r.admin_user_id)}</code>], ["action", "操作", (r) => text(r.action)], ["target", "対象", (r) => `${text(r.target_type)} / ${text(r.target_id)}`], ["result_status", "結果", (r) => <Badge value={r.result_status} />], ["request_id", "Request ID", (r) => <code>{text(r.request_id)}</code>],
  ]} />;
}
