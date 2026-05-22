const BUNDLE_URL = 'data/derived/signal_cycle_bundle.json';
const POLL_MS = 10 * 60 * 1000;
let bundle = null;
let chart = null;
let activeTab = 'coupling';
let lastStamp = '';

const $ = id => document.getElementById(id);
const num = v => Number.isFinite(Number(v)) ? Number(v) : null;
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtNum = (v, d=2) => num(v) == null ? '—' : Number(v).toFixed(d);
const fmtPct = (v, d=2) => num(v) == null ? '—' : `${(Number(v)*100).toFixed(d)}%`;
const fmtSignedPct = (v, d=2) => num(v) == null ? '—' : `${Number(v) >= 0 ? '+' : ''}${(Number(v)*100).toFixed(d)}%`;
const fmtMetricPct = (v, d=2) => {
  const x = num(v);
  if (x == null) return '—';
  if (Math.abs(x) > 10) return 'outlier';
  return `${(x*100).toFixed(d)}%`;
};
const fmtHours = v => {
  const h = num(v);
  if (h == null) return '—';
  if (h >= 48) return `${(h/24).toFixed(1)}d`;
  if (h < 1) return `${(h*60).toFixed(0)}m`;
  return `${h.toFixed(h < 10 ? 1 : 0)}h`;
};
function clsDelta(v) { const x = num(v); return x == null ? '' : x > 0 ? 'good' : x < 0 ? 'bad' : ''; }
function pill(text, cls='ok') { return `<span class="pill ${cls}">${esc(text)}</span>`; }
function sourceLabel(v) { return v === 'live_scorecard' ? 'live prior scorecard' : v === 'backtest_model_comparison' ? 'backtest fallback' : 'none'; }
function horizonSort(rows){ return [...(rows||[])].sort((a,b)=>{ const ha=String(a.horizon||'').replace('h',''); const hb=String(b.horizon||'').replace('h',''); return (Number(ha)||999)-(Number(hb)||999); }); }

async function loadBundle(manual=false) {
  try {
    const res = await fetch(`${BUNDLE_URL}?v=${Date.now()}`, { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const json = await res.json();
    if (!json || !json.generated_at) throw new Error('bundle missing generated_at');
    if (json.generated_at !== lastStamp || manual) {
      bundle = json;
      lastStamp = json.generated_at;
      render();
    }
    $('pollState').textContent = manual ? 'refreshed' : '10m';
  } catch (err) {
    $('notice').innerHTML = `No generated bundle loaded. Run the GitHub Action. Error: <code>${esc(err.message)}</code>`;
    $('pollState').textContent = 'waiting';
    if (!bundle) renderEmpty();
  }
}

function renderEmpty() {
  $('generatedAt').textContent = 'waiting';
  $('kpis').innerHTML = ['Cycle rows','Score rows','Backtest rows','Live rows','β audit','Coupling'].map(k => `<div class="kpi"><span>${k}</span><b>0</b><small>waiting for generated bundle</small></div>`).join('');
  $('signalRead').innerHTML = '<div class="empty">No generated artifacts loaded. This app never shows dummy signals.</div>';
  $('topicTable').innerHTML = '<div class="empty">No cycle JSON parsed.</div>';
  $('horizonTable').innerHTML = '<div class="empty">No scored market artifacts parsed.</div>';
  $('backtestTable').innerHTML = '<div class="empty">No model comparison parsed.</div>';
  $('couplingTable').innerHTML = '<div class="empty">No coupling rows emitted.</div>';
  $('liveTable').innerHTML = '<div class="empty">No live prediction state loaded.</div>';
  $('sourceHealth').innerHTML = '<div class="empty">Source health will appear after the workflow runs.</div>';
  renderChart();
}

function render() {
  if (!bundle) return renderEmpty();
  const s = bundle.summary || {};
  $('generatedAt').textContent = bundle.generated_at || '—';
  $('mode').textContent = sourceLabel(s.score_source);
  $('notice').innerHTML = `${esc(bundle.source_policy || 'Strict source mode')}`;
  $('horizonSource').textContent = sourceLabel(s.score_source);
  $('kpis').innerHTML = `
    <div class="kpi"><span>Cycle rows</span><b>${s.cycle_rows ?? 0}</b><small>${s.topics ?? 0} normalized topics</small></div>
    <div class="kpi"><span>Score rows</span><b>${s.score_rows ?? 0}</b><small>${sourceLabel(s.score_source)}</small></div>
    <div class="kpi"><span>Backtest rows</span><b>${s.backtest_rows ?? 0}</b><small>${s.backtest_horizons ?? 0} horizons</small></div>
    <div class="kpi"><span>Live rows</span><b>${s.live_prediction_rows ?? 0}</b><small>state only, not scored</small></div>
    <div class="kpi"><span>β mode / floor</span><b>${fmtNum(s.beta_mode,2)}</b><small>${fmtPct(s.beta_floor_share,1)} at ${fmtNum(s.beta_floor_watch,2)}</small></div>
    <div class="kpi"><span>Coupling rows</span><b>${s.coupling_rows ?? 0}</b><small>${s.candidate_coupling_rows ?? 0} candidates</small></div>`;
  renderRead(); renderTopics(); renderHorizons(); renderBacktest(); renderCoupling(); renderLive(); renderHealth(); renderChart();
}

function renderRead() {
  const s = bundle.summary || {};
  const horizons = bundle.market_horizons || [];
  const coupling = bundle.coupling_rows || [];
  const candidates = coupling.filter(r => r.status === 'candidate coupling');
  const best = candidates[0] || coupling.find(r => r.status === 'mixed coupling') || coupling[0];
  const h1 = horizons.find(h => h.horizon === 'h1');
  const primary = horizons.filter(h => h.horizon !== 'h1');
  const betaWarn = Number(s.beta_floor_share) >= .75;
  const dustWarn = Number(s.dust_nonzero_share) < .25;
  let html = '';
  html += `<div class="read-card"><b>Artifact status</b><span>${s.cycle_rows || 0} cycle rows, ${s.score_rows || 0} scored market rows, ${s.backtest_rows || 0} backtest rows. Score source: <strong>${sourceLabel(s.score_source)}</strong>.</span></div>`;
  html += `<div class="read-card"><b>Current verdict</b><span>${esc(s.verdict || '—')}. Live predictions are shown as state only; they do not create hit/PnL.</span></div>`;
  if (h1) html += `<div class="read-card"><b>h1 read</b><span>Diagnostic/dust only. Δ hit ${fmtSignedPct(h1.delta_hit)} · Δ PnL ${fmtSignedPct(h1.delta_pnl)} · rows ${h1.realized_rows ?? '—'}.</span></div>`;
  if (primary.length) {
    const bestH = [...primary].sort((a,b)=>((b.delta_pnl ?? -9)+(b.delta_hit ?? -9))-((a.delta_pnl ?? -9)+(a.delta_hit ?? -9)))[0];
    html += `<div class="read-card"><b>Primary horizon read</b><span>Best non-h1 loaded horizon is <strong>${esc(bestH.horizon)}</strong>: Δ hit ${fmtSignedPct(bestH.delta_hit)} · Δ PnL ${fmtSignedPct(bestH.delta_pnl)} · rows ${bestH.realized_rows ?? '—'}.</span></div>`;
  } else {
    html += `<div class="read-card"><b>Primary horizon read</b><span>No non-h1 scored market horizon is available yet. Coupling cannot be confirmed.</span></div>`;
  }
  if (best) html += `<div class="read-card"><b>Top coupling row</b><span>${esc(best.topic)} / ${esc(best.horizon)} · ${esc(best.status)} · pressure ${fmtNum(best.retained_pressure_score,1)} · β ${fmtNum(best.topic_beta_mode,2)} · dust ${fmtNum(best.topic_dust_median,3)}.</span></div>`;
  html += `<div class="read-card"><b>Beta audit</b><span class="${betaWarn ? 'warn':'good'}">${betaWarn ? 'β is floor-locked. Use the cycle clock, but do not trust exact β until expanded-grid validation.' : 'β is not dominated by the watched floor.'}</span></div>`;
  html += `<div class="read-card"><b>Dust audit</b><span class="${dustWarn ? 'warn':'good'}">${dustWarn ? 'Many parsed dust values are zero/missing. Coupling pressure still uses ΔAIC/phase, but dust interpretation needs source audit.' : 'Dust values are present enough to interpret topic pressure.'}</span></div>`;
  $('signalRead').innerHTML = html || '<div class="empty">No reads available.</div>';
}

function table(headers, rows) {
  if (!rows.length) return '<div class="empty">No rows.</div>';
  return `<table><thead><tr>${headers.map(h=>`<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table>`;
}

function renderTopics() {
  const rows = (bundle.topic_summaries || []).slice(0, 100).map(r => `<tr>
    <td>${esc(r.topic)}</td><td class="num">${r.cycle_rows ?? '—'}</td><td class="num">${fmtNum(r.retained_pressure_score,1)}</td>
    <td class="num">${fmtHours(r.lambda_median_hours)}</td><td class="num">${fmtNum(r.beta_mode,2)}</td>
    <td class="num">${fmtPct(r.beta_floor_share,0)}</td><td class="num">${fmtNum(r.dust_median,3)}</td>
    <td>${pill(r.dust_audit || '—', r.dust_audit === 'ok' ? 'ok' : 'warn')}</td><td class="num">${fmtNum(r.delta_aic_median,2)}</td><td>${pill(r.beta_verdict || '—', r.beta_verdict === 'floor-locked' ? 'warn' : 'ok')}</td>
  </tr>`);
  $('topicTable').innerHTML = table(['topic','rows','pressure','λq','β mode','β floor','dust','dust audit','ΔAIC','β audit'], rows);
}

function horizonRows(rows) {
  return rows.map(r => `<tr>
    <td>${esc(r.horizon)}</td><td>${esc(r.score_source || '')}</td><td>${esc(r.best_model || '—')}</td><td class="num">${r.realized_rows ?? '—'}</td>
    <td class="num">${fmtPct(r.baseline_hit)}</td><td class="num">${fmtPct(r.s2_hit)}</td><td class="num ${clsDelta(r.delta_hit)}">${fmtSignedPct(r.delta_hit)}</td>
    <td class="num">${fmtPct(r.baseline_pnl)}</td><td class="num">${fmtPct(r.s2_pnl)}</td><td class="num ${clsDelta(r.delta_pnl)}">${fmtSignedPct(r.delta_pnl)}</td>
    <td class="num">${fmtMetricPct(r.best_mae)}</td>
  </tr>`);
}
function renderHorizons() {
  const rows = bundle.market_horizons || [];
  const fallback = (!rows.length && (bundle.backtest_horizons || []).length) ? bundle.backtest_horizons : [];
  if (rows.length) {
    $('horizonTable').innerHTML = table(['h','source','best','rows','base hit','s2 hit','Δ hit','base PnL','s2 PnL','Δ PnL','MAE'], horizonRows(rows));
  } else if (fallback.length) {
    $('horizonTable').innerHTML = '<div class="source-note">No live scored horizon artifact recognized. Showing real backtest/model-comparison rows only.</div>' + table(['h','source','best','rows','base hit','s2 hit','Δ hit','base PnL','s2 PnL','Δ PnL','MAE'], horizonRows(fallback));
  } else {
    $('horizonTable').innerHTML = '<div class="empty">No scored horizon rows recognized. Source Health will show whether market files loaded but schema was not recognized.</div>';
  }
}
function renderBacktest() {
  $('backtestTable').innerHTML = table(['h','source','best','rows','base hit','s2 hit','Δ hit','base PnL','s2 PnL','Δ PnL','MAE'], horizonRows(bundle.backtest_horizons || []));
}

function renderCoupling() {
  const rows = (bundle.coupling_rows || []).slice(0, 180).map(r => `<tr>
    <td>${esc(r.topic)}</td><td>${esc(r.horizon)}</td><td class="num">${fmtNum(r.retained_pressure_score,1)}</td>
    <td class="num">${fmtHours(r.topic_lambda_hours)}</td><td class="num">${fmtNum(r.topic_beta_mode,2)}</td>
    <td class="num">${fmtPct(r.topic_beta_floor_share,0)}</td><td class="num">${fmtNum(r.topic_dust_median,3)}</td><td>${pill(r.topic_dust_audit || '—', r.topic_dust_audit === 'ok' ? 'ok' : 'warn')}</td><td class="num">${fmtNum(r.topic_delta_aic_median,2)}</td>
    <td class="num ${clsDelta(r.delta_hit)}">${fmtSignedPct(r.delta_hit)}</td>
    <td class="num ${clsDelta(r.delta_pnl)}">${fmtSignedPct(r.delta_pnl)}</td>
    <td class="num ${clsDelta(r.coupling_score)}">${fmtNum(r.coupling_score,2)}</td>
    <td>${pill(r.status, r.status === 'candidate coupling' ? 'ok' : r.status === 'mixed coupling' ? 'warn' : r.status === 'dust diagnostic' ? 'warn' : 'bad')}</td>
  </tr>`);
  $('couplingTable').innerHTML = table(['topic','h','pressure','λq','β','β floor','dust','dust audit','ΔAIC','Δ hit','Δ PnL','score','status'], rows);
}

function renderLive() {
  const rows = (bundle.live_predictions || []).slice(0, 120).map(r => `<tr>
    <td>${esc(r.ticker)}</td><td>${esc(r.horizon)}</td><td>${esc(r.prediction)}</td><td class="num">${fmtSignedPct(r.expected_return)}</td><td class="num">${fmtPct(r.probability,1)}</td><td>${esc(r.asof_date || '')}</td><td class="num">${fmtNum(r.asof_close,3)}</td>
  </tr>`);
  $('liveTable').innerHTML = table(['ticker','h','signal','pred ret','confidence','asof','close'], rows);
}

function renderHealth() {
  const rows = (bundle.source_health || []).map(r => `<tr>
    <td>${esc(r.group)}</td><td>${esc(r.kind)}</td><td>${r.ok ? pill('loaded','ok') : pill('failed','bad')}</td>
    <td>${esc(r.schema_mode || '')}</td><td class="num">${r.rows ?? 0}</td><td class="num">${r.raw_rows ?? ''}</td><td>${esc(r.warning || r.error || '')}</td><td class="mono">${esc(r.url)}</td>
  </tr>`);
  $('sourceHealth').innerHTML = table(['group','kind','status','schema','parsed','raw','note','url'], rows);
}

function axisColor(){return getComputedStyle(document.body).getPropertyValue('--muted').trim() || '#8ea4ad'}
function textColor(){return getComputedStyle(document.body).getPropertyValue('--text').trim() || '#e8f1f4'}
function initChart() { if (!chart) chart = echarts.init($('mainChart')); }
function renderChart() {
  initChart();
  if (!bundle) {
    chart.setOption({ title:{ text:'Waiting for generated bundle', left:'center', top:'middle', textStyle:{ color:'#8ea4ad', fontSize:13 } } }, true);
    return;
  }
  if (activeTab === 'cycle') return renderCycleChart();
  if (activeTab === 'horizon') return renderHorizonChart();
  if (activeTab === 'beta') return renderBetaChart();
  if (activeTab === 'backtest') return renderBacktestChart();
  renderCouplingChart();
}
function emptyChart(text) { chart.setOption({ title:{ text, left:'center', top:'middle', textStyle:{ color:axisColor(), fontSize:13 } }, xAxis:[], yAxis:[], series:[] }, true); }
function renderCycleChart(){
  const rows=(bundle.topic_summaries||[]).slice(0,16).reverse();
  if (!rows.length) return emptyChart('No cycle rows parsed');
  chart.setOption({backgroundColor:'transparent',grid:{left:128,right:24,top:24,bottom:32},tooltip:{trigger:'axis'},xAxis:{type:'value',name:'retained pressure',axisLabel:{color:axisColor()},axisLine:{lineStyle:{color:axisColor()}},splitLine:{lineStyle:{color:'rgba(128,128,128,.18)'}}},yAxis:{type:'category',data:rows.map(r=>r.topic),axisLabel:{color:axisColor()},axisLine:{lineStyle:{color:axisColor()}}},series:[{type:'bar',data:rows.map(r=>r.retained_pressure_score),itemStyle:{color:'#6fb7ff'}}]},true);
}
function renderHorizonChart(){
  const scored = horizonSort(bundle.market_horizons || []);
  const backtest = horizonSort(bundle.backtest_horizons || []);
  const liveCounts = horizonSort(bundle.live_horizon_counts || []);
  const rows = scored.length ? scored : backtest;
  if (rows.length) {
    const title = scored.length ? 'Scored horizon ladder' : 'Backtest horizon reference — no live scorecard parsed';
    chart.setOption({backgroundColor:'transparent',title:{text:title,left:10,top:4,textStyle:{color:axisColor(),fontSize:12,fontWeight:600}},legend:{top:24,textStyle:{color:textColor()}},grid:{left:58,right:28,top:58,bottom:42},tooltip:{trigger:'axis',valueFormatter:v=>fmtSignedPct(v)},xAxis:{type:'category',data:rows.map(r=>r.horizon),axisLabel:{color:axisColor()},axisLine:{lineStyle:{color:axisColor()}}},yAxis:{type:'value',name:'S2 - baseline',axisLabel:{formatter:v=>`${(v*100).toFixed(1)}%`,color:axisColor()},axisLine:{lineStyle:{color:axisColor()}},splitLine:{lineStyle:{color:'rgba(128,128,128,.18)'}}},series:[{name:'Δ hit',type:'bar',data:rows.map(r=>r.delta_hit),itemStyle:{color:'#66e3a1'}},{name:'Δ PnL',type:'bar',data:rows.map(r=>r.delta_pnl),itemStyle:{color:'#ffd166'}}]},true);
    return;
  }
  if (liveCounts.length) {
    chart.setOption({backgroundColor:'transparent',title:{text:'Live prediction horizon coverage — not scored',left:10,top:4,textStyle:{color:axisColor(),fontSize:12,fontWeight:600}},grid:{left:52,right:28,top:46,bottom:42},tooltip:{trigger:'axis'},xAxis:{type:'category',data:liveCounts.map(r=>r.horizon),axisLabel:{color:axisColor()},axisLine:{lineStyle:{color:axisColor()}}},yAxis:{type:'value',name:'live rows',axisLabel:{color:axisColor()},axisLine:{lineStyle:{color:axisColor()}},splitLine:{lineStyle:{color:'rgba(128,128,128,.18)'}}},series:[{name:'live rows',type:'bar',data:liveCounts.map(r=>r.rows),itemStyle:{color:'#6fb7ff'}}]},true);
    return;
  }
  emptyChart('No scored horizons, backtest horizons, or live horizon counts parsed');
}
function renderBacktestChart(){
  const rows=bundle.backtest_horizons||[];
  if (!rows.length) return emptyChart('No backtest comparison parsed');
  chart.setOption({backgroundColor:'transparent',legend:{textStyle:{color:textColor()}},grid:{left:52,right:28,top:38,bottom:42},tooltip:{trigger:'axis',valueFormatter:v=>fmtSignedPct(v)},xAxis:{type:'category',data:rows.map(r=>r.horizon),axisLabel:{color:axisColor()},axisLine:{lineStyle:{color:axisColor()}}},yAxis:{type:'value',name:'backtest delta',axisLabel:{formatter:v=>`${(v*100).toFixed(1)}%`,color:axisColor()},axisLine:{lineStyle:{color:axisColor()}},splitLine:{lineStyle:{color:'rgba(128,128,128,.18)'}}},series:[{name:'Δ hit',type:'bar',data:rows.map(r=>r.delta_hit),itemStyle:{color:'#66e3a1'}},{name:'Δ PnL',type:'bar',data:rows.map(r=>r.delta_pnl),itemStyle:{color:'#ffd166'}}]},true);
}
function renderBetaChart(){
  const rows=(bundle.topic_summaries||[]).slice(0,20).reverse();
  if (!rows.length) return emptyChart('No beta diagnostics parsed');
  chart.setOption({backgroundColor:'transparent',legend:{textStyle:{color:textColor()}},grid:{left:128,right:34,top:38,bottom:36},tooltip:{trigger:'axis'},xAxis:{type:'value',min:0,max:1,axisLabel:{formatter:v=>`${(v*100).toFixed(0)}%`,color:axisColor()},axisLine:{lineStyle:{color:axisColor()}},splitLine:{lineStyle:{color:'rgba(128,128,128,.18)'}}},yAxis:{type:'category',data:rows.map(r=>r.topic),axisLabel:{color:axisColor()},axisLine:{lineStyle:{color:axisColor()}}},series:[{name:'β floor share',type:'bar',data:rows.map(r=>r.beta_floor_share),itemStyle:{color:'#b197fc'}}]},true);
}
function renderCouplingChart(){
  const rows=(bundle.coupling_rows||[]).filter(r=>r.status !== 'dust diagnostic').slice(0,28).reverse();
  if (rows.length) {
    chart.setOption({backgroundColor:'transparent',title:{text:'Coupling score from real cycle pressure × scored horizon lift',left:10,top:4,textStyle:{color:axisColor(),fontSize:12,fontWeight:600}},grid:{left:190,right:40,top:46,bottom:32},tooltip:{trigger:'axis'},xAxis:{type:'value',name:'research coupling score',axisLabel:{color:axisColor()},axisLine:{lineStyle:{color:axisColor()}},splitLine:{lineStyle:{color:'rgba(128,128,128,.18)'}}},yAxis:{type:'category',data:rows.map(r=>`${r.topic} / ${r.horizon}`),axisLabel:{color:axisColor()},axisLine:{lineStyle:{color:axisColor()}}},series:[{type:'bar',data:rows.map(r=>r.coupling_score),itemStyle:{color:p=>p.value>=0?'#66e3a1':'#ff6b6b'}}]},true);
    return;
  }
  const topics=(bundle.topic_summaries||[]).slice(0,18).reverse();
  if (topics.length) {
    chart.setOption({backgroundColor:'transparent',title:{text:'No scored non-h1 coupling yet — showing real cycle pressure only',left:10,top:4,textStyle:{color:axisColor(),fontSize:12,fontWeight:600}},grid:{left:128,right:28,top:48,bottom:34},tooltip:{trigger:'axis'},xAxis:{type:'value',name:'retained pressure',axisLabel:{color:axisColor()},axisLine:{lineStyle:{color:axisColor()}},splitLine:{lineStyle:{color:'rgba(128,128,128,.18)'}}},yAxis:{type:'category',data:topics.map(r=>r.topic),axisLabel:{color:axisColor()},axisLine:{lineStyle:{color:axisColor()}}},series:[{type:'bar',data:topics.map(r=>r.retained_pressure_score),itemStyle:{color:'#6fb7ff'}}]},true);
    return;
  }
  emptyChart('No cycle rows parsed. Coupling cannot be evaluated.');
}

function bind() {
  document.querySelectorAll('.tab').forEach(btn => btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active'); activeTab = btn.dataset.tab; renderChart();
  }));
  $('refreshBtn').addEventListener('click', () => loadBundle(true));
  $('themeBtn').addEventListener('click', () => { const light=document.body.classList.toggle('light'); $('themeBtn').textContent = light ? 'Dark' : 'Light'; setTimeout(renderChart, 80); });
  window.addEventListener('resize', () => chart && chart.resize());
  document.addEventListener('visibilitychange', () => { if (!document.hidden) loadBundle(false); });
}
bind();
loadBundle(false);
setInterval(() => loadBundle(false), POLL_MS);
