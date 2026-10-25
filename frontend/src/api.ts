export type ApiEvent = { seq: number; ts: string; actor: string; type: string; run?: number; phase?: string; payload: Record<string, unknown> };
export type CaseFile = { case_id: string; question: string; as_of: string; status: string; phase: string; runs: Run[]; status_reasons?: string[] };
export type Run = { run: number; claims: Claim[]; attacks: unknown[]; responses: unknown[]; rulings: Ruling[]; positions: unknown[]; final_ranges: unknown[] };
export type Claim = { id: string; side: string; statement: string; type: string; parameter?: string; bound_value?: number; evidence?: Evidence[] };
export type Evidence = { doc_id?: string; table_id?: string; row?: number; quote?: string };
export type Ruling = { claim_id: string; verdict: string; finding: string; rationale: string };
export type ExposureRow = { country: string; sector: string; thor?: { low: number; high: number }; loki?: { low: number; high: number }; odin?: { low: number; high: number }; baseline_value?: number; value_at_risk?: { low: number; high: number } };

const base = import.meta.env.VITE_API_URL ?? "http://localhost:8000/api";
export async function get<T>(path: string): Promise<T> { const res = await fetch(`${base}${path}`); if (!res.ok) throw new Error((await res.json()).error?.message ?? res.statusText); return res.json() as Promise<T>; }
export async function post<T>(path: string, body?: unknown): Promise<T> { const res = await fetch(`${base}${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined }); if (!res.ok) throw new Error((await res.json()).error?.message ?? res.statusText); return res.json() as Promise<T>; }
export function stream(caseId: string, since: number, onEvent: (event: ApiEvent) => void): EventSource { const source = new EventSource(`${base}/cases/${caseId}/events?since=${since}`); source.onmessage = (message) => onEvent(JSON.parse(message.data) as ApiEvent); for (const name of ["phase_changed", "claim_submitted", "ruling_issued", "final_range", "briefing_ready", "budget", "sealed_commit", "attack_filed", "response_filed"]) source.addEventListener(name, (message) => onEvent(JSON.parse((message as MessageEvent<string>).data) as ApiEvent)); return source; }
